"""Standalone checks for the candidate/return/switch pairing and build-specific offsets."""
import importlib.util
from pathlib import Path
import re
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('affinity', ROOT/'tools/analyze-load-balance-affinity.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


def event(name, args=''):
    return f' cyclictest-42 [000] d...3.. 1.000000: {name}: (kernel_function) {args}\n'


def cycle(reason='affinity', moved=False):
    trace = event('la_ni_enter') + event('la_lb_enter','dst=0 domain=0xffff idle=2')
    trace += event('la_group','group=0xab imbalance=1') + event('la_queue','rq=0xac') + event('la_source','src=2 nr=3')
    mask = '0xe' if reason == 'affinity' else '0xf'
    trace += event('la_candidate', f'task=0xb task_tid=12 task_tgid=10 task_comm="cem" mask_lo={mask} src=2 dst=0 migration=2')
    trace += event('la_pcpu',f'per_cpu={1 if reason == "per_cpu" else 0}')
    if reason != 'per_cpu':
        trace += event('la_cm_enter','task=0xb src=2 dst=0')
        if reason in ['affinity','running','cache_locality']:
            probe = {'affinity':'la_affine','running':'la_running','cache_locality':'la_hot'}[reason]
            trace += event(probe,'task=0xb src=2 dst=0 running=1 failed=0 tries=1')
        trace += event('la_cm_exit',f'accepted={0 if reason in ["affinity","running","cache_locality"] else 1}')
    if moved:
        trace += event('la_detach','task=0xb dst=0') + event('la_attach','task=0xb dst=0')
    trace += event('la_lb_exit',f'moved={int(moved)}') + event('la_ni_exit',f'result={int(moved)}')
    if not moved: trace += event('la_idle')
    trace += event('sched_switch',f'prev_comm=cyclictest prev_pid=42 next_comm=swapper/0 next_pid={12 if moved else 0}')
    return trace


for reason in ['affinity','running','cache_locality','per_cpu','eligible']:
    result = module.summarize(cycle(reason),42)
    assert result['valid'], result
    key = 'eligible_not_detached' if reason == 'eligible' else reason
    assert result['totals'][key] == 1
assert module.summarize(cycle(),42)['totals']['long_idle_all_examined_rejected_affinity'] == 1
result = module.summarize(cycle('eligible',True),42)
assert result['valid'] and result['totals']['moved'] == result['totals']['long_to_task'] == 1, result
assert not module.summarize(cycle().replace(event('la_affine','task=0xb src=2 dst=0 running=1 failed=0 tries=1'),''),42)['valid']
assert not module.summarize(cycle('eligible',True).replace(event('la_attach','task=0xb dst=0'),''),42)['valid']
assert not module.summarize(cycle().replace('task_tid=12','task_tid=0'),42)['valid']
assert module.summarize(event('la_lb_exit','moved=0')+cycle()+event('la_ni_enter'),42)['valid']
accepted=cycle('eligible')
accepted=accepted.replace(event('la_cm_exit','accepted=1'),event('la_running','task=0xb running=0')+event('la_hot','task=0xb failed=3 tries=1')+event('la_cm_exit','accepted=1'))
assert module.summarize(accepted,42)['valid']
assert module.summarize(accepted,42)['totals'].get('running',0)==0
assert module.summarize(accepted,42)['totals'].get('cache_locality',0)==0
# Two candidates with different reasons must not be called an all-affinity search.
one=cycle(); insertion=one.index(event('la_lb_exit','moved=0'))
second=event('la_candidate','task=0xc task_tid=13 task_tgid=10 task_comm="cem" mask_lo=0xf src=2 dst=0 migration=2')
second+=event('la_pcpu','per_cpu=0')+event('la_cm_enter','task=0xc src=2 dst=0')+event('la_running','task=0xc running=1')+event('la_cm_exit','accepted=0')
result=module.summarize(one[:insertion]+second+one[insertion:],42)
assert result['valid'] and result['totals']['candidates']==2
assert result['totals'].get('long_idle_all_examined_rejected_affinity',0)==0
# Corrupting a kretprobe miss count must invalidate the acquisition.
with tempfile.TemporaryDirectory() as tmp:
    trace=Path(tmp)/'sample.trace';trace.write_text(cycle())
    trace.with_suffix('.stats').write_text('overrun: 0\ncommit overrun: 0\ndropped events: 0\n')
    names=re.findall(r'^[pr]\d*:lb_affinity/(\w+)',(ROOT/'tools/load-balance-affinity.sh').read_text(),re.M)
    profile=''.join(f'{name} 10 0\n' for name in names)
    assert len(names)==18
    for suffix in ['profile-before','profile-after']:trace.with_suffix('.'+suffix).write_text(profile)
    module.quality(trace)
    trace.with_suffix('.profile-after').write_text(profile.replace('la_cm_exit 10 0','la_cm_exit 10 1'))
    try:module.quality(trace)
    except ValueError:pass
    else:raise AssertionError('Miss non rilevato')
# Check every internal offset against the captured machine instructions.
expected={
 'load_balance': {360:'cbz',656:'cbz',676:'ldr',904:'mov',916:'b.ne',1036:'bl',2824:'bl'},
 'can_migrate_task.part.0': {64:'stp',292:'cbnz',468:'b.hi'},
}
files={'load_balance':'load_balance.disassembly.txt','can_migrate_task.part.0':'can_migrate_task.disassembly.txt'}
for symbol, checks in expected.items():
    text=(ROOT/'diagnostics/load-balance-affinity-preparation'/files[symbol]).read_text()
    base=int(re.search(r'([0-9a-f]+) <'+re.escape(symbol)+r'>:',text)[1],16)
    for offset,instruction in checks.items():
        match=re.search(rf'^{base+offset:x}:\s+[0-9a-f]+\s+(\S+)',text,re.M)
        assert match and match[1]==instruction,(symbol,offset,match)
cem_rows=[{'command': name+' -conf cem/config.yaml', 'allowed':'1-3', 'tid':index}
          for index,name in enumerate(['tkHPick_pose_estimation','tkHPick_instance_segmentation','tkCore_bag_play'],1)]
assert not module.check_cem(cem_rows)[1]
cem_rows[0]['allowed']='0-5'
assert module.check_cem(cem_rows)[1]
assert module.check_cem([])[1]
print('OK: abbinamenti, motivi, migrazioni, confini, miss, offset e controllo affinità CEM.')
