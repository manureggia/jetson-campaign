"""Latency statistics of a cyclictest-job campaign in the style of the thesis figures.

Groups Media / P99 / P99,9 / Massimo, bars Baseline / INTERFGEN / tkHPick, logarithmic y axis.
Across repetitions: mean of the means, mean of the P99 and of the P99.9, max of the maxima.
One panel per victim CPU (and per job profile if the campaign has more than one).
Usage: .venv/bin/python analysis/job_latency_stats.py results/thesis/<campaign> [<campaign> ...]
Reads report/runs.csv (PASS measurements); with several campaigns, side by side panels on the same
axis, in the order given. Writes report/figure_tesi/latenza_statistiche.{pdf,svg} of the last one.
"""
import argparse
import csv
import statistics
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

SCENARIOS = [('baseline', 'Baseline', '#cbcbc4'), ('interfgen', 'INTERFGEN', '#3a5c4b'), ('demo', 'tkHPick', '#bb9841')]
STATS = [('mean_us', 'Media', statistics.mean), ('p99_us', 'P99', statistics.mean),
         ('p99_9_us', 'P99,9', statistics.mean), ('max_us', 'Massimo', max)]
JOBS = {'none': 'nessun lavoro', 'h3_frame': 'frame H3 Vision, scrittura 1,31 MB', 'h3_track': 'tracking H3 Vision, 60 marker',
        'h3_frame_track': 'frame + tracking H3 Vision'}
INK, GRID, AXIS = '#212b26', '#e7e9e4', '#afb5af'
plt.rcParams.update({'font.family': ['Helvetica', 'DejaVu Sans'], 'font.size': 9, 'axes.spines.top': False,
                     'axes.spines.right': False, 'axes.edgecolor': AXIS, 'text.color': INK, 'axes.labelcolor': INK,
                     'xtick.color': INK, 'ytick.color': INK, 'legend.frameon': False, 'svg.fonttype': 'none',
                     'pdf.fonttype': 42, 'savefig.facecolor': 'white', 'savefig.bbox': 'tight'})


def label(value):
    return (f'{value:.1f}' if value < 10 else f'{value:.0f}').replace('.', ',')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign', type=Path, nargs='+')
    campaigns = [path.resolve() for path in parser.parse_args().campaign]
    data, panels = {}, []
    for campaign in campaigns:
        with (campaign / 'report/runs.csv').open() as stream:
            rows = [row for row in csv.DictReader(stream) if row['status'] == 'PASS' and row['kind'] == 'measurement']
        keys = sorted({(row['core'], row.get('job') or '') for row in rows})
        if any(key in data for key in keys):
            raise SystemExit(f'{campaign.name}: CPU and job already in an earlier campaign')
        for row in rows:
            data.setdefault((row['core'], row.get('job') or ''), {}).setdefault(row['scenario'], []).append(row)
        panels += keys
    # Only the scenarios the campaign ran, centred on each group.
    scenarios = [s for s in SCENARIOS if any(s[0] in data[panel] for panel in panels)]
    fig, axes = plt.subplots(1, len(panels), figsize=(5.2 * len(panels), 3.4), sharey=True, squeeze=False)
    width = .26
    for ax, (core, job) in zip(axes[0], panels):
        for k, (scenario, _, color) in enumerate(scenarios):
            runs = data[core, job].get(scenario, [])
            for i, (key, _, reduce) in enumerate(STATS):
                values = [float(r[key]) for r in runs if r.get(key)]
                if not values:
                    continue
                x, value = i + (k - (len(scenarios) - 1) / 2) * (width + .02), reduce(values)
                ax.bar(x, value, width, color=color, edgecolor='white', linewidth=.6)
                ax.text(x, value * 1.08, label(value), ha='center', va='bottom', rotation=90, fontsize=7)
        ax.set_yscale('log')
        ax.set_ylim(1, None)
        ax.yaxis.set_major_locator(FixedLocator([1, 2, 5, 10, 20, 50, 100, 200, 500, 1000]))
        ax.yaxis.set_minor_locator(NullLocator())
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f'{v:g}'))
        ax.set_ylim(1, ax.get_ylim()[1] * 2.2)
        ax.set_xticks(range(len(STATS)), [name for _, name, _ in STATS], fontsize=9.5)
        ax.grid(True, axis='y', color=GRID, linewidth=.8)
        ax.set_axisbelow(True)
        title = f'Vittima su CPU{core}' + (f' – {JOBS.get(job, job)}' if job else '')
        ax.set_title(title, fontweight='bold', fontsize=11)
    axes[0][0].set_ylabel('Latenza [µs]')
    fig.legend([plt.Rectangle((0, 0), 1, 1, color=c) for _, _, c in scenarios], [n for _, n, _ in scenarios],
               loc='upper center', ncol=len(scenarios), bbox_to_anchor=(.5, 1.02), handlelength=1.2, columnspacing=2)
    fig.tight_layout(rect=(0, 0, 1, .9))
    target = campaigns[-1] / 'report/figure_tesi'
    target.mkdir(parents=True, exist_ok=True)
    for ext in ('pdf', 'svg'):
        fig.savefig(target / f'latenza_statistiche.{ext}')
    print(target / 'latenza_statistiche.pdf')


if __name__ == '__main__':
    main()
