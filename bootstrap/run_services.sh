#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"
export SKIP_AUTH="${SKIP_AUTH:-1}"

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

python -m services.validation.main &
VPID=$!
python -m services.review.main &
RPID=$!

echo "validation PID=$VPID (8010)"
echo "review PID=$RPID (8014)"
trap 'kill $VPID $RPID 2>/dev/null || true' EXIT
wait
