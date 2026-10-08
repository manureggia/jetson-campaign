"""Reproducible per-run exports, weighted moments, and plain SVG charts."""
import csv
import html
import math
import statistics
from collections import defaultdict
from pathlib import Path

from .common import read_json, save_json
from .config import PASSES
from .pmu import EVENTS, pass_events


def selected_attempts(attempts):
    groups = defaultdict(list)
    for attempt in attempts:
        item = attempt["item"]
        groups[(item["core"], item["scenario"], item["kind"], item["run"])].append(attempt)
    result = []
    for candidates in groups.values():
        generation = max(x.get("generation", 0) for x in candidates)
        current = [x for x in candidates if x.get("generation", 0) == generation]
        valid = [x for x in current if x.get("outcome") == "PASS" and x.get("download_verified")]
        result.append(valid[0] if valid else current[-1])
    return result


def aggregate(rows):
    groups = defaultdict(list)
    for row in rows:
        if (row["status"] == "PASS" and row["kind"] == "measurement"
                and all(row.get(k) is not None for k in ("mean_us", "min_us", "max_us", "samples"))):
            groups[(row["core"], row["scenario"], row["placement"])].append(row)
    output = []
    for (core, scenario, placement), values in sorted(groups.items()):
        complete = all(v.get("sum_us") is not None and v.get("sum_sq_us") is not None for v in values)
        n = sum(v.get("samples", 0) for v in values)
        total = sum(v["sum_us"] for v in values) if complete else None
        square = sum(v["sum_sq_us"] for v in values) if complete else None
        means = [v["mean_us"] for v in values]
        output.append({"core": core, "scenario": scenario, "placement": placement, "runs": len(values),
                       "samples": n, "pooled_mean_us": total / n if complete and n else None,
                       "pooled_std_us": math.sqrt(max(0, square * n - total * total)) / n if complete and n else None,
                       "run_mean_us": statistics.mean(means),
                       "between_run_std_us": statistics.stdev(means) if len(means) > 1 else None,
                       "min_us": min(v["min_us"] for v in values), "max_us": max(v["max_us"] for v in values)})
    return output


def write_csv(path, rows, fields=None):
    fields = fields or sorted({key for row in rows for key in row})
    with Path(path).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def chart(path, rows):
    width, height = 860, max(150, 70 + len(rows) * 55)
    maximum = max((row["max_us"] for row in rows), default=1) or 1
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           '<rect width="100%" height="100%" fill="white"/>',
           '<text x="20" y="30" font-family="sans-serif" font-size="20">Latenza massima per gruppo (µs)</text>']
    for index, row in enumerate(rows):
        y = 55 + index * 55
        label = html.escape(f"CPU{row['core']} / {row['scenario']} / {row['placement']}")
        bar = 440 * row["max_us"] / maximum
        svg += [f'<text x="20" y="{y+18}" font-family="sans-serif" font-size="12">{label}</text>',
                f'<rect x="330" y="{y}" width="{bar}" height="27" fill="#327ca8"/>',
                f'<text x="{340+bar}" y="{y+19}" font-family="sans-serif" font-size="12">{row["max_us"]:g}</text>']
    path.write_text("\n".join(svg + ["</svg>"]))


def generate(campaign):
    campaign = Path(campaign)
    ledger = read_json(campaign / "campaign.json")
    config = read_json(campaign / "config.json")
    rows, details, provenance, warnings = [], [], [], []
    for attempt in selected_attempts(ledger["attempts"]):
        item = attempt["item"]
        out = campaign / "results" / attempt["relative"]
        data = read_json(out / "metrics.json", {"passes": []})
        row = {"core": item["core"], "scenario": item["scenario"], "run": item["run"], "kind": item["kind"],
               "placement": config["scenarios"][item["scenario"]].get("placement", "none"),
               "status": attempt.get("outcome", "INCOMPLETE"), "attempt": attempt["relative"],
               "flamegraph": str((out / "pass_profiling/core.svg").relative_to(campaign)) if (out / "pass_profiling/core.svg").exists() else None}
        catalog = {key: {"event": name, "available": True} for key, (name, _) in EVENTS.items()}
        catalog["task_clock"] = {"event": "task-clock", "available": True}
        configured = config.get("perf_passes")
        passes = list(configured) if configured is not None else PASSES
        latency_pass = "pass_core" if "core" in passes else "pass_" + passes[0]
        for pass_name in passes:
            scopes = ["victim_task"] if pass_name.startswith(("victim_", "task_")) else ["victim_cpu", "interferer"]
            for scope in scopes:
                for logical in pass_events(pass_name, catalog, configured=configured):
                    row[f"{scope}__pass_{pass_name}__{logical}"] = None
        for key in ("min_us", "max_us", "mean_us", "std_us", "samples", "sum_us", "sum_sq_us", "interrupts_total", "interrupts_victim"):
            row[key] = None
        for measured in data["passes"]:
            pass_name = measured["pass"]
            for message in measured.get("warnings", []):
                warnings.append({"attempt": attempt["relative"], "pass": pass_name, "message": message,
                                 "metrics": str((out / pass_name / "metrics.json").relative_to(campaign))})
            if pass_name in {latency_pass, "pass_profiling"}:
                row.update(measured["latency"] or {})
                irq = measured["interrupts"]
                row.update(interrupts_total=irq["total"], interrupts_victim=irq["victim"])
                for column in ("min_us", "max_us", "mean_us", "std_us", "samples", "interrupts_total", "interrupts_victim"):
                    raw_name = "interrupts_delta.csv" if column.startswith("interrupts_") else "cyclictest.json"
                    provenance.append({"attempt": attempt["relative"], "column": column, "pass": pass_name,
                        "scope": "victim_cpu" if column.startswith("interrupts_") else "victim_task", "event": "",
                        "raw": str((out / pass_name / raw_name).relative_to(campaign)),
                        "metrics": str((out / pass_name / "metrics.json").relative_to(campaign))})
            for scope, metrics in measured.get("perf", {}).items():
                for logical, record in metrics.items():
                    key = f"{scope}__{pass_name}__{logical}"
                    row[key] = record["value"]
                    provenance.append({"attempt": attempt["relative"], "column": key,
                        "pass": pass_name, "scope": scope, "event": record["event"],
                        "raw": str((out / pass_name / record["raw"]).relative_to(campaign)),
                        "metrics": str((out / pass_name / "metrics.json").relative_to(campaign))})
        if row["status"] == "PASS" and not data["passes"]:
            row.update(status="INCOMPLETE", report_issue="Missing measurements despite PASS marker")
        rows.append(row)
        details.append({"run": row, "details": data})
    target = campaign / "report"
    target.mkdir(exist_ok=True)
    aggregates = aggregate(rows)
    write_csv(target / "runs.csv", rows)
    write_csv(target / "aggregates.csv", aggregates)
    write_csv(target / "provenance.csv", provenance)
    write_csv(target / "warnings.csv", warnings, ["attempt", "pass", "message", "metrics"])
    save_json(target / "warnings.json", warnings)
    save_json(target / "runs.json", details)
    save_json(target / "aggregates.json", aggregates)
    save_json(target / "attempts.json", ledger["attempts"])
    chart(target / "latency.svg", aggregates)
    text = ["# Risultati campagna", "", f"Configurazione: `{ledger['config_hash']}`", "",
            "Le colonne PMU includono scope e passata: provengono da finestre separate.",
            "Profiling e tentativi invalidi sono esclusi dagli aggregati. Valori vuoti indicano dati non disponibili.",
            "La deviazione standard interna/pooled è della popolazione; quella fra run è campionaria (N/A con n=1).", "",
            "| Core | Scenario | Tipo | Run | Stato | Risultati |", "|---|---|---|---|---|---|"]
    for row in rows:
        text.append(f"| {row['core']} | {row['scenario']} | {row['kind']} | {row['run']} | {row['status']} | [raw](../results/{row['attempt']}/) |")
    if warnings:
        text += ["", "## Avvisi non bloccanti", "",
                 "Le acquisizioni sono proseguite. I dati disponibili sono conservati; "
                 "i totali IRQ non validabili restano N/A. Valutare questi avvisi prima di usare i risultati.", "",
                 "| Tentativo | Passata | Avviso | Dettagli |", "|---|---|---|---|"]
        for warning in warnings:
            text.append(f"| {warning['attempt']} | {warning['pass']} | {warning['message']} | "
                        f"[metriche](../{warning['metrics']}) |")
        text += ["", "[CSV avvisi](warnings.csv) · [JSON avvisi](warnings.json)"]
    text += ["", "![Latenze](latency.svg)", "", "[CSV run](runs.csv) · [JSON](runs.json) · [Provenienza PMU](provenance.csv)"]
    (target / "summary.md").write_text("\n".join(text) + "\n")
    return target
