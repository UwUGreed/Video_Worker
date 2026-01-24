import os, uuid, subprocess, shutil, wave, contextlib
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, PlainTextResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(BASE_DIR, "out")
os.makedirs(OUT_ROOT, exist_ok=True)

def node_exe():
    n = shutil.which("node")
    if n: return n
    cand = r"C:\Program Files\nodejs\node.exe"
    return cand

def wav_duration_seconds(path):
    try:
        with contextlib.closing(wave.open(path, "rb")) as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return max(frames / float(rate), 0.1)
    except Exception:
        return 60.0  # fallback

def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "").strip())
    return (p.stdout or "").strip()

app = FastAPI()

@app.post("/render")
async def render(text: str = Form(...), audio: UploadFile = File(...)):
    job = uuid.uuid4().hex[:8]
    od = os.path.join(OUT_ROOT, job)
    os.makedirs(od, exist_ok=True)

    audio_wav = os.path.join(od, "voice.wav")
    with open(audio_wav, "wb") as f:
        f.write(await audio.read())

    # 1) render story.png via node/playwright
    png = os.path.join(od, "story.png")
    node = node_exe()
    cmd = [node, os.path.join(BASE_DIR, "render.js"), od, text]
    r = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
    open(os.path.join(od, "render.log"), "w", encoding="utf-8").write(
        "CMD: " + str(cmd) + "\nRC: " + str(r.returncode) +
        "\n---STDOUT---\n" + (r.stdout or "") +
        "\n---STDERR---\n" + (r.stderr or "")
    )
    if r.returncode != 0:
        return PlainTextResponse("render.js failed. See: " + os.path.join(od, "render.log"), status_code=500)
    if not os.path.exists(png):
        return PlainTextResponse("story.png not created. See: " + os.path.join(od, "render.log"), status_code=500)

    # 2) combine scroll video + audio
    dur = wav_duration_seconds(audio_wav)
    out_mp4 = os.path.join(od, "final.mp4")

    vf = (
        "[1:v]scale=1080:-1:flags=lanczos,format=rgba,"
        ""
        "crop=1080:1920:0:if(gt(ih\\,1920)\\,(ih-1920)*t/(0.85*{dur})\\,0)[txt];"
        "[0:v][txt]overlay=0:0,format=yuv420p[v]"
    ).format(dur=dur)

    try:
        run([
            "ffmpeg","-y",
            "-f","lavfi","-i","color=size=1080x1920:rate=30:color=black",
            "-loop","1","-i", png,
            "-i", audio_wav,
            "-filter_complex", vf,
            "-map","[v]","-map","2:a",
            "-c:v","libx264","-preset","veryfast","-crf","18",
            "-c:a","aac","-b:a","192k",
            "-shortest",
            out_mp4
        ], cwd=od)
    except Exception as e:
        return PlainTextResponse("ffmpeg failed: " + str(e), status_code=500)

    return FileResponse(out_mp4, media_type="video/mp4", filename="final.mp4")










