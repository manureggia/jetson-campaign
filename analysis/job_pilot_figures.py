"""Thesis figures of a cyclictest-job campaign: mean, P99 and max per job profile and scenario.

latency: what cyclictest measures, from the programmed release to the thread reading the clock
         in user space after the wake-up (scheduling latency).
completion: from the programmed release to the end of the last useful work of the activation,
            read before going back to sleep. Without a job it coincides with the latency.

Across repetitions: mean of the means, mean of the P99s, max of the maxima; each repetition is a dot.
Reads report/runs.csv; writes PDF and SVG to report/figure_tesi/.
"""
import argparse
import csv
import json
import statistics
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

SCENARIOS = {'baseline': 'Baseline', 'interfgen': 'Interfgen (memcpy su CPU0–2)', 'demo': 'Demo CEM'}
# Slots 1-3 of the validated categorical palette.
COLORS = {'baseline': '#2a78d6', 'interfgen': '#eb6834', 'demo': '#1baf7a'}
LABELS = {'none': 'Nessun\nlavoro', 'dasm': 'DASM\n2 + 2 kB', 'planner_seq': 'Planner\nseq. 1,28 MB',
          'lidar_seq': 'Lidar seq.\n1,5 + 2 MB', 'none_10ms': 'Nessun\nlavoro', 'planner_chase': 'Planner\ncasuale 1,28 MB',
          'lidar_chase': 'Lidar casuale\n1,5 + 2 MB'}
STATS = [('mean', 'Media', statistics.mean), ('p99', 'P99', statistics.mean), ('max', 'Massimo', max)]
INK, MUTED, GRID = '#18283C', '#5B6B7C', '#D9DEE4'
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9, 'axes.titlesize': 10,
                     'axes.labelsize': 9, 'axes.spines.top': False, 'axes.spines.right': False,
                     'axes.edgecolor': '#ADB7C3', 'axes.linewidth': .8, 'text.color': INK,
                     'axes.labelcolor': INK, 'xtick.color': MUTED, 'ytick.color': MUTED,
                     'legend.frameon': False, 'svg.fonttype': 'none', 'pdf.fonttype': 42,
                     'savefig.facecolor': 'white', 'savefig.bbox': 'tight'})


def comma(value, digits):
    return f'{value:.{digits}f}'.replace('.', ',')


def load(campaign):
    """{(job, scenario): {'latency': {stat: [per run]}, 'completion': {...}}} in µs."""
    data = {}
    with (campaign / 'report/runs.csv').open() as stream:
        for row in csv.DictReader(stream):
            if row['status'] != 'PASS' or row['kind'] != 'measurement':
                continue
            entry = data.setdefault((row['job'], row['scenario']), {'latency': {}, 'completion': {}})
            for stat, _, _ in STATS:
                latency = float(row[f'{stat}_us'])
                completion = row.get(f'response_{stat}_us')
                entry['latency'].setdefault(stat, []).append(latency)
                # No job: the activation ends at the clock read, completion = latency.
                entry['completion'].setdefault(stat, []).append(float(completion) if completion else latency)
    return data


def periods(campaign, data):
    config = json.loads((campaign / 'config.json').read_text())
    groups = {}
    for job, spec in config['jobs'].items():
        if any(key[0] == job for key in data):
            interval = (spec or {}).get('interval_us', config['cyclictest']['interval_us'])
            groups.setdefault(f'{interval / 1000:g} ms', []).append(job)
    return list(groups.items())


def panel(ax, data, jobs, metric, stat, reduce, scale, digits):
    width = .27
    for k, scenario in enumerate(SCENARIOS):
        for i, job in enumerate(jobs):
            entry = data.get((job, scenario))
            if not entry:
                continue
            runs = [v / scale for v in entry[metric][stat]]
            x = i + (k - 1) * (width + .03)
            value = reduce(runs)
            ax.bar(x, value, width, color=COLORS[scenario], edgecolor='white', linewidth=.5)
            if len(runs) > 1:
                ax.scatter([x] * len(runs), runs, s=8, color=INK, zorder=3)
            # Tiny values on a ms axis (no job) keep a readable µs label.
            text = comma(value * scale, 1) + ' µs' if scale > 1 and value < .1 else comma(value, digits)
            ax.text(x, value, text, ha='center', va='bottom', fontsize=6.5, color=MUTED,
                    rotation=90 if text.endswith('µs') else 0)
    ax.set_xticks(range(len(jobs)), [LABELS[j] for j in jobs], fontsize=7.5)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f'{v:g}'.replace('.', ',')))
    ax.grid(True, axis='y', color=GRID, linewidth=.6)
    ax.set_axisbelow(True)
    ax.margins(y=.15)


def figure(data, groups, metric, title, name, target):
    # Columns: period groups (own y scale); rows: statistics.
    fig, axes = plt.subplots(len(STATS), len(groups), figsize=(4.4 * len(groups) + .6, 2.3 * len(STATS)),
                             squeeze=False, gridspec_kw={'width_ratios': [len(g) for _, g in groups]})
    for c, (period, jobs) in enumerate(groups):
        big = metric == 'completion' and period != '1 ms'
        scale, unit, digits = (1000, 'ms', 2) if big else (1, 'µs', 1)
        for r, (stat, label, reduce) in enumerate(STATS):
            ax = axes[r][c]
            panel(ax, data, jobs, metric, stat, reduce, scale, digits)
            ax.set_title(f'{label} · periodo {period}', loc='left')
            ax.set_ylabel(unit)
            if r < len(STATS) - 1:
                ax.set_xticklabels([])
    handles = [plt.Rectangle((0, 0), 1, 1, color=color) for color in COLORS.values()]
    fig.legend(handles, SCENARIOS.values(), loc='upper left', ncol=3, bbox_to_anchor=(0, .955))
    fig.suptitle(title, x=.01, ha='left', fontsize=10.5)
    fig.tight_layout(rect=(0, 0, 1, .93))
    for ext in ('pdf', 'svg'):
        fig.savefig(target / f'{name}.{ext}')
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign', type=Path)
    campaign = parser.parse_args().campaign.resolve()
    data = load(campaign)
    groups = periods(campaign, data)
    target = campaign / 'report/figure_tesi'
    target.mkdir(exist_ok=True)
    for old in target.glob('*'):
        old.unlink()
    figure(data, groups, 'latency',
           'Latenza di cyclictest su CPU3 isolata: dal rilascio al ritorno in user space dopo il risveglio',
           'latenza', target)
    figure(data, groups, 'completion',
           'Completion time su CPU3 isolata: dal rilascio alla fine dell\'ultimo lavoro utile dell\'attivazione',
           'completion_time', target)
    print(target)


if __name__ == '__main__':
    main()
