#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"

if [[ -f "$ROOT_DIR/video-worker.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/video-worker.env"
  set +a
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8002}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python runtime not found at $PYTHON_BIN. Run ./setup.sh first." >&2
  exit 1
fi

exec "$PYTHON_BIN" -m uvicorn video_api:app --host "$HOST" --port "$PORT"
