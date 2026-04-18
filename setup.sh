#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
source .venv/bin/activate

mkdir -p out shorts/clips shorts/state shorts/credentials
chmod u+rwX out shorts shorts/clips shorts/state shorts/credentials
chmod +x \
  start_video.sh \
  check_env.sh \
  create_server_bundle.sh \
  install_systemd_services.sh \
  shorts/post_next_short.py \
  shorts/run_maintenance.py \
  shorts/publish_instagram.py \
  shorts/publish_tiktok.py \
  shorts/tiktok_status.py \
  shorts/tiktok_token.py

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
