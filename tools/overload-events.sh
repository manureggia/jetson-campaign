#!/usr/bin/env bash
# Only kernel build ID 40157ac384cbddc13766cb58d82594769fbd09fb.
set -euo pipefail
T=/sys/kernel/tracing
GROUP=overload_origin
NAMES=(ov_scan_enter ov_scan_return ov_scan_nr ov_scan_misfit ov_scan_write ov_enqueue_fair ov_enqueue_return_fair ov_enqueue_rt ov_enqueue_return_rt ov_enqueue_dl ov_enqueue_return_dl ov_enqueue_stop ov_enqueue_return_stop ov_fair_set ov_rt_set ov_dl_set ov_stop_set ov_reader ov_nanosleep)
if (( EUID != 0 )); then echo 'Eseguire con sudo bash.' >&2; exit 1; fi
verify_kernel() {
    printf '%s  /sys/kernel/notes\n' \
      e09c7f29fe0eabd63df26ade5734c070fdf58d3b9b3a8ee020b8d9ca33ecdaf4 \
      | sha256sum -c -
}
case "${1:-}" in
setup)
    verify_kernel
    if [[ -d "$T/events/$GROUP" ]]; then
        echo 'Gruppo già presente: usare record, oppure remove prima di setup.' >&2
        exit 1
    fi
    created=()
    rollback() {
        for name in "${created[@]}"; do
            printf -- '-:%s/%s\n' "$GROUP" "$name" >> "$T/kprobe_events" || true
        done
    }
    trap rollback ERR
    while IFS= read -r definition; do
        name=${definition#*:overload_origin/}
        name=${name%% *}
        echo "Creazione $name"
        if ! printf '%s\n' "$definition" >> "$T/kprobe_events"; then
            printf 'Definizione rifiutata:\n%s\n' "$definition" >&2
            if [[ -r $T/error_log ]]; then
                tail -40 "$T/error_log" >&2
            else
                dmesg | tail -30 >&2 || true
            fi
            rollback
            exit 1
        fi
        created+=("$name")
    done <<'PROBES'
p:overload_origin/ov_scan_enter find_busiest_group env=%x0:x64 rd=+2400(+24(%x0)):x64 dst_cpu=+20(%x0):s32
r128:overload_origin/ov_scan_return find_busiest_group
p:overload_origin/ov_scan_nr find_busiest_group+428 env=%x28:x64 rd=+2400(+24(%x28)):x64 parent=+0(+0(%x28)):x64 cpu=%x5:u32 nr=%x12:u32 rq=%x4:x64 curr_tid=+1424(+2312(%x4)):s32 curr_comm=+1928(+2312(%x4)):string
p:overload_origin/ov_scan_misfit find_busiest_group+648 env=%x28:x64 rd=+2400(+24(%x28)):x64 parent=+0(+0(%x28)):x64 cpu=%x5:u32 misfit=%x0:u64
p:overload_origin/ov_scan_write find_busiest_group+2684 env=%x28:x64 rd=%x1:x64 value=%x6:u32 old_snapshot=+88(%x1):u32 dst_cpu=+20(%x28):s32
p:overload_origin/ov_enqueue_fair enqueue_task_fair rq=%x0:x64 rd=+2400(%x0):x64 cpu=+2520(%x0):s32 task=%x1:x64 task_tid=+1424(%x1):s32 task_tgid=+1428(%x1):s32 task_comm=+1928(%x1):string policy=+960(%x1):u32
r128:overload_origin/ov_enqueue_return_fair enqueue_task_fair
p:overload_origin/ov_enqueue_rt enqueue_task_rt rq=%x0:x64 rd=+2400(%x0):x64 cpu=+2520(%x0):s32 task=%x1:x64 task_tid=+1424(%x1):s32 task_tgid=+1428(%x1):s32 task_comm=+1928(%x1):string policy=+960(%x1):u32
r128:overload_origin/ov_enqueue_return_rt enqueue_task_rt
p:overload_origin/ov_enqueue_dl enqueue_task_dl rq=%x0:x64 rd=+2400(%x0):x64 cpu=+2520(%x0):s32 task=%x1:x64 task_tid=+1424(%x1):s32 task_tgid=+1428(%x1):s32 task_comm=+1928(%x1):string policy=+960(%x1):u32
r128:overload_origin/ov_enqueue_return_dl enqueue_task_dl
p:overload_origin/ov_enqueue_stop enqueue_task_stop rq=%x0:x64 rd=+2400(%x0):x64 cpu=+2520(%x0):s32 task=%x1:x64 task_tid=+1424(%x1):s32 task_tgid=+1428(%x1):s32 task_comm=+1928(%x1):string policy=+960(%x1):u32
r128:overload_origin/ov_enqueue_return_stop enqueue_task_stop
p:overload_origin/ov_fair_set enqueue_task_fair+632 rd=%x0:x64 rq=%x20:x64 cpu=+2520(%x20):s32 old_nr=%x19:u32 nr=+4(%x20):u32 curr_tid=+1424(+2312(%x20)):s32 curr_comm=+1928(+2312(%x20)):string
p:overload_origin/ov_rt_set enqueue_top_rt_rq+204 rd=%x0:x64 rq=%x20:x64 cpu=+2520(%x20):s32 old_nr=%x21:u32 nr=+4(%x20):u32 curr_tid=+1424(+2312(%x20)):s32 curr_comm=+1928(+2312(%x20)):string
p:overload_origin/ov_dl_set enqueue_task_dl+956 rd=%x0:x64 rq=%x21:x64 cpu=+2520(%x21):s32 old_nr=%x20:u32 nr=+4(%x21):u32 curr_tid=+1424(+2312(%x21)):s32 curr_comm=+1928(+2312(%x21)):string
p:overload_origin/ov_stop_set enqueue_task_stop+116 rd=%x0:x64 rq=%x19:x64 cpu=+2520(%x19):s32 old_nr=%x20:u32 nr=+4(%x19):u32 curr_tid=+1424(+2312(%x19)):s32 curr_comm=+1928(+2312(%x19)):string
p:overload_origin/ov_reader newidle_balance.constprop.0+156 rd=+2400(%x19):x64 value=%x0:u32
p:overload_origin/ov_nanosleep __arm64_sys_clock_nanosleep
PROBES
    trap - ERR
    echo 'Creati diciannove eventi overload_origin.'
    ;;
record)
    verify_kernel
    tid=${2:?Specificare TID}
    prefix=${3:?Specificare prefisso dei file di output}
    [[ $tid =~ ^[1-9][0-9]*$ && -d /proc/$tid ]] || { echo 'TID non valido.' >&2; exit 1; }
    [[ $prefix == /home/nvidia/codex-work/*/* ]] || { echo 'Output richiesto sotto codex-work.' >&2; exit 1; }
    [[ -d ${prefix%/*} ]] || { echo 'Cartella output assente.' >&2; exit 1; }
    for suffix in trace stats profile-before profile-after status tasks-before tasks-after formats; do
        [[ ! -e $prefix.$suffix ]] || { echo 'File output già presente: cambiare prefisso.' >&2; exit 1; }
    done
    for name in "${NAMES[@]}"; do
        [[ -d $T/events/$GROUP/$name ]] || { echo 'Eseguire setup prima di record.' >&2; exit 1; }
    done
    instance="$T/instances/overload-origin-$$"
    mkdir "$instance"
    cleanup() {
        printf '0\n' > "$instance/tracing_on" || true
        printf '0\n' > "$instance/events/$GROUP/enable" || true
        rmdir "$instance" || true
    }
    trap cleanup EXIT
    printf '0\n' > "$instance/tracing_on"
    printf 'nop\n' > "$instance/current_tracer"
    printf 'mono\n' > "$instance/trace_clock"
    # Include every online CPU; overload writers can execute outside CPU 0.
    mask=$(python3 - <<'MASK'
from pathlib import Path
bits = 0
for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
    limits = list(map(int, part.split('-')))
    for cpu in range(limits[0], limits[-1] + 1):
        bits |= 1 << cpu
print(','.join(f'{(bits >> shift) & 0xffffffff:08x}' for shift in reversed(range(0, bits.bit_length(), 32))))
MASK
)
    printf '%s\n' "$mask" > "$instance/tracing_cpumask"
    printf '16384\n' > "$instance/buffer_size_kb"
    for name in ov_reader ov_nanosleep; do
        printf 'common_pid == %s\n' "$tid" > "$instance/events/$GROUP/$name/filter"
    done
    printf 'nr > 1 && parent == 0\n' > "$instance/events/$GROUP/ov_scan_nr/filter"
    printf 'parent == 0\n' > "$instance/events/$GROUP/ov_scan_misfit/filter"
    printf '1\n' > "$instance/events/$GROUP/enable"
    for name in "${NAMES[@]}"; do
        cat "$T/events/$GROUP/$name/format"
    done > "$prefix.formats"
    ps -eLo pid,tid,cls,rtprio,psr,comm,args > "$prefix.tasks-before"
    cat "/proc/$tid/status" > "$prefix.status"
    cat "$T/kprobe_profile" > "$prefix.profile-before"
    printf '1\n' > "$instance/tracing_on"
    sleep 5
    printf '0\n' > "$instance/tracing_on"
    for stats in "$instance"/per_cpu/cpu*/stats; do
        echo "${stats%/stats}"
        cat "$stats"
    done > "$prefix.stats"
    ps -eLo pid,tid,cls,rtprio,psr,comm,args > "$prefix.tasks-after"
    cat "$instance/trace" > "$prefix.trace"
    cat "$T/kprobe_profile" > "$prefix.profile-after"
    if [[ -n ${SUDO_UID:-} && -n ${SUDO_GID:-} ]]; then
        chown "$SUDO_UID:$SUDO_GID" "$prefix".{trace,stats,profile-before,profile-after,status,tasks-before,tasks-after,formats}
    fi
    echo "Salvati $prefix.trace e statistiche."
    ;;
remove)
    for name in "${NAMES[@]}"; do
        if [[ -d "$T/events/$GROUP/$name" ]]; then
            printf -- '-:%s/%s\n' "$GROUP" "$name" >> "$T/kprobe_events"
        fi
    done
    ;;
*) echo 'Uso: overload-events.sh setup | record TID PREFIX | remove' >&2; exit 2 ;;
esac
