"""Local YAML loading; resolved configuration is portable JSON."""
import copy
import math
import random
import re
import shlex
from pathlib import Path

from .common import digest

PASSES = ["core", "victim_core", "memory", "victim_memory", "l1d", "l1i", "l2", "l3",
          "victim_l1d", "victim_l1i", "victim_l2", "victim_l3"]
FEATURES = {"error_classification", "ambiguous_progress"}


def keys(value, allowed, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    unknown = set(value) - set(allowed.split())
    if unknown:
        raise ValueError(f"Unknown {label} settings: {sorted(unknown)}")


def positive(value, name, zero=False, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < 0 or (not zero and value == 0) or (integer and not isinstance(value, int)):
        raise ValueError(f"Invalid {name}: {value}")


def command(value, name):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a nonempty shell command string")


def validate(config):
    c = copy.deepcopy(config)
    keys(c, "name victim_cores isolcpu perf_cpus perf_passes randomization command-pre-test command-post-test repetitions duration_s cooldown_s poll_s timeout_grace_s shutdown_timeout_s min_free_gb results_dir perf_min_running_pct cyclictest scenarios profiling recovery llm", "campaign")
    c.setdefault("name", "jetson-campaign")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", c["name"]):
        raise ValueError("name must contain only letters, numbers, _ and -")
    c.setdefault("victim_cores", [0, 3])
    if not c["victim_cores"] or len(set(c["victim_cores"])) != len(c["victim_cores"]):
        raise ValueError("victim_cores must be nonempty and unique")
    for core in c["victim_cores"]:
        positive(core, "core", zero=True, integer=True)
    isolated = c.setdefault("isolcpu", [])
    if isinstance(isolated, int) and not isinstance(isolated, bool):
        isolated = c["isolcpu"] = [isolated]
    if not isinstance(isolated, list) or len(set(isolated)) != len(isolated):
        raise ValueError("isolcpu must be an integer or a list of unique CPUs")
    for core in isolated:
        positive(core, "isolcpu", zero=True, integer=True)
    if not set(isolated) <= set(c["victim_cores"]):
        raise ValueError("isolcpu must be a subset of victim_cores")
    perf_cpus = c.setdefault("perf_cpus", [])
    if not isinstance(perf_cpus, list) or len(set(perf_cpus)) != len(perf_cpus):
        raise ValueError("perf_cpus must be a list of unique CPUs")
    for core in perf_cpus:
        positive(core, "perf_cpus", zero=True, integer=True)
    for key in ("command-pre-test", "command-post-test"):
        commands = c.setdefault(key, [])
        if not isinstance(commands, list):
            raise ValueError(f"{key} must be a list of shell commands")
        for value in commands:
            command(value, key)
    for key, default in [("repetitions", 1), ("duration_s", 300)]:
        c.setdefault(key, default)
        positive(c[key], key, integer=True)
    if "randomization" in c:
        settings = c["randomization"]
        keys(settings, "enabled seed", "randomization")
        settings.setdefault("enabled", False)
        if type(settings["enabled"]) is not bool:
            raise ValueError("randomization.enabled must be boolean")
        if settings["enabled"] and "seed" not in settings:
            raise ValueError("randomization.seed is required when randomization is enabled")
        if "seed" in settings:
            positive(settings["seed"], "randomization.seed", zero=True, integer=True)
    for key, default in [("cooldown_s", 10), ("poll_s", 2), ("timeout_grace_s", 30),
                         ("shutdown_timeout_s", 10), ("min_free_gb", 2)]:
        c.setdefault(key, default)
        positive(c[key], key, zero=key == "cooldown_s")
    c.setdefault("results_dir", "results")
    c.setdefault("perf_min_running_pct", 90)
    positive(c["perf_min_running_pct"], "perf_min_running_pct")
    if c["perf_min_running_pct"] > 100:
        raise ValueError("perf_min_running_pct must be <= 100")
    if "perf_passes" in c:
        from .pmu import EVENTS
        passes = c["perf_passes"]
        if not isinstance(passes, dict) or not passes:
            raise ValueError("perf_passes must be a nonempty mapping of pass names to event lists")
        for name, events in passes.items():
            if isinstance(name, str) and name.startswith("task_"):
                raise ValueError("perf_passes: replace the task_ prefix with victim_")
            if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name)
                    or name in {"profiling", "victim_"}):
                raise ValueError(f"Invalid perf_passes name: {name}")
            if not isinstance(events, list) or not events or any(not isinstance(e, str) for e in events):
                raise ValueError(f"perf_passes.{name} must be a nonempty list of event names")
            if len(set(events)) != len(events):
                raise ValueError(f"perf_passes.{name} events must be unique")
            for event in events:
                base, sep, modifier = event.partition(":")
                if base not in {*EVENTS, "task_clock"} or (sep and (modifier not in {"u", "k"} or base == "task_clock")):
                    raise ValueError(f"Unknown perf event: {event}; use a PMU key with optional :u or :k, or task_clock")
    ct = c.setdefault("cyclictest", {})
    keys(ct, "command priority interval_us histogram_us extra_args", "cyclictest")
    for key, value in {"command": "cyclictest", "priority": 90, "interval_us": 1000,
                       "histogram_us": 1000, "extra_args": []}.items():
        ct.setdefault(key, value)
    # Only a single executable here: shell commands are for the workload profiles.
    if len(shlex.split(ct["command"])) != 1:
        raise ValueError("cyclictest.command must name one executable")
    for key in ("priority", "interval_us", "histogram_us"):
        positive(ct[key], "cyclictest." + key, integer=True)
    if ct["priority"] > 99:
        raise ValueError("FIFO priority must be <= 99")
    if isinstance(ct["extra_args"], str):
        ct["extra_args"] = shlex.split(ct["extra_args"])
    # Deliberately a small allowlist: aliases/combined short flags cannot override protocol.
    allowed_extra = {"--verbose", "-v", "--unbuffered", "-u"}
    if not isinstance(ct["extra_args"], list) or any(x not in allowed_extra for x in ct["extra_args"]):
        raise ValueError("extra_args permits only --verbose/-v and --unbuffered/-u; protocol flags are managed")
    scenarios = c.setdefault("scenarios", {"baseline": {"enabled": True}})
    for name, spec in scenarios.items():
        if name not in {"baseline", "interfgen", "demo"} or not isinstance(spec, dict):
            raise ValueError(f"Invalid scenario {name}")
        allowed = {"baseline": "enabled warmup_s", "interfgen": "enabled placement cpus warmup_s command",
                   "demo": "enabled placement cpus profile definition"}
        keys(spec, allowed[name], name)
        spec.setdefault("enabled", True)
        if type(spec["enabled"]) is not bool:
            raise ValueError("enabled must be boolean")
        if not spec["enabled"]:
            continue
        if name != "baseline":
            spec.setdefault("placement", "same_cluster" if name == "interfgen" else "system")
            if spec["placement"] not in {"same_cluster", "system", "explicit"}:
                raise ValueError("Invalid placement")
            if spec["placement"] == "explicit":
                if not spec.get("cpus"):
                    raise ValueError("explicit placement requires cpus")
                for cpu in spec["cpus"]:
                    positive(cpu, "cpus", zero=True, integer=True)
        if name == "interfgen":
            command(spec.get("command"), "interfgen.command")
            spec.setdefault("warmup_s", 1)
            positive(spec["warmup_s"], "warmup_s", zero=True)
        if name == "demo":
            profile = spec.get("definition")
            if not isinstance(profile, dict) or not profile.get("processes"):
                raise ValueError("DEMO requires a profile with processes")
            keys(profile, "name environment_script resources warmup_s startup_timeout_s shutdown_timeout_s processes foreign_process_names", "DEMO profile")
            if not isinstance(profile["processes"], list) or not isinstance(profile.get("resources", {}), dict):
                raise ValueError("DEMO processes must be a list and resources a mapping")
            for key, default in [("warmup_s", 30), ("startup_timeout_s", 120), ("shutdown_timeout_s", 10)]:
                profile.setdefault(key, default)
                positive(profile[key], key, zero=key == "warmup_s")
            names = set()
            for proc in profile["processes"]:
                keys(proc, "name command ready_log_regex ready_command repeat_on_success", "DEMO process")
                name_ = proc.get("name", "")
                if not re.fullmatch(r"[a-zA-Z0-9_-]+", name_) or name_ in names:
                    raise ValueError("DEMO process names must be safe and unique")
                names.add(name_)
                command(proc.get("command"), "process.command")
                if "ready_log_regex" in proc:
                    re.compile(proc["ready_log_regex"])
                if "ready_command" in proc:
                    command(proc["ready_command"], "ready_command")
                if "ready_command" in proc and "ready_log_regex" in proc:
                    raise ValueError("Choose one readiness check per process")
                if type(proc.get("repeat_on_success", False)) is not bool:
                    raise ValueError("repeat_on_success must be boolean")
            targets = []
            for target, source in profile.get("resources", {}).items():
                path = Path(target)
                if path.is_absolute() or ".." in path.parts or target in {"", "."}:
                    raise ValueError("resource targets must be relative paths inside the workspace")
                if not Path(source).is_absolute():
                    raise ValueError("resource sources must be absolute remote paths")
                if any(path == p or path in p.parents or p in path.parents for p in targets):
                    raise ValueError("resource targets must not overlap")
                targets.append(path)
    profile = c.setdefault("profiling", {})
    keys(profile, "enabled repetitions duration_s", "profiling")
    profile.setdefault("enabled", True)
    if type(profile["enabled"]) is not bool:
        raise ValueError("profiling.enabled must be boolean")
    for key, default in [("repetitions", 1), ("duration_s", c["duration_s"])]:
        profile.setdefault(key, default)
        positive(profile[key], "profiling." + key, integer=True)
    recovery = c.setdefault("recovery", {})
    keys(recovery, "max_retries", "recovery")
    recovery.setdefault("max_retries", 2)
    positive(recovery["max_retries"], "max_retries", zero=True, integer=True)
    llm = c.setdefault("llm", {})
    keys(llm, "enabled backend model url confidence_threshold timeout_s required_features features", "llm")
    for key, default in {"enabled": False, "backend": "ollama", "model": "qwen3:8b",
                         "url": "http://localhost:11434", "confidence_threshold": .85,
                         "timeout_s": 30, "required_features": [], "features": ["error_classification"]}.items():
        llm.setdefault(key, default)
    if type(llm["enabled"]) is not bool or llm["backend"] not in {"none", "ollama"}:
        raise ValueError("Invalid LLM backend/enabled")
    positive(llm["timeout_s"], "llm.timeout_s")
    positive(llm["confidence_threshold"], "confidence_threshold", zero=True)
    if llm["confidence_threshold"] > 1 or not set(llm["features"]) <= FEATURES or not set(llm["required_features"]) <= set(llm["features"]):
        raise ValueError("Invalid LLM threshold/features")
    if llm["required_features"] and (not llm["enabled"] or llm["backend"] == "none"):
        raise ValueError("required_features requires an enabled LLM")
    return c


def load_config(path):
    import yaml
    path = Path(path).resolve()
    sources = {"campaign.yaml": path.read_text()}
    try:
        c = yaml.safe_load(sources["campaign.yaml"])
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(c, dict):
        raise ValueError("Campaign must be a YAML mapping")
    demo = c.get("scenarios", {}).get("demo", {})
    if demo.get("enabled", True) and "profile" in demo:
        profile = (path.parent / demo["profile"]).resolve()
        sources["demo.yaml"] = profile.read_text()
        try:
            demo["definition"] = yaml.safe_load(sources["demo.yaml"])
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid DEMO YAML: {exc}") from exc
    c = validate(c)
    return c, sources


def config_hash(c):
    if "perf_passes" in c:
        # Pass order affects acquisition and the primary latency window.
        c = {**c, "perf_passes": list(c["perf_passes"].items())}
    return digest(c)


def matrix(c, core=None, scenario=None):
    rows = []
    for name in ("baseline", "interfgen", "demo"):
        if not c["scenarios"].get(name, {}).get("enabled", False):
            continue
        for cpu in c["victim_cores"]:
            for run in range(1, c["repetitions"] + 1):
                rows.append(dict(core=cpu, scenario=name, run=run, kind="measurement", passes=list(c.get("perf_passes", PASSES)),
                                 duration_s=c["duration_s"]))
            if c["profiling"]["enabled"]:
                for run in range(1, c["profiling"]["repetitions"] + 1):
                    rows.append(dict(core=cpu, scenario=name, run=run, kind="profiling", passes=["profiling"],
                                     duration_s=c["profiling"]["duration_s"]))
    settings = c.get("randomization", {})
    if settings.get("enabled"):
        rng = random.Random(settings["seed"])
        measurements = []
        for run in range(1, c["repetitions"] + 1):
            block = [row for row in rows if row["kind"] == "measurement" and row["run"] == run]
            rng.shuffle(block)
            measurements.extend(block)
        # Diagnostic profiling stays separate from the randomized measurement blocks.
        rows = measurements + [row for row in rows if row["kind"] == "profiling"]
    rows = [row for row in rows if (core is None or row["core"] == core)
            and (not scenario or row["scenario"] == scenario)]
    if not rows:
        raise ValueError("Selection contains no experiments")
    return rows


def selected_config(c, core=None, scenario=None, kind=None):
    result = copy.deepcopy(c)
    if core is not None:
        if core not in c["victim_cores"]:
            raise ValueError("Selected core is not in victim_cores")
        result["victim_cores"] = [core]
    if scenario:
        if not c["scenarios"].get(scenario, {}).get("enabled"):
            raise ValueError("Selected scenario is not enabled")
        for name, spec in result["scenarios"].items():
            spec["enabled"] = name == scenario
    if kind == "measurement":
        result["profiling"]["enabled"] = False
    return result


def placement(spec, victim, topology):
    online = set(topology["online"])
    if victim not in online:
        raise ValueError(f"Victim CPU{victim} offline")
    mode = spec.get("placement", "same_cluster")
    if mode == "same_cluster":
        cpus = set(topology["clusters"].get(str(victim), [])) - {victim}
    elif mode == "system":
        cpus = online - {victim}
    else:
        cpus = set(spec["cpus"])
    if not cpus or victim in cpus or not cpus <= online:
        raise ValueError(f"Invalid interferer placement for CPU{victim}: {sorted(cpus)}")
    return sorted(cpus)
