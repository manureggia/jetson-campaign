#!/usr/bin/env bash
# Kernel build 40157ac384cbddc13766cb58d82594769fbd09fb only. No scheduler changes.
set -euo pipefail
T=/sys/kernel/tracing
GROUP=lb_affinity
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
NAMES=(la_ni_enter la_ni_exit la_lb_enter la_lb_exit la_group la_queue la_source la_candidate la_pcpu la_cm_enter la_cm_exit la_affine la_running la_hot la_detach la_attach la_idle la_active)
verify_kernel() {
    printf '%s  /sys/kernel/notes\n' e09c7f29fe0eabd63df26ade5734c070fdf58d3b9b3a8ee020b8d9ca33ecdaf4 | sha256sum -c -
    [[ $(cat /sys/devices/system/cpu/online) == 0-5 ]] || { echo 'Attese CPU online 0-5; ricontrollare il setup.' >&2; exit 1; }
}
if [[ ${1:-} == preflight ]]; then
    verify_kernel
    python3 - <<'PY'
import subprocess
for line in subprocess.check_output(['ps','-eLo','pid,tid,cls,rtprio,psr,comm,args'],text=True).splitlines():
    if 'cyclictest' in line and 'python3' not in line:
        print(line)
PY
    exit
fi
(( EUID == 0 )) || { echo 'Usare sudo bash per setup, record e remove.' >&2; exit 1; }
case "${1:-}" in
setup)
    verify_kernel
    [[ ! -d $T/events/$GROUP ]] || { echo 'Gruppo già presente: usare record oppure remove.' >&2; exit 1; }
    created=()
    rollback() {
        for name in "${created[@]}"; do printf -- '-:%s/%s\n' "$GROUP" "$name" >> "$T/kprobe_events" || true; done
    }
    trap rollback ERR
    while IFS= read -r definition; do
        echo "Creazione ${definition%% *}"
        if ! printf '%s\n' "$definition" >> "$T/kprobe_events"; then
            echo "Definizione rifiutata: $definition" >&2
            rollback
            exit 1
        fi
        name=${definition#*:lb_affinity/}; created+=("${name%% *}")
    done <<'PROBES'
p:lb_affinity/la_ni_enter newidle_balance.constprop.0
r128:lb_affinity/la_ni_exit newidle_balance.constprop.0 result=$retval:s32
p:lb_affinity/la_lb_enter load_balance dst=%x0:s32 domain=%x2:x64 idle=%x3:u32
r128:lb_affinity/la_lb_exit load_balance moved=$retval:s32
p:lb_affinity/la_group load_balance+360 group=%x0:x64 imbalance=+256(%x29):s64
p:lb_affinity/la_queue load_balance+656 rq=%x20:x64
p:lb_affinity/la_source load_balance+676 src=+2520(%x20):s32 nr=+4(%x20):u32
p:lb_affinity/la_candidate load_balance+904 task=%x27:x64 task_tid=+1424(%x27):s32 task_tgid=+1428(%x27):s32 task_comm=+1928(%x27):string mask_lo=+0(+968(%x27)):x64 src=+224(%x29):s32 dst=+228(%x29):s32 migration=+292(%x29):u32
p:lb_affinity/la_pcpu load_balance+916 per_cpu=%x0:u32
p:lb_affinity/la_cm_enter can_migrate_task.part.0 task=%x0:x64 src=+16(%x1):s32 dst=+20(%x1):s32
r128:lb_affinity/la_cm_exit can_migrate_task.part.0 accepted=$retval:s32
p:lb_affinity/la_affine can_migrate_task.part.0+64 task=%x19:x64 src=+16(%x25):s32 dst=+20(%x25):s32
p:lb_affinity/la_running can_migrate_task.part.0+292 task=%x19:x64 running=%x1:u32
p:lb_affinity/la_hot can_migrate_task.part.0+468 task=%x19:x64 failed=%x2:u32 tries=%x1:u32
p:lb_affinity/la_detach load_balance+1036 task=%x0:x64 dst=%x1:s32
p:lb_affinity/la_attach attach_task task=%x1:x64 dst=+2520(%x0):s32
p:lb_affinity/la_idle pick_next_task_idle
p:lb_affinity/la_active load_balance+2824 src=%x0:s32
PROBES
    trap - ERR
    echo 'Creati 18 eventi lb_affinity.'
    ;;
record)
    verify_kernel
    tid=${2:?Specificare TID del worker}; prefix=${3:?Specificare prefisso output}; seconds=${4:-3}
    [[ $tid =~ ^[1-9][0-9]*$ && -d /proc/$tid ]] || { echo 'TID assente/non valido.' >&2; exit 1; }
    [[ $seconds =~ ^[1-5]$ ]] || { echo 'Durata ammessa: 1-5 secondi.' >&2; exit 1; }
    [[ $prefix == /home/nvidia/codex-work/*/* && -d ${prefix%/*} ]] || { echo 'Output richiesto in una cartella sotto codex-work.' >&2; exit 1; }
    [[ $(awk '/^Cpus_allowed_list:/{print $2}' /proc/"$tid"/status) == 0 ]] || { echo 'Il worker deve avere affinità esclusiva CPU 0.' >&2; exit 1; }
    [[ $(cat /proc/"$tid"/comm) == cyclictest ]] || { echo 'Il TID non appartiene a cyclictest.' >&2; exit 1; }
    python3 - "$tid" <<'PYWORKER'
import os, sys
pid = int(sys.argv[1])
if os.sched_getscheduler(pid) != os.SCHED_FIFO or os.sched_getparam(pid).sched_priority != 90:
    raise SystemExit('Atteso worker FIFO 90, non thread di gestione.')
PYWORKER
    for name in "${NAMES[@]}"; do [[ -d $T/events/$GROUP/$name ]] || { echo 'Eseguire setup prima di record.' >&2; exit 1; }; done
    for suffix in trace stats profile-before profile-after status-before status-after tasks-before tasks-after affinity-before affinity-after summary.txt summary.json; do
        [[ ! -e $prefix.$suffix ]] || { echo "Output già presente: $prefix.$suffix" >&2; exit 1; }
    done
    snapshot() {
        cat /proc/"$tid"/status > "$prefix.status-$1"
        ps -eLo pid,tid,cls,rtprio,psr,comm,args > "$prefix.tasks-$1"
        python3 "$HERE/analyze-load-balance-affinity.py" --snapshot > "$prefix.affinity-$1"
    }
    if [[ ${prefix##*/} == demo* ]]; then
        python3 "$HERE/analyze-load-balance-affinity.py" --check-cem
    fi
    instance="$T/instances/lb-affinity-$$"
    mkdir "$instance"
    cleanup() {
        printf '0\n' > "$instance/tracing_on" || true
        printf '0\n' > "$instance/events/enable" || true
        rmdir "$instance" || true
        if [[ -n ${SUDO_UID:-} && -n ${SUDO_GID:-} ]]; then
            for file in "$prefix".*; do [[ ! -f $file ]] || chown "$SUDO_UID:$SUDO_GID" "$file"; done
        fi
    }
    trap cleanup EXIT
    printf '0\n' > "$instance/tracing_on"
    printf 'nop\n' > "$instance/current_tracer"
    printf 'mono\n' > "$instance/trace_clock"
    printf '1\n' > "$instance/tracing_cpumask"
    printf '64\n' > "$instance/buffer_size_kb"
    printf '32768\n' > "$instance/per_cpu/cpu0/buffer_size_kb"
    printf 'common_pid == %s\n' "$tid" > "$instance/events/$GROUP/filter"
    printf 'prev_pid == %s || next_pid == %s\n' "$tid" "$tid" > "$instance/events/sched/sched_switch/filter"
    printf '1\n' > "$instance/events/$GROUP/enable"
    printf '1\n' > "$instance/events/sched/sched_switch/enable"
    printf '1\n' > "$instance/events/sched/sched_migrate_task/enable"
    snapshot before
    cat "$T/kprobe_profile" > "$prefix.profile-before"
    printf '1\n' > "$instance/tracing_on"
    sleep "$seconds"
    printf '0\n' > "$instance/tracing_on"
    cat "$instance/per_cpu/cpu0/stats" > "$prefix.stats"
    cat "$instance/trace" > "$prefix.trace"
    cat "$T/kprobe_profile" > "$prefix.profile-after"
    snapshot after
    python3 "$HERE/analyze-load-balance-affinity.py" "$prefix.trace" --tid "$tid" > "$prefix.summary.txt"
    cat "$prefix.summary.txt"
    echo "Salvati $prefix.trace e controlli."
    ;;
remove)
    for name in "${NAMES[@]}"; do
        if [[ -d $T/events/$GROUP/$name ]]; then printf -- '-:%s/%s\n' "$GROUP" "$name" >> "$T/kprobe_events"; fi
    done
    ;;
*) echo 'Uso: preflight | setup | record TID PREFIX [1-5 secondi] | remove' >&2; exit 2 ;;
esac
