"""Extracted from ../scripts/analyze_results.py; see docs/REUSE.md."""
import math
from pathlib import Path
NA = "N/A"

def percentile(histogram, percentile_value):
    pairs = sorted((float(bucket), int(count)) for bucket, count in histogram.items())
    total = sum(count for _, count in pairs)
    if not total:
        return None
    target = math.ceil(total * percentile_value)
    cumulative = 0
    for bucket, count in pairs:
        cumulative += count
        if cumulative >= target:
            return bucket
    return pairs[-1][0]

def irq_kind(label, name):
    text = f"{label} {name}".lower()
    if "timer" in text:
        return "timer"
    if "rescheduling" in text or label.upper() == "RES":
        return "scheduler_ipi"
    if label.upper() in {"IPI", "CAL", "TLB", "LOC", "ERR"} or "ipi" in text:
        return "ipi_or_local"
    return "hardware_irq" if label.strip().isdigit() else "local_non_numeric"

def parse_interrupt_snapshot(path: Path):
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return [], {}
    if not lines:
        return [], {}
    cpus = [token for token in lines[0].split() if token.startswith("CPU")]
    rows = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        label, remainder = line.split(":", 1)
        tokens = remainder.split()
        counts = []
        for token in tokens[: len(cpus)]:
            try:
                counts.append(int(token))
            except ValueError:
                break
        if len(counts) != len(cpus):
            continue
        name = " ".join(tokens[len(cpus):]) or NA
        rows[label.strip()] = (counts, name)
    return cpus, rows
