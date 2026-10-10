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

python -m validation_service.main &
VPID=$!
python -m extraction_service.main &
EPID=$!
python -m review_service.main &
RPID=$!

echo "validation_service PID=$VPID (8010)"
echo "extraction_service PID=$EPID (8012)"
echo "review_service PID=$RPID (8014)"
trap 'kill $VPID $EPID $RPID 2>/dev/null || true' EXIT
wait
