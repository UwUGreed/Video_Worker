#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
if [[ -f package-lock.json ]]; then
  npm ci
else
  npm install
fi
npx playwright install chromium

echo "Setup complete."
echo "Next:"
echo "  source .venv/bin/activate"
echo "  ./check_env.sh"
echo "  cp video-worker.env.example video-worker.env   # optional runtime overrides"
echo "  ./start_video.sh"
