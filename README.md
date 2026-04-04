# Video Worker

This folder is a clean GitHub-ready package of the video rendering worker from this repo.

It exposes a FastAPI endpoint that:

- accepts story text
- accepts a WAV voice track
- optionally accepts an image
- renders a fast stepped read-through video of the story post
- returns the finished `final.mp4`

## Included Files

- `video_api.py`: API server and ffmpeg pipeline
- `render.js`: story image renderer
- `template.html`: original style reference for the renderer
- `package.json` and `package-lock.json`: Node dependencies
- `requirements.txt`: Python dependencies
- `start_video.sh`: starts the API server
- `setup.sh`: bootstraps Python and Node dependencies
- `check_env.sh`: verifies host dependencies

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

1. Push this folder to GitHub as its own repo or as a subfolder in your repo.
2. On the new server:

```bash
git clone <your-repo-url>
cd Video_Worker_GitHub
./setup.sh
source .venv/bin/activate
./check_env.sh
./start_video.sh
```

## Server Startup

Default bind:

- host: `0.0.0.0`
- port: `8002`

You can override them:

```bash
HOST=0.0.0.0 PORT=8002 ./start_video.sh
```

## Runtime Environment Flags

These are optional:

- `VIDEO_ENCODER`
  - example: `VIDEO_ENCODER=h264_nvenc`
- `VIDEO_GPU_FILTERS`
  - `1` enables experimental GPU filter paths
  - `0` disables them

Example:

```bash
VIDEO_ENCODER=h264_nvenc VIDEO_GPU_FILTERS=0 ./start_video.sh
```

## Render Endpoint

Multipart `POST` to `/render`

Fields:

- `text`
- `audio`
- `image` optional

Example:

```bash
curl -X POST "http://127.0.0.1:8002/render" \
  -F 'text=Your story text here' \
  -F "audio=@/full/path/to/voice.wav" \
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

## Output Behavior

Each render creates a unique job folder under `out/`.

- in-progress encode: `final.encoding.mp4`
- finished output: `final.mp4`
- latest completed copy: `out/current/`

## Render Behavior

- The renderer generates a sequence of overlapping viewport steps through the post instead of a full continuous scroll.
- Slide timing is distributed to fit the uploaded audio duration so the end of the story is not cut off.
- Long stories are no longer capped to a fixed layout height before segment generation.

## Notes

- Do not commit `.venv`, `node_modules`, or `out/`
- Restart the server after code changes
- If the host has trouble with GPU filters, start with:

```bash
VIDEO_ENCODER=h264_nvenc VIDEO_GPU_FILTERS=0 ./start_video.sh
```
