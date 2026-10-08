"""Pair targeted tracefs probes; distinguish actual affinity rejection from snapshots."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re


def snapshot():
    rows = []
    for status in Path('/proc').glob('[0-9]*/task/[0-9]*/status'):
        try:
            fields = dict(line.split(':', 1) for line in status.read_text().splitlines() if ':' in line)
            tid = int(fields['Pid']); tgid = int(fields['Tgid'])
            rows.append({'tid': tid, 'tgid': tgid, 'comm': fields['Name'].strip(),
                         'allowed': fields['Cpus_allowed_list'].strip(),
                         'command': (Path('/proc') / str(tgid) / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip()})
        except (OSError, KeyError, ValueError):
            continue  # A task may disappear while /proc is read.
    return rows


def check_cem(rows):
    families = ['tkHPick_pose_estimation', 'tkHPick_instance_segmentation', 'tkCore_bag_play']
    counts = Counter()
    errors = []
    for row in rows:
        command = row['command'].split()
        family = Path(command[0]).name if command else ''
        if family not in families:
            continue
        counts[family] += 1
        cpus = set()
        for part in row['allowed'].split(','):
            bounds = [int(x) for x in part.split('-')]
            cpus.update(range(bounds[0], bounds[-1] + 1))
        if not cpus or not cpus <= {1, 2, 3}:
            errors.append(f"{family} TID {row['tid']}: CPU consentite {row['allowed']}")
    for family in families:
        if not counts[family]:
            errors.append(f'{family}: nessun thread osservato')
    return dict(counts), errors


def fields(line):
    result = dict(re.findall(r'\b([a-z_]+)=("[^"]*"|[^\s]+)', line))
    for key, value in result.items():
        if re.fullmatch(r'-?\d+|0x[0-9a-fA-F]+', value):
            result[key] = int(value, 16 if value.startswith('0x') else 10)
        else:
            result[key] = value.strip('"')
    return result


def summarize(trace, tid):
    totals, errors = Counter(), Counter()
    tasks = defaultdict(Counter)
    examples = defaultdict(list)
    current = pending = balance = cm = None
    seen = False
    cycles = []

    def bad(message):
        errors[message] += 1

    def require_candidate():
        if balance is None or not balance['candidates']:
            bad('evento candidato senza candidato'); return None
        return balance['candidates'][-1]

    for number, line in enumerate(trace.splitlines(), 1):
        event_match = re.search(r'\b(la_[a-z_]+|sched_switch|sched_migrate_task): ', line)
        if not event_match:
            continue
        name = event_match[1]; v = fields(line)
        if '(fault)' in line:
            bad('lettura memoria fallita')
        if name == 'sched_migrate_task':
            totals['migration_events_cpu0'] += 1
            continue  # Unrelated migrations are never assigned by temporal proximity.
        if name == 'sched_switch':
            if v.get('prev_pid') == tid and pending is not None:
                cycle = pending; pending = None
                cycle['next_pid'] = v['next_pid']; cycle['next_comm'] = v['next_comm']
                cycles.append(cycle)
            elif v.get('prev_pid') == tid and current is not None:
                bad('switch prima di ni_exit')
            continue
        if name == 'la_ni_enter':
            if current is not None or balance is not None or cm is not None:
                bad('newidle annidato o ritorno mancante')
            if pending is not None:
                bad('nuovo newidle prima dello switch'); pending = None
            seen = True
            current = {'balances': [], 'line': number, 'idle_probe': False}
            continue
        if not seen:
            totals['leading_boundary_events_discarded'] += 1
            continue
        if name == 'la_idle':
            if pending is not None:
                pending['idle_probe'] = True
            continue
        if name == 'la_ni_exit':
            if current is None or balance is not None or cm is not None:
                bad('ni_exit non abbinato'); continue
            current['result'] = v['result']; pending = current; current = None
            continue
        if current is None:
            if name == 'la_lb_enter':
                totals['balance_outside_newidle'] += 1
            continue
        if name == 'la_lb_enter':
            if current is None:
                totals['balance_outside_newidle'] += 1; continue
            if balance is not None:
                bad('load_balance annidato')
            balance = {'line': number, **v, 'groups': [], 'queues': [], 'sources': [],
                       'candidates': [], 'attached': [], 'active': 0}
            if v['dst'] != 0 or v['idle'] != 2:
                bad('load_balance destinazione/modalità inattesa')
            continue
        if balance is None:
            # attach_task and can_migrate_task can run through other scheduler paths.
            if name not in ('la_cm_enter', 'la_cm_exit', 'la_affine', 'la_running', 'la_hot', 'la_attach'):
                bad('evento interno fuori load_balance')
            continue
        if name == 'la_group':
            balance['groups'].append(v['group'])
        elif name == 'la_queue':
            balance['queues'].append(v['rq'])
        elif name == 'la_source':
            balance['sources'].append(v)
        elif name == 'la_candidate':
            if cm is not None:
                bad('nuovo candidato prima del ritorno cm')
            if v['task_tid'] <= 0 or not v['mask_lo'] & 0x3f or v['dst'] != 0:
                bad('dati candidato invalidi')
            balance['candidates'].append({**v, 'line': number, 'reason': None,
                                          'accepted': None, 'detached': False})
        elif name == 'la_pcpu':
            candidate = require_candidate()
            if candidate is not None and v['per_cpu'] & 0xff:
                candidate['reason'] = 'per_cpu'; candidate['accepted'] = 0
        elif name == 'la_cm_enter':
            candidate = require_candidate()
            if candidate is None:
                continue
            if cm is not None or candidate['task'] != v['task'] or candidate['dst'] != v['dst'] or candidate['src'] != v['src']:
                bad('cm non abbinato al candidato')
            cm = candidate
        elif name in ('la_affine', 'la_running', 'la_hot'):
            if cm is None or cm['task'] != v['task']:
                bad('motivo non abbinato al candidato'); continue
            if name == 'la_running' and v['running'] == 0:
                continue
            if name == 'la_hot' and v['failed'] > v['tries']:
                continue  # The kernel permits a forced migration after repeated failures.
            reason = {'la_affine': 'affinity', 'la_running': 'running', 'la_hot': 'cache_locality'}[name]
            if cm['reason'] is not None:
                bad('due motivi per lo stesso candidato')
            cm['reason'] = reason
            if reason == 'affinity' and v['dst'] != 0:
                bad('esclusione affinità verso altra CPU')
        elif name == 'la_cm_exit':
            if cm is None:
                bad('ritorno cm senza ingresso'); continue
            cm['accepted'] = v['accepted']
            if v['accepted'] not in (0, 1) or (v['accepted'] == 0) != (cm['reason'] is not None):
                bad('esito cm non coerente col motivo')
            cm = None
        elif name == 'la_detach':
            candidate = require_candidate()
            if candidate is None:
                continue
            if candidate['task'] != v['task'] or candidate['accepted'] != 1 or v['dst'] != 0:
                bad('distacco non abbinato al candidato accettato')
            candidate['detached'] = True
        elif name == 'la_attach':
            balance['attached'].append(v['task'])
            if v['dst'] != 0:
                bad('inserimento verso altra CPU')
        elif name == 'la_active':
            balance['active'] += 1
        elif name == 'la_lb_exit':
            if cm is not None:
                bad('load_balance termina con cm pendente')
            balance['moved'] = v['moved']
            detached = [c['task'] for c in balance['candidates'] if c['detached']]
            if v['moved'] < 0 or Counter(detached) != Counter(balance['attached']) or len(detached) != v['moved']:
                bad('distacchi/inserimenti/ritorno incoerenti')
            current['balances'].append(balance); balance = None
        else:
            bad('evento sconosciuto')

    if current is not None or pending is not None:
        totals['trailing_boundary_cycles_discarded'] += 1
    for cycle in cycles:
        totals['newidle_cycles_with_switch'] += 1
        totals[f"newidle_return_{cycle['result']}"] += 1
        if not cycle['balances']:
            totals['short_cycles'] += 1; continue
        totals['long_cycles'] += 1
        idle = cycle['next_pid'] == 0
        totals['long_to_idle' if idle else 'long_to_task'] += 1
        all_affinity = True
        any_candidate = False
        active = any(b['active'] for b in cycle['balances'])
        moved = sum(b['moved'] for b in cycle['balances'])
        for b in cycle['balances']:
            totals['load_balance_calls'] += 1
            totals['directly_moved_tasks'] += b['moved']
            totals['active_requests'] += b['active']
            totals['load_balance_zero' if b['moved'] == 0 else 'load_balance_positive'] += 1
            if not b['candidates']:
                if not any(b['groups']): totals['zero_candidates_no_group'] += 1
                elif not any(b['queues']): totals['zero_candidates_no_queue'] += 1
                else: totals['zero_candidates_source_selected'] += 1
            for c in b['candidates']:
                any_candidate = True
                if c['accepted'] is None:
                    bad('candidato senza verifica completa')
                outcome = c['reason'] or ('moved' if c['detached'] else 'eligible_not_detached')
                totals['candidates'] += 1; totals[outcome] += 1
                all_affinity = all_affinity and outcome == 'affinity'
                key = (c['task_tgid'], c['task_tid'], c['task_comm'])
                tasks[key]['examined'] += 1; tasks[key][outcome] += 1
                if len(examples[outcome]) < 3:
                    examples[outcome].append({'candidate_line': c['line'], 'tgid': c['task_tgid'],
                                             'tid': c['task_tid'], 'comm': c['task_comm'],
                                             'src': c['src'], 'dst': c['dst'],
                                             'mask_lo': hex(c['mask_lo']), 'next_pid': cycle['next_pid']})
        if idle and not moved and not active:
            totals['long_idle_no_direct_move_no_active'] += 1
            if any_candidate and all_affinity:
                totals['long_idle_all_examined_rejected_affinity'] += 1
            elif any_candidate:
                totals['long_idle_other_or_mixed_reasons'] += 1
            else:
                totals['long_idle_no_candidates'] += 1
    if not cycles:
        bad('nessun newidle completo seguito da switch')
    return {'valid': not errors, 'totals': dict(totals), 'errors': dict(errors),
            'threads': [{'tgid': g, 'tid': t, 'comm': comm, **dict(counts)}
                        for (g, t, comm), counts in sorted(tasks.items())],
            'examples': dict(examples),
            'scope': 'direct CPU0 newly-idle balance under worker; deferred active requests flagged, not followed'}


def quality(trace):
    prefix = str(trace)[:-len('.trace')]
    stats = Path(prefix + '.stats').read_text()
    parsed = dict(re.findall(r'^(overrun|commit overrun|dropped events):\s*(\d+)', stats, re.M))
    if len(parsed) != 3 or any(int(v) for v in parsed.values()):
        raise ValueError('Statistiche assenti/incomplete o eventi persi')
    profiles = []
    for suffix in ['profile-before', 'profile-after']:
        hits = {}
        for line in Path(prefix + '.' + suffix).read_text().splitlines():
            columns = line.split()
            if len(columns) == 3 and columns[0].split('/')[-1].startswith('la_'):
                hits[columns[0].split('/')[-1]] = int(columns[2])
        profiles.append(hits)
    if len(profiles[0]) != 18 or profiles[0].keys() != profiles[1].keys():
        raise ValueError('Profili incompleti: attesi tutti i 18 probe')
    if any(profiles[1][k] != profiles[0][k] for k in profiles[0]):
        raise ValueError('Miss kprobe/kretprobe aumentati: ripetere la cattura')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path, nargs='?')
    parser.add_argument('--tid', type=int)
    parser.add_argument('--snapshot', action='store_true')
    parser.add_argument('--check-cem', action='store_true')
    args = parser.parse_args()
    if args.check_cem:
        counts, errors = check_cem(snapshot())
        for name, count in counts.items():
            print(f'{name}: {count} thread')
        if errors:
            parser.error('; '.join(errors))
        print('AFFINITA CEM OK: tutti i thread osservati sono confinati alle CPU 1–3.')
        return
    if args.snapshot:
        print(json.dumps(snapshot(), ensure_ascii=False)); return
    if not args.trace or not args.tid or args.trace.suffix != '.trace':
        parser.error('Richiesti TRACE.trace e --tid TID')
    try:
        quality(args.trace)
        result = summarize(args.trace.read_text(), args.tid)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    output = Path(str(args.trace)[:-len('.trace')] + '.summary.json')
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    print('VALIDO:', result['valid'])
    for key, value in sorted(result['totals'].items()):
        print(f'{key}: {value}')
    print('\nTGID TID NOME ESAMINATI AFFINITA RUNNING CACHE PER_CPU MIGRATI ALTRI_AMMISSIBILI')
    for row in result['threads']:
        print(row['tgid'], row['tid'], row['comm'], row['examined'],
              *(row.get(key, 0) for key in ['affinity','running','cache_locality','per_cpu','moved','eligible_not_detached']))
    if result['errors']:
        parser.error(f"Anomalie: {result['errors']}; non usare per conclusioni")


if __name__ == '__main__':
    main()
