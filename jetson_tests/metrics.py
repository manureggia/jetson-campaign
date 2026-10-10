"""Deterministic metrics, preserving original values and provenance."""
import csv
import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .common import save_json
from .legacy_parsers import percentile, irq_kind, parse_interrupt_snapshot


def number(text):
    try:
        value = Decimal(str(text).strip())
        if not value.is_finite():
            return None
        return int(value) if value == value.to_integral_value() else float(value)
    except (InvalidOperation, ValueError):
        return None


def histogram_summary(histogram, scale=1):
    """Moments in µs; scale is the number of histogram units per µs (1000 for -N)."""
    pairs = [(int(x), int(n)) for x, n in histogram.items()]
    if any(x < 0 or n < 0 for x, n in pairs):
        raise ValueError("Negative histogram bucket/count")
    n = sum(count for _, count in pairs)
    total = sum(x * count for x, count in pairs) / scale
    squares = sum(x * x * count for x, count in pairs) / (scale * scale)
    if scale == 1:
        total, squares = int(total), int(squares)
    return {"samples": n, "sum_us": total, "sum_sq_us": squares,
            "mean_us": total / n if n else None,
            "std_us": math.sqrt(max(0, squares * n - total * total)) / n if n else None}


def ranked_quantile(histogram, samples, p):
    """Quantile over all samples when overflow exists: overflows lie above the range,
    so a rank that falls inside the histogram is still exact; otherwise None."""
    target = math.ceil(samples * p)
    cumulative = 0
    for bucket, count in sorted((float(x), int(c)) for x, c in histogram.items()):
        cumulative += count
        if cumulative >= target:
            return bucket
    return None


def cyclictest(path, victim):
    path = Path(path)
    data = json.loads(path.read_text())
    threads = [t for t in data.get("thread", {}).values() if t.get("cpu") == victim]
    if len(threads) != 1 or data.get("return_code", 0) != 0:
        raise ValueError("Missing/ambiguous cyclictest victim thread or unsuccessful output")
    thread = threads[0]
    # Output keys stay in µs; -N runs (resolution_in_ns) are converted, never rounded.
    scale = 1000 if data.get("resolution_in_ns", 0) else 1
    def us(value):
        value = number(value)
        return None if value is None else (value / scale if scale != 1 else value)
    raw = thread.get("histogram", {})
    histogram = {int(x) / scale if scale != 1 else x: n for x, n in raw.items()}
    moments = histogram_summary(raw, scale)
    n = thread.get("cycles")
    if type(n) is not int or n <= 0 or moments["samples"] > n:
        raise ValueError("Invalid cyclictest sample count")
    complete = moments["samples"] == n
    result = {"min_us": us(thread.get("min")), "max_us": us(thread.get("max")),
              "mean_us": moments["mean_us"] if complete else us(thread.get("avg")),
              "reported_mean_us": us(thread.get("avg")), "samples": n,
              "resolution_ns": 1 if scale != 1 else 1000,
              "histogram_samples": moments["samples"], "overflow_samples": n - moments["samples"],
              "histogram_complete": complete, "raw": path.name,
              "std_us": moments["std_us"] if complete else None,
              "sum_us": moments["sum_us"] if complete else None,
              "sum_sq_us": moments["sum_sq_us"] if complete else None}
    for label, p in [("p50_us", .5), ("p90_us", .9), ("p99_us", .99), ("p99_9_us", .999)]:
        result[label] = percentile(histogram, p) if complete else ranked_quantile(histogram, n, p)
    if any(result[k] is None for k in ("min_us", "max_us", "mean_us")):
        raise ValueError("Missing cyclictest summary")
    return result


def perf(path, events, minimum_pct=90):
    """Parse perf stat -x, --no-big-num (not perf's human-oriented output)."""
    result = {logical: {"value": None, "status": "missing", "event": event,
                        "raw": Path(path).name} for logical, event in events.items()}
    if not Path(path).exists():
        return result
    reverse = {event: logical for logical, event in events.items()}
    for row in csv.reader(Path(path).read_text().splitlines()):
        if len(row) < 3 or row[0].startswith("#"):
            continue
        event = row[2].strip()
        logical = reverse.get(event)
        if logical is None:
            continue  # No prefix matching: l1d_cache must not match l1d_cache_refill.
        value = number(row[0])
        # perf -G inserts a cgroup column before time_running and running_pct.
        offset = 1 if len(row) > 3 and number(row[3]) is None else 0
        running = number(row[3 + offset]) if len(row) > 3 + offset else None
        pct = number(row[4 + offset]) if len(row) > 4 + offset else None
        status = "ok" if value is not None else row[0].strip()
        if value is not None and (pct is None or not 0 < pct <= 100 or pct < minimum_pct):
            status = "insufficient_running_time"
        result[logical] = {"value": value if status == "ok" else None, "reported_value": value,
                           "unit": row[1], "event": event, "status": status,
                           "time_running_ns": running, "running_pct": pct,
                           "time_enabled_ns": running * 100 / pct if running is not None and pct and pct > 0 else None,
                           "raw": Path(path).name}
    return result


def interrupts(before_path, after_path, victim, output):
    cpus, before = parse_interrupt_snapshot(Path(before_path))
    after_cpus, after = parse_interrupt_snapshot(Path(after_path))
    rows, issues = [], []
    if not cpus or cpus != after_cpus or not before or not after:
        issues.append("Missing snapshot or CPU header changed")
    else:
        for irq in sorted(before.keys() | after.keys()):
            old, old_name = before.get(irq, ([], ""))
            new, new_name = after.get(irq, ([], ""))
            for i, cpu in enumerate(cpus):
                a = old[i] if old else None
                b = new[i] if new else None
                valid = a is not None and b is not None and b >= a and old_name == new_name
                if not valid:
                    issues.append(f"Changed/missing/reset IRQ {irq} on {cpu}")
                rows.append(dict(irq=irq, description=new_name or old_name,
                                 kind=irq_kind(irq, new_name or old_name), cpu=cpu,
                                 before=a, after=b, delta=b - a if valid else None))
    output = Path(output)
    with output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["irq", "description", "kind", "cpu", "before", "after", "delta"])
        writer.writeheader()
        writer.writerows(rows)
    totals = {cpu: sum(r["delta"] for r in rows if r["cpu"] == cpu) for cpu in cpus} if not issues else {}
    return {"per_cpu": totals, "total": sum(totals.values()) if totals else None,
            "victim": totals.get(f"CPU{victim}"), "issues": issues, "raw": output.name}


def softirqs(before_path, after_path, victim, output):
    """Per-CPU /proc/softirqs deltas; victim holds the counts of the victim CPU."""
    cpus, before = parse_interrupt_snapshot(Path(before_path))
    after_cpus, after = parse_interrupt_snapshot(Path(after_path))
    rows, issues = [], []
    if not cpus or cpus != after_cpus or not before or not after:
        issues.append("Missing softirq snapshot or CPU header changed")
    else:
        for name in sorted(before.keys() | after.keys()):
            old, new = before.get(name, ([], ""))[0], after.get(name, ([], ""))[0]
            for i, cpu in enumerate(cpus):
                a = old[i] if old else None
                b = new[i] if new else None
                valid = a is not None and b is not None and b >= a
                if not valid:
                    issues.append(f"Changed/missing/reset softirq {name} on {cpu}")
                rows.append(dict(softirq=name, cpu=cpu, before=a, after=b, delta=b - a if valid else None))
    output = Path(output)
    with output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["softirq", "cpu", "before", "after", "delta"])
        writer.writeheader()
        writer.writerows(rows)
    victim_rows = {r["softirq"]: r["delta"] for r in rows if r["cpu"] == f"CPU{victim}"}
    return {"victim": victim_rows, "issues": issues, "raw": output.name}


# Below this many SCHED softirqs per second an isolated victim is considered quiet.
SCHED_SOFTIRQ_BURST_PER_S = 1


def process_pass(directory, victim, minimum_pct):
    directory = Path(directory)
    meta = json.loads((directory / "measurement.json").read_text())
    result = {"pass": directory.name, "window": meta, "latency": None, "perf": {}, "issues": [], "warnings": [],
              # Per-task/cgroup counters run on every context switch of cyclictest and
              # inflate its latency (+2.4-3.2 µs measured): only CPU-wide passes are a reference.
              "latency_role": ("perturbed_by_task_counters" if directory.name.startswith("pass_victim_")
                               else "reference")}
    try:
        result["latency"] = cyclictest(directory / "cyclictest.json", victim)
        if not result["latency"]["histogram_complete"]:
            result["warnings"].append(
                f"Histogram overflow: {result['latency']['overflow_samples']} samples above the range; "
                "max and in-range quantiles kept, std/pooled moments unavailable")
    except (ValueError, OSError) as exc:
        result["issues"].append(str(exc))
    result["interrupts"] = interrupts(directory / "interrupts_before.txt", directory / "interrupts_after.txt",
                                         victim, directory / "interrupts_delta.csv")
    result["warnings"] += result["interrupts"]["issues"]
    result["softirqs"] = softirqs(directory / "softirqs_before.txt", directory / "softirqs_after.txt",
                                  victim, directory / "softirqs_delta.csv")
    result["warnings"] += result["softirqs"]["issues"]
    sched = result["softirqs"]["victim"].get("SCHED") or 0
    seconds = meta.get("cyclictest_end_monotonic", 0) - meta.get("cyclictest_start_monotonic", 0)
    # On an isolated victim, SCHED softirqs are NOHZ idle-balance work done for other CPUs:
    # they add kernel instructions and stalls to this pass's PMU counters.
    if meta.get("cgroup") and seconds > 0 and sched / seconds >= SCHED_SOFTIRQ_BURST_PER_S:
        result["warnings"].append(
            f"SCHED softirq burst on isolated CPU{victim}: {sched} ({sched / seconds:.0f}/s); "
            "PMU counters of this pass include idle load-balance work")
    for scope, expected in meta.get("scopes", {}).items():
        result["perf"][scope] = perf(directory / f"perf_{scope}.csv", expected, minimum_pct)
        for key, record in result["perf"][scope].items():
            if key in meta.get("unavailable_events", []):
                record.update(value=None, status="unavailable_on_this_hardware")
                continue
            if record["status"] != "ok":
                result["issues"].append(f"{scope}/{key}: {record['status']}")
    if "victim_task" in result["perf"] and result["latency"]:
        task = result["perf"]["victim_task"]
        instructions = task.get("instructions", {}).get("value")
        if instructions is None:
            split = [task.get(key, {}).get("value") for key in ("instructions:u", "instructions:k")]
            if all(value is not None for value in split):
                instructions = sum(split)
        if instructions is not None and instructions <= result["latency"]["samples"]:
            result["issues"].append("victim_task instructions do not cover cyclictest samples")
    save_json(directory / "metrics.json", result)
    return result
