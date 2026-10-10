#!/usr/bin/env bash
# Campagne della tesi, in sequenza: base (CPU0, CPU3) -> cpuset (CPU0, CPU3) -> cpuset + IRQ (CPU3).
# La configurazione IRQ è l'ultima: applica e poi ripristina da sola l'esclusione di CPU3.
set -euo pipefail

operation=${1:-plan}
case "$operation" in
    plan|doctor|run) ;;
    *) echo 'Uso: run-thesis.sh [plan|doctor|run] [opzioni del controller]' >&2; exit 2 ;;
esac
if (( $# )); then shift; fi
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$project_dir"
exec "$project_dir/.venv/bin/python" -m jetson_tests "$operation" \
    --config "$project_dir/campaign-thesis-base.yaml" \
             "$project_dir/campaign-thesis-cpuset.yaml" \
             "$project_dir/campaign-thesis-cpuset-irq-cpu3.yaml" "$@"
