#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUTPUT_PATH="${1:-/tmp/video-worker-server-bundle-$STAMP.tar.gz}"

mkdir -p "$(dirname "$OUTPUT_PATH")"

EXTRA_EXCLUDES=()
case "$OUTPUT_PATH" in
  "$ROOT_DIR"/*)
    EXTRA_EXCLUDES+=("--exclude=${OUTPUT_PATH#"$ROOT_DIR"/}")
    ;;
esac

tar \
  --exclude='.git' \
  --exclude='.codex' \
  --exclude='.venv' \
  --exclude='node_modules' \
  --exclude='out' \
  --exclude='Python_clipper' \
  --exclude='__pycache__' \
  --exclude='*/__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.pyo' \
  --exclude='*.swp' \
  "${EXTRA_EXCLUDES[@]}" \
  -czf "$OUTPUT_PATH" \
  -C "$ROOT_DIR" \
  .

echo "Created transfer bundle: $OUTPUT_PATH"
echo "This bundle includes shorts env files, queue state, clips, and any files present under shorts/credentials/."
