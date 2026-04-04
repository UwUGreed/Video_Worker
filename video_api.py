import os, uuid, subprocess, shutil, wave, contextlib, time
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, PlainTextResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(BASE_DIR, "out")
os.makedirs(OUT_ROOT, exist_ok=True)

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 720
OUTPUT_FPS = 12
OUTPUT_AUDIO_BITRATE = "128k"

def node_exe():
    n = shutil.which("node")
    if n: return n
    cand = r"C:\Program Files\nodejs\node.exe"
    return cand

def wav_duration_seconds(path):
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path
            ],
            capture_output=True, text=True, timeout=10
        )
        val = float(result.stdout.strip())
        if val > 0:
            return val
    except Exception:
        pass
    return 30.0

def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "").strip())
    return (p.stdout or "").strip()

def ffmpeg_has_encoder(name):
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        text=True
    )
    return p.returncode == 0 and name in (p.stdout or "")

def nvidia_runtime_available():
    p = subprocess.run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
        capture_output=True,
        text=True
    )
    return p.returncode == 0 and bool((p.stdout or "").strip())

def preferred_video_encoder():
    forced = (os.environ.get("VIDEO_ENCODER") or "").strip().lower()
    if forced:
        return forced
    if ffmpeg_has_encoder("h264_nvenc") and nvidia_runtime_available():
        return "h264_nvenc"
    return "libx264"

def gpu_filters_enabled():
    value = (os.environ.get("VIDEO_GPU_FILTERS") or "1").strip().lower()
    return value not in {"0", "false", "no", "off"}

def build_ffmpeg_cmd(png, audio_wav, scroll_dur, out_path, encoder, filter_mode="cpu"):
    if encoder == "h264_nvenc" and filter_mode == "gpu":
        filter_complex = (
            "[0:v]format=yuv420p,hwupload_cuda[base];"
            "[1:v]format=yuv420p,hwupload_cuda,"
            "scale_cuda=w={width}:h=-2:format=yuv420p:interp_algo=lanczos[txt];"
            "[base][txt]overlay_cuda=x=0:y=if(gt(overlay_h\\,{height})\\,-(overlay_h-{height})*t/{scroll_dur}\\,0)[v]"
        ).format(width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT, scroll_dur=scroll_dur)
    elif encoder == "h264_nvenc" and filter_mode == "gpu_scale":
        filter_complex = (
            "[1:v]format=rgba,hwupload_cuda,"
            "scale_cuda=w={width}:h=-2:interp_algo=lanczos,"
            "hwdownload,format=rgba,"
            "crop={width}:{height}:0:if(gt(ih\\,{height})\\,(ih-{height})*t/{scroll_dur}\\,0)[txt];"
            "[0:v][txt]overlay=0:0,format=yuv420p[v]"
        ).format(width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT, scroll_dur=scroll_dur)
    else:
        filter_complex = (
            "[0:v]scale={width}:-1:flags=lanczos,format=rgba,"
            "crop={width}:{height}:0:if(gt(ih\\,{height})\\,(ih-{height})*t/{scroll_dur}\\,0)[txt];"
            "[txt]scale={width}:{height}:force_original_aspect_ratio=increase,"
            "crop={width}:{height},format=yuv420p[v]"
        ).format(width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT, scroll_dur=scroll_dur)

    cmd = [
        "ffmpeg","-y",
        "-loop","1","-i", png,
        "-i", audio_wav,
        "-filter_complex", filter_complex,
        "-map","[v]","-map","1:a",
    ]

    if encoder == "h264_nvenc":
        cmd.extend([
            "-c:v","h264_nvenc",
            "-preset","p1",
            "-cq","30",
            "-rc","vbr",
            "-pix_fmt","yuv420p",
        ])
    else:
        cmd.extend([
            "-c:v","libx264",
            "-preset","ultrafast",
            "-crf","26",
            "-pix_fmt","yuv420p",
        ])

    cmd.extend([
        "-c:a","aac","-b:a",OUTPUT_AUDIO_BITRATE,
        "-movflags","+faststart",
        "-progress","pipe:1",
        "-nostats",
        "-shortest",
        out_path
    ])
    return cmd

def log_job(job, message, end="\n"):
    print(f"[render:{job}] {message}", end=end, flush=True)

def run_ffmpeg_with_progress(cmd, cwd, duration_seconds, job):
    p = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    last_emit = 0.0
    last_percent = -1
    last_line = ""
    recent_lines = []

    try:
        for raw_line in p.stdout:
            line = raw_line.strip()
            if not line:
                continue
            last_line = line
            recent_lines.append(line)
            if len(recent_lines) > 20:
                recent_lines.pop(0)

            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            if key not in {"out_time_ms", "out_time_us"}:
                continue

            try:
                out_time_us = int(value)
            except ValueError:
                continue

            if key == "out_time_ms":
                out_time_us *= 1000

            seconds_done = max(out_time_us / 1_000_000.0, 0.0)
            percent = 100 if duration_seconds <= 0 else min(int((seconds_done / duration_seconds) * 100), 100)
            now = time.time()

            if percent != last_percent and (percent == 100 or now - last_emit >= 1.0):
                log_job(job, f"encoding {percent}% ({seconds_done:.1f}s / {duration_seconds:.1f}s)", end="\r")
                last_emit = now
                last_percent = percent
    finally:
        rc = p.wait()
        print("", flush=True)

    if rc != 0:
        detail = "\n".join(recent_lines[-8:]).strip()
        raise RuntimeError(detail or last_line or f"ffmpeg exited with code {rc}")

def cleanup_dir(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)

app = FastAPI()

@app.post("/render")
async def render(text: str = Form(...), audio: UploadFile = File(...), image: UploadFile | None = File(None)):
    job = uuid.uuid4().hex[:12]
    od = os.path.join(OUT_ROOT, job)
    os.makedirs(od, exist_ok=True)
    log_job(job, "job started")

    audio_wav = os.path.join(od, "voice.wav")
    with open(audio_wav, "wb") as f:
        f.write(await audio.read())

    image_path = ""
    if image and image.filename:
        ext = os.path.splitext(image.filename)[1].lower() or ".png"
        image_path = os.path.join(od, f"image{ext}")
        with open(image_path, "wb") as f:
            f.write(await image.read())

    # 1) render story.png via node/playwright
    png = os.path.join(od, "story.png")
    node = node_exe()
    cmd = [node, os.path.join(BASE_DIR, "render.js"), od, text, image_path]
    log_job(job, "rendering story image")
    r = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
    open(os.path.join(od, "render.log"), "w", encoding="utf-8").write(
        "CMD: " + str(cmd) + "\nRC: " + str(r.returncode) +
        "\n---STDOUT---\n" + (r.stdout or "") +
        "\n---STDERR---\n" + (r.stderr or "")
    )
    if r.returncode != 0:
        log_job(job, "story image render failed")
        return PlainTextResponse("render.js failed. See: " + os.path.join(od, "render.log"), status_code=500)
    if not os.path.exists(png):
        log_job(job, "story image missing after render")
        return PlainTextResponse("story.png not created. See: " + os.path.join(od, "render.log"), status_code=500)
    log_job(job, "story image ready")

    # 2) combine scroll video + audio
    dur = wav_duration_seconds(audio_wav)
    out_mp4 = os.path.join(od, "final.mp4")
    out_tmp_mp4 = os.path.join(od, "final.encoding.mp4")
    scroll_dur = max(dur, 1.0)

    encoder = preferred_video_encoder()
    log_job(job, f"encoding video for {dur:.1f}s of audio with {encoder}")

    if encoder == "h264_nvenc":
        attempts = [
            ("h264_nvenc", "cpu", "NVENC + CPU filters"),
            ("libx264", "cpu", "CPU encode"),
        ]
    else:
        attempts = [("libx264", "cpu", "CPU encode")]

    last_error = None
    for attempt_encoder, filter_mode, label in attempts:
        try:
            if attempt_encoder != encoder or filter_mode != attempts[0][1]:
                log_job(job, f"retrying with {attempt_encoder} and {label.lower()}")
            run_ffmpeg_with_progress(
                build_ffmpeg_cmd(png, audio_wav, scroll_dur, out_tmp_mp4, attempt_encoder, filter_mode),
                cwd=od,
                duration_seconds=dur,
                job=job
            )
            last_error = None
            break
        except Exception as e:
            last_error = e
            log_job(job, f"{attempt_encoder} with {label.lower()} failed: {e}")

    if last_error is not None:
        return PlainTextResponse("ffmpeg failed: " + str(last_error), status_code=500)

    if not os.path.exists(out_tmp_mp4):
        log_job(job, "ffmpeg finished without output file")
        return PlainTextResponse("ffmpeg did not create output file", status_code=500)

    os.replace(out_tmp_mp4, out_mp4)

    latest_dir = os.path.join(OUT_ROOT, "current")
    cleanup_dir(latest_dir)
    shutil.copytree(od, latest_dir)

    response = FileResponse(out_mp4, media_type="video/mp4", filename="final.mp4")
    response.headers["Content-Disposition"] = 'attachment; filename="final.mp4"; filename*=UTF-8\'\'final.mp4'
    response.headers["Content-Type"] = "video/mp4"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Filename"] = "final.mp4"
    response.headers["X-Job-Id"] = job
    log_job(job, f"done: {out_mp4}")
    return response










