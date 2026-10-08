#!/usr/bin/env bash
set -euo pipefail
(( EUID == 0 )) || { echo 'Usare sudo.' >&2; exit 1; }
TID=${1:?TID richiesto}
OUT=${2:?Directory richiesta}
LABEL=${3:?baseline oppure demo}
[[ $TID =~ ^[1-9][0-9]*$ && -d /proc/$TID ]] || exit 2
[[ $OUT == /home/nvidia/codex-work/* && -d $OUT ]] || exit 2
[[ $LABEL == baseline || $LABEL == demo ]] || exit 2
K=$(cd -- "$(dirname -- "$0")" && pwd)
files=()
own_files() {
    if [[ -n ${SUDO_UID:-} && -n ${SUDO_GID:-} && ${#files[@]} -gt 0 ]]; then
        chown "$SUDO_UID:$SUDO_GID" "${files[@]}"
    fi
}
trap own_files EXIT
# Check every destination before starting; never overwrite an acquisition.
for i in 1 2 3; do
    for mode in count measure; do
        for suffix in json stderr status-before status-after tasks; do
            [[ ! -e $OUT/$LABEL-$mode-$i.$suffix ]] || { echo 'Output già presente.' >&2; exit 1; }
        done
    done
done
for i in 1 2 3; do
    # Alternate instrumented/uninstrumented order to reduce order effects.
    modes=(count measure)
    (( i % 2 == 0 )) && modes=(measure count)
    for mode in "${modes[@]}"; do
        prefix="$OUT/$LABEL-$mode-$i"
        for suffix in json stderr status-before status-after tasks; do
            files+=("$prefix.$suffix")
            : > "$prefix.$suffix"
        done
        cat "/proc/$TID/status" > "$prefix.status-before"
        ps -eLo pid,tid,cls,rtprio,psr,comm,args > "$prefix.tasks"
        echo "$LABEL $mode $i/3: 15 secondi"
        "$K/newidle-pmu" "$mode" "$TID" 15 > "$prefix.json" 2> "$prefix.stderr"
        cat "/proc/$TID/status" > "$prefix.status-after"
    done
done
echo "Risultati: $OUT"
