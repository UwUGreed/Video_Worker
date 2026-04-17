#!/usr/bin/env bash
set -euo pipefail

echo "Checking Python..."
python3 --version

echo "Checking Node..."
node --version
npm --version

echo "Checking Playwright..."
npx playwright --version
node - <<'JS'
const fs = require("fs");
const { chromium } = require("playwright");
const browserPath = chromium.executablePath();
console.log(`playwright chromium: ${browserPath}`);
if (!fs.existsSync(browserPath)) {
  process.exitCode = 1;
  console.error("playwright chromium binary is missing");
}
JS

echo "Checking ffmpeg..."
ffmpeg -hide_banner -version | head -n 1

echo "Checking ImageMagick..."
magick -version | head -n 2

echo "Checking cloudflared..."
if command -v cloudflared >/dev/null 2>&1; then
  cloudflared --version | head -n 1
else
  echo "cloudflared not found"
fi

echo "Checking NVIDIA runtime..."
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
else
  echo "nvidia-smi not found"
fi

echo "Checking NVENC availability..."
ffmpeg -hide_banner -encoders | grep -E "h264_nvenc|hevc_nvenc" || true

echo "Checking optional Python packages..."
python3 - <<'PY'
mods = [
    ("whisper", "shorts transcription"),
    ("googleapiclient", "YouTube uploads"),
    ("google_auth_oauthlib", "YouTube OAuth"),
]
for mod, label in mods:
    try:
        __import__(mod)
        print(f"{label}: ok")
    except Exception:
        print(f"{label}: missing")
PY

echo "Environment check complete."
