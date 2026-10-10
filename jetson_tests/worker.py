"""Detached, bounded single-attempt worker; stdlib only, executed on the Jetson."""
import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from .common import RunState, append_json, digest, file_hash, read_json, save_json, timestamp
from .config import config_hash, validate, selected_config
from .flamegraph import core_profile_quality
from .hardware import doctor, foreign_workloads, machine_info, shell, telemetry
from .metrics import process_pass
from .pmu import pass_events, placement, topology
from .processes import Processes, RunFailure, boot_id, matches, process_info, process_table, thread_ids


CGROUP_HELPER = "/usr/local/sbin/jetson-campaign-cgroup"
PERF_ATTACH_ATTEMPTS = 3


def checked_root(path):
    path = Path(path).resolve()
    base = Path("/home/nvidia/codex-work").resolve()
    if path == base or base not in path.parents:
        raise ValueError("Remote workspace must be task-specific under /home/nvidia/codex-work")
    return path


def cgroup_name(relative):
    return "jetson-campaign-" + digest(relative)[:16]


def cgroup_exec(name, argv, rtprio):
    return ["sudo", "-n", CGROUP_HELPER, "exec", "--rtprio", str(rtprio), name, "--", *argv]


def cgroup_call(directory, operation, *args, check=True):
    """Run the fixed privileged helper synchronously and journal the exact result."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    argv = ["sudo", "-n", CGROUP_HELPER, operation, *map(str, args)]
    started = time.monotonic()
    result = subprocess.run(argv, capture_output=True, timeout=30)
    (directory / f"cgroup_{operation}.stdout.txt").write_bytes(result.stdout)
    (directory / f"cgroup_{operation}.stderr.txt").write_bytes(result.stderr)
    append_json(directory / "commands.jsonl", {"event": "cgroup", "argv": argv,
                "exit_code": result.returncode, "duration_s": time.monotonic() - started,
                "timestamp": timestamp()})
    if check and result.returncode:
        raise RunFailure(f"cgroup {operation} failed: {result.stderr.decode(errors='replace')[-1000:]}", "external")
    return result


def campaign_hook(root, phase):
    root = checked_root(root)
    c = validate(read_json(root / "config.json"))
    state_path = root / "hooks/state.json"
    state = read_json(state_path, {"cycle": 0, "active": False})
    if phase == "pre" and state["active"]:
        return state
    if phase == "post" and not state["active"]:
        return state
    if phase == "pre":
        state = {"cycle": state["cycle"] + 1, "active": False, "phase": "pre", "started": timestamp()}
    else:
        state.update(phase="post", started=timestamp())
    save_json(state_path, state)
    directory = root / "hooks" / f"cycle_{state['cycle']:03d}" / phase
    commands = c[f"command-{phase}-test"]
    manager = Processes(directory, time.monotonic() + max(60, c["timeout_grace_s"]) * max(1, len(commands)))
    environment = dict(os.environ, JETSON_CAMPAIGN_ROOT=str(root), JETSON_CAMPAIGN_ID=root.name)
    try:
        for index, value in enumerate(commands, 1):
            job = manager.spawn(f"command_{index:03d}", shell(value), cwd=root, env=environment)
            if manager.wait(job, max(60, c["timeout_grace_s"])):
                raise RunFailure(f"{phase}-test command {index} failed", "external")
        state.update(active=phase == "pre", phase="active" if phase == "pre" else "closed",
                     finished=timestamp())
        save_json(state_path, state)
        return state
    except BaseException as exc:
        state.update(active=True, phase=phase + "_failed", error=str(exc), finished=timestamp())
        save_json(state_path, state)
        raise
    finally:
        manager.cleanup()


def snapshot(directory, suffix):
    for name in ("interrupts", "softirqs"):
        (directory / f"{name}_{suffix}.txt").write_bytes(Path(f"/proc/{name}").read_bytes())
    save_json(directory / f"telemetry_{suffix}.json", telemetry())


def resource_manifest(workspace):
    return {str(p.relative_to(workspace)): file_hash(p) for p in sorted(workspace.rglob("*")) if p.is_file()}


def prepare_resources(profile, workspace):
    workspace.mkdir(parents=True, exist_ok=False)
    for target, source in profile.get("resources", {}).items():
        dst = workspace / target
        dst.parent.mkdir(parents=True, exist_ok=True)
        src = Path(source)
        if src.is_dir():
            # Dereference links to snapshot the actual files; reject directory links to avoid cycles.
            if any(p.is_symlink() and p.is_dir() for p in src.rglob("*")):
                raise RunFailure(f"Directory symlink in resource {source}; supply a bounded resource tree")
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    return resource_manifest(workspace)


def collect_workspace_outputs(workspace, resources, directory):
    if not workspace.exists():
        return
    for path in workspace.rglob("*"):
        if path.is_file():
            relative = str(path.relative_to(workspace))
            if relative not in resources or file_hash(path) != resources[relative]:
                output = directory / "workspace_outputs" / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, output)


def readiness(manager, job, spec, profile, workspace):
    deadline = min(manager.deadline, time.monotonic() + profile["startup_timeout_s"])
    started = time.monotonic()
    while time.monotonic() < deadline:
        manager.check()
        ready = time.monotonic() - started >= 5
        if spec.get("ready_log_regex"):
            path = Path(job["record"]["stdout"])
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 65536))
                text = stream.read().decode(errors="replace")
            ready = re.search(spec["ready_log_regex"], text) is not None
        elif spec.get("ready_command"):
            check = manager.spawn(f"ready_{spec['name']}_{len(manager.jobs)}",
                                  shell(spec["ready_command"], profile.get("environment_script")), cwd=workspace)
            ready = manager.wait(check, min(5, max(.1, deadline - time.monotonic()))) == 0
        if ready:
            return
        manager.sleep(.2)
    raise RunFailure(f"Startup timeout: {spec['name']}", "timeout")


def start_interferers(c, item, manager, workspace, directory, cpus):
    jobs = []
    if item["scenario"] == "interfgen":
        spec = c["scenarios"]["interfgen"]
        for cpu in cpus:
            jobs.append(manager.spawn(f"interfgen_cpu{cpu}", shell(spec["command"]),
                                      cwd=workspace, cpus=[cpu], required=True, output_dir=directory))
    elif item["scenario"] == "demo":
        profile = c["scenarios"]["demo"]["definition"]
        for proc in profile["processes"]:
            command = proc["command"]
            if proc.get("repeat_on_success"):
                command = "while true; do\n bash -c " + shlex.quote(command) + "\n code=$?\n [ $code -eq 0 ] || exit $code\n done"
            job = manager.spawn(proc["name"], shell(command, profile.get("environment_script")),
                                cwd=workspace, cpus=cpus, required=True, output_dir=directory)
            jobs.append(job)
            readiness(manager, job, proc, profile, workspace)
    manager.refresh()
    manager.check_affinity()
    return jobs


def cyclic_command(c, cpu, duration, directory):
    ct = c["cyclictest"]
    # -N: latencies and histogram buckets in ns; without it cyclictest truncates to whole µs.
    # -h is what fills the JSON histogram. Without --histfile the dense dump (one line per ns,
    # 14 MB for 1 ms) goes to stdout, hence /dev/null: the JSON keeps every non-empty bucket.
    return [shlex.split(ct["command"])[0], "-a", str(cpu), "-t", "1", "-p", str(ct["priority"]),
            "--policy=fifo", "-m", "-i", str(ct["interval_us"]), "-D", str(duration), "-N",
            "-h", str(ct["histogram_us"] * 1000), "-q", f"--json={directory / 'cyclictest.json'}",
            "--histfile=/dev/null", *ct["extra_args"]]


def workload_command(c, cpu, duration, directory, pass_name, event_args, has_events,
                     isolated_group=None, perf_cpus=None):
    perf_cpus = list(perf_cpus or [])
    argv = cyclic_command(c, cpu, duration, directory)
    child = (["taskset", "-c", str(cpu), *argv] if isolated_group or perf_cpus else argv)
    if isolated_group:
        child = cgroup_exec(isolated_group, child, c["cyclictest"]["priority"])
    composite = False
    if pass_name.startswith("victim_") and has_events and not isolated_group:
        argv = ["perf", "stat", "--no-big-num", "-x,", *event_args,
                "-o", str(directory / "perf_victim_task.csv"), "--", *child]
        composite = True
    elif pass_name == "profiling":
        argv = ["perf", "record", "-a", "-C", str(cpu), "--clockid", "mono",
                "-e", "cpu-clock", "-F", "99", "--call-graph", "dwarf,8192",
                "-o", str(directory / "perf.data"), "--", *child]
        composite = True
    else:
        argv = child
    if composite and perf_cpus:
        argv = ["taskset", "-c", ",".join(map(str, perf_cpus)), *argv]
    manager_cpus = None if isolated_group or (composite and perf_cpus) else [cpu]
    return argv, manager_cpus, composite


def progress_check(c, item, manager, state, directory, pending):
    llm = c["llm"]
    if not llm["enabled"] or "ambiguous_progress" not in llm["features"]:
        return pending
    now = time.monotonic()
    if pending and pending.get("id"):
        response = read_json(directory / "llm_decision.json")
        if response and response.get("request_id") == pending["id"]:
            action = response.get("action")
            append_json(directory / "llm_applied.jsonl", response)
            (directory / "llm_request.json").unlink(missing_ok=True)
            if action in {"retry", "restart_workload", "restart_monitor", "abort_run", "request_human_review"}:
                raise RunFailure(f"LLM requested {action}", "llm_" + action)
            return {"next": now + 60}
        if now > pending["deadline"]:
            (directory / "llm_request.json").unlink(missing_ok=True)
            if "ambiguous_progress" in llm["required_features"]:
                raise RunFailure("Required semantic progress check unavailable", "human_review")
            return {"next": now + 60}
        return pending
    if pending and now < pending["next"]:
        return pending
    logs = []
    for job in manager.jobs:
        if job["required"]:
            path = Path(job["record"]["stdout"])
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 4096))
                logs.append({"process": job["record"]["name"], "text": stream.read().decode(errors="replace")})
    request_id = digest([str(directory), state.value.get("pass"), now])[:20]
    save_json(directory / "llm_request.json", {"request_id": request_id, "feature": "ambiguous_progress",
              "context": logs, "allowed_actions": ["continue", "retry", "restart_workload", "restart_monitor", "abort_run", "request_human_review"],
              "timestamp": timestamp(), "phase": "RUNNING", "pass": state.value.get("pass")})
    return {"id": request_id, "deadline": now + llm["timeout_s"] + 10}


def check_monitors(manager, monitors):
    """Report failed counters while the acquisition is still running."""
    for job in monitors:
        code = manager.finish_log(job)
        if code is None or code == 0:
            continue
        record = job["record"]
        path = Path(record["stderr"])
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 8192))
            stderr = stream.read().decode(errors="replace")
        attach_race = (record["name"] == "perf_interferer" and
                       re.search(r"sys_perf_event_open\(\).*returned with 3 \(No such process\)", stderr))
        category = "perf_attach" if attach_race else "exit_code"
        append_json(manager.directory / "monitor_failures.jsonl", {
            "timestamp": timestamp(), "command_id": record["id"], "name": record["name"],
            "exit_code": code, "stderr": str(path), "stderr_tail": stderr, "category": category})
        raise RunFailure(f"perf failed: {record['name']} (exit {code}): {stderr.strip()}", category)


def acquire(c, item, pass_name, catalog, manager, state, directory, interferers, isolated_group=None,
            perf_cpus=None):
    # PID attachment (perf -p) could fail with ESRCH; interferers are now counted CPU-wide,
    # so this retry is a safety net only. Keep every discarded window
    # separately, then publish the successful pass at its usual report path.
    if pass_name == "profiling" or pass_name.startswith("victim_") or not interferers:
        return acquire_once(c, item, pass_name, catalog, manager, state, directory, interferers,
                            isolated_group, perf_cpus)
    directory = Path(directory)
    for attempt in range(1, PERF_ATTACH_ATTEMPTS + 1):
        target = directory / f"acquisition_{attempt:03d}"
        target.mkdir()
        first_job = len(manager.jobs)
        try:
            metrics = acquire_once(c, item, pass_name, catalog, manager, state, target, interferers,
                                   isolated_group, perf_cpus)
        except RunFailure as exc:
            # A retry must stop both cyclictest and every counter first.
            for job in reversed(manager.jobs[first_job:]):
                manager.stop(job)
            if (target / "interrupts_before.txt").exists() and not (target / "interrupts_after.txt").exists():
                snapshot(target, "after")
            save_json(target / "failure.json", {"message": str(exc), "category": exc.category})
            if exc.category != "perf_attach" or attempt == PERF_ATTACH_ATTEMPTS:
                raise
            state.transition("RETRY_MONITORS", acquisition_attempt=attempt + 1, retry_reason=str(exc))
            continue
        metrics["pass"] = directory.name
        metrics["window"]["acquisition_attempt"] = attempt
        save_json(target / "measurement.json", metrics["window"])
        save_json(target / "metrics.json", metrics)
        for path in target.iterdir():
            if path.is_file():
                shutil.copy2(path, directory / path.name)
        return metrics


def acquire_once(c, item, pass_name, catalog, manager, state, directory, interferers, isolated_group=None,
                 perf_cpus=None):
    duration = item["duration_s"]
    cpu = item["core"]
    perf_cpus = list(perf_cpus or [])
    window = {"started": timestamp(), "start_monotonic": time.monotonic(), "scopes": {},
              "unavailable_events": [k for k, v in catalog.items() if not v["available"]]}
    state.transition("SNAPSHOT_BEFORE", **{"pass": pass_name})
    snapshot(directory, "before")
    state.transition("START_MONITORS")
    monitors = []
    events = {} if pass_name == "profiling" else pass_events(pass_name, catalog, configured=c.get("perf_passes"))
    expected = {} if pass_name == "profiling" else pass_events(pass_name, catalog, include_unavailable=True,
                                                              configured=c.get("perf_passes"))
    window["unavailable_events"] = [key for key in expected if key not in events]
    args = [arg for event in events.values() for arg in ("-e", event)]
    if pass_name != "profiling" and not pass_name.startswith("victim_"):
        scopes = [("victim_cpu", ["-a", "-C", str(cpu)])]
        # CPU-wide on the interferer CPUs: per-task counters would be switched at every
        # context switch of the workload and slow it down only in this pass.
        interferer_cpus = sorted({c for job in interferers for c in (job["cpus"] or [])})
        save_json(directory / "interferer_attachment.json", {"cpus": interferer_cpus, "scope": "cpu-wide",
                  "note": "Counts everything running on the interferer CPUs, workload included. Disjoint from victim CPU."})
        if interferer_cpus:
            scopes.append(("interferer", ["-a", "-C", ",".join(map(str, interferer_cpus))]))
        for scope, scope_args in scopes:
            argv = ["perf", "stat", "--no-big-num", "-x,", *scope_args, *args,
                    "--timeout", str(duration * 1000), "-o", str(directory / f"perf_{scope}.csv")]
            window["scopes"][scope] = expected
            if events:
                monitors.append(manager.spawn("perf_" + scope, argv, cpus=perf_cpus or None,
                                              output_dir=directory))
    state.transition("START_WORKLOADS")
    if pass_name.startswith("victim_"):
        window["scopes"]["victim_task"] = expected
        if isolated_group and events:
            argv = ["perf", "stat", "--no-big-num", "-x,", "-a", "-C", str(cpu), *args,
                    "-G", isolated_group, "--timeout", str(duration * 1000),
                    "-o", str(directory / "perf_victim_task.csv")]
            monitors.append(manager.spawn("perf_victim_task", argv, cpus=perf_cpus or None,
                                          output_dir=directory))
    argv, manager_cpus, composite = workload_command(
        c, cpu, duration, directory, pass_name, args, bool(events), isolated_group, perf_cpus)
    cyclic = manager.spawn("cyclictest", argv, cpus=manager_cpus, output_dir=directory)
    window["cyclictest_start_monotonic"] = cyclic["record"]["start_monotonic"]
    state.transition("RUNNING")
    fifo_seen = False
    cgroup_seen = not isolated_group
    perf_affinity_seen = not (composite and perf_cpus)
    pending = {"next": time.monotonic() + 60}
    last_telemetry = 0
    while cyclic["proc"].poll() is None:
        manager.check()
        check_monitors(manager, monitors)
        for identity in cyclic["known"].values():
            if not matches(identity):
                continue
            for tid in thread_ids(identity["pid"]):
                try:
                    if os.sched_getscheduler(tid) == os.SCHED_FIFO and os.sched_getparam(tid).sched_priority == c["cyclictest"]["priority"]:
                        fifo_seen = True
                        if isolated_group:
                            cgroup_seen |= (Path(f"/proc/{tid}/cgroup").read_text().strip().endswith("/" + isolated_group))
                except (OSError, ProcessLookupError):
                    pass
        if composite and perf_cpus and matches(cyclic["record"]["identity"]):
            try:
                perf_affinity_seen |= set(os.sched_getaffinity(cyclic["record"]["identity"]["pid"])) <= set(perf_cpus)
            except ProcessLookupError:
                pass
        if time.monotonic() - last_telemetry >= 5:
            append_json(directory / "telemetry.jsonl", telemetry())
            save_json(manager.directory / "heartbeat.json", {"timestamp": timestamp(), "phase": "RUNNING", "pass": pass_name})
            last_telemetry = time.monotonic()
        pending = progress_check(c, item, manager, state, manager.directory, pending)
        if time.monotonic() - cyclic["record"]["start_monotonic"] > duration + c["timeout_grace_s"]:
            raise RunFailure("cyclictest exceeded acquisition timeout", "timeout")
        time.sleep(.1)
    (manager.directory / "llm_request.json").unlink(missing_ok=True)
    code = manager.finish_log(cyclic)
    window["cyclictest_end_monotonic"] = time.monotonic()
    state.transition("STOP_WORKLOADS")
    manager.stop(cyclic)
    state.transition("STOP_MONITORS")
    if code != 0:
        for job in monitors:
            manager.stop(job)
        raise RunFailure(f"cyclictest/perf exit code {code}", "exit_code")
    for job in monitors:
        manager.wait(job, c["timeout_grace_s"])
        check_monitors(manager, [job])
    window["monitors"] = [{"name": job["record"]["name"], "command_id": job["record"]["id"],
                           "start_monotonic": job["record"]["start_monotonic"]} for job in monitors]
    state.transition("SNAPSHOT_AFTER")
    snapshot(directory, "after")
    window.update(finished=timestamp(), end_monotonic=time.monotonic(), fifo_observed=fifo_seen)
    window.update(cgroup=isolated_group, cgroup_observed=cgroup_seen,
                  perf_cpus=perf_cpus, perf_affinity_observed=perf_affinity_seen)
    window["elapsed_s"] = window["end_monotonic"] - window["start_monotonic"]
    save_json(directory / "measurement.json", window)
    if not fifo_seen:
        raise RunFailure("No cyclictest thread observed at requested FIFO priority", "affinity")
    if not cgroup_seen:
        raise RunFailure("cyclictest did not enter the isolated cgroup", "affinity")
    if not perf_affinity_seen:
        raise RunFailure("perf frontend did not remain on perf_cpus", "affinity")
    state.transition("PROCESS_RESULTS")
    if pass_name == "profiling":
        tools = Path(__file__).resolve().parent.parent / "tools/FlameGraph"
        for name, command in [
            ("decode", ["perf", "script", "--no-inline", "-i", str(directory / "perf.data"), "--ns",
                        "-F", "sw:comm,pid,tid,cpu,time,event,ip,sym,dso"]),
            ("collapsed", ["perl", str(tools / "stackcollapse-perf.pl"), "--pid", "--tid", str(directory / "decode.stdout.txt")]),
            ("flamegraph", ["perl", str(tools / "flamegraph.pl"), "--title", f"{item['scenario']} CPU{cpu}", str(directory / "collapsed.stdout.txt")])]:
            job = manager.spawn(name, command, output_dir=directory)
            if manager.wait(job, max(60, duration)):
                raise RunFailure(f"Flamegraph {name} failed", "exit_code")
        shutil.copyfile(directory / "flamegraph.stdout.txt", directory / "core.svg")
        quality = core_profile_quality(directory / "decode.stdout.txt")
        save_json(directory / "profile_quality.json", quality)
        if not quality["core_samples"] or not quality["stack_samples"]:
            raise RunFailure("Empty flamegraph", "validation")
    metrics = process_pass(directory, cpu, c["perf_min_running_pct"])
    state.transition("VALIDATE")
    if metrics["latency"] is None or metrics["issues"]:
        raise RunFailure("Incomplete measurements: " + "; ".join(metrics["issues"]), "validation")
    return metrics


def finish_manifest(directory):
    entries = {str(p.relative_to(directory)): file_hash(p) for p in sorted(directory.rglob("*"))
               if p.is_file() and p.name not in {"manifest.json", "worker.stdout.txt", "worker.stderr.txt", "llm_request.json", "llm_decision.json"}
               and not p.name.endswith(".tmp")}
    save_json(directory / "manifest.json", entries)


def execute(root, request):
    root = checked_root(root)
    c = validate(read_json(root / "config.json"))
    item = request["item"]
    directory = (root / "results" / request["relative"]).resolve()
    if root / "results" not in directory.parents:
        raise ValueError("Attempt path escapes workspace")
    directory.mkdir(parents=True, exist_ok=True)
    state = RunState(directory)
    if state.value["phase"] != "PENDING":
        raise ValueError("Attempt already exists; never rerun into existing results")
    profile = c["scenarios"].get("demo", {}).get("definition", {})
    startup = len(profile.get("processes", [])) * profile.get("startup_timeout_s", 0)
    budget = len(item["passes"]) * (item["duration_s"] * 3 + c["timeout_grace_s"] + startup + profile.get("warmup_s", 30) + 60)
    shutdown_s = profile.get("shutdown_timeout_s", c["shutdown_timeout_s"]) if item["scenario"] == "demo" else c["shutdown_timeout_s"]
    manager = Processes(directory, time.monotonic() + budget, shutdown_s)
    workspace = root / "workspaces" / digest(request["relative"])[:16]
    resources = {}
    isolated_group = cgroup_name(request["relative"]) if item["core"] in c["isolcpu"] else None
    effective_perf_cpus = request.get("perf_cpus", c["perf_cpus"])
    cgroup_active = False
    lock = (root.parent / ".jetson-campaign.lock").open("a+")
    outcome, failure = "INCOMPLETE", None
    def interrupted(signum, frame):
        raise RunFailure(f"Worker received signal {signum}", "interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    def hard_timeout(signum, frame):
        raise RunFailure("Worker hard timeout", "timeout")
    signal.signal(signal.SIGALRM, hard_timeout)
    signal.setitimer(signal.ITIMER_REAL, budget)
    save_json(directory / "worker.json", {"identity": process_info(os.getpid()), "boot_id": boot_id()})
    save_json(directory / "config.json", c)
    save_json(directory / "request.json", request)
    for path in (root / "sources").glob("*.yaml"):
        shutil.copy2(path, directory / path.name)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunFailure("Another campaign holds the Jetson lock", "external") from exc
        state.transition("PRECHECK")
        info = doctor(selected_config(c, item["core"], item["scenario"], item["kind"]), directory / "precheck", probe=False)
        previous = read_json(root / "doctor/doctor.json", {})
        if not info["ok"] or not previous.get("ok"):
            raise RunFailure("Precheck failed: " + "; ".join(info["errors"] or previous.get("errors", ["run doctor first"])), "external")
        if previous.get("boot_id") != boot_id():
            raise RunFailure("Boot changed since doctor; rerun doctor before acquisition", "external")
        cpus = [] if item["scenario"] == "baseline" else placement(c["scenarios"][item["scenario"]], item["core"], topology())
        save_json(directory / "metadata.json", {**machine_info(), "started": timestamp(), "item": item,
                  "interferer_cpus": cpus, "code": read_json(root / "provenance.json"),
                  "isolated_cgroup": isolated_group, "perf_cpus": effective_perf_cpus,
                  "perf_placement": request.get("perf_placement"),
                  "perf_version": (root / "doctor/perf_version.stdout.txt").read_text() if (root / "doctor/perf_version.stdout.txt").exists() else None,
                  "config_hash": config_hash(c), "protocol": "a78ae-separate-passes-v1"})
        state.transition("SETUP", **{"pass": item["passes"][0]})
        if isolated_group:
            save_json(directory / "cgroup.json", {"name": isolated_group, "cpu": item["core"]})
            cgroup_active = True
            cgroup_call(directory, "create", isolated_group, item["core"])
        resources = prepare_resources(profile if item["scenario"] == "demo" else {}, workspace)
        # Freeze asset content for this campaign, including on resume.
        resource_lock = root / f"resources_{item['scenario']}.json"
        original = read_json(resource_lock)
        if original is not None and original != resources:
            raise RunFailure("DEMO resource content changed; create a new campaign", "external")
        save_json(resource_lock, resources)
        save_json(directory / "resources.json", resources)
        metrics = []
        for index, pass_name in enumerate(item["passes"]):
            if index:
                state.transition("SETUP", **{"pass": pass_name})
            target = directory / ("pass_" + pass_name)
            target.mkdir()
            foreign = foreign_workloads(c)
            if foreign:
                raise RunFailure(f"Foreign workloads before pass: {foreign}", "external")
            interferers = start_interferers(c, item, manager, workspace, target, cpus)
            state.transition("WARMUP")
            warmup = profile.get("warmup_s", 30) if item["scenario"] == "demo" else c["scenarios"][item["scenario"]].get("warmup_s", 0)
            manager.sleep(warmup)
            try:
                metrics.append(acquire(c, item, pass_name, info["events"], manager, state, target,
                                       interferers, isolated_group, effective_perf_cpus))
            finally:
                for job in reversed(interferers):
                    manager.stop(job)
                if not (target / "interrupts_after.txt").exists() and (target / "interrupts_before.txt").exists():
                    snapshot(target, "after")
            manager.sleep(c["cooldown_s"])
        save_json(directory / "metrics.json", {"item": item, "passes": metrics})
        outcome = "PASS"
    except BaseException as exc:
        failure = {"message": str(exc), "category": getattr(exc, "category", "implementation"), "traceback": traceback.format_exc()}
        outcome = "INCOMPLETE" if failure["category"] in {"interrupted", "external", "human_review", "llm_request_human_review"} else "FAIL"
        save_json(directory / "failure.json", failure)
        state.transition("FAILED", error=failure["message"])
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        state.transition("CLEANUP")
        try:
            manager.cleanup()
        except BaseException as exc:
            outcome = "INCOMPLETE"
            save_json(directory / "cleanup_error.json", {"error": str(exc)})
        if cgroup_active:
            try:
                cgroup_call(directory, "delete", isolated_group)
                cgroup_active = False
            except BaseException as exc:
                outcome = "INCOMPLETE"
                save_json(directory / "cleanup_error.json", {"error": str(exc), "cgroup": isolated_group})
        # Preserve all newly generated/modified workload files; unchanged input assets stay remote.
        collect_workspace_outputs(workspace, resources, directory)
        metadata = read_json(directory / "metadata.json", {})
        metadata.update(finished=timestamp(), outcome=outcome, final_telemetry=telemetry())
        save_json(directory / "metadata.json", metadata)
        state.transition("COMPLETE", outcome=outcome)
        finish_manifest(directory)
        lock.close()
    return 0 if outcome == "PASS" else 1


def launch(root, request_path):
    root = checked_root(root)
    request = read_json(request_path)
    out = (root / "results" / request["relative"]).resolve()
    if root / "results" not in out.parents:
        raise ValueError("Invalid attempt path")
    out.mkdir(parents=True, exist_ok=True)
    # Serialize start/reconciliation even if the SSH response is lost.
    with (out / "launch.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = read_json(out / "launch.json")
        if previous:
            return previous
        with (out / "worker.stdout.txt").open("wb") as stdout, (out / "worker.stderr.txt").open("wb") as stderr:
            proc = subprocess.Popen([sys.executable, "-m", "jetson_tests.worker", "execute", "--root", str(root),
                                     "--request", str(request_path)], cwd=root / "code", start_new_session=True,
                                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        result = {"identity": process_info(proc.pid), "boot_id": boot_id(), "timestamp": timestamp()}
        save_json(out / "launch.json", result)
        threading.Thread(target=proc.wait, daemon=True).start()
        return result


def status(root, relative):
    root = checked_root(root)
    out = (root / "results" / relative).resolve()
    if root / "results" not in out.parents:
        raise ValueError("Invalid attempt path")
    launch_info = read_json(out / "launch.json", {})
    alive = (launch_info.get("boot_id") == boot_id() and launch_info.get("identity")
             and matches(launch_info["identity"]) and process_info(launch_info["identity"]["pid"])["state"] != "Z")
    return {"state": read_json(out / "status.json", {}), "alive": bool(alive), "boot_id": boot_id(),
            "manifest_ready": (out / "manifest.json").exists(),
            "llm_request": read_json(out / "llm_request.json")}


def reconcile(root, relative):
    root = checked_root(root)
    current = status(root, relative)
    if current["alive"]:
        return current
    out = root / "results" / relative
    if current["manifest_ready"]:
        return current
    with (root.parent / ".jetson-campaign.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        errors = []
        for job in reversed(read_json(out / "processes.json", [])):
            if job["boot_id"] != boot_id():
                continue
            root_identity = job["identity"]
            root_now = process_info(root_identity["pid"])
            known = {p["pid"]: p for p in job.get("known", [])}
            if root_now is None or root_now["start"] == root_identity["start"]:
                known.update({p["pid"]: p for p in process_table() if p["sid"] == root_identity["sid"]})
            for identity in reversed(list(known.values())):
                if not matches(identity):
                    continue
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    if not matches(identity) or process_info(identity["pid"])["state"] == "Z":
                        break
                    try:
                        os.kill(identity["pid"], sig)
                    except ProcessLookupError:
                        break
                    time.sleep(.2)
                if matches(identity) and process_info(identity["pid"])["state"] != "Z":
                    errors.append(identity)
        save_json(out / "reconciliation.json", {"timestamp": timestamp(), "boot_id": boot_id(),
                  "survivors": errors, "reason": "Worker terminated without final manifest"})
        save_json(out / "status.json", {"phase": "COMPLETE", "outcome": "INCOMPLETE", "timestamp": timestamp(),
                  "error": "Interrupted worker; reconciliation performed"})
        save_json(out / "failure.json", {"category": "cleanup" if errors else "interrupted",
                  "message": "Interrupted worker; inspect reconciliation.json"})
        if errors:
            save_json(out / "cleanup_error.json", {"survivors": errors})
        cgroup = read_json(out / "cgroup.json", {}).get("name")
        if cgroup and not errors:
            try:
                cgroup_call(out, "delete", cgroup)
            except BaseException as exc:
                errors.append({"cgroup": cgroup, "error": str(exc)})
                save_json(out / "cleanup_error.json", {"survivors": errors})
        collect_workspace_outputs(root / "workspaces" / digest(relative)[:16], read_json(out / "resources.json", {}), out)
        finish_manifest(out)
    return status(root, relative)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["doctor", "launch", "execute", "status", "reconcile", "hook"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--request")
    parser.add_argument("--relative")
    parser.add_argument("--core", type=int)
    parser.add_argument("--scenario")
    parser.add_argument("--phase", choices=["pre", "post"])
    args = parser.parse_args()
    root = checked_root(args.root)
    if args.command == "doctor":
        value = doctor(selected_config(validate(read_json(root / "config.json")), args.core, args.scenario), root / "doctor")
    elif args.command == "launch":
        value = launch(root, args.request)
    elif args.command == "status":
        value = status(root, args.relative)
    elif args.command == "reconcile":
        value = reconcile(root, args.relative)
    elif args.command == "hook":
        value = campaign_hook(root, args.phase)
    else:
        return execute(root, read_json(args.request))
    print(json.dumps(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
