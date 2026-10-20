"""Table and figure of a cyclictest-job campaign: latency and job times per job profile and scenario.

Reads report/runs.csv (written by the controller) and writes report/job_pilot.{csv,md,svg}.
Per-wakeup PMU values are counts of the CPU-wide core pass divided by cyclictest samples;
cycle-type counts are converted to µs at the fixed CPU3 clock (1.728 GHz).
"""
import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

SCENARIOS = ['baseline', 'interfgen', 'demo']
GHZ = 1.728
PMU = {'cycles_us': ('cycles', GHZ * 1000), 'memory_stall_us': ('memory_stall', GHZ * 1000),
       'instructions_u': ('instructions:u', 1), 'instructions_k': ('instructions:k', 1),
       'bus_accesses': ('bus_accesses', 1)}


def number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def load(campaign):
    rows = []
    with (campaign / 'report/runs.csv').open() as stream:
        for row in csv.DictReader(stream):
            if row['status'] != 'PASS' or row['kind'] != 'measurement':
                continue
            samples = number(row['samples'])
            out = {'job': row['job'], 'scenario': row['scenario'], 'run': int(row['run'])}
            for key in ('mean_us', 'p99_us', 'p99_9_us', 'max_us', 'exec_mean_us', 'exec_p99_us', 'exec_max_us',
                        'response_mean_us', 'response_p99_9_us', 'response_max_us', 'job_missed'):
                out[key] = number(row.get(key))
            for key, (event, scale) in PMU.items():
                value = number(row.get(f'victim_cpu__pass_core__{event}'))
                out[key + '_per_wakeup'] = value / samples / scale if value is not None and samples else None
            rows.append(out)
    return rows


def with_ratios(rows):
    """Deltas and slowdowns against the baseline of the same job profile and run."""
    base = {(r['job'], r['run']): r for r in rows if r['scenario'] == 'baseline'}
    for row in rows:
        ref = base.get((row['job'], row['run']))
        row['delta_mean_us'] = row['mean_us'] - ref['mean_us'] if ref else None
        row['delta_p99_9_us'] = row['p99_9_us'] - ref['p99_9_us'] if ref else None
        row['exec_slowdown'] = (row['exec_mean_us'] / ref['exec_mean_us']
                                if ref and row['exec_mean_us'] and ref['exec_mean_us'] else None)
    return rows


def fmt(value, digits=3):
    return '' if value is None else f'{value:.{digits}f}'


def figure(rows, jobs, target):
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8))
    width = .8 / len(SCENARIOS)
    for index, scenario in enumerate(SCENARIOS):
        values = {r['job']: r for r in rows if r['scenario'] == scenario}
        x = [i + (index - 1) * width for i in range(len(jobs))]
        axes[0].bar(x, [values[j]['mean_us'] if j in values else 0 for j in jobs], width, label=scenario)
        axes[0].scatter(x, [values[j]['p99_9_us'] if j in values else None for j in jobs], marker='_', s=120, color='black')
        axes[1].bar(x, [values[j]['exec_slowdown'] or 0 if j in values else 0 for j in jobs], width, label=scenario)
    axes[0].set_title('Latenza di risveglio: media (barre) e P99,9 (tratti), µs')
    axes[1].set_title('Rallentamento dell\'esecuzione rispetto alla baseline (×)')
    axes[1].axhline(1, color='#ADB7C3', linewidth=1)
    for ax in axes:
        ax.set_xticks(range(len(jobs)), jobs, rotation=25, ha='right')
        ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign', type=Path)
    campaign = parser.parse_args().campaign.resolve()
    rows = with_ratios(load(campaign))
    # Profiles in configuration order (increasing footprint), not in the randomized run order.
    jobs = [j for j in json.loads((campaign / 'config.json').read_text())['jobs'] if any(r['job'] == j for r in rows)]
    target = campaign / 'report'
    fields = list(rows[0])
    with (target / 'job_pilot.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    text = ['# Pilota cyclictest-job', '',
            'Latenze e tempi in µs; Δ rispetto alla baseline dello stesso profilo; PMU per risveglio, CPU-wide.', '',
            '| Profilo | Scenario | Lat. media | Δ media | P99,9 | Δ P99,9 | Max | Exec media | Exec ×  | '
            'Exec P99 | Saltati | Cicli µs | Stall mem µs | Istr. user | Bus acc. |',
            '|' + '---|' * 15]
    for row in sorted(rows, key=lambda r: (jobs.index(r['job']), SCENARIOS.index(r['scenario']), r['run'])):
        text.append(f"| {row['job']} | {row['scenario']} | {fmt(row['mean_us'])} | {fmt(row['delta_mean_us'])} | "
                    f"{fmt(row['p99_9_us'])} | {fmt(row['delta_p99_9_us'])} | {fmt(row['max_us'], 2)} | "
                    f"{fmt(row['exec_mean_us'], 1)} | {fmt(row['exec_slowdown'], 2)} | {fmt(row['exec_p99_us'], 1)} | "
                    f"{fmt(row['job_missed'], 0)} | {fmt(row['cycles_us_per_wakeup'], 2)} | "
                    f"{fmt(row['memory_stall_us_per_wakeup'], 2)} | {fmt(row['instructions_u_per_wakeup'], 0)} | "
                    f"{fmt(row['bus_accesses_per_wakeup'], 0)} |")
    text += ['', '![Pilota](job_pilot.svg)']
    (target / 'job_pilot.md').write_text('\n'.join(text) + '\n')
    figure(rows, jobs, target / 'job_pilot.svg')
    print(target / 'job_pilot.md')


if __name__ == '__main__':
    main()
