"""Read-only inventory and bounded preflight probes; no sudo or system changes."""
import os
import platform
import resource
import shlex
import shutil
import time
from pathlib import Path

from .common import save_json, timestamp
from .config import PASSES
from .pmu import topology, inventory, placement, pass_events
from .processes import Processes, RunFailure, process_table


CGROUP_HELPER = "/usr/local/sbin/jetson-campaign-cgroup"


def read(path):
    try:
        return Path(path).read_text().replace("\x00", " ").strip()
    except (OSError, TypeError, UnicodeError):
        return None


def telemetry():
    paths = [p for name in ("scaling_cur_freq", "scaling_governor", "scaling_min_freq", "scaling_max_freq")
             for p in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/" + name)]
    paths += list(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
    return {"timestamp": timestamp(), "values": {str(p): read(p) for p in paths if p.is_file()}}


def machine_info():
    return {"hostname": platform.node(), "model": read("/proc/device-tree/model"),
            "soc": read("/proc/device-tree/compatible"), "cpu": read("/proc/cpuinfo"),
            "kernel": platform.release(), "os_release": read("/etc/os-release"),
            "boot_id": read("/proc/sys/kernel/random/boot_id"),
            "perf_event_paranoid": read("/proc/sys/kernel/perf_event_paranoid"),
            "rt_runtime_us": read("/proc/sys/kernel/sched_rt_runtime_us"),
            "rtprio_limit": resource.getrlimit(resource.RLIMIT_RTPRIO),
            "memlock_limit": resource.getrlimit(resource.RLIMIT_MEMLOCK),
            "telemetry": telemetry()}


def shell(command, environment_script=None):
    prefix = f"source {shlex.quote(environment_script)} || exit $?\n" if environment_script else ""
    return ["bash", "-c", prefix + command]


def foreign_workloads(c):
    """Reject known workload executables outside this worker (never signal them)."""
    names = {"meminterf", "iox-roudi", "tkHPick_instance_segmentation", "tkHPick_pose_estimation", "tkCore_bag_play"}
    demo = c["scenarios"].get("demo", {}).get("definition", {})
    commands = [p["command"] for p in demo.get("processes", [])]
    interf = c["scenarios"].get("interfgen", {})
    if interf.get("command"):
        commands.append(interf["command"])
    for cmd in commands:
        words = shlex.split(cmd)
        if words:
            names.add(Path(words[0]).name)
    names.update(demo.get("foreign_process_names", []))
    found = []
    for row in process_table():
        try:
            words = Path(f"/proc/{row['pid']}/cmdline").read_bytes().split(b"\0")
            name = Path(words[0].decode()).name
            if name in names and row["state"] != "Z":
                found.append({**row, "executable": name})
        except (OSError, UnicodeError):
            continue
    return found


def doctor(c, directory, probe=True):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    info = machine_info()
    info.update(errors=[], warnings=[], timestamp=timestamp())
    try:
        info["topology"] = topology()
        info["events"] = inventory()
    except OSError as exc:
        info["errors"].append(f"Topology/PMU unavailable: {exc}")
        info.update(topology={"online": [], "clusters": {}}, events={})
    if "0xd42" not in (info["cpu"] or "").lower():
        info["errors"].append("This verified PMU mapping requires Cortex-A78AE (MIDR part 0xd42)")
    configured = c.get("perf_passes")
    requested = {key: event for name in (configured or PASSES)
                 for key, event in pass_events(name, info["events"], include_unavailable=True,
                                               configured=configured).items()} if info["events"] else {}
    required = {key.partition(":")[0] for key in requested} & {"cycles", "instructions"}
    for name in required:
        if not info["events"].get(name, {}).get("available"):
            info["errors"].append(f"Essential PMU event missing or changed: {name}")
    info["placements"] = {}
    for core in c["victim_cores"]:
        if core not in info["topology"]["online"]:
            info["errors"].append(f"Victim CPU{core} unavailable")
        for scenario, spec in c["scenarios"].items():
            if scenario != "baseline" and spec.get("enabled"):
                try:
                    info["placements"][f"{core}/{scenario}"] = placement(spec, core, info["topology"])
                except ValueError as exc:
                    info["errors"].append(str(exc))
    for core in c["perf_cpus"]:
        if core not in info["topology"]["online"]:
            info["errors"].append(f"perf CPU{core} unavailable")
    tools = ["bash", "python3", "perf", "taskset", shlex.split(c["cyclictest"]["command"])[0]]
    if c["isolcpu"]:
        tools.append("sudo")
        helper = Path(CGROUP_HELPER)
        if not helper.is_file() or not os.access(helper, os.X_OK):
            info["errors"].append(f"Missing cgroup helper: {CGROUP_HELPER}")
        elif helper.stat().st_uid != 0 or helper.stat().st_mode & 0o022:
            info["errors"].append("Cgroup helper must be root-owned and not group/world writable")
    for tool in tools:
        if not shutil.which(tool):
            info["errors"].append(f"Missing command: {tool}")
    if c["profiling"]["enabled"] and not shutil.which("perl"):
        info["errors"].append("Missing perl for FlameGraph")
    free = shutil.disk_usage(directory).free
    info["free_bytes"] = free
    if free < c["min_free_gb"] * 1e9:
        info["errors"].append("Insufficient disk space")
    foreign = foreign_workloads(c)
    info["foreign_workloads"] = foreign
    if foreign:
        info["errors"].append("Foreign DEMO/INTERFGEN workload active; operator must stop it")
    if int(info["rtprio_limit"][0]) < c["cyclictest"]["priority"] and info["rtprio_limit"][0] != -1 and os.geteuid() != 0:
        info["errors"].append("Insufficient FIFO RLIMIT_RTPRIO; no sudo invoked")
    demo_spec = c["scenarios"].get("demo", {})
    profile = demo_spec.get("definition", {}) if demo_spec.get("enabled") else {}
    if profile.get("environment_script") and not Path(profile["environment_script"]).is_file():
        info["errors"].append("Missing DEMO environment_script")
    for target, source in profile.get("resources", {}).items():
        if not Path(source).exists():
            info["errors"].append(f"Missing resource {target}: {source}")
    if probe:
        manager = Processes(directory, time.monotonic() + 180)
        def execute(name, argv, timeout=15):
            try:
                job = manager.spawn(name, argv, cwd=directory)
                return manager.wait(job, timeout)
            except (OSError, RunFailure) as exc:
                info["errors"].append(f"{name}: {exc}")
                return -1
        try:
            for name, argv in [("uname", ["uname", "-a"]), ("lscpu", ["lscpu"]),
                               ("perf_version", ["perf", "--version"]), ("perf_list", ["perf", "list", "--details"]),
                               ("cyclictest_help", [shlex.split(c["cyclictest"]["command"])[0], "--help"])]:
                execute(name, argv)
            if c["isolcpu"] and Path(CGROUP_HELPER).is_file():
                if execute("cgroup_helper", ["sudo", "-n", CGROUP_HELPER, "probe", "--rtprio",
                                             str(c["cyclictest"]["priority"])]) != 0:
                    info["errors"].append("Cgroup helper is not authorized through sudo -n")
            for tool, args in [("nvpmodel", ["-q"]), ("jetson_clocks", ["--show"])]:
                if shutil.which(tool):
                    execute(tool, [tool, *args])
            interf_spec = c["scenarios"].get("interfgen", {})
            check_commands = [("interfgen", interf_spec.get("command") if interf_spec.get("enabled") else None, None)]
            check_commands += [(p["name"], p["command"], profile.get("environment_script")) for p in profile.get("processes", [])]
            for name, cmd, envscript in check_commands:
                if cmd:
                    executable = shlex.split(cmd)[0]
                    if execute("which_" + name, shell("command -v " + shlex.quote(executable), envscript)) != 0:
                        info["errors"].append(f"Executable not available in configured environment: {executable}")
            if shutil.which("perf"):
                for core in c["victim_cores"]:
                    if execute(f"perf_permission_{core}", ["perf", "stat", "-a", "-C", str(core), "-e", "cycles", "--", "sleep", ".05"]) != 0:
                        info["errors"].append(f"perf system-wide access unavailable on CPU{core}")
                # Probe actual event usability independently, retaining stderr for every event.
                for key, selector in requested.items():
                    event = info["events"][key.partition(":")[0]]
                    if event["available"]:
                        code = execute("event_" + key.replace(":", "_"), ["perf", "stat", "-x,", "-e", selector, "--", "sleep", ".05"])
                        event["usable"] = code == 0
                        if code:
                            info["errors" if configured is not None else "warnings"].append(f"Event probe failed: {key}")
                if c["profiling"]["enabled"]:
                    rc = execute("profile_probe", ["perf", "record", "-a", "-C", str(c["victim_cores"][0]),
                                 "-e", "cpu-clock", "-F", "99", "--call-graph", "dwarf,8192",
                                 "-o", str(directory / "profile_probe.data"), "--", "sleep", ".1"])
                    if rc:
                        info["errors"].append("perf record DWARF sampling unavailable")
        finally:
            manager.cleanup()
    info["ok"] = not info["errors"]
    save_json(directory / "doctor.json", info)
    return info
