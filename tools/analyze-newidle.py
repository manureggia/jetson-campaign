"""Count decisions from the dedicated newidle tracefs acquisition (stdlib only)."""
import argparse
from collections import Counter
from pathlib import Path
import re

REASONS = {0: 'non classificato', 1: 'wakeup pendente', 2: 'CPU non attiva',
           3: 'idle sotto soglia', 4: 'overload zero', 5: 'controlli iniziali superati'}


def summarize(trace):
    paths, checks, errors = Counter(), Counter(), Counter()
    state = None
    entries = completed = sleeps = outside = 0
    for line in trace.splitlines():
        m = re.search(r'\b(entry|pending|active|idle|overload|domain_cost|domain_flags|balance|return|nanosleep): ', line)
        if not m:
            continue
        event = m[1]
        values = {k: int(v) for k, v in re.findall(r'\b(operand|idle_ns|limit_ns|cost_ns)=(\d+)', line)}
        if event == 'nanosleep':
            sleeps += 1
        elif event == 'entry':
            if state is not None:
                errors['ingresso senza ritorno precedente'] += 1
            entries += 1
            state = [0, 0]
        elif state is None:
            if event == 'balance':
                outside += 1
            elif event != 'return':
                errors['evento interno senza ingresso'] += 1
        elif event == 'pending' and values['operand'] != 0:
            state[0] = 1
        elif event == 'active' and not values['operand'] & 1:
            state[0] = 2
        elif event == 'idle':
            if values['limit_ns'] != 499999:
                errors['limite inatteso'] += 1
            if values['idle_ns'] <= values['limit_ns']:
                state[0] = 3
        elif event == 'overload':
            state[0] = 5 if values['operand'] else 4
        elif event == 'domain_cost':
            checks['costo: stop' if values['idle_ns'] < values['cost_ns'] else 'costo: passa'] += 1
        elif event == 'domain_flags':
            checks['dominio abilitato' if values['operand'] & 1 else 'dominio saltato'] += 1
        elif event == 'balance':
            state[1] += 1
        elif event == 'return':
            paths[tuple(state)] += 1
            if state[0] == 0:
                errors['motivo non classificato'] += 1
            completed += 1
            state = None
    return entries, completed, sleeps, outside, paths, checks, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path)
    args = parser.parse_args()
    stats = args.trace.with_suffix('.stats')
    if not stats.exists():
        parser.error('File .stats richiesto per verificare gli eventi persi.')
    for key, value in re.findall(r'^(overrun|commit overrun|dropped events):\s*(\d+)', stats.read_text(), re.M):
        if int(value):
            parser.error(f'{key}={value}: acquisizione incompleta, ripetere.')
    entries, completed, sleeps, outside, paths, checks, errors = summarize(args.trace.read_text())
    if not completed:
        parser.error('Nessuna invocazione completa: verificare TID e acquisizione.')
    print(f'nanosleep={sleeps}; ingressi={entries}; completi={completed}; load_balance esterni={outside}')
    for (reason, calls), count in sorted(paths.items()):
        print(f'paths[{reason}, {calls}]={count}: {REASONS[reason]}')
    passed = sum(n for (r, _), n in paths.items() if r == 5)
    balances = sum(lb * n for (r, lb), n in paths.items() if r == 5)
    print(f'Ramo ricerca: {passed}/{completed} ({100 * passed / completed:.2f}%)')
    if passed:
        print(f'load_balance per ingresso nel ramo ricerca: {balances / passed:.4f}')
    for key, value in sorted(checks.items()):
        print(f'{key}: {value}')
    if errors:
        parser.error(f'Anomalie, non usare per conclusioni: {dict(errors)}')


if __name__ == '__main__':
    main()
