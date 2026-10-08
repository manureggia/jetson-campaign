#!/usr/bin/env bash
set -euo pipefail

operation=${1:-plan}
case "$operation" in
    plan|doctor|run) ;;
    *) echo 'Uso: run-final-tests.sh [plan|doctor|run] [opzioni del controller]' >&2; exit 2 ;;
esac
if (( $# )); then shift; fi
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$project_dir"
exec "$project_dir/.venv/bin/python" -m jetson_tests "$operation" \
    --config "$project_dir/campaign-final-affinity.yaml" \
             "$project_dir/campaign-final-cpuset.yaml" "$@"
