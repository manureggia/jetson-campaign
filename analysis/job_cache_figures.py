"""Thesis figures of a cyclictest-job campaign with cache passes (campaign-job-cache.yaml).

latenza_media: mean latency of cyclictest per job profile, baseline / interfgen / tkHPick (CEM demo).
refill_ratio:  victim CPU (CPU-wide), 100 × refills / accesses per cache level, same grouping.
Reads report/runs.csv (PASS measurements; with more repetitions, mean of the per-run values); writes PDF and SVG to report/figure_tesi/.
"""
import argparse
import csv
import json
import statistics
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# Colors and layout of the thesis latency figures (Baseline / INTERFGEN / tkHPick).
SCENARIOS = {'baseline': 'Baseline', 'interfgen': 'INTERFGEN', 'demo': 'tkHPick'}
COLORS = {'baseline': '#cbcbc4', 'interfgen': '#3a5c4b', 'demo': '#bb9841'}
LABELS = {'none': 'Nessun lavoro', 'dasm': 'DASM\n2 + 2 kB', 'planner_seq': 'Planner seq.\n1,28 MB',
          'lidar_seq': 'Lidar seq.\n1,5 + 2 MB'}
CACHES = [('l1d', 'L1 dati', 'cache_l1'), ('l1i', 'L1 istruzioni', 'cache_l1'),
          ('l2', 'L2', 'cache_l2_l3'), ('l3', 'L3 (attribuibile al core)', 'cache_l2_l3')]
INK, MUTED, GRID = '#212b26', '#212b26', '#e7e9e4'
plt.rcParams.update({'font.family': ['Helvetica', 'DejaVu Sans'], 'font.size': 9, 'axes.spines.top': False,
                     'axes.spines.right': False, 'axes.edgecolor': '#afb5af', 'text.color': INK,
                     'axes.labelcolor': INK, 'xtick.color': MUTED, 'ytick.color': MUTED,
                     'legend.frameon': False, 'svg.fonttype': 'none', 'pdf.fonttype': 42,
                     'savefig.facecolor': 'white', 'savefig.bbox': 'tight'})
comma = lambda v, d: f'{v:.{d}f}'.replace('.', ',')


def load(campaign):
    data = {}
    with (campaign / 'report/runs.csv').open() as stream:
        for row in csv.DictReader(stream):
            if row['status'] == 'PASS' and row['kind'] == 'measurement':
                data.setdefault((row['job'], row['scenario']), []).append(row)
    return data


def value(row, key):
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def grouped(ax, data, jobs, metric, digits, unit='', percent=False):
    """metric(row) -> value; bars = mean over runs, dots = runs when there is more than one."""
    width = .26
    for k, scenario in enumerate(SCENARIOS):
        for i, job in enumerate(jobs):
            runs = [v for v in (metric(r) for r in data.get((job, scenario), [])) if v is not None]
            if not runs:
                continue
            x, mean = i + (k - 1) * (width + .03), statistics.mean(runs)
            ax.bar(x, mean, width, color=COLORS[scenario], edgecolor='white', linewidth=.6)
            label = comma(mean, digits) + unit
            base = [v for v in (metric(r) for r in data.get((job, 'baseline'), [])) if v is not None]
            if percent and scenario != 'baseline' and base:
                # Increase over the baseline of the same job profile.
                label += f' ({100 * (mean / statistics.mean(base) - 1):+.0f}%)'
            ax.text(x, mean, ' ' + label, ha='center', va='bottom', rotation=90, fontsize=7, color=INK)
    ax.set_xticks(range(len(jobs)), [LABELS[j] for j in jobs], fontsize=8)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'{v:g}'.replace('.', ',')))
    ax.grid(True, axis='y', color=GRID, linewidth=.6)
    ax.set_axisbelow(True)
    ax.margins(y=.3)


def legend(fig, y):
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in COLORS.values()]
    fig.legend(handles, SCENARIOS.values(), loc='upper center', ncol=3, bbox_to_anchor=(.5, y),
               handlelength=1.2, columnspacing=2)


def save(fig, target, name):
    for ext in ('pdf', 'svg'):
        fig.savefig(target / f'{name}.{ext}')
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign', type=Path)
    campaign = parser.parse_args().campaign.resolve()
    data = load(campaign)
    jobs = [j for j in json.loads((campaign / 'config.json').read_text())['jobs'] if j in LABELS and any(k[0] == j for k in data)]
    target = campaign / 'report/figure_tesi'
    target.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    grouped(ax, data, jobs, lambda r: value(r, 'mean_us'), 2, percent=True)
    ax.set_ylabel('Latenza media (µs)')
    fig.suptitle('Latenza media di cyclictest su CPU3 isolata (cpuset + IRQ), periodo 1 ms', x=.01, ha='left', fontsize=10.5)
    legend(fig, .93)
    fig.tight_layout(rect=(0, 0, 1, .86))
    save(fig, target, 'latenza_media')

    fig, axes = plt.subplots(2, 2, figsize=(10, 6.4), sharex=True)
    for ax, (cache, name, pass_name) in zip(axes.flat, CACHES):
        def ratio(row, cache=cache, pass_name=pass_name):
            refills = value(row, f'victim_cpu__pass_{pass_name}__{cache}_refills')
            accesses = value(row, f'victim_cpu__pass_{pass_name}__{cache}_accesses')
            return 100 * refills / accesses if refills is not None and accesses else None
        grouped(ax, data, jobs, ratio, 1, '%')
        ax.set_title(name, loc='left', fontsize=10)
        ax.set_ylabel('% refill / accessi')
    fig.suptitle('CPU vittima (CPU3, CPU-wide) – refill ratio per livello di cache', x=.01, ha='left', fontsize=10.5)
    legend(fig, .955)
    fig.text(.01, -.01, '100 × somma dei refill / somma degli accessi dello stesso livello, nella stessa passata. '
             'L3: risposte dati da fuori cluster, non il tasso di miss della cache condivisa.', fontsize=7.5, color=MUTED)
    fig.tight_layout(rect=(0, 0, 1, .91))
    save(fig, target, 'refill_ratio')
    print(target)


if __name__ == '__main__':
    main()
