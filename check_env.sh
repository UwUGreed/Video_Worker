#!/usr/bin/env bash
set -euo pipefail

echo "Checking Python..."
python3 --version

echo "Checking Node..."
node --version
npm --version

echo "Checking ffmpeg..."
ffmpeg -hide_banner -version | head -n 1

echo "Checking ImageMagick..."
magick -version | head -n 2

echo "Checking NVIDIA runtime..."
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
else
  echo "nvidia-smi not found"
fi

echo "Checking NVENC availability..."
ffmpeg -hide_banner -encoders | grep -E "h264_nvenc|hevc_nvenc" || true

echo "Environment check complete."

