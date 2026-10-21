#!/usr/bin/env python3
"""Decomposition of the mean-latency increase of cyclictest-job into stall components, in µs.

Usage: python3 -I decompose_phase_pmu_job.py <out dir>   (needs phase_pmu_job.csv from analyze_phase_pmu_job.py)

For each job profile and load (CEM/tkHPick on the left, interfgen on the right), the increase of the mean
latency over the baseline of the same profile (pass without probes) is split in the proportions measured
in the latency window W1..W4 of the probed pass:
  data       Δ stall_backend_mem          (waiting for data from memory)
  frontend   Δ stall_frontend             (waiting for instructions)
  other      Δ cycles − data − frontend   (other backend stalls and non-stalled cycles)
  throttling Δ window time − Δ cycles     (time without counted cycles)
The probed window grows about twice as much as the unprobed latency (the probes slow down too),
hence proportions rather than absolute window values. Writes latency_decomposition.{pdf,svg,md}.
"""
import csv
import statistics as st
import sys
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

OUT = Path(sys.argv[1])
rows = list(csv.DictReader((OUT / 'phase_pmu_job.csv').open()))
present = {(r['profile'], r['scenario']) for r in rows}
NAMES = {'none': 'Nessun lavoro', 'h3_frame': 'Frame H3\nscrittura 1,31 MB'}
LOADS = [('cem', 'tkHPick (demo CEM)'), ('interfgen', 'Interfgen')]
PARTS = [('data', 'Attesa di dati dalla memoria', '#454C47', None),
         ('frontend', 'Attesa di istruzioni (stall del frontend)', '#97A39C', None),
         ('other', 'Altri cicli', '#D9DBD6', None),
         ('throttling', 'Tempo senza cicli contati (throttling)', 'white', '////')]
profiles = [p for p in NAMES if (p, 'baseline') in present]


def mean(profile, scenario, key):
    return st.mean(float(r[key]) for r in rows if r['profile'] == profile and r['scenario'] == scenario)


def decompose(profile, load):
    a, b = (profile, load), (profile, 'baseline')
    d = {k: mean(*a, k) - mean(*b, k) for k in ('latency_us', 'win_t_us', 'win_cycles', 'win_stall_mem', 'win_stall_fe')}
    raw = {'data': d['win_stall_mem'], 'frontend': d['win_stall_fe'],
           'other': d['win_cycles'] - d['win_stall_mem'] - d['win_stall_fe'],
           'throttling': d['win_t_us'] - d['win_cycles']}
    # Negative leftovers are noise of a few ns: they are not drawn and not counted in the shares.
    positive = {k: max(0.0, v) for k, v in raw.items()}
    total = sum(positive.values())
    return d['latency_us'], mean(*b, 'latency_us'), {k: d['latency_us'] * v / total for k, v in positive.items()}, raw


fmt = lambda v, n=2: f'{v:.{n}f}'.replace('.', ',')
plt.rcParams.update({'font.family': ['Helvetica', 'DejaVu Sans'], 'font.size': 9, 'axes.spines.top': False,
                     'axes.spines.right': False, 'axes.edgecolor': '#ADB7C3', 'text.color': '#18283C',
                     'axes.labelcolor': '#18283C', 'xtick.color': '#5B6B7C', 'ytick.color': '#18283C',
                     'legend.frameon': False, 'svg.fonttype': 'none', 'pdf.fonttype': 42, 'hatch.linewidth': .6})
fig, axes = plt.subplots(1, len(LOADS), figsize=(11, 1.6 + .75 * len(profiles)), sharex=True, sharey=True)
text = ['# Scomposizione dell\'aumento di latenza media (µs per iterazione)', '',
        'Δ rispetto alla baseline dello stesso profilo. Quote misurate nella finestra W1..W4 con le sonde, '
        'applicate al Δ della latenza senza sonde.', '',
        '| Profilo | Carico | Δ latenza | % sulla baseline | dati | istruzioni | altri cicli | throttling | '
        'Δ finestra con sonde: tempo |', '|---|---|---|---|---|---|---|---|---|']
for ax, (load, title) in zip(axes, LOADS):
    for y, profile in enumerate(reversed(profiles)):
        if (profile, load) not in present:
            continue
        latency, base, parts, raw = decompose(profile, load)
        left = 0
        for key, _, color, hatch in PARTS:
            width = parts[key]
            if width < .01:  # a few ns: below the resolution of the decomposition
                continue
            ax.barh(y, width, left=left, height=.6, color=color, hatch=hatch,
                    edgecolor='#454C47' if hatch else 'white', linewidth=.6)
            if width > .2:
                ax.text(left + width / 2, y, f'+{fmt(width)}', ha='center', va='center', fontsize=7.5,
                        color='white' if key == 'data' else '#18283C', fontweight='bold' if key == 'data' else None)
            left += width
        ax.text(left + .04, y, f'+{fmt(latency)} µs\n({100 * latency / base:+.0f}%)'.replace('.', ','),
                va='center', fontsize=8)
        text.append(f"| {NAMES[profile].replace(chr(10), ' ')} | {title} | {fmt(latency, 3)} | "
                    f"{100 * latency / base:.0f}% | {fmt(parts['data'], 3)} | {fmt(parts['frontend'], 3)} | "
                    f"{fmt(parts['other'], 3)} | {fmt(parts['throttling'], 3)} | "
                    f"{fmt(raw['data'] + raw['frontend'] + raw['other'] + raw['throttling'], 3)} |")
    ax.set_yticks(range(len(profiles)), [NAMES[p] for p in reversed(profiles)])
    ax.set_title(title, loc='left', fontsize=10)
    ax.grid(True, axis='x', color='#E3E6E2', linewidth=.6)
    ax.set_axisbelow(True)
    ax.tick_params(axis='y', length=0)
    ax.set_xlabel('Aumento della latenza media rispetto alla baseline (µs)')
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'{v:g}'.replace('.', ',')))
axes[0].set_xlim(0, axes[0].get_xlim()[1] + .45)
handles = [plt.Rectangle((0, 0), 1, 1, facecolor=c, hatch=h, edgecolor='#454C47' if h else c) for _, _, c, h in PARTS]
fig.legend(handles, [name for _, name, _, _ in PARTS], loc='upper center', ncol=4, bbox_to_anchor=(.5, 1.0))
fig.tight_layout(rect=(0, 0, 1, .9))
for ext in ('pdf', 'svg'):
    fig.savefig(OUT / f'latency_decomposition.{ext}', bbox_inches='tight', facecolor='white')
plt.close(fig)

# Mean latency of the same runs (pass without probes): its percentages match the decomposition.
# Colors and layout of the thesis latency figures (Baseline / INTERFGEN / tkHPick).
SCEN = [('baseline', 'Baseline', '#cbcbc4'), ('interfgen', 'INTERFGEN', '#3a5c4b'), ('cem', 'tkHPick', '#bb9841')]
INK, GRID, AXIS = '#212b26', '#e7e9e4', '#afb5af'
fig, ax = plt.subplots(figsize=(7.4, 3.6))
width = .26
text += ['', '## Latenza media (passata senza sonde)', '', '| Profilo | Baseline | Interfgen | tkHPick |', '|---|---|---|---|']
for i, profile in enumerate(profiles):
    base = mean(profile, 'baseline', 'latency_us')
    cells = []
    for k, (scenario, _, color) in enumerate(SCEN):
        if (profile, scenario) not in present:
            cells.append('')
            continue
        x, value = i + (k - 1) * (width + .02), mean(profile, scenario, 'latency_us')
        ax.bar(x, value, width, color=color, edgecolor='white', linewidth=.6)
        label = fmt(value) if scenario == 'baseline' else f'{fmt(value)} ({100 * (value / base - 1):+.0f}%)'
        ax.text(x, value + .08, label, ha='center', va='bottom', rotation=90, fontsize=7, color=INK)
        cells.append(label)
    text.append(f"| {NAMES[profile].replace(chr(10), ' ')} | " + ' | '.join(cells) + ' |')
ax.set_xticks(range(len(profiles)), [NAMES[p] for p in profiles], fontsize=9, color=INK)
ax.set_ylabel('Latenza media [µs]', color=INK)
ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'{v:g}'.replace('.', ',')))
ax.tick_params(colors=INK)
ax.grid(True, axis='y', color=GRID, linewidth=.8)
ax.set_axisbelow(True)
ax.margins(y=.3)
for side in ('left', 'bottom'):
    ax.spines[side].set_color(AXIS)
ax.set_title('Vittima su CPU3 isolata', fontweight='bold', fontsize=11, color=INK)
fig.legend([plt.Rectangle((0, 0), 1, 1, color=c) for _, _, c in SCEN], [n for _, n, _ in SCEN],
           loc='upper center', ncol=3, bbox_to_anchor=(.5, 1.0), handlelength=1.2, columnspacing=2)
fig.tight_layout(rect=(0, 0, 1, .95))
for ext in ('pdf', 'svg'):
    fig.savefig(OUT / f'latenza_media.{ext}', bbox_inches='tight', facecolor='white')
(OUT / 'latency_decomposition.md').write_text('\n'.join(text) + '\n')
print(OUT / 'latency_decomposition.md')
