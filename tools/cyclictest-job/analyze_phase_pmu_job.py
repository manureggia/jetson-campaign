#!/usr/bin/env python3
"""Per-phase PMU counters of cyclictest-job profiles (run_phase_pmu_job.sh output).

Usage: python3 -I analyze_phase_pmu_job.py <out dir>
Per cyclictest iteration: times and cycle-like events in µs at 1.728 GHz, instructions as counts.
Latency comes from the pass without probes ("none"); phases from the "fine" pass.
Window = W1..W4 (idle exit -> return to user space), job = O1 (user work, the cyclictest-job job).
Writes phase_pmu_job.md and phase_pmu_job.csv into <out dir> (input of decompose_phase_pmu_job.py).
"""
import csv
import json
import re
import statistics as st
import sys
from pathlib import Path


MHZ = 1728.0
EVENTS = ['cycles', 'inst', 'stall_fe', 'stall_be', 'stall_mem']
LINE = re.compile(r'^PHASE (\w+) n=(\d+) ns=(\d+) (.*)$')
GROUPS = {'win': ['W1', 'W2', 'W3', 'W4'], 'W1': ['W1'], 'W2': ['W2'], 'W3': ['W3'], 'W4': ['W4'], 'job': ['O1']}
ORDER = ['none', 'h3_frame']
COLORS = {'baseline': '#2a78d6', 'interfgen': '#eb6834', 'cem': '#1baf7a'}
SCEN_LABEL = {'baseline': 'Baseline', 'interfgen': 'Interfgen', 'cem': 'CEM (tkHPick)'}
OUT = Path(sys.argv[1])


def phases(path):
    result = {}
    for line in path.read_text().splitlines():
        if m := LINE.match(line):
            values = dict(kv.split('=') for kv in m[4].split())
            result[m[1]] = {'ns': int(m[3]), **{e: int(values[e]) for e in EVENTS}}
    state = re.search(r'STATE tid=\d+ done=\d+ clean=(\d+) lost=(\d+)', path.read_text())
    return result, int(state[1]), int(state[2])


rows = []
for d in sorted(p for p in OUT.iterdir() if p.is_dir() and '__' in p.name):
    profile, rest = d.name.split('__')
    scenario, rep = rest.rsplit('_', 1)
    plain = json.loads((d / 'none_cyclictest.json').read_text())['thread']['0']
    fine = json.loads((d / 'fine_cyclictest.json').read_text())['thread']['0']
    data, clean, lost = phases(d / 'fine_pmu.txt')
    n = fine['cycles']
    row = {'profile': profile, 'scenario': scenario, 'rep': int(rep), 'latency_us': plain['avg'] / 1000,
           'latency_probes_us': fine['avg'] / 1000, 'clean_share': clean / (clean + lost)}
    for group, keys in GROUPS.items():
        row[f'{group}_t_us'] = sum(data[k]['ns'] for k in keys) / n / 1000
        for e in EVENTS:
            row[f'{group}_{e}'] = sum(data[k][e] for k in keys) / n / (1 if e == 'inst' else MHZ)
    rows.append(row)

with (OUT / 'phase_pmu_job.csv').open('w', newline='') as stream:
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)

profiles = [p for p in ORDER if any(r['profile'] == p for r in rows)]
mean = lambda p, s, k: st.mean(r[k] for r in rows if r['profile'] == p and r['scenario'] == s)
fmt = lambda v, d=3: f'{v:.{d}f}'.replace('.', ',')
scenarios = [s for s in COLORS if any(r['scenario'] == s for r in rows)]
text = ['# PMU per fase con cyclictest-job (CPU3 in cpuset)', '',
        'Valori per iterazione, media delle ripetizioni. Latenza dalla passata senza sonde; fasi dalla passata fine.',
        'Finestra = W1..W4 (uscita idle → ritorno in user space); lavoro = O1. Tempi e stall in µs.', '',
        '| Profilo | Scenario | Latenza | Finestra: tempo | cicli | stall mem | stall BE | stall FE | istr. | '
        'Lavoro O1: tempo | stall mem |', '|' + '---|' * 11]
for p in profiles:
    for s in scenarios:
        text.append(f"| {p} | {s} | {fmt(mean(p, s, 'latency_us'))} | {fmt(mean(p, s, 'win_t_us'))} | "
                    f"{fmt(mean(p, s, 'win_cycles'))} | {fmt(mean(p, s, 'win_stall_mem'))} | "
                    f"{fmt(mean(p, s, 'win_stall_be'))} | {fmt(mean(p, s, 'win_stall_fe'))} | "
                    f"{mean(p, s, 'win_inst'):.0f} | {fmt(mean(p, s, 'job_t_us'), 1)} | {fmt(mean(p, s, 'job_stall_mem'), 1)} |")
text += ['', '## Differenze (µs per iterazione)', '',
         'Δ profilo = profilo − riferimento senza lavoro allo stesso periodo, stesso scenario; '
         'Δ carico = scenario con carico − baseline dello stesso profilo.', '',
         '| Profilo | Confronto | Δ latenza | Δ stall mem finestra | Δ stall BE finestra | Δ stall FE finestra | Δ cicli finestra |',
         '|---|---|---|---|---|---|---|']
points = []
for p in profiles:
    ref = 'none'
    pairs = [(f'{s} vs baseline', (p, s), (p, 'baseline')) for s in scenarios if s != 'baseline']
    if p != ref:
        pairs += [(f'vs {ref}, {s}', (p, s), (ref, s)) for s in scenarios]
    for label, a, b in pairs:
        if not all(any((r['profile'], r['scenario']) == x for r in rows) for x in (a, b)):
            continue
        delta = {k: mean(*a, k) - mean(*b, k) for k in ('latency_us', 'win_stall_mem', 'win_stall_be', 'win_stall_fe', 'win_cycles')}
        points.append((p, label, delta))
        text.append(f"| {p} | {label} | {fmt(delta['latency_us'])} | {fmt(delta['win_stall_mem'])} | "
                    f"{fmt(delta['win_stall_be'])} | {fmt(delta['win_stall_fe'])} | {fmt(delta['win_cycles'])} |")

text.append(f"\nIterazioni chiuse correttamente dalla sonda: min {min(r['clean_share'] for r in rows):.4f}.")
(OUT / 'phase_pmu_job.md').write_text('\n'.join(text) + '\n')

print(OUT / 'phase_pmu_job.md')
