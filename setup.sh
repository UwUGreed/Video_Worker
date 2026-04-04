#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
npm install

echo "Setup complete."
echo "Next:"
echo "  source .venv/bin/activate"
echo "  ./check_env.sh"
echo "  ./start_video.sh"

