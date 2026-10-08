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


def histogram_summary(histogram):
    pairs = [(int(x), int(n)) for x, n in histogram.items()]
    if any(x < 0 or n < 0 for x, n in pairs):
        raise ValueError("Negative histogram bucket/count")
    n = sum(count for _, count in pairs)
    total = sum(x * count for x, count in pairs)
    squares = sum(x * x * count for x, count in pairs)
    return {"samples": n, "sum_us": total, "sum_sq_us": squares,
            "mean_us": total / n if n else None,
            "std_us": math.sqrt(max(0, squares * n - total * total)) / n if n else None}


def cyclictest(path, victim):
    path = Path(path)
    data = json.loads(path.read_text())
    threads = [t for t in data.get("thread", {}).values() if t.get("cpu") == victim]
    if len(threads) != 1 or data.get("return_code", 0) != 0:
        raise ValueError("Missing/ambiguous cyclictest victim thread or unsuccessful output")
    thread = threads[0]
    if data.get("resolution_in_ns", 0):
        raise ValueError("Unexpected nanosecond cyclictest output")
    histogram = thread.get("histogram", {})
    moments = histogram_summary(histogram)
    n = thread.get("cycles")
    if type(n) is not int or n <= 0 or moments["samples"] > n:
        raise ValueError("Invalid cyclictest sample count")
    complete = moments["samples"] == n
    result = {"min_us": number(thread.get("min")), "max_us": number(thread.get("max")),
              "mean_us": moments["mean_us"] if complete else number(thread.get("avg")),
              "reported_mean_us": number(thread.get("avg")), "samples": n,
              "histogram_samples": moments["samples"], "overflow_samples": n - moments["samples"],
              "histogram_complete": complete, "raw": path.name,
              "std_us": moments["std_us"] if complete else None,
              "sum_us": moments["sum_us"] if complete else None,
              "sum_sq_us": moments["sum_sq_us"] if complete else None}
    for label, p in [("p50_us", .5), ("p90_us", .9), ("p99_us", .99), ("p99_9_us", .999)]:
        result[label] = percentile(histogram, p) if complete else None
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


def process_pass(directory, victim, minimum_pct):
    directory = Path(directory)
    meta = json.loads((directory / "measurement.json").read_text())
    result = {"pass": directory.name, "window": meta, "latency": None, "perf": {}, "issues": [], "warnings": []}
    try:
        result["latency"] = cyclictest(directory / "cyclictest.json", victim)
        if not result["latency"]["histogram_complete"]:
            result["issues"].append("Histogram overflow: full std/quantiles unavailable")
    except (ValueError, OSError) as exc:
        result["issues"].append(str(exc))
    result["interrupts"] = interrupts(directory / "interrupts_before.txt", directory / "interrupts_after.txt",
                                         victim, directory / "interrupts_delta.csv")
    result["warnings"] += result["interrupts"]["issues"]
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
