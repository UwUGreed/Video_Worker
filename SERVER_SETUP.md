# Server Setup

This is the end-to-end setup path for moving Video Worker to another Linux machine and leaving it running unattended.

Assumptions:

- the target machine is Debian or Ubuntu based
- you want the API server, scheduled YouTube posting, and maintenance cleanup to survive reboots
- you want your existing `shorts/*.env` files to transfer with the code
- if you already have YouTube OAuth files, you want those transferred too

## 1. Decide how you are transferring the project

You have two supported transfer paths.

### Option A: private Git repo

Use this if you are comfortable storing the `shorts/*.env` files in a private repository.

1. The repo no longer ignores:
   - `shorts/youtube.env`
   - `shorts/instagram.env`
   - `shorts/tiktok.env`
2. Add and commit those files to your private repo.
3. If you use YouTube uploads, also copy these to the new server after cloning:
   - `shorts/credentials/client_secret.json`
   - `shorts/credentials/token.json`

### Option B: transfer bundle

Use this if you do not want to commit secrets to Git.

Create the bundle on the source machine:

```bash
./create_server_bundle.sh
```

That archive includes:

- the repo files
- `shorts/*.env`
- anything currently present under `shorts/credentials/`
- current queue state and queued clip files under `shorts/state/` and `shorts/clips/`

Copy the resulting `.tar.gz` to the new server and extract it there.

## 2. Install system packages on the new server

Install the base packages first:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv ffmpeg imagemagick nodejs npm
```

Install `cloudflared` if you want:

- Instagram quick-tunnel posting
- TikTok posting through Buffer

Official Cloudflare docs:

- `https://developers.cloudflare.com/tunnel/setup/`
- `https://try.cloudflare.com/`

## 3. Put the repo on the new machine

### If you are cloning a private repo

```bash
git clone <your-private-repo-url> /opt/video-worker
cd /opt/video-worker
```

If YouTube uploading is enabled, copy these files from the old machine into the cloned repo before continuing:

```bash
shorts/credentials/client_secret.json
shorts/credentials/token.json
```

### If you are using the transfer bundle

```bash
sudo mkdir -p /opt/video-worker
sudo chown "$USER":"$USER" /opt/video-worker
tar -xzf /path/to/video-worker-server-bundle-*.tar.gz -C /opt/video-worker
cd /opt/video-worker
```

## 4. Create the runtime env files

If you did not transfer the real env files, create them now.

### API runtime env

```bash
cp video-worker.env.example video-worker.env
```

Edit `video-worker.env` only if you want to override defaults like host, port, encoder, or auto-shorts behavior.

### Shorts envs

If needed:

```bash
cp shorts/youtube.env.example shorts/youtube.env
cp shorts/instagram.env.example shorts/instagram.env
cp shorts/tiktok.env.example shorts/tiktok.env
```

For hands-off operation, these files should exist before you install services:

- `video-worker.env`
- `shorts/youtube.env`
- `shorts/instagram.env` if you plan to post to Instagram
- `shorts/tiktok.env` if you plan to post to TikTok

## 5. Install app dependencies

From the repo root:

```bash
./setup.sh
```

What this does:

- creates `.venv`
- installs Python dependencies
- installs Node dependencies
- installs the Playwright Chromium browser used by `render.js`

Then verify the machine:

```bash
source .venv/bin/activate
./check_env.sh
```

If `cloudflared not found` appears and you want Instagram quick-tunnel or Buffer TikTok posting, install `cloudflared` before continuing.

## 6. Verify the credentials you need

### YouTube automation

You need all of these:

- `shorts/youtube.env`
- `shorts/credentials/client_secret.json`
- `shorts/credentials/token.json`

If `token.json` does not exist yet, generate it on the new server:

```bash
source .venv/bin/activate
set -a
source shorts/youtube.env
set +a
python3 shorts/post_next_short.py --authorize
```

### Instagram direct posting

You need:

- `shorts/instagram.env`
- `cloudflared` installed if `INSTAGRAM_PUBLISH_METHOD=quick_tunnel`

### TikTok direct posting

You need:

- `shorts/tiktok.env`

If `TIKTOK_PUBLISH_BACKEND=buffer`, you also need:

- `BUFFER_API_KEY` in `shorts/tiktok.env`
- `cloudflared` installed

## 7. Smoke test before installing services

### API test

Start the API once in the foreground:

```bash
./start_video.sh
```

In a second shell:

```bash
curl http://127.0.0.1:8002/healthz
```

You should get JSON back with `"status": "ok"`.

Stop the foreground server after this test.

### Queue test

Check the uploader queue:

```bash
source .venv/bin/activate
python3 shorts/post_next_short.py --status
```

If you want to preview the next YouTube post without publishing it:

```bash
source .venv/bin/activate
set -a
source shorts/youtube.env
set +a
python3 shorts/post_next_short.py --dry-run
```

## 8. Install the systemd services

Run:

```bash
sudo ./install_systemd_services.sh
```

This installs and enables:

- `video-worker-api.service`
- `video-worker-shorts-poster.timer`
- `video-worker-maintenance.timer`

By default it runs the services as the owner of the repo directory. If you need to override that:

```bash
sudo VIDEO_WORKER_SERVICE_USER=<linux-user> VIDEO_WORKER_SERVICE_GROUP=<linux-group> ./install_systemd_services.sh
```

## 9. Verify everything is live

Check the API service:

```bash
systemctl status video-worker-api.service --no-pager
curl http://127.0.0.1:8002/healthz
```

Check the timers:

```bash
systemctl list-timers --all | grep video-worker
```

Read logs:

```bash
journalctl -u video-worker-api.service -n 100 --no-pager
journalctl -u video-worker-shorts-poster.service -n 100 --no-pager
journalctl -u video-worker-maintenance.service -n 100 --no-pager
```

## 10. What happens automatically after this

Once the services are installed:

1. `video-worker-api.service` starts on boot and keeps the `/render` API online.
2. Every successful `/render` call creates `final.mp4`.
3. The API automatically launches the shorts background job.
4. The background job generates three shorts and appends them to the local queue.
5. `video-worker-shorts-poster.timer` posts one queued short to YouTube at `07:30`, `12:30`, and `16:30`.
6. `video-worker-maintenance.timer` runs hourly and prunes old render output, stale queue history, and orphaned clips.
7. If the server reboots, systemd restores the API service and the timers automatically.

## 11. Optional direct-post checks

Instagram one-off test:

```bash
source .venv/bin/activate
set -a
source shorts/instagram.env
set +a
python3 shorts/publish_instagram.py --file shorts/clips/example.mp4 --caption "Smoke test"
```

TikTok one-off test:

```bash
source .venv/bin/activate
set -a
source shorts/tiktok.env
set +a
python3 shorts/publish_tiktok.py --file shorts/clips/example.mp4 --title "Smoke test"
```

Buffer-backed TikTok test:

```bash
source .venv/bin/activate
set -a
source shorts/tiktok.env
set +a
python3 shorts/publish_tiktok.py --file shorts/clips/example.mp4 --title "Smoke test" --buffer
```

## 12. If you want truly zero-touch later

These are the only things you should have to revisit:

- rotating expired Instagram, TikTok, or Buffer secrets
- renewing or recreating the YouTube OAuth token if Google revokes it
- changing schedules in `install_systemd_services.sh` if you want different posting times
- updating system packages or repo dependencies when you intentionally upgrade the stack

For normal day-to-day use, once the env files, YouTube credentials, and systemd services are in place, the box should run unattended.
