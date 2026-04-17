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
- `publish_instagram.py`
  - posts one local mp4 to Instagram as a Reel
- `publish_tiktok.py`
  - posts one local mp4 directly to TikTok
- `run_maintenance.py`
  - manual storage cleanup command
- `youtube.env.example`
  - environment template for YouTube posting
- `instagram.env.example`
  - environment template for Instagram Reel posting
- `tiktok.env.example`
  - environment template for TikTok direct posting
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

This repo includes systemd templates and a root-level installer:

- `shorts/systemd/video-worker-shorts-poster.service`
- `shorts/systemd/video-worker-shorts-poster.timer`
- `shorts/systemd/video-worker-maintenance.service`
- `shorts/systemd/video-worker-maintenance.timer`
- `../install_systemd_services.sh`

They are configured for:

- `07:30`
- `12:30`
- `16:30`
- hourly maintenance cleanup

### 1. Recommended install path

The easiest way to install the API service plus both timers is:

```bash
sudo ./install_systemd_services.sh
```

That script automatically writes units for the current repo path, runs them as the repo owner by default, enables the API service, and enables both timers.

### 2. Manual template install

If you prefer to copy the templates yourself, the template path assumes the repo lives at:

```bash
/opt/video-worker
```

If your repo lives elsewhere, edit the copied unit files and replace `/opt/video-worker` with your real repo path.

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

## Manual Instagram Reel Posting

These commands are separate from the YouTube queue. They publish a specific local mp4 file that you point them at.

### 1. Create the env file

```bash
cp shorts/instagram.env.example shorts/instagram.env
```

Fill in:

- `INSTAGRAM_IG_USER_ID`
- `INSTAGRAM_ACCESS_TOKEN`
- `INSTAGRAM_APP_SECRET` (optional but recommended for app secret proof)
- `INSTAGRAM_PUBLISH_METHOD=quick_tunnel` to use the default temporary public URL flow
- `INSTAGRAM_CTA_TEXT` if you want to change the default follow call-to-action
- `INSTAGRAM_HASHTAGS` if you want Instagram-specific hashtags. If unset, Instagram reuses `YOUTUBE_HASHTAGS`.

The default Instagram path now hosts the local mp4 on a temporary localhost server, exposes it through a short-lived Cloudflare quick tunnel, sends that public `video_url` to Instagram, waits for processing to finish, then tears the tunnel down. That means `cloudflared` must be installed on the machine that runs the publish script.
By default, Instagram captions also append `Dont forget to Like and follow` plus the same short-form hashtags used elsewhere in the project.

### 2. Meta app setup

Use the official Instagram Platform content publishing flow:

- connect an Instagram professional account to a Facebook Page
- create a Meta app
- enable Instagram Platform / content publishing
- obtain an access token with publishing permissions

Official docs:

- `https://developers.facebook.com/docs/instagram-platform/content-publishing/`
- `https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media`

Install Cloudflare Tunnel for the default local-file publishing path:

- `https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/`
- `https://developers.cloudflare.com/tunnel/setup/`

### 3. Publish a Reel

```bash
source .venv/bin/activate
set -a
source shorts/instagram.env
set +a
python3 shorts/publish_instagram.py \
  --file shorts/clips/example.mp4 \
  --caption "Example reel caption #shorts"
```

Useful flags:

- `--reels-only`
  - skip sharing to the main feed
- `--thumb-offset-ms 1000`
  - choose a frame from the video for the cover
- `--cover-url https://.../cover.jpg`
  - use a public JPEG as the cover image
- `--resumable-upload`
  - use the older Meta resumable upload flow instead of the default quick tunnel

Notes:

- Quick tunnels are intended for testing and development. They are a pragmatic fallback for this project, but they are not Cloudflare's recommended production setup.
- `--no-wait` is only safe with `--resumable-upload`. The quick tunnel path must stay alive until Instagram finishes fetching the video.
- If you already have a publish-capable Meta Graph token and want the old behavior, set `INSTAGRAM_PUBLISH_METHOD=resumable`.
- This machine may not always be able to resolve the random `trycloudflare.com` hostname immediately even when Instagram can. The script treats the local probe as best-effort and waits for the Instagram container status as the real success signal.

## Manual TikTok Direct Posting

These commands are also separate from the YouTube queue. They can either publish a specific local mp4 file directly to TikTok or upload it to TikTok as a draft for manual editing.

### 1. Create the env file

```bash
cp shorts/tiktok.env.example shorts/tiktok.env
```

Fill in:

- `TIKTOK_CLIENT_KEY`
- `TIKTOK_CLIENT_SECRET`
- `TIKTOK_REDIRECT_URI`
- `TIKTOK_ACCESS_TOKEN`
- `TIKTOK_REFRESH_TOKEN` once you complete OAuth
- `TIKTOK_PUBLISH_BACKEND=native` for the direct TikTok API, or `buffer` to publish through Buffer instead
- `TIKTOK_DESCRIPTION_TEXT` if you want to change the default TikTok description line
- `BUFFER_API_KEY` if you use the Buffer backend
- optionally `BUFFER_TIKTOK_CHANNEL_ID` or `BUFFER_TIKTOK_CHANNEL_NAME` if the Buffer account has more than one TikTok channel connected

### 2. TikTok app setup

Use the official TikTok Content Posting API flow:

- create a TikTok developer app
- add the Content Posting API product
- enable Direct Post
- get approval for the `video.publish` scope if you want direct posting
- get approval for the `video.upload` scope if you want inbox draft uploads
- add a Login Kit redirect URI such as `http://localhost:6583/callback/`
- run OAuth for the TikTok account that should receive the post

Official docs:

- `https://developers.tiktok.com/doc/content-posting-api-get-started/`
- `https://developers.tiktok.com/doc/content-posting-api-reference-direct-post`
- `https://developers.tiktok.com/doc/content-posting-api-reference-upload-video`
- `https://developers.tiktok.com/doc/oauth-user-access-token-management/`

Important:

- unaudited TikTok apps are restricted to private posting
- the script queries creator settings first and honors the privacy levels returned by TikTok
- the client key and secret alone are not enough to post; TikTok posting still requires a user access token with `video.publish`
- draft upload uses the separate `video.upload` scope and delivers the clip to TikTok's inbox flow for final editing/posting
- if `TIKTOK_PUBLISH_BACKEND=buffer`, the script posts through Buffer's TikTok integration instead of TikTok's native Content Posting API and reuses the local Cloudflare quick-tunnel flow to host the video temporarily

### 3. Local OAuth flow

The easiest local flow is to register `http://localhost:6583/callback/` in TikTok and let the helper listen on that port. It generates the authorize URL, waits for TikTok to redirect back to the local callback, exchanges the code automatically, and writes the returned tokens into `shorts/tiktok.env`.

```bash
source .venv/bin/activate
set -a
source shorts/tiktok.env
set +a
python3 shorts/tiktok_token.py local-oauth --scope "user.info.basic,video.publish,video.upload" --open-browser
```

If you prefer to do the code exchange manually after TikTok redirects back with `?code=...`, you can still run:

```bash
source .venv/bin/activate
set -a
source shorts/tiktok.env
set +a
python3 shorts/tiktok_token.py exchange-code \
  --code "<tiktok_oauth_code>" \
  --redirect-uri "http://localhost:6583/callback/" \
  --code-verifier "<pkce_code_verifier>" \
  --print-env
```

If your access token expires later, refresh it with:

```bash
source .venv/bin/activate
set -a
source shorts/tiktok.env
set +a
python3 shorts/tiktok_token.py refresh --print-env
```

### 4. Publish a video

```bash
source .venv/bin/activate
set -a
source shorts/tiktok.env
set +a
python3 shorts/publish_tiktok.py \
  --file shorts/clips/example.mp4 \
  --title "Example TikTok caption #storytime"
```

Useful flags:

- `--privacy-level SELF_ONLY`
- `--disable-comment`
- `--disable-duet`
- `--disable-stitch`
- `--cover-timestamp-ms 1000`
- `--aigc`

To publish through Buffer instead of the native TikTok API:

```bash
source .venv/bin/activate
set -a
source shorts/tiktok.env
set +a
python3 shorts/publish_tiktok.py \
  --file shorts/clips/example.mp4 \
  --title "Example TikTok caption #storytime" \
  --buffer
```

Buffer backend notes:

- `--buffer` is equivalent to `TIKTOK_PUBLISH_BACKEND=buffer`
- Buffer delivery currently supports caption + video only from this script, and it reuses the same default TikTok caption builder
- by default the TikTok caption includes the provided title, `TIKTOK_DESCRIPTION_TEXT`, `TIKTOK_CTA_TEXT`, and `TIKTOK_HASHTAGS`
- Buffer publishing keeps a temporary Cloudflare URL alive while the post sends, so `--no-wait` is intentionally not supported there

### 5. Upload a TikTok draft

```bash
source .venv/bin/activate
set -a
source shorts/tiktok.env
set +a
python3 shorts/publish_tiktok.py \
  --file shorts/clips/example.mp4 \
  --draft
```

Important:

- draft upload uses TikTok's inbox flow instead of creating the post immediately
- TikTok's video draft upload API does not accept caption/privacy metadata up front, so you finish those inside TikTok after the inbox notification arrives
- TikTok does not guarantee a short processing window for inbox delivery, so `SEND_TO_USER_INBOX` may take longer than the local poll timeout on some uploads
- if you only need the `publish_id`, add `--no-wait` and check the status later with `shorts/tiktok_status.py`
- draft upload remains native-only and is not available when `TIKTOK_PUBLISH_BACKEND=buffer`

### 6. Check TikTok upload status

```bash
source .venv/bin/activate
set -a
source shorts/tiktok.env
set +a
python3 shorts/tiktok_status.py v_inbox_file~v2.123456789
```

To keep polling until TikTok reaches a terminal state:

```bash
python3 shorts/tiktok_status.py v_inbox_file~v2.123456789 --watch
```

## Python Integration

If you want to call the new publishers from your own code instead of the CLI wrappers:

```python
from shorts.publisher import post_reel_to_instagram, post_video_to_tiktok

instagram_result = post_reel_to_instagram(
    "shorts/clips/example.mp4",
    "Example reel caption #shorts",
)

tiktok_result = post_video_to_tiktok(
    "shorts/clips/example.mp4",
    "Example TikTok caption #storytime",
)
```

### Recommended integration pattern

Keep these as separate calls from the existing YouTube queue for now:

1. render or generate the short
2. call `post_next_short.py` for YouTube if you want the queued YouTube flow
3. call `publish_instagram.py` for Instagram if you want the same clip there
4. call `publish_tiktok.py` for TikTok if you want the same clip there

That keeps the current YouTube systemd timer working while you experiment with Instagram and TikTok credentials.
