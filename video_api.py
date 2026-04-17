import base64, json, os, uuid, subprocess, shutil, time
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(BASE_DIR, "out")
os.makedirs(OUT_ROOT, exist_ok=True)

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 720
OUTPUT_FPS = 12
OUTPUT_AUDIO_BITRATE = "128k"
DEFAULT_X264_PRESET = "veryfast"
DEFAULT_X264_CRF = "18"
DEFAULT_NVENC_PRESET = "p5"
DEFAULT_NVENC_CQ = "20"
DEFAULT_BACKGROUND_MUSIC_VOLUME = 0.04
DEFAULT_BACKGROUND_MUSIC_CANDIDATES = (
    os.path.join("assets", "lofi-bed.mp3"),
    os.path.join("assets", "lofi-bed.wav"),
    os.path.join("assets", "lofi-bed.m4a"),
    os.path.join("assets", "lofi-bed.aac"),
    os.path.join("assets", "lofi-bed.ogg"),
    os.path.join("assets", "lofi-bed.flac"),
)
SHORTS_OUTPUT_DIR = os.path.join(BASE_DIR, "shorts", "clips")

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
    if not shutil.which("nvidia-smi"):
        return False
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

def video_encoder_settings(encoder):
    if encoder == "h264_nvenc":
        preset = (os.environ.get("VIDEO_NVENC_PRESET") or "").strip() or DEFAULT_NVENC_PRESET
        quality_value = (os.environ.get("VIDEO_NVENC_CQ") or "").strip() or DEFAULT_NVENC_CQ
        return {
            "codec": "h264_nvenc",
            "preset_flag": "-preset",
            "preset_value": preset,
            "quality_flag": "-cq",
            "quality_value": quality_value,
        }

    preset = (os.environ.get("VIDEO_X264_PRESET") or "").strip() or DEFAULT_X264_PRESET
    quality_value = (os.environ.get("VIDEO_X264_CRF") or "").strip() or DEFAULT_X264_CRF
    return {
        "codec": "libx264",
        "preset_flag": "-preset",
        "preset_value": preset,
        "quality_flag": "-crf",
        "quality_value": quality_value,
    }

def log_job(job, message, end="\n"):
    print(f"[render:{job}] {message}", end=end, flush=True)

def background_music_volume():
    raw = (os.environ.get("VIDEO_BACKGROUND_MUSIC_VOLUME") or "").strip()
    if not raw:
        return DEFAULT_BACKGROUND_MUSIC_VOLUME
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_BACKGROUND_MUSIC_VOLUME
    return max(0.0, min(value, 1.0))

def resolve_background_music_path():
    configured = (os.environ.get("VIDEO_BACKGROUND_MUSIC") or "").strip()
    if configured:
        candidate = configured if os.path.isabs(configured) else os.path.join(BASE_DIR, configured)
        if os.path.exists(candidate):
            return candidate
        return ""

    for rel_path in DEFAULT_BACKGROUND_MUSIC_CANDIDATES:
        candidate = os.path.join(BASE_DIR, rel_path)
        if os.path.exists(candidate):
            return candidate
    return ""

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

def encode_file_base64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")

def auto_shorts_enabled():
    raw = (os.environ.get("VIDEO_AUTO_SHORTS") or "").strip().lower()
    if not raw:
        return True
    return raw not in {"0", "false", "no", "off"}

def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def log_cleanup_summary(job, summary):
    if not summary:
        return
    log_job(
        job,
        (
            "storage cleanup: "
            f"render_dirs={summary.get('render_dirs_deleted', 0)}, "
            f"orphan_clips={summary.get('orphan_clips_deleted', 0)}, "
            f"queue_pruned={summary.get('queue_entries_pruned', 0)}, "
            f"freed_bytes={summary.get('freed_bytes', 0)}"
        ),
    )
    errors = summary.get("errors") or []
    if errors:
        log_job(job, "storage cleanup warnings: " + "; ".join(errors[:3]))

def generate_shorts_for_render(job, source_video, job_dir):
    log_path = os.path.join(job_dir, "shorts.log")
    manifest_path = os.path.join(job_dir, "shorts_manifest.json")
    active_marker_path = os.path.join(job_dir, ".shorts_in_progress")
    cleanup_runner = None

    try:
        from shorts.karen_clipper import generate_featured_shorts
        from shorts.publisher import register_generated_shorts, run_storage_maintenance
        cleanup_runner = run_storage_maintenance

        os.makedirs(SHORTS_OUTPUT_DIR, exist_ok=True)
        with open(active_marker_path, "w", encoding="utf-8") as f:
            f.write("running\n")
        log_job(job, "auto shorts started")
        short_items = generate_featured_shorts(
            source_video,
            output_dir=SHORTS_OUTPUT_DIR,
            base_name_override=f"{job}_short",
            return_metadata=True,
        )
        short_paths = [item["output_path"] for item in short_items]
        queued_entries = register_generated_shorts(job, short_items)
        write_json(
            manifest_path,
            {
                "job_id": job,
                "source_video": source_video,
                "shorts": short_paths,
                "queued": [entry["queue_id"] for entry in queued_entries],
            },
        )
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("status=ok\n")
            for path in short_paths:
                f.write(path + "\n")
        log_job(job, f"auto shorts ready: {len(short_paths)} clips")
    except Exception as e:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("status=error\n")
            f.write(str(e) + "\n")
        log_job(job, f"auto shorts failed: {e}")
    finally:
        if os.path.exists(active_marker_path):
            try:
                os.unlink(active_marker_path)
            except OSError:
                pass
        try:
            if cleanup_runner is None:
                from shorts.publisher import run_storage_maintenance

                cleanup_runner = run_storage_maintenance
            cleanup_summary = cleanup_runner()
        except Exception as cleanup_error:
            log_job(job, f"storage cleanup failed: {cleanup_error}")
        else:
            log_cleanup_summary(job, cleanup_summary)

app = FastAPI()


@app.get("/healthz")
async def healthz():
    return JSONResponse(
        {
            "status": "ok",
            "auto_shorts": auto_shorts_enabled(),
            "output_root": OUT_ROOT,
            "shorts_output_dir": SHORTS_OUTPUT_DIR,
        }
    )

@app.post("/render")
async def render(
    background_tasks: BackgroundTasks,
    text: str = Form(...),
    audio: UploadFile = File(...),
    image: UploadFile | None = File(None),
    music: UploadFile | None = File(None)
):
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

    background_music_path = ""
    mix_volume = background_music_volume()
    if music and music.filename:
        ext = os.path.splitext(music.filename)[1].lower() or ".mp3"
        background_music_path = os.path.join(od, f"music{ext}")
        with open(background_music_path, "wb") as f:
            f.write(await music.read())
        log_job(job, f"using uploaded background music at volume {mix_volume:.2f}")
    else:
        default_music_path = resolve_background_music_path()
        if default_music_path:
            ext = os.path.splitext(default_music_path)[1].lower() or ".mp3"
            background_music_path = os.path.join(od, f"music{ext}")
            shutil.copyfile(default_music_path, background_music_path)
            log_job(job, f"using default background music at volume {mix_volume:.2f}")

    # 1) render scroll.webm via node/playwright
    dur = wav_duration_seconds(audio_wav)
    scroll_webm = os.path.join(od, "scroll.webm")
    node = node_exe()
    cmd = [node, os.path.join(BASE_DIR, "render.js"), od, text, str(dur), image_path]
    log_job(job, "rendering scroll video")
    r = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
    open(os.path.join(od, "render.log"), "w", encoding="utf-8").write(
        "CMD: " + str(cmd) + "\nRC: " + str(r.returncode) +
        "\n---STDOUT---\n" + (r.stdout or "") +
        "\n---STDERR---\n" + (r.stderr or "")
    )
    if r.returncode != 0:
        log_job(job, "scroll video render failed")
        return PlainTextResponse("render.js failed. See: " + os.path.join(od, "render.log"), status_code=500)
    if not os.path.exists(scroll_webm):
        log_job(job, "scroll video missing after render")
        return PlainTextResponse("scroll.webm not created. See: " + os.path.join(od, "render.log"), status_code=500)
    log_job(job, "scroll video ready")

    # 2) combine scroll video + audio
    out_mp4 = os.path.join(od, "final.mp4")
    out_tmp_mp4 = os.path.join(od, "final.encoding.mp4")

    encoder = preferred_video_encoder()
    encoder_settings = video_encoder_settings(encoder)
    log_job(
        job,
        (
            f"muxing video with {encoder_settings['codec']} "
            f"({encoder_settings['preset_flag'][1:]}={encoder_settings['preset_value']}, "
            f"{encoder_settings['quality_flag'][1:]}={encoder_settings['quality_value']})"
        )
    )

    mux_cmd = ["ffmpeg", "-y", "-i", scroll_webm, "-i", audio_wav]
    if background_music_path:
        mux_cmd.extend([
            "-stream_loop", "-1",
            "-i", background_music_path,
            "-filter_complex",
            (
                f"[1:a]aresample=async=1:first_pts=0[voice];"
                f"[2:a]aresample=async=1:first_pts=0,volume={mix_volume:.3f}[music];"
                "[voice][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            ),
            "-map", "0:v:0",
            "-map", "[aout]",
        ])

    mux_cmd.extend([
        "-c:v", encoder_settings["codec"],
        encoder_settings["preset_flag"], encoder_settings["preset_value"],
        encoder_settings["quality_flag"], encoder_settings["quality_value"],
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", OUTPUT_AUDIO_BITRATE,
        "-movflags", "+faststart",
        "-shortest",
        out_tmp_mp4
    ])

    try:
        run_ffmpeg_with_progress(mux_cmd, cwd=od, duration_seconds=dur, job=job)
    except Exception as e:
        return PlainTextResponse("ffmpeg failed: " + str(e), status_code=500)

    if not os.path.exists(out_tmp_mp4):
        log_job(job, "ffmpeg finished without output file")
        return PlainTextResponse("ffmpeg did not create output file", status_code=500)

    os.replace(out_tmp_mp4, out_mp4)

    latest_dir = os.path.join(OUT_ROOT, "current")
    cleanup_dir(latest_dir)
    shutil.copytree(od, latest_dir)

    if auto_shorts_enabled():
        background_tasks.add_task(generate_shorts_for_render, job, out_mp4, od)
    else:
        from shorts.publisher import run_storage_maintenance

        background_tasks.add_task(run_storage_maintenance)

    response = FileResponse(out_mp4, media_type="video/mp4", filename="final.mp4")
    response.headers["Content-Disposition"] = 'attachment; filename="final.mp4"; filename*=UTF-8\'\'final.mp4'
    response.headers["Content-Type"] = "video/mp4"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Filename"] = "final.mp4"
    response.headers["X-Job-Id"] = job
    log_job(job, f"done: {out_mp4}")
    return response


@app.post("/render_shorts")
async def render_shorts(video: UploadFile = File(...)):
    job = uuid.uuid4().hex[:12]
    od = os.path.join(OUT_ROOT, f"shorts_{job}")
    os.makedirs(od, exist_ok=True)
    log_job(job, "shorts job started")

    ext = os.path.splitext(video.filename or "")[1].lower() or ".mp4"
    source_video = os.path.join(od, f"source{ext}")
    with open(source_video, "wb") as f:
        f.write(await video.read())

    shorts_dir = os.path.join(od, "clips")
    os.makedirs(shorts_dir, exist_ok=True)

    try:
        from shorts.karen_clipper import generate_featured_shorts
        short_paths = generate_featured_shorts(source_video, output_dir=shorts_dir)
    except Exception as e:
        log_job(job, f"shorts render failed: {e}")
        return PlainTextResponse("shorts render failed: " + str(e), status_code=500)

    latest_dir = os.path.join(OUT_ROOT, "current_shorts")
    cleanup_dir(latest_dir)
    shutil.copytree(od, latest_dir)

    payload = {
        "job_id": job,
        "encoding": "base64",
        "mime_type": "video/mp4",
        "short_count": len(short_paths),
        "short1": None,
        "short2": None,
        "short3": None,
        "short1_filename": None,
        "short2_filename": None,
        "short3_filename": None,
    }

    for idx, path in enumerate(short_paths[:3], 1):
        payload[f"short{idx}"] = encode_file_base64(path)
        payload[f"short{idx}_filename"] = os.path.basename(path)

    try:
        from shorts.publisher import run_storage_maintenance

        cleanup_summary = run_storage_maintenance()
    except Exception as cleanup_error:
        log_job(job, f"storage cleanup failed: {cleanup_error}")
    else:
        log_cleanup_summary(job, cleanup_summary)

    log_job(job, f"done shorts: {shorts_dir}")
    response = JSONResponse(payload)
    response.headers["X-Job-Id"] = job
    return response










