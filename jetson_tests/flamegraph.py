"""Quality parser reused from scripts/demo_flamegraph.py; see docs/REUSE.md."""
import re
HEADER = re.compile(r'^\s*(.*?)\s+(\d+)(?:/|\s+)(\d+)\s+\[(\d+)\]\s+(\d+\.\d+):\s+(\S+):\s*(.*)$')

def blocks(handle):
    block = []
    for line in handle:
        if HEADER.match(line) and block:
            yield block
            block = []
        if line.strip() or block:
            block.append(line)
    if block:
        yield block

def core_profile_quality(source):
    counts = {'core_samples': 0, 'stack_samples': 0, 'unknown_frames': 0,
              'unparsed_records': 0}
    with source.open() as handle:
        for block in blocks(handle):
            match = HEADER.match(block[0])
            if not match or match.group(6) != 'cpu-clock':
                if any(line.strip() and not line.startswith('#') for line in block):
                    counts['unparsed_records'] += 1
                continue
            counts['core_samples'] += 1
            frames = [line for line in block[1:] if re.match(r'^\s+[0-9a-f]+\s+', line)]
            counts['stack_samples'] += bool(frames)
            counts['unknown_frames'] += sum('[unknown]' in line for line in frames)
    return counts
