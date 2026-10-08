"""Identify overload writers sharing the root_domain read by cyclictest."""
import argparse
from collections import Counter, defaultdict
from pathlib import Path
import re

EVENTS = ('scan_enter', 'scan_return', 'scan_nr', 'scan_misfit', 'scan_write',
          'enqueue_fair', 'enqueue_return_fair', 'enqueue_rt', 'enqueue_return_rt',
          'enqueue_dl', 'enqueue_return_dl', 'enqueue_stop', 'enqueue_return_stop',
          'fair_set', 'rt_set', 'dl_set', 'stop_set',
          'reader', 'nanosleep')
LINE = re.compile(r'-(\d+)\s+\[(\d+)\]\s+\S+\s+(\d+\.\d+):\s+ov_(' + '|'.join(EVENTS) + r'):\s+(.*)')
FIELD = re.compile(r'(\w+)=("[^"]*"|\S+)')


def summarize(trace):
    queues, scans = defaultdict(list), defaultdict(list)
    reads, writers, causes, tasks, errors = (Counter() for _ in range(5))
    writes = []
    sleeps = boundary = 0
    for line in trace.splitlines():
        m = LINE.search(line)
        if not m:
            continue
        tid, executing_cpu, time, event, body = m.groups()
        # PID 0 is a different idle task on each CPU.
        context = (tid, executing_cpu if tid == '0' else None)
        f = dict(FIELD.findall(body))
        def number(name):
            value = f[name]
            return int(value, 16 if value.startswith('0x') else 10)
        if '(fault)' in f.values():
            errors['lettura di memoria fallita'] += 1
            continue
        if event == 'nanosleep':
            sleeps += 1
        elif event == 'reader':
            reads[(f['rd'], number('value'))] += 1
        elif event.startswith('enqueue_') and not event.startswith('enqueue_return_'):
            queues[context].append(dict(f, sched_class=event.removeprefix('enqueue_')))
        elif event.startswith('enqueue_return_'):
            if queues[context]:
                if queues[context][-1]['sched_class'] != event.removeprefix('enqueue_return_'):
                    errors['ritorno classe enqueue incoerente'] += 1
                queues[context].pop()
            else:
                boundary += 1
        elif event == 'scan_enter':
            scans[context].append((f['env'], Counter()))
        elif event == 'scan_return':
            if scans[context]:
                scans[context].pop()
            else:
                boundary += 1
        elif event in ('scan_nr', 'scan_misfit'):
            matching = [c for env, c in scans[context] if env == f['env']]
            if matching:
                matching[-1][(event, number('cpu'), number('nr') if event == 'scan_nr' else number('misfit'), f.get('curr_comm', ''), f.get('curr_tid', ''))] += 1
            else:
                boundary += 1
        elif event == 'scan_write':
            c = next((c for env, c in reversed(scans[context]) if env == f['env']), None)
            if c is None:
                boundary += 1
                c = Counter()
            writes.append((f['rd'], event, number('value'), f, c))
        elif event.endswith('_set'):
            if number('old_nr') >= 2 or number('nr') < 2:
                errors['soglia add_nr_running incoerente'] += 1
            q = next((q for q in reversed(queues[context]) if q['rq'] == f['rq'] and q['rd'] == f['rd'] and q['sched_class'] == event.removesuffix('_set')), None)
            # RT helper can also be called without enqueue_task_rt: do not reuse an old task.
            task = (q['task_comm'], q['task_tid'], q['task_tgid'], q['policy']) if q else ('NON_ASSOCIATO', '', '', '')
            f = dict(f, executing_cpu=executing_cpu, task_identity=task)
            writes.append((f['rd'], event, 1, f, Counter()))
    domains = {rd for rd, _ in reads}
    for rd, event, value, f, c in writes:
        if rd not in domains:
            continue
        writers[(rd, event, value)] += 1
        for cause, count in c.items():
            causes[(rd, value, *cause)] += count
        if 'task_identity' in f:
            tasks[(rd, event, number_from(f['cpu']), *f['task_identity'], f['curr_comm'], f['curr_tid'])] += 1
    return dict(reads=reads, writers=writers, causes=causes, tasks=tasks,
                errors=errors, sleeps=sleeps, boundary=boundary)


def number_from(value):
    return int(value, 16 if value.startswith('0x') else 10)


def check_files(path):
    stats = path.with_suffix('.stats')
    if not stats.exists():
        raise ValueError('File .stats assente: impossibile verificare perdite')
    for name, value in re.findall(r'^(overrun|commit overrun|dropped events):\s*(\d+)', stats.read_text(), re.M):
        if int(value):
            raise ValueError(f'{name}={value}: acquisizione con perdite')
    profiles = []
    for suffix in ('.profile-before', '.profile-after'):
        p = path.with_suffix(suffix)
        if not p.exists():
            raise ValueError(f'{p.name} assente')
        profiles.append({(m[1].split('/')[-1][3:] if m[1].split('/')[-1].startswith('ov_') else m[1].split('/')[-1]): int(m[3]) for line in p.read_text().splitlines()
                         if (m := re.match(r'\s*(\S+)\s+(\d+)\s+(\d+)\s*$', line))})
    for event in EVENTS:
        if event not in profiles[1] or event not in profiles[0]:
            raise ValueError(f'Contatore kprobe assente: {event}')
        if profiles[1][event] != profiles[0][event]:
            raise ValueError(f'Miss kprobe durante la cattura: {event}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path)
    args = parser.parse_args()
    try:
        check_files(args.trace)
        s = summarize(args.trace.read_text())
        if not s['reads']:
            raise ValueError('Nessuna lettura del worker: verificare TID e attività')
        if s['errors']:
            raise ValueError(str(dict(s['errors'])))
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    print(f"clock_nanosleep worker: {s['sleeps']}")
    for label in ('reads', 'writers', 'causes', 'tasks'):
        print(f'\n{label}:')
        for key, count in s[label].most_common():
            print(f'{count:8d} {key}')
    print(f"\nEventi senza ingresso nella finestra: {s['boundary']}")
    print('Gli scrittori mostrati condividono rd con il worker; non sono abbinamenti esatti scrittura/lettura.')
    print('causes conta contributi nr_running/misfit alle scansioni; tasks identifica enqueue attivi ai setter.')
    print('curr è un campione del task corrente, non un elenco completo della coda.')


if __name__ == '__main__':
    main()
