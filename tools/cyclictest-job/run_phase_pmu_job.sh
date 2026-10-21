#!/usr/bin/env bash
# PMU counters per cyclictest phase (pmu-phases eBPF tool) with cyclictest-job profiles, on CPU3 in an
# exclusive cpuset partition: baseline, interfgen (meminterf memcpy 128 MiB on CPU0-2) and the CEM demo
# (tkHPick segmentation and pose estimation, roudi, bag player on CPU0-2).
# Same procedure as pmu-phases/run_phase_pmu.sh (12-13 Oct 2026); the job runs in phase O1.
# Runs on the Jetson from a directory holding phase_pmu, phase_pmu.bpf.o and this script.
# Usage: ./run_phase_pmu_job.sh REPS DUR      Env: PROFILES, LOADS, MODES (default "none fine").
set -u
export LC_ALL=C
WORK=$(cd "$(dirname "$0")" && pwd)
OUT=$WORK/${OUTNAME:-out}
CT=/home/nvidia/codex-work/cyclictest-job-20261014/cyclictest-job
MEMINTERF=/home/nvidia/hesoc-mark/membench/meminterf
WS=/home/nvidia/codex-work/latency-decomp/ws        # same TensorRT engines as the campaigns
ENVSCRIPT=/opt/tk/tk-install/setup.sh
OC3=/sys/class/hwmon/hwmon4/oc3_event_cnt           # soctherm over-current throttle events
DEMO_GAP=${DEMO_GAP:-5}
LOADS=${LOADS:-none interfgen cem}
declare -A DEMO_CMD=(
  [roudi]="/opt/tk/deps/bin/iox-roudi -l info -k 1 -m on -x off -c roudi.toml"
  [segmentation]="export DISPLAY=:0; tkHPick_instance_segmentation -conf cem/config.yaml"
  [pose]="export DISPLAY=:0; tkHPick_pose_estimation -conf cem/config.yaml"
  [player]="while true; do bash -c 'tkCore_bag_play cem_multi.mcap'; code=\$?; [ \$code -eq 0 ] || exit \$code; done"
)
HELPER=/usr/local/sbin/jetson-campaign-cgroup
MODES=${MODES:-none fine}
PROFILES=${PROFILES:-none h3_frame}
REPS=${1:-2}
DUR=${2:-30}
# One 1024x1024 10-bit H3 Vision frame written per period, as in campaign-thesis-base-h3.yaml.
declare -A ARGS=(
  [none]="-i 1000"
  [h3_frame]="-i 1000 --job-write=1310720"
)
PIDS=()
CG=""
stop_all() {
  sudo -n pkill -INT -x phase_pmu 2>/dev/null
  local i
  for ((i=${#PIDS[@]}-1; i>=0; i--)); do kill -TERM -- "-${PIDS[i]}" 2>/dev/null; done
  for p in "${PIDS[@]}"; do
    for _ in $(seq 50); do kill -0 -- "-$p" 2>/dev/null || break; sleep 0.1; done
    kill -KILL -- "-$p" 2>/dev/null
  done
  PIDS=()
  [ -n "$CG" ] && sudo -n "$HELPER" delete "$CG"
  CG=""
}
trap 'stop_all; exit 130' INT TERM

one_pass() {   # $1 dir, $2 mode (none|coarse|fine), $3 profile
  local d=$1 mode=$2 profile=$3
  if [ "$mode" != none ]; then
    (cd "$WORK" && sudo -n taskset -c 4,5 ./phase_pmu "$mode" > "$d/${mode}_pmu.txt" 2>&1 &)
    sleep 3    # attach before cyclictest starts
  fi
  # shellcheck disable=SC2086
  sudo -n "$HELPER" exec --rtprio 90 "$CG" -- taskset -c 3 "$CT" -a 3 -t 1 -p 90 --policy=fifo \
    -m -D "$DUR" -N -h 1000000 -q --json="$d/${mode}_cyclictest.json" --histfile=/dev/null ${ARGS[$profile]} \
    > "$d/${mode}_cyclictest.stdout.txt" 2> "$d/${mode}_cyclictest.stderr.txt"
  if [ "$mode" != none ]; then
    sudo -n pkill -INT -x phase_pmu
    for _ in $(seq 50); do pgrep -x phase_pmu > /dev/null || break; sleep 0.1; done
  fi
}

run_one() {   # $1 id, $2 load (none|interfgen|cem), $3 profile
  local id=$1 load=$2 profile=$3 d=$OUT/$1
  mkdir -p "$d"
  if { pgrep -ax iox-roudi; pgrep -af '^(tkHPick_|tkCore_bag_play|cyclictest)'; pgrep -x phase_pmu; pgrep -x meminterf; } > "$d/foreign.txt"; then
    echo "FAIL foreign processes before $id" | tee -a "$OUT/errors.txt"; return
  fi
  CG=jetson-campaign-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')
  sudo -n "$HELPER" create "$CG" 3 || { CG=""; return; }
  echo "$(date +%s.%N) load_start $load $profile" > "$d/marks.txt"
  if [ "$load" = interfgen ]; then
    for c in 0 1 2; do
      setsid taskset -c "$c" "$MEMINTERF" --size=128 --iterations=1000000 --test=memcpy \
        > "$d/interfgen_cpu$c.log" 2>&1 &
      PIDS+=($!)
    done
  elif [ "$load" = cem ]; then
    for n in roudi segmentation pose player; do
      (cd "$WS" && exec setsid taskset -c 0,1,2 bash -c "source $ENVSCRIPT || exit \$?
${DEMO_CMD[$n]}") > "$d/demo_$n.log" 2>&1 &
      PIDS+=($!)
      sleep "$DEMO_GAP"
    done
  fi
  sleep 10   # warmup, as in the thesis campaigns
  local oc_before; oc_before=$(cat "$OC3")
  for mode in $MODES; do one_pass "$d" "$mode" "$profile"; done
  echo "oc3_events=$(( $(cat "$OC3") - oc_before ))" > "$d/oc3_events.txt"
  pgrep -af 'iox-roudi|tkHPick|tkCore_bag_play|meminterf' > "$d/load_processes_end.txt"
  echo "$(date +%s.%N) measure_end" >> "$d/marks.txt"
  stop_all
  echo "$(date -Is) $id" >> "$OUT/progress.log"
}

mkdir -p "$OUT"
{ uname -a; cat /proc/irq/default_smp_affinity; cat /sys/devices/system/cpu/cpu3/cpufreq/scaling_cur_freq
  sha256sum "$CT" phase_pmu phase_pmu.bpf.o
  echo "REPS=$REPS DUR=$DUR MODES=$MODES PROFILES=$PROFILES LOADS=$LOADS"; } > "$OUT/env.txt" 2>&1
for rep in $(seq 1 "$REPS"); do
  # Order alternates per repetition to spread slow drifts over profiles and scenarios.
  list=$PROFILES
  [ $((rep % 2)) = 0 ] && list=$(echo $PROFILES | tr ' ' '\n' | tac | tr '\n' ' ')
  for profile in $list; do
    for load in $LOADS; do
      name=$load; [ "$load" = none ] && name=baseline
      run_one "${profile}__${name}_$rep" "$load" "$profile"
    done
  done
done
echo "$(date -Is) done" >> "$OUT/progress.log"
