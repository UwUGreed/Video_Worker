import os, uuid, subprocess, shutil, wave, contextlib, time
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, PlainTextResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(BASE_DIR, "out")
os.makedirs(OUT_ROOT, exist_ok=True)

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 720
OUTPUT_FPS = 24
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

def build_ffmpeg_cmd(segments_txt, audio_wav, audio_duration, out_path, encoder):
    if encoder == "h264_nvenc":
        encode_args = [
            "-c:v", "h264_nvenc",
            "-preset", "p1",
            "-cq", "28",
            "-rc", "vbr",
            "-pix_fmt", "yuv420p",
        ]
    else:
        encode_args = [
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-pix_fmt", "yuv420p",
        ]

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", segments_txt,
        "-i", audio_wav,
        "-map", "0:v",
        "-map", "1:a",
        "-t", str(audio_duration),
        "-c:a", "aac",
        "-b:a", OUTPUT_AUDIO_BITRATE,
        "-movflags", "+faststart",
        "-r", str(OUTPUT_FPS),
        "-threads", "0",
    ] + encode_args + [out_path]

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

    # 1) render segments via node
    dur = wav_duration_seconds(audio_wav)
    node = node_exe()
    cmd = [node, os.path.join(BASE_DIR, "render.js"), od, text, str(dur), image_path]
    log_job(job, "rendering segments")
    r = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
    open(os.path.join(od, "render.log"), "w", encoding="utf-8").write(
        "CMD: " + str(cmd) + "\nRC: " + str(r.returncode) +
        "\n---STDOUT---\n" + (r.stdout or "") +
        "\n---STDERR---\n" + (r.stderr or "")
    )
    if r.returncode != 0:
        log_job(job, "segment render failed")
        return PlainTextResponse("render.js failed. See: " + os.path.join(od, "render.log"), status_code=500)
    segments_txt = os.path.join(od, "segments.txt")
    if not os.path.exists(segments_txt):
        log_job(job, "segments.txt not created after render")
        return PlainTextResponse("render.js failed. See: " + os.path.join(od, "render.log"), status_code=500)
    log_job(job, "segments ready")

    # 2) combine segments + audio
    out_mp4 = os.path.join(od, "final.mp4")
    out_tmp_mp4 = os.path.join(od, "final.encoding.mp4")

    encoder = preferred_video_encoder()
    log_job(job, f"encoding video for {dur:.1f}s of audio with {encoder}")

    if encoder == "h264_nvenc":
        attempts = [
            ("h264_nvenc", "cpu", "NVENC"),
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
                build_ffmpeg_cmd(segments_txt, audio_wav, dur, out_tmp_mp4, attempt_encoder),
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










