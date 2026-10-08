"""POSIX controller CLI. No workload runs locally; SSH is mockable via Transport."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
from itertools import count
import json
import shlex
from pathlib import Path
import subprocess
import sys
import time

from .common import attempt_relative, digest, file_hash, read_json, save_json, timestamp
from .config import config_hash, load_config, matrix
from .llm import LLMRequest, decide
from .progress import TerminalProgress, _time
from .report import generate, selected_attempts
from .transport import SSH, Transport, verify_download

REPOSITORY = Path(__file__).resolve().parent.parent


def provenance():
    def git(*args):
        result = subprocess.run(["git", "-C", str(REPOSITORY), *args], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None
    files = {str(p.relative_to(REPOSITORY)): file_hash(p) for sub in ("jetson_tests", "tools/FlameGraph")
             for p in (REPOSITORY / sub).rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    helper = REPOSITORY / "tools/jetson-campaign-cgroup"
    files[str(helper.relative_to(REPOSITORY))] = file_hash(helper)
    return {"git_commit": git("rev-parse", "HEAD"), "git_status": git("status", "--porcelain"),
            "files": files, "code_hash": digest(files)}


def reserve_remote_dir(name):
    remote = "/home/nvidia/codex-work/" + name
    script = ("import pathlib,sys\n"
              "try: pathlib.Path(sys.argv[1]).mkdir()\n"
              "except FileExistsError: print('exists')\n"
              "else: print('created')\n")
    try:
        result = subprocess.run([*SSH, shlex.join(["python3", "-c", script, remote])],
                                capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out reserving remote campaign directory") from exc
    if result.returncode:
        raise RuntimeError("Could not reserve remote campaign directory: " + result.stderr.strip())
    status = result.stdout.strip()
    if status not in {"created", "exists"}:
        raise RuntimeError("Unexpected remote campaign reservation response: " + status)
    return status == "created"


def create_campaign(c, sources, destination=None, reserve_remote=None):
    base = Path(destination).name if destination else c["name"]
    parent = Path(destination).parent if destination else REPOSITORY / c["results_dir"]
    for index in count():
        campaign_id = base if index == 0 else f"{base}_{index}"
        out = Path(destination) if destination else parent / campaign_id
        try:
            out.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if destination:
                raise
            continue
        try:
            reserved = reserve_remote is None or reserve_remote(campaign_id)
        except BaseException:
            out.rmdir()
            raise
        if reserved:
            break
        out.rmdir()
    out = out.resolve()
    (out / "sources").mkdir()
    for name, text in sources.items():
        (out / "sources" / name).write_text(text)
    save_json(out / "config.json", c)
    save_json(out / "provenance.json", provenance())
    save_json(out / "campaign.json", {"id": campaign_id, "config_hash": config_hash(c), "created": timestamp(),
              "remote": "/home/nvidia/codex-work/" + campaign_id, "attempts": []})
    save_json(out / "order.json", {"config_hash": config_hash(c), "experiments": matrix(c)})
    return out


def preflight(out, transport, deploy=True, core=None, scenario=None):
    ledger = read_json(out / "campaign.json")
    if deploy:
        transport.upload(ledger["remote"], out, REPOSITORY)
    filters = {key: value for key, value in {"core": core, "scenario": scenario}.items() if value is not None}
    result = transport.worker(ledger["remote"], "doctor", **filters)
    transport.fetch(ledger["remote"] + "/doctor", out / "doctor")
    save_json(out / "doctor.json", result)
    return result


def _item_key(item):
    return tuple(item[key] for key in ("core", "scenario", "kind", "run"))


def campaign_matrix(out, c, core=None, scenario=None):
    saved = read_json(Path(out) / "order.json")
    if saved is None:
        return matrix(c, core, scenario)  # Campaigns created before order persistence.
    expected = matrix(c)
    rows = saved["experiments"]
    if (saved["config_hash"] != config_hash(c)
            or sorted(rows, key=_item_key) != sorted(expected, key=_item_key)):
        raise ValueError("Saved acquisition order does not match the campaign configuration")
    selection = [row for row in rows if (core is None or row["core"] == core)
                 and (not scenario or row["scenario"] == scenario)]
    if not selection:
        raise ValueError("Selection contains no experiments")
    return selection


def perf_conflict_request(c, info):
    configured = c["perf_cpus"]
    if not configured:
        return None
    online = set(info["topology"]["online"])
    rows = []
    seen = set()
    for item in matrix(c):
        key = (item["core"], item["scenario"])
        if key in seen:
            continue
        seen.add(key)
        interferers = set(info["placements"].get(f"{item['core']}/{item['scenario']}", []))
        collision = sorted(set(configured) & ({item["core"]} | interferers))
        offline = sorted(set(configured) - online)
        if collision or offline:
            rows.append({"core": item["core"], "scenario": item["scenario"],
                         "interferers": sorted(interferers), "collision": collision,
                         "offline": offline, "free": sorted(online - {item["core"]} - interferers)})
    if not rows:
        return None
    fingerprint = digest({"configured": configured, "online": sorted(online), "conflicts": rows})[:20]
    can_fallback = all(row["free"] for row in rows)
    can_collide = not any(row["offline"] for row in rows)
    options = (["fallback"] if can_fallback else []) + (["collision"] if can_collide else []) + ["stop"]
    return {"request_id": fingerprint, "configured": configured, "online": sorted(online),
            "conflicts": rows, "options": options, "created": timestamp(), "timeout_s": 600}


def perf_placement(c, info, out):
    configured = c["perf_cpus"]
    request = perf_conflict_request(c, info)
    if request is None:
        return {"policy": "configured" if configured else "unrestricted",
                "configured": configured, "effective": {}}
    fingerprint = request["request_id"]
    rows = request["conflicts"]
    saved = read_json(Path(out) / "perf-placement.json", {})
    if saved.get("request_id") == fingerprint and saved.get("policy") in {"fallback", "collision"}:
        return saved
    options = request["options"]
    can_fallback = "fallback" in options
    telegram = Path(out) / "telegram"
    save_json(telegram / "perf-placement-request.json", request)
    print("perf_cpus conflicts with the campaign placement; waiting up to 600s for Telegram decision", flush=True)
    deadline = time.monotonic() + 600
    action = None
    while time.monotonic() < deadline:
        decision = read_json(telegram / "perf-placement-decision.json", {})
        if decision.get("request_id") == fingerprint and decision.get("action") in options:
            action = decision["action"]
            break
        time.sleep(1)
    if action is None:
        action = "fallback" if can_fallback else "stop"
        save_json(telegram / "perf-placement-decision.json",
                  {"request_id": fingerprint, "action": action, "source": "timeout", "timestamp": timestamp()})
    if action == "stop":
        raise RuntimeError("No safe perf_cpus placement selected")
    result = {"request_id": fingerprint, "policy": action, "configured": configured,
              "effective": {f"{row['core']}/{row['scenario']}": row["free"] for row in rows}
              if action == "fallback" else {}, "decided": timestamp()}
    save_json(Path(out) / "perf-placement.json", result)
    return result


def effective_perf_cpus(c, decision, item):
    return decision.get("effective", {}).get(f"{item['core']}/{item['scenario']}", c["perf_cpus"])


def run_hook(out, ledger, c, transport, phase):
    if not (c["command-pre-test"] or c["command-post-test"]):
        return None
    value = transport.worker(ledger["remote"], "hook", phase=phase)
    transport.fetch(ledger["remote"] + "/hooks", Path(out) / "hooks")
    return value


def matrix_complete(c, attempts):
    for item in matrix(c):
        history = [row for row in attempts if row["item"] == item]
        if not history:
            return False
        generation = max(row.get("generation", 0) for row in history)
        if not any(row.get("outcome") == "PASS" for row in history if row.get("generation", 0) == generation):
            return False
    return True


def campaign_has_failure(attempts):
    for item in {_item_key(row["item"]) for row in attempts}:
        history = [row for row in attempts if _item_key(row["item"]) == item]
        generation = max(row.get("generation", 0) for row in history)
        current = [row for row in history if row.get("generation", 0) == generation]
        if current[-1].get("outcome") == "FAIL" and not any(row.get("outcome") == "PASS" for row in current):
            return True
    return False


def item_needs_run(item, attempts, args):
    previous = [row for row in attempts if row["item"] == item]
    if not previous or args.force:
        return True
    generation = max(row.get("generation", 0) for row in previous)
    current = [row for row in previous if row.get("generation", 0) == generation]
    if any(row.get("outcome") == "PASS" for row in current):
        return False
    return current[-1].get("outcome") != "FAIL" or args.rerun_failed


def wait_attempt(out, ledger, attempt, transport, c, progress=None):
    relative, root = attempt["relative"], ledger["remote"]
    seen, pending = set(), None
    # Background inference never holds up SSH polling; remote hard deadlines remain independent.
    executor = ThreadPoolExecutor(max_workers=1)
    delay = c["poll_s"]
    try:
        while True:
            try:
                status = transport.worker(root, "status", relative=relative)
                delay = c["poll_s"]
            except (RuntimeError, ValueError) as exc:
                message = f"SSH unavailable; detached worker retains its timeout. Retry in {delay:g}s: {exc}"
                if progress:
                    progress.message(message)
                else:
                    print(message, flush=True)
                time.sleep(delay)
                delay = min(30, delay * 2)
                continue
            if progress:
                progress.update(status | {"attempt": relative})
            if not status["alive"]:
                if not status["manifest_ready"]:
                    if progress:
                        progress.update(phase="Riconciliazione del worker", force=True)
                    transport.worker(root, "reconcile", relative=relative)
                break
            request = status.get("llm_request")
            if pending and pending[1].done():
                request_id, future = pending
                final = future.result()
                if request and request["request_id"] == request_id:
                    transport.put_json(root + "/results/" + relative + "/llm_decision.json", final)
                pending = None
            if request and request["request_id"] not in seen and pending is None:
                seen.add(request["request_id"])
                llm_request = LLMRequest(request["request_id"], request["feature"], request["context"], request["allowed_actions"])
                pending = (request["request_id"], executor.submit(decide, c["llm"], llm_request,
                           out / "llm" / digest(relative)[:16], attempt["retries_left"]))
            time.sleep(c["poll_s"])
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    target = out / "results" / relative
    if progress:
        progress.update(phase="Download e verifica degli artefatti", force=True)
    transport.fetch(root + "/results/" + relative, target)
    verify_download(target)
    attempt.update(outcome=read_json(target / "status.json")["outcome"], download_verified=True, finished=timestamp())
    metadata = read_json(target / "metadata.json", {})
    started, finished = _time(metadata.get("started")), _time(metadata.get("finished"))
    if started and finished and finished > started:
        # Use worker time so a late resume/download does not inflate the ETA.
        attempt["worker_elapsed_s"] = (finished - started).total_seconds()
    save_json(out / "campaign.json", ledger)
    if progress:
        progress.update({"alive": False}, phase="Esito: " + attempt["outcome"], force=True)
    return target


def run_campaign(out, c, args, transport=None):
    transport = transport or Transport(out / "transport")
    ledger = read_json(out / "campaign.json")
    if config_hash(c) != ledger["config_hash"]:
        raise ValueError("Configuration/profile changed: create a new campaign")
    saved_code = read_json(out / "provenance.json")
    if saved_code["code_hash"] != provenance()["code_hash"]:
        raise ValueError("Runner code changed: use the original revision or create a new campaign")
    with (out / ".controller.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        selection = campaign_matrix(out, c, args.core, args.scenario)
        progress = TerminalProgress(out, ledger, selection, c,
            reset_items=[item for item in selection if (args.force or args.rerun_failed)
                         and item_needs_run(item, ledger["attempts"], args)],
            settled_items=[item for item in selection if not item_needs_run(item, ledger["attempts"], args)])
        progress.message(f"Campagna: {ledger['id']} | {len(selection)} test selezionati")
        progress.message("ETA stimata: include warmup/cooldown; si adatta ai tempi osservati. Retry futuri esclusi.")
        hook_active = False
        remote_active = False
        try:
            # First reconcile outstanding attempts; never rerun doctor against an active owned DEMO.
            for attempt in ledger["attempts"]:
                if not attempt.get("download_verified"):
                    remote_active = True
                    progress.message("Ripresa del tentativo: " + attempt["relative"])
                    wait_attempt(out, ledger, attempt, transport, c, progress)
                    remote_active = False
            # Hooks and perf policy cover the complete YAML matrix, even for a filtered invocation.
            progress.update(phase="Preflight sulla Jetson", force=True)
            info = preflight(out, transport, deploy=not ledger["attempts"])
            if not info["ok"]:
                generate(out)
                raise RuntimeError("Jetson preflight failed; inspect " + str(out / "doctor/doctor.json"))
            progress.clear()
            perf = perf_placement(c, info, out)
            save_json(out / "plan.json", {"experiments": selection, "placements": info["placements"],
                                          "perf_placement": perf})
            progress.message("Observed interferer CPU masks: " + json.dumps(info["placements"], sort_keys=True))
            if any(item_needs_run(item, ledger["attempts"], args) for item in selection):
                hook_active = bool(c["command-pre-test"] or c["command-post-test"])
                if hook_active:
                    progress.update(phase="Hook pre-test", force=True)
                run_hook(out, ledger, c, transport, "pre")
            for item in selection:
                previous = [a for a in ledger["attempts"] if a["item"] == item]
                if previous and not args.force:
                    latest_generation = max(a.get("generation", 0) for a in previous)
                    current = [a for a in previous if a.get("generation", 0) == latest_generation]
                    if any(a.get("outcome") == "PASS" for a in current):
                        continue
                    if current[-1].get("outcome") == "FAIL" and not args.rerun_failed:
                        continue
                generation = max((a.get("generation", 0) for a in previous), default=0) + int(args.force)
                used = sum(a.get("generation", 0) == generation for a in previous)
                if args.rerun_failed and previous and not args.force:
                    generation += 1
                    used = 0
                for retry in range(used, c["recovery"]["max_retries"] + 1):
                    relative = attempt_relative(item, len(previous) + 1)
                    attempt = {"item": item, "relative": relative, "generation": generation,
                               "outcome": "INCOMPLETE", "download_verified": False,
                               "retries_left": c["recovery"]["max_retries"] - retry, "created": timestamp()}
                    ledger["attempts"].append(attempt)
                    previous.append(attempt)
                    save_json(out / "campaign.json", ledger)
                    perf_cpus = effective_perf_cpus(c, perf, item)
                    request = {"item": item, "relative": relative, "perf_cpus": perf_cpus,
                               "perf_placement": perf["policy"]}
                    request_path = ledger["remote"] + "/request_" + digest(relative)[:16] + ".json"
                    transport.put_json(request_path, request)
                    progress.message(f"Running {relative}")
                    progress.update({"attempt": relative, "alive": True,
                                     "state": {"phase": "Avvio del worker"}}, force=True)
                    transport.worker(ledger["remote"], "launch", request=request_path)
                    remote_active = True
                    target = wait_attempt(out, ledger, attempt, transport, c, progress)
                    remote_active = False
                    progress.update(phase="Aggiornamento del report", force=True)
                    generate(out)
                    if attempt["outcome"] == "PASS":
                        break
                    failure = read_json(target / "failure.json", {})
                    category = failure.get("category", "unknown")
                    progress.message(f"Tentativo {attempt['outcome']}: {failure.get('message', category)} | "
                                     f"Retry disponibili: {attempt['retries_left']}")
                    if (target / "cleanup_error.json").exists() or category in {"external", "implementation", "human_review", "llm_request_human_review"}:
                        raise RuntimeError(f"Campaign requires attention: {failure}; artifacts: {target}")
                    if c["llm"]["enabled"] and "error_classification" in c["llm"]["features"]:
                        progress.update(phase="Interpretazione dell'errore", force=True)
                        logs = []
                        for p in sorted(target.glob("pass_*/*.stderr.txt")):
                            with p.open("rb") as stream:
                                stream.seek(max(0, p.stat().st_size - 2048))
                                logs.append({"file": str(p.relative_to(target)), "text": stream.read().decode(errors="replace")})
                        request = LLMRequest(digest(relative)[:20], "error_classification", logs,
                                             ["continue", "request_human_review"])
                        decision = decide(c["llm"], request, out / "llm" / digest(relative)[:16], attempt["retries_left"])
                        if decision["action"] == "request_human_review" and "error_classification" in c["llm"]["required_features"]:
                            raise RuntimeError("Required error interpretation needs human review")
                    if category == "llm_abort_run":
                        break
            generate(out)
            if matrix_complete(c, ledger["attempts"]) or campaign_has_failure(ledger["attempts"]):
                hook_active = False
                if c["command-pre-test"] or c["command-post-test"]:
                    progress.update(phase="Hook post-test", force=True)
                run_hook(out, ledger, c, transport, "post")
            progress.update({"alive": False}, phase="Controller terminato", force=True)
        except BaseException:
            progress.clear()
            if hook_active and not remote_active:
                run_hook(out, ledger, c, transport, "post")
            raise
        finally:
            progress.close()
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reliable Jetson campaigns; commands are configured in YAML")
    parser.add_argument("command", choices=["doctor", "plan", "run", "resume", "report"])
    parser.add_argument("--config", nargs="+", action="extend", metavar="YAML",
                        help="One or more campaign YAMLs, executed sequentially")
    parser.add_argument("--campaign", type=Path)
    parser.add_argument("--core", type=int)
    parser.add_argument("--scenario", choices=["baseline", "interfgen", "demo"])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rerun-failed", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            if not args.campaign:
                raise ValueError("report requires --campaign")
            print(generate(args.campaign.resolve()))
            return 0
        if args.command == "resume" or args.resume:
            if not args.campaign:
                raise ValueError("resume requires --campaign")
            if args.config and len(args.config) != 1:
                raise ValueError("resume accepts at most one --config for the selected campaign")
            out = args.campaign.resolve()
            c = load_config(args.config[0])[0] if args.config else read_json(out / "config.json")
            campaigns = [(out, c, None)]
        else:
            paths = [Path(path) for path in (args.config or [REPOSITORY / "campaign/campaign.yaml"])]
            if len(paths) > 1 and args.campaign:
                raise ValueError("--campaign names one destination; use it with a single --config")
            # Validate every YAML and selection before reserving any remote directory.
            campaigns = [(path, *load_config(path)) for path in paths]
            plans = []
            for path, c, sources in campaigns:
                experiments = matrix(c, args.core, args.scenario)
                seconds = sum(len(x["passes"]) * x["duration_s"] for x in experiments)
                plans.append({"experiments": experiments, "acquisition_seconds": seconds,
                              "acquisition_hours": seconds / 3600,
                              "note": "Excludes setup, warmup, cooldown, processing and retries. CPU masks resolved by doctor on Jetson."})
            if args.command == "plan":
                if len(plans) == 1:
                    output = plans[0]
                else:
                    seconds = sum(plan["acquisition_seconds"] for plan in plans)
                    output = {"campaigns": [{"config": str(path.resolve()), "name": c["name"], **plan}
                                            for (path, c, _), plan in zip(campaigns, plans)],
                              "execution": "sequential", "acquisition_seconds": seconds,
                              "acquisition_hours": seconds / 3600}
                print(json.dumps(output, indent=2))
                return 0
        for path, c, sources in campaigns:
            out = path if sources is None else create_campaign(c, sources, args.campaign, reserve_remote_dir)
            print(f"Local campaign: {out}", flush=True)
            if args.command == "doctor":
                info = preflight(out, Transport(out / "transport"), core=args.core, scenario=args.scenario)
                print(json.dumps({"ok": info["ok"], "errors": info["errors"], "placements": info["placements"]}, indent=2))
                print(f"Per acquisire nella stessa campagna: python -m jetson_tests resume --campaign {out}")
                if not info["ok"]:
                    return 2
                continue
            run_campaign(out, c, args)
            print(f"Report: {out / 'report/summary.md'}")
            warnings = read_json(out / "report/warnings.json", [])
            if warnings:
                print(f"Avvisi non bloccanti: {len(warnings)}. Dettagli: {out / 'report/warnings.csv'}")
            ledger = read_json(out / "campaign.json")
            selected = selected_attempts(ledger["attempts"])
            if (any(a.get("outcome") != "PASS" for a in selected)
                    or any(not any(a["item"] == item and a.get("outcome") == "PASS" for a in selected)
                           for item in matrix(c, args.core, args.scenario))):
                return 2
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Controller interrupted. Remote attempt retains its deadline; resume this campaign to retrieve it.", file=sys.stderr)
        return 130
