# Shorts Setup

This folder now handles the full shorts pipeline:

- generate 3 shorts automatically after every normal `/render`
- keep the clip order as `1 -> 2 -> 3`
- queue those clips locally for posting
- post one queued short at a time to YouTube on a Linux schedule

## Folder Layout

- `karen_clipper.py`
  - creates the 3 shorts
  - keeps the full frame visible in a 9:16 layout
  - keeps the burned subtitles and clickbait headline
  - speeds clips to `1.5x`
- `publisher.py`
  - manages the local posting queue
  - stores queue state under `shorts/state/`
  - uploads the next queued short to YouTube
- `post_next_short.py`
  - CLI entrypoint for the local daemon or manual posting
- `run_maintenance.py`
  - manual storage cleanup command
- `youtube.env.example`
  - environment template for YouTube posting
- `systemd/`
  - Linux service + timer templates for posting plus rolling maintenance
- `clips/`
  - generated shorts waiting to be posted
- `credentials/`
  - local OAuth files for the YouTube uploader
- `state/`
  - queue state for pending and posted shorts

## What Happens Now

1. Your normal `/render` request finishes and returns `final.mp4`.
2. A background job automatically creates 3 shorts.
3. Those shorts are written into `shorts/clips/`.
4. The same background job appends them to the local queue in the same order they were created.
5. The posting task runs at `07:30`, `12:30`, and `16:30`.
6. Each run posts exactly one queued short, starting with clip 1, then 2, then 3.
7. After a successful upload, the posted short file is deleted locally.
8. Old render folders, logs, and posted queue history are pruned automatically.

If you generate multiple videos before posting catches up, the queue keeps append order across batches.

## Default YouTube Metadata

Each short is queued with:

- a clickbait title derived from the clip headline
- the suffix `| Greentext Story`
- a default description line: `3chan-style greentext story short.`
- hashtags: `#shorts #greentext #storytime`

You can override hashtags and tags in `shorts/youtube.env`.

## Full Setup

### 1. Install repo dependencies

From the repo root:

```bash
./setup.sh
source .venv/bin/activate
./check_env.sh
```

### 2. Prepare YouTube API credentials

Create a Google Cloud project and enable the YouTube Data API v3.

Create an OAuth client for a desktop app, then place the downloaded json file here:

```bash
shorts/credentials/client_secret.json
```

If you want it somewhere else, set `YOUTUBE_CLIENT_SECRET_FILE` in `shorts/youtube.env`.

### 3. Create the local YouTube env file

Copy the template:

```bash
cp shorts/youtube.env.example shorts/youtube.env
```

Then edit it for your machine.

Default template values:

- `YOUTUBE_CLIENT_SECRET_FILE`
- `YOUTUBE_TOKEN_FILE`
- `YOUTUBE_PRIVACY_STATUS`
- `YOUTUBE_CATEGORY_ID`
- `YOUTUBE_HASHTAGS`
- `YOUTUBE_TAGS`
- `VIDEO_DELETE_POSTED_SHORTS`
- `VIDEO_POSTED_QUEUE_KEEP`
- `VIDEO_POSTED_QUEUE_RETENTION_DAYS`
- `VIDEO_RENDER_KEEP_JOBS`
- `VIDEO_RENDER_RETENTION_DAYS`
- `VIDEO_RENDER_MAX_GB`
- `VIDEO_RENDER_MIN_AGE_MINUTES`
- `VIDEO_ORPHAN_CLIP_RETENTION_DAYS`

### 4. Run the one-time OAuth flow

This creates the token file used by the scheduled uploader.

```bash
source .venv/bin/activate
set -a
source shorts/youtube.env
set +a
python3 shorts/post_next_short.py --authorize
```

That writes the refreshable token to the path in `YOUTUBE_TOKEN_FILE`.

### 5. Start the main API

Normal renders will auto-create queued shorts as soon as the main video finishes.

```bash
source .venv/bin/activate
./start_video.sh
```

If you ever want to disable auto-queueing shorts:

```bash
VIDEO_AUTO_SHORTS=0 ./start_video.sh
```

### 6. Manual queue checks

Show queue counts:

```bash
source .venv/bin/activate
python3 shorts/post_next_short.py --status
```

Preview the next post without uploading it:

```bash
source .venv/bin/activate
set -a
source shorts/youtube.env
set +a
python3 shorts/post_next_short.py --dry-run
```

Post the next queued short immediately:

```bash
source .venv/bin/activate
set -a
source shorts/youtube.env
set +a
python3 shorts/post_next_short.py
```

Run the maintenance sweep manually:

```bash
source .venv/bin/activate
set -a
source shorts/youtube.env
set +a
python3 shorts/run_maintenance.py
```

## Linux Scheduled Posting

This repo includes systemd templates:

- `shorts/systemd/video-worker-shorts-poster.service`
- `shorts/systemd/video-worker-shorts-poster.timer`
- `shorts/systemd/video-worker-maintenance.service`
- `shorts/systemd/video-worker-maintenance.timer`

They are configured for:

- `07:30`
- `12:30`
- `16:30`
- hourly maintenance cleanup

### 1. Replace the repo path

The template assumes the repo lives at:

```bash
/opt/video-worker
```

If your repo lives elsewhere, edit all copied systemd files and replace `/opt/video-worker` with your real repo path.

### 2. Install the files

```bash
sudo cp shorts/systemd/video-worker-shorts-poster.service /etc/systemd/system/
sudo cp shorts/systemd/video-worker-shorts-poster.timer /etc/systemd/system/
sudo cp shorts/systemd/video-worker-maintenance.service /etc/systemd/system/
sudo cp shorts/systemd/video-worker-maintenance.timer /etc/systemd/system/
```

### 3. Enable the timer

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now video-worker-shorts-poster.timer
sudo systemctl enable --now video-worker-maintenance.timer
```

### 4. Inspect the schedule

```bash
systemctl list-timers | grep video-worker-shorts-poster
systemctl list-timers | grep video-worker-maintenance
```

### 5. Check the service logs

```bash
journalctl -u video-worker-shorts-poster.service -n 100 --no-pager
journalctl -u video-worker-maintenance.service -n 100 --no-pager
```

## Local Queue Files

Runtime queue files are written under `shorts/state/`.

Important files:

- `shorts/state/queue.json`
  - ordered list of pending and posted shorts
- `shorts/clips/`
  - actual mp4 files waiting to be posted
- `out/<job>/shorts_manifest.json`
  - per-render record of which shorts were created
- `out/<job>/shorts.log`
  - per-render background shorts log

Cleanup behavior:

- posted clip files are deleted right after a successful upload
- posted queue history is capped by count and age
- orphan clips not referenced by the queue are deleted after the configured grace period
- old render job folders in `out/` are pruned by age, count, and size budget

## How Ordering Works

When one render creates its 3 shorts, they are appended like this:

1. `clip_01`
2. `clip_02`
3. `clip_03`

The scheduled uploader always picks the earliest queued item still marked `pending`.

That means:

- clip 1 posts before clip 2
- clip 2 posts before clip 3
- older renders post before newer renders

## Notes

- If an upload fails, the item is returned to `pending` with the last error saved in the queue file.
- If you want to customize the clickbait title format, edit `build_youtube_title()` in `shorts/karen_clipper.py`.
- If you want different hashtags or tags, update `shorts/youtube.env`.
- If you want to keep local posted shorts, set `VIDEO_DELETE_POSTED_SHORTS=0`.
