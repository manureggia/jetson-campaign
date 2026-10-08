"""Explicit Cortex-A78AE mapping, checked against sysfs at runtime."""
from pathlib import Path
from .config import placement

PMU = "armv8_cortex_a78"
# ARM 101779_0001_06_en C2.3. No generic aliases or invented DRAM proxy.
EVENTS = {
    "cycles": ("cpu_cycles", 0x11), "instructions": ("inst_retired", 0x08),
    "backend_stall": ("stall_backend", 0x24), "memory_stall": ("stall_backend_mem", 0x4005),
    "memory_accesses": ("mem_access", 0x13), "bus_accesses": ("bus_access", 0x19),
    "bus_cycles": ("bus_cycles", 0x1D),
    "l1d_accesses": ("l1d_cache", 0x04), "l1d_refills": ("l1d_cache_refill", 0x03),
    "l1d_long_miss_reads": ("l1d_cache_lmiss_rd", 0x39),
    "l1i_accesses": ("l1i_cache", 0x14), "l1i_refills": ("l1i_cache_refill", 0x01),
    "l1i_long_misses": ("l1i_cache_lmiss", 0x4006),
    "l2_accesses": ("l2d_cache", 0x16), "l2_refills": ("l2d_cache_refill", 0x17),
    "l2_long_miss_reads": ("l2d_cache_lmiss_rd", 0x4009),
    "l3_accesses": ("l3d_cache", 0x2B), "l3_refills": ("l3d_cache_refill", 0x2A),
    "l3_long_miss_reads": ("l3d_cache_lmiss_rd", 0x400B),
    "branch_predictions": ("br_pred", 0x12),
    "branch_mispredictions": ("br_mis_pred", 0x10),
    "branches_retired": ("br_retired", 0x21),
    "branch_mispredictions_retired": ("br_mis_pred_retired", 0x22),
}


def expand(spec):
    out = set()
    for part in spec.strip().split(","):
        if "-" in part:
            a, b = map(int, part.split("-"))
            out.update(range(a, b + 1))
        elif part:
            out.add(int(part))
    return sorted(out)


def topology(root=Path("/sys/devices/system/cpu")):
    online = expand((root / "online").read_text())
    clusters = {}
    for cpu in online:
        base = root / f"cpu{cpu}"
        shared = []
        for index in (base / "cache").glob("index*"):
            if (index / "level").read_text().strip() == "3":
                shared = expand((index / "shared_cpu_list").read_text())
        if not shared:
            cluster_file = base / "topology/cluster_cpus_list"
            if cluster_file.exists():
                shared = expand(cluster_file.read_text())
        clusters[str(cpu)] = shared
    return {"online": online, "clusters": clusters}


def inventory(root=Path("/sys/bus/event_source/devices")):
    records = {}
    for logical, (name, expected) in EVENTS.items():
        path = root / PMU / "events" / name
        text = path.read_text().strip() if path.exists() else None
        matches = text is not None and text.lower() == f"event=0x{expected:04x}"
        # Kernel versions may omit leading zeroes.
        if text and text.startswith("event="):
            try:
                matches = int(text.split("=", 1)[1], 0) == expected
            except ValueError:
                matches = False
        records[logical] = {"event": f"{PMU}/{name}/", "encoding": text,
                            "expected_encoding": hex(expected), "available": matches,
                            "reason": "verified_sysfs" if matches else "missing_or_encoding_mismatch"}
    records["task_clock"] = {"event": "task-clock", "available": True, "reason": "software_event"}
    return records


def pass_events(name, catalog, include_unavailable=False, configured=None):
    base = name.removeprefix("victim_").removeprefix("task_")  # Historical reports.
    if configured is not None:
        keys = configured[name]
    elif base == "core":
        keys = ["cycles", "instructions", "task_clock", "backend_stall", "memory_stall"]
    elif base == "memory":
        keys = ["cycles", "memory_accesses", "bus_accesses", "bus_cycles"]
    else:
        miss = base + ("_long_misses" if base == "l1i" else "_long_miss_reads")
        keys = ["cycles", base + "_accesses", base + "_refills", miss]
    result = {}
    for key in keys:
        logical, _, modifier = key.partition(":")
        record = catalog[logical]
        if include_unavailable or record["available"]:
            # Named PMU events use /u, /k; generic events use :u, :k.
            event = record["event"]
            result[key] = event + (("" if event.endswith("/") else ":") + modifier if modifier else "")
    return result
