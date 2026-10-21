#!/usr/bin/env bash
# Smoke test of the h3track build on the Jetson, CPU3 without cpuset, 20 s per run:
#   A/B without job (old binary vs new), h3_frame (old vs new), h3track baseline and with
#   interfgen (meminterf memcpy 128 MiB on CPU0-2), and user instructions per activation
#   (frame_old: the old binary, for h3_frame equivalence):
#   perf stat over 10 s and 30 s runs, difference / 20000 activations (init cancels out).
# Run from the build directory (cyclictest-job next to this script). Writes smoke/.
set -u
cd "$(dirname "$0")"
OLD=/home/nvidia/codex-work/cyclictest-job-20261014/cyclictest-job
NEW=./cyclictest-job
MEMINTERF=/home/nvidia/hesoc-mark/membench/meminterf
C='-a 3 -t 1 -p 90 --policy=fifo -m -N -h 1000000 -q --histfile=/dev/null -i 1000'
mkdir -p smoke
run() { # name binary seconds extra...
  local name=$1 bin=$2 sec=$3; shift 3
  $bin $C -D $sec --json=smoke/$name.json "$@" > smoke/$name.out 2>&1; echo "$name exit=$?"
}
for r in 1 2; do
  run ab_old_$r $OLD 20
  run ab_new_$r $NEW 20
done
run frame_old $OLD 20 --job-write=1310720 --job-trace=smoke/frame_old.bin
run frame_new $NEW 20 --job-write=1310720 --job-trace=smoke/frame_new.bin
run h3track_baseline $NEW 20 --job-algo=h3track --job-trace=smoke/h3track_baseline.bin

pids=()
for cpu in 0 1 2; do
  taskset -c $cpu $MEMINTERF --size=128 --iterations=1000000 --test=memcpy > smoke/meminterf_$cpu.log 2>&1 &
  pids+=($!)
done
sleep 10
run h3track_interfgen $NEW 20 --job-algo=h3track --job-trace=smoke/h3track_interfgen.bin
kill "${pids[@]}" 2>/dev/null
wait 2>/dev/null

for job in none frame frame_old h3track; do
  bin=$NEW
  case $job in
    none) args=() ;;
    frame) args=(--job-write=1310720) ;;
    frame_old) args=(--job-write=1310720); bin=$OLD ;;
    h3track) args=(--job-algo=h3track) ;;
  esac
  for sec in 10 30; do
    perf stat -x, -e instructions:u,cycles:u -o smoke/perf_${job}_$sec.csv -- \
      $bin $C -D $sec --json=smoke/perf_${job}_$sec.json "${args[@]}" > smoke/perf_${job}_$sec.out 2>&1
    echo "perf_${job}_$sec exit=$?"
  done
done

python3 - <<'PY'
import csv, json, glob
def thread(name):
    return json.load(open(f'smoke/{name}.json'))['thread']['0']
for f in sorted(glob.glob('smoke/*.json')):
    name = f[6:-5]
    if name.startswith('perf_'):
        continue
    t = thread(name); j = t.get('job')
    line = f"{name:20s} cycles {t['cycles']:6d} lat avg {t['avg']/1000:7.3f} max {t['max']/1000:8.2f} us"
    if j:
        line += f" | exec avg {j['exec']['avg']/1000:7.1f} max {j['exec']['max']/1000:7.1f} us | missed {j['missed']}"
        if 'h3' in j:
            h = j['h3']
            line += f" | hash {h['input_hash']} check {h['check_errors']} created {h['created']} matched {h['matched']}"
    print(line)
def counts(name):
    out = {}
    for row in csv.reader(open(f'smoke/{name}.csv')):
        if len(row) > 2 and row[0].strip().isdigit():
            out[row[2].split(':')[0]] = int(row[0])
    return out, thread(name)['cycles']
for job in ('none', 'frame', 'frame_old', 'h3track'):
    (a, na), (b, nb) = counts(f'perf_{job}_10'), counts(f'perf_{job}_30')
    n = nb - na
    print(f"{job:9s} per activation: instructions:u {(b['instructions'] - a['instructions']) / n:10.0f}"
          f"  cycles:u {(b['cycles'] - a['cycles']) / n:10.0f}  ({n} activations)")
PY
