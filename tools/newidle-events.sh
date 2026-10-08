#!/usr/bin/env bash
# Only kernel build ID 40157ac384cbddc13766cb58d82594769fbd09fb.
set -euo pipefail
T=/sys/kernel/tracing
GROUP=newidle_branches
NAMES=(entry pending active idle overload domain_cost domain_flags balance return nanosleep)
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
        printf '%s\n' "$definition" >> "$T/kprobe_events"
        name=${definition#*:newidle_branches/}
        created+=("${name%% *}")
    done <<'PROBES'
p:newidle_branches/entry newidle_balance.constprop.0
p:newidle_branches/pending newidle_balance.constprop.0+80 operand=%x0:u32
p:newidle_branches/active newidle_balance.constprop.0+120 operand=%x0:u32
p:newidle_branches/idle newidle_balance.constprop.0+144 idle_ns=%x1:u64 limit_ns=%x0:u64
p:newidle_branches/overload newidle_balance.constprop.0+156 operand=%x0:u32
p:newidle_branches/domain_cost newidle_balance.constprop.0+580 idle_ns=%x1:u64 cost_ns=%x0:u64
p:newidle_branches/domain_flags newidle_balance.constprop.0+588 operand=%x0:u32
p:newidle_branches/balance load_balance
r:newidle_branches/return newidle_balance.constprop.0
p:newidle_branches/nanosleep __arm64_sys_clock_nanosleep
PROBES
    trap - ERR
    echo 'Creati dieci eventi newidle_branches.'
    ;;
record)
    verify_kernel
    tid=${2:?Specificare TID}
    prefix=${3:?Specificare prefisso dei file di output}
    [[ $tid =~ ^[1-9][0-9]*$ && -d /proc/$tid ]] || { echo 'TID non valido.' >&2; exit 1; }
    [[ $prefix == /home/nvidia/codex-work/*/* ]] || { echo 'Output richiesto sotto codex-work.' >&2; exit 1; }
    [[ -d ${prefix%/*} ]] || { echo 'Cartella output assente.' >&2; exit 1; }
    for suffix in trace stats profile-before profile-after status; do
        [[ ! -e $prefix.$suffix ]] || { echo 'File output già presente: cambiare prefisso.' >&2; exit 1; }
    done
    for name in "${NAMES[@]}"; do
        [[ -d $T/events/$GROUP/$name ]] || { echo 'Eseguire setup prima di record.' >&2; exit 1; }
    done
    instance="$T/instances/newidle-branches-$$"
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
    printf '1\n' > "$instance/tracing_cpumask"
    printf '64\n' > "$instance/buffer_size_kb"
    printf '16384\n' > "$instance/per_cpu/cpu0/buffer_size_kb"
    printf 'common_pid == %s\n' "$tid" > "$instance/events/$GROUP/filter"
    printf '1\n' > "$instance/events/$GROUP/enable"
    cat "/proc/$tid/status" > "$prefix.status"
    cat "$T/kprobe_profile" > "$prefix.profile-before"
    printf '1\n' > "$instance/tracing_on"
    sleep 5
    printf '0\n' > "$instance/tracing_on"
    cat "$instance/per_cpu/cpu0/stats" > "$prefix.stats"
    cat "$instance/trace" > "$prefix.trace"
    cat "$T/kprobe_profile" > "$prefix.profile-after"
    if [[ -n ${SUDO_UID:-} && -n ${SUDO_GID:-} ]]; then
        chown "$SUDO_UID:$SUDO_GID" "$prefix".{trace,stats,profile-before,profile-after,status}
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
*) echo 'Uso: newidle-events.sh setup | record TID PREFIX | remove' >&2; exit 2 ;;
esac
