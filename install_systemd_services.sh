#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script with sudo so it can write to /etc/systemd/system." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_USER="${VIDEO_WORKER_SERVICE_USER:-$(stat -c %U "$ROOT_DIR")}"
SERVICE_GROUP="${VIDEO_WORKER_SERVICE_GROUP:-$(stat -c %G "$ROOT_DIR")}"

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
  echo "Missing $ROOT_DIR/.venv/bin/python. Run ./setup.sh first." >&2
  exit 1
fi

if [[ ! -x "$ROOT_DIR/start_video.sh" ]]; then
  echo "start_video.sh is not executable." >&2
  exit 1
fi

cat >"$SYSTEMD_DIR/video-worker-api.service" <<EOF
[Unit]
Description=Video Worker API server
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$ROOT_DIR
EnvironmentFile=-$ROOT_DIR/video-worker.env
EnvironmentFile=-$ROOT_DIR/shorts/youtube.env
EnvironmentFile=-$ROOT_DIR/shorts/instagram.env
EnvironmentFile=-$ROOT_DIR/shorts/tiktok.env
ExecStart=$ROOT_DIR/start_video.sh
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

cat >"$SYSTEMD_DIR/video-worker-shorts-poster.service" <<EOF
[Unit]
Description=Upload the next queued Video Worker short to enabled platforms
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$ROOT_DIR
EnvironmentFile=-$ROOT_DIR/video-worker.env
EnvironmentFile=-$ROOT_DIR/shorts/youtube.env
EnvironmentFile=-$ROOT_DIR/shorts/instagram.env
EnvironmentFile=-$ROOT_DIR/shorts/tiktok.env
ExecStart=$ROOT_DIR/.venv/bin/python $ROOT_DIR/shorts/post_next_short.py
EOF

cat >"$SYSTEMD_DIR/video-worker-shorts-poster.timer" <<'EOF'
[Unit]
Description=Run the Video Worker shorts poster on the daily publishing schedule

[Timer]
OnCalendar=*-*-* 07:30:00
OnCalendar=*-*-* 12:30:00
OnCalendar=*-*-* 16:30:00
Persistent=true
Unit=video-worker-shorts-poster.service

[Install]
WantedBy=timers.target
EOF

cat >"$SYSTEMD_DIR/video-worker-maintenance.service" <<EOF
[Unit]
Description=Prune Video Worker storage, logs, and posted short history

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$ROOT_DIR
EnvironmentFile=-$ROOT_DIR/video-worker.env
EnvironmentFile=-$ROOT_DIR/shorts/youtube.env
ExecStart=$ROOT_DIR/.venv/bin/python $ROOT_DIR/shorts/run_maintenance.py
EOF

cat >"$SYSTEMD_DIR/video-worker-maintenance.timer" <<'EOF'
[Unit]
Description=Run Video Worker storage maintenance on a rolling schedule

[Timer]
OnBootSec=15m
OnUnitActiveSec=1h
Persistent=true
Unit=video-worker-maintenance.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now video-worker-api.service
systemctl enable --now video-worker-shorts-poster.timer
systemctl enable --now video-worker-maintenance.timer

echo "Installed and started:"
echo "  video-worker-api.service"
echo "  video-worker-shorts-poster.timer"
echo "  video-worker-maintenance.timer"
echo
systemctl --no-pager --full status video-worker-api.service || true
systemctl list-timers --all | grep -E 'video-worker-(shorts-poster|maintenance)' || true
