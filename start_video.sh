#!/usr/bin/env bash
set -euo pipefail

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8002}"

uvicorn video_api:app --host "$HOST" --port "$PORT"
