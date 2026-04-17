# Video Worker

This folder is a clean GitHub-ready package of the video rendering worker from this repo.

It exposes a FastAPI endpoint that:

- accepts story text
- accepts a WAV voice track
- optionally mixes in a very quiet background music bed
- optionally accepts an image
- renders a scrolling story video
- returns the finished `final.mp4`
- automatically starts a 3-short background job after each normal render
- queues those shorts locally for scheduled YouTube posting
- deletes posted shorts and prunes old render/log storage on a rolling basis
- can also render 3 shorts and return them as `short1`, `short2`, `short3`

## Included Files

- `video_api.py`: API server and ffmpeg pipeline
- `render.js`: story image renderer
- `template.html`: original style reference for the renderer
- `package.json` and `package-lock.json`: Node dependencies
- `requirements.txt`: Python dependencies
- `start_video.sh`: starts the API server
- `setup.sh`: bootstraps Python and Node dependencies
- `check_env.sh`: verifies host dependencies
- `create_server_bundle.sh`: packages the repo plus env files for server transfer
- `install_systemd_services.sh`: installs the API service and shorts timers
- `video-worker.env.example`: runtime overrides for the API service
- `shorts/`: clip generation, queueing, local YouTube poster, and Linux timer templates

## What You Need On The Target Server

Install these system dependencies first:

- `python3`
- `python3-venv`
- `node`
- `npm`
- `ffmpeg`
- `ImageMagick` with `magick`

For GPU encoding:

- NVIDIA driver
- `nvidia-smi`
- `ffmpeg` build with `h264_nvenc`

## Recommended Transfer Flow

1. Use either a private Git repo or `./create_server_bundle.sh`.
2. On the new server:

```bash
git clone <your-repo-url>
cd Video_Worker_GitHub
./setup.sh
source .venv/bin/activate
./check_env.sh
./start_video.sh
```

For the full unattended server path, including env transfer, YouTube credential transfer, and systemd installation, follow [SERVER_SETUP.md](/home/Anon/Desktop/Github/Video_Worker/SERVER_SETUP.md:1).

## Server Startup

Default bind:

- host: `0.0.0.0`
- port: `8002`

You can override them:

```bash
HOST=0.0.0.0 PORT=8002 ./start_video.sh
```

If you want those settings to persist for systemd, copy `video-worker.env.example` to `video-worker.env` and set them there.

## Runtime Environment Flags

These are optional:

- `VIDEO_ENCODER`
  - example: `VIDEO_ENCODER=h264_nvenc`
- `VIDEO_GPU_FILTERS`
  - `1` enables experimental GPU filter paths
  - `0` disables them
- `VIDEO_X264_PRESET`
  - default: `veryfast`
  - use `medium` or `slow` for higher quality on CPU
- `VIDEO_X264_CRF`
  - default: `18`
  - lower is higher quality; `16` to `20` is a practical range
- `VIDEO_NVENC_PRESET`
  - default: `p5`
  - use `p6` or `p7` for more quality if your GPU can handle it
- `VIDEO_NVENC_CQ`
  - default: `20`
  - lower is higher quality; `18` to `22` is a practical range
- `VIDEO_BACKGROUND_MUSIC`
  - optional path to a default background track
  - relative paths resolve from the repo root
- `VIDEO_BACKGROUND_MUSIC_VOLUME`
  - default: `0.04`
  - keep this low; `0.02` to `0.06` is a good range
- `VIDEO_AUTO_SHORTS`
  - default: enabled
  - set `VIDEO_AUTO_SHORTS=0` to disable automatic shorts generation after `/render`
- `VIDEO_DELETE_POSTED_SHORTS`
  - default: `1`
  - deletes local short mp4s after a successful YouTube upload
- `VIDEO_POSTED_QUEUE_KEEP`
  - default: `120`
  - keeps only the newest posted queue records
- `VIDEO_POSTED_QUEUE_RETENTION_DAYS`
  - default: `21`
  - drops posted queue history older than this
- `VIDEO_RENDER_KEEP_JOBS`
  - default: `6`
  - soft-keep count for recent `out/<job>/` folders
- `VIDEO_RENDER_RETENTION_DAYS`
  - default: `2`
  - age limit for old render folders and their logs
- `VIDEO_RENDER_MAX_GB`
  - default: `8`
  - storage cap for retained render job folders
- `VIDEO_RENDER_MIN_AGE_MINUTES`
  - default: `60`
  - grace window that protects very recent render folders from cleanup
- `VIDEO_ORPHAN_CLIP_RETENTION_DAYS`
  - default: `2`
  - deletes stray short files not referenced by the active queue

Example:

```bash
VIDEO_ENCODER=h264_nvenc VIDEO_GPU_FILTERS=0 ./start_video.sh
```

Recommended higher-quality CPU render:

```bash
VIDEO_ENCODER=libx264 VIDEO_X264_PRESET=medium VIDEO_X264_CRF=16 ./start_video.sh
```

## Render Endpoint

Multipart `POST` to `/render`

Fields:

- `text`
- `audio`
- `music` optional
- `image` optional

Example:

```bash
curl -X POST "http://127.0.0.1:8002/render" \
  -F 'text=Your story text here' \
  -F "audio=@/full/path/to/voice.wav" \
  -F "music=@/full/path/to/lofi-bed.mp3" \
  -F "image=@/full/path/to/image.jpg" \
  -o final.mp4
```

Without image:

```bash
curl -X POST "http://127.0.0.1:8002/render" \
  -F 'text=Your story text here' \
  -F "audio=@/full/path/to/voice.wav" \
  -o final.mp4
```

Default repo-level background music:

- drop a file at `assets/lofi-bed.mp3`
- also supported: `assets/lofi-bed.wav`, `.m4a`, `.aac`, `.ogg`, `.flac`
- if present, it is mixed in automatically even when you do not upload `music`

Automatic shorts after `/render`:

- after `final.mp4` is created, the server starts a background shorts job automatically
- generated shorts are written to `shorts/clips/`
- filenames are prefixed with the render job id so they do not overwrite each other
- generated shorts are appended to the local posting queue in clip order `1 -> 2 -> 3`
- each render job also gets `shorts.log` and `shorts_manifest.json` inside its `out/<job>/` folder
- successfully posted shorts are deleted from `shorts/clips/`
- old render folders, logs, and posted queue history are pruned automatically
- this does not change the main `/render` response; it still returns `final.mp4`

Scheduled YouTube posting:

- `shorts/post_next_short.py` posts the next queued short
- `shorts/run_maintenance.py` runs the storage cleanup pass manually
- systemd templates are included under `shorts/systemd/`
- the included schedule is `07:30`, `12:30`, and `16:30` every day
- an hourly maintenance timer is also included to keep storage trimmed
- full setup instructions live in `shorts/README.md`
- the unattended server install flow lives in [SERVER_SETUP.md](/home/Anon/Desktop/Github/Video_Worker/SERVER_SETUP.md:1)

## Shorts Endpoint

Multipart `POST` to `/render_shorts`

Fields:

- `video`

Response:

- JSON
- `short1`, `short2`, `short3` are base64-encoded mp4 payloads for n8n
- `short1_filename`, `short2_filename`, `short3_filename` include the output names
- `short_count` tells you how many clips were actually produced

Example:

```bash
curl -X POST "http://127.0.0.1:8002/render_shorts" \
  -F "video=@/full/path/to/video.mp4"
```

## Output Behavior

Each render creates a unique job folder under `out/`.

- in-progress encode: `final.encoding.mp4`
- finished output: `final.mp4`
- latest completed copy: `out/current/`
- latest completed shorts copy: `out/current_shorts/`
- older job folders are cleaned automatically based on the retention env vars above

## Notes

- Do not commit `.venv`, `node_modules`, or `out/`
- Do not commit `shorts/credentials/`, generated `shorts/clips/`, or `shorts/state/`
- Restart the server after code changes
- If the host has trouble with GPU filters, start with:

```bash
VIDEO_ENCODER=h264_nvenc VIDEO_GPU_FILTERS=0 ./start_video.sh
```
