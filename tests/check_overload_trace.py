"""Verify writer/task attribution without assigning writes from unrelated queues."""
import importlib.util
from pathlib import Path
import tempfile

spec = importlib.util.spec_from_file_location('overload', Path(__file__).resolve().parents[1] / 'tools/analyze-overload.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
def event(name, fields='', tid=20, cpu=2):
    return f' task-{tid} [{cpu:03d}] d...1.. 1.000000: ov_{name}: (symbol) {fields}\n'
trace = ''.join([
    event('reader', 'rd=0xab value=1', tid=100, cpu=0),
    event('enqueue_fair', 'rq=0xaa rd=0xab cpu=2 task_tid=30 task_tgid=30 task_comm="CEM worker" policy=0'),
    event('fair_set', 'rq=0xaa rd=0xab cpu=2 old_nr=1 nr=2 curr_comm="other" curr_tid=31'),
    event('enqueue_return_fair'),
    event('rt_set', 'rq=0xaa rd=0xab cpu=2 old_nr=1 nr=2 curr_comm="other" curr_tid=31'),
    event('scan_enter', 'env=0xcc rd=0xab dst_cpu=0'),
    event('scan_nr', 'env=0xcc rd=0xab cpu=2 nr=3 curr_comm="CEM" curr_tid=30'),
    event('scan_write', 'env=0xcc rd=0xab value=1'),
    event('scan_return'),
    event('stop_set', 'rq=0xee rd=0xff cpu=3 old_nr=1 nr=2 curr_comm="unrelated" curr_tid=32'),
])
s = m.summarize(trace)
assert sum(s['writers'].values()) == 3
assert sum(s['causes'].values()) == 1
assert len(s['tasks']) == 2
assert any(k[3:7] == ('"CEM worker"', '30', '30', '0') for k in s['tasks'])
assert any(k[3] == 'NON_ASSOCIATO' for k in s['tasks'])
assert not s['errors']
# Idle tasks on separate CPUs share PID 0, but must have independent stacks.
idle = ''.join([
    event('enqueue_fair', 'rq=0xaa rd=0xab cpu=1 task_tid=40 task_tgid=40 task_comm="one" policy=0', tid=0, cpu=1),
    event('enqueue_rt', 'rq=0xbb rd=0xab cpu=2 task_tid=41 task_tgid=41 task_comm="two" policy=1', tid=0, cpu=2),
    event('enqueue_return_fair', tid=0, cpu=1),
    event('enqueue_return_rt', tid=0, cpu=2),
])
assert not m.summarize(idle)['errors']
# Losses and missed return probes must invalidate the acquisition.
with tempfile.TemporaryDirectory() as directory:
    p = Path(directory) / 'demo.trace'
    p.with_suffix('.stats').write_text('overrun: 0\ncommit overrun: 0\ndropped events: 0\n')
    for suffix in ('.profile-before', '.profile-after'):
        p.with_suffix(suffix).write_text(''.join(f'ov_{e} 100 0\n' for e in m.EVENTS))
    m.check_files(p)
    p.with_suffix('.profile-after').write_text(''.join(f'ov_{e} 100 {int(e == "enqueue_return_fair")}\n' for e in m.EVENTS))
    try:
        m.check_files(p)
        raise AssertionError('Miss kretprobe non rilevato')
    except ValueError:
        pass
print('OK: dominio, task attivo, scrittori non associati e miss kretprobe')
