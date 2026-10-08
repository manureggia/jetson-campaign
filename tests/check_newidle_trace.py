"""Run with python3 tests/check_newidle_trace.py; no Jetson required."""
import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'tools/analyze-newidle.py'
spec = importlib.util.spec_from_file_location('newidle', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
trace = '\n'.join('ct-123 [000] .... 1.000: ' + event for event in [
    'nanosleep: (addr)', 'entry: (addr)',
    'idle: (addr) idle_ns=900000 limit_ns=499999',
    'overload: (addr) operand=0', 'return: (addr)',
    'entry: (addr)', 'idle: (addr) idle_ns=900000 limit_ns=499999',
    'overload: (addr) operand=1', 'balance: (addr)',
    'balance: (addr)', 'return: (addr)',
    'entry: (addr)', 'idle: (addr) idle_ns=100 limit_ns=499999',
    'return: (addr)',
])
e, c, n, outside, paths, _, errors = module.summarize(trace)
assert (e, c, n, outside) == (3, 3, 1, 0)
assert paths == {(4, 0): 1, (5, 2): 1, (3, 0): 1}
assert not errors
assert module.summarize('entry: (addr)\nreturn: (addr)')[-1]
print('newidle trace parser: OK')
