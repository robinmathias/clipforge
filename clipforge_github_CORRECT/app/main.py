from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from urllib.parse import urlparse
import json
import math
import re
import shutil
import subprocess
import threading
import time
import uuid

import yt_dlp

BASE = Path(__file__).resolve().parent.parent
UPLOADS = BASE / "data" / "uploads"
OUTPUTS = BASE / "data" / "outputs"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ClipForge")
app.mount("/static", StaticFiles(directory=BASE / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "app" / "templates")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-5000:])
    return p.stdout


def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "video.mp4")
    return name[:120]


def status_path(job: str) -> Path:
    return OUTPUTS / job / "status.json"


def public_job(job: dict) -> dict:
    return {
        k: v for k, v in job.items()
        if k not in {"source_path", "segments", "settings"}
    }


def save_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        serializable = dict(job)
        serializable.pop("segments", None)
        path = status_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")


def load_job(job_id: str) -> dict | None:
    with JOBS_LOCK:
        if job_id in JOBS:
            return JOBS[job_id]
    path = status_path(job_id)
    if not path.exists():
        return None
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
        with JOBS_LOCK:
            JOBS[job_id] = job
        return job
    except Exception:
        return None


def update_job(job_id: str, **changes):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(changes)
        job["updated_at"] = time.time()
    save_job(job_id)


def set_progress(job_id: str, progress: int, stage: str, message: str):
    update_job(
        job_id,
        progress=max(0, min(100, int(progress))),
        stage=stage,
        message=message,
    )


def validate_video_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("Video link peab algama http:// või https://")
    host = (parsed.hostname or "").lower()
    allowed = (
        host == "youtu.be"
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "twitch.tv"
        or host.endswith(".twitch.tv")
    )
    if not allowed:
        raise RuntimeError("Praegu toetab URL sisestus ainult YouTube'i ja Twitchi linke.")
    return url


def download_video(url: str, job_id: str) -> Path:
    output_template = str(UPLOADS / f"{job_id}_downloaded.%(ext)s")

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            if total:
                frac = max(0.0, min(1.0, done / total))
                pct = 5 + round(frac * 18)
                set_progress(job_id, pct, "download", f"Downloading source video… {round(frac * 100)}%")
        elif d.get("status") == "finished":
            set_progress(job_id, 24, "download", "Source video downloaded. Preparing audio…")

    options = {
        "format": "bv*+ba/b",
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        prepared = Path(ydl.prepare_filename(info))

    mp4_path = UPLOADS / f"{job_id}_downloaded.mp4"
    if mp4_path.exists():
        return mp4_path
    if prepared.exists():
        return prepared
    matches = sorted(UPLOADS.glob(f"{job_id}_downloaded.*"))
    if matches:
        return matches[0]
    raise RuntimeError("Videot ei õnnestunud lingilt alla laadida.")


def video_duration(video_path: Path) -> float:
    out = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)
    ]).strip()
    return max(1.0, float(out))


def transcribe(video_path: Path, model_size: str, job_id: str):
    from faster_whisper import WhisperModel

    set_progress(job_id, 27, "transcribe", f"Loading {model_size} speech model…")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    duration = video_duration(video_path)
    segments, _ = model.transcribe(
        str(video_path),
        vad_filter=True,
        beam_size=5,
        word_timestamps=False,
    )
    rows = []
    for s in segments:
        text = (s.text or "").strip()
        if text:
            rows.append({"start": float(s.start), "end": float(s.end), "text": text})
        frac = max(0.0, min(1.0, float(s.end) / duration))
        pct = 30 + round(frac * 34)
        set_progress(job_id, pct, "transcribe", f"Transcribing speech… {round(frac * 100)}%")
    set_progress(job_id, 65, "highlights", "Transcript ready. Scoring highlight candidates…")
    return rows


def keyword_points(text: str) -> float:
    t = text.lower()
    strong = [
        "no way", "oh my god", "omg", "insane", "unbelievable", "crazy", "what just happened",
        "clutch", "win", "won", "goal", "save", "kill", "ace", "record", "best", "worst",
        "uskumatu", "appi", "issand", "päriselt", "võimatu", "hull", "võit", "värav", "parim",
        "mida asja", "ei ole võimalik", "ma ei usu", "jessas",
    ]
    medium = [
        "wow", "bro", "dude", "damn", "haha", "lol", "funny", "listen", "watch", "look",
        "wait", "story", "because", "but then", "here's", "this is", "you need", "remember",
        "vaata", "oota", "kuula", "sest", "aga siis", "tead", "mõtle", "naljakas",
    ]
    return sum(3.4 for k in strong if k in t) + sum(1.35 for k in medium if k in t)


def hook_points(text: str) -> float:
    t = text.strip().lower()
    starts = [
        "did you", "do you", "have you", "here's", "this is", "you won't", "watch", "look", "wait",
        "kas sa", "kas te", "vaata", "kuula", "oota", "tead mis", "see on", "ma näitan",
    ]
    score = 0.0
    if any(t.startswith(x) for x in starts):
        score += 3.0
    if "?" in text[:90]:
        score += 1.4
    if len(text.split()) >= 18:
        score += 0.8
    return score


def window_score(window_segments: list[dict], start: float, end: float) -> tuple[float, str]:
    text = " ".join(s["text"] for s in window_segments).strip()
    if not text:
        return -99.0, text

    duration = max(1.0, end - start)
    speech_seconds = sum(max(0.0, min(s["end"], end) - max(s["start"], start)) for s in window_segments)
    speech_ratio = min(1.0, speech_seconds / duration)
    words = text.split()
    word_density = len(words) / duration

    score = 0.0
    score += keyword_points(text)
    score += hook_points(text)
    score += min(text.count("!") * 1.1, 4.0)
    score += min(text.count("?") * 0.7, 2.1)
    score += min(len(words) / 45.0, 3.0)
    score += speech_ratio * 3.2

    # Sweet spot for short-form: enough speech to carry a complete idea, but not nonstop rambling.
    if 1.5 <= word_density <= 3.8:
        score += 2.0
    elif word_density < 0.7:
        score -= 2.5

    # Reward conversational/emotional patterns and a self-contained ending.
    lowered = text.lower()
    if any(x in lowered for x in ["but", "so", "because", "then", "aga", "siis", "sest", "seega"]):
        score += 1.0
    if text.rstrip().endswith(("!", "?", ".")):
        score += 0.5

    # Penalize very repetitive windows.
    unique_ratio = len(set(w.lower().strip(".,!?\"'") for w in words)) / max(1, len(words))
    if unique_ratio < 0.45:
        score -= 1.5

    return score, text


def rank_candidates(segments: list[dict], target_len: int) -> list[dict]:
    if not segments:
        return []
    video_end = segments[-1]["end"]
    candidates = []

    # Candidate starts are tied to spoken segments so clips begin near real speech.
    for anchor in segments:
        start = max(0.0, anchor["start"] - 2.0)
        end = min(video_end, start + target_len)
        if end - start < min(10, target_len * 0.55):
            continue
        window = [s for s in segments if s["end"] >= start and s["start"] <= end]
        score, text = window_score(window, start, end)
        candidates.append({"start": start, "end": end, "score": score, "text": text})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def choose_clips(segments: list[dict], count: int = 3, target_len: int = 35) -> list[dict]:
    candidates = rank_candidates(segments, target_len)
    picked = []
    for c in candidates:
        if all(abs(c["start"] - p["start"]) > target_len * 0.72 for p in picked):
            picked.append(c)
        if len(picked) >= count:
            break
    return picked or candidates[:count]


def make_title(text: str, index: int) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip(" .,!?:;-")
    if not cleaned:
        return f"Clip {index}"
    words = cleaned.split()
    title = " ".join(words[:8])
    if len(words) > 8:
        title += "…"
    return title[0].upper() + title[1:] if title else f"Clip {index}"


def fmt_ass_time(sec: float) -> str:
    h = int(sec // 3600)
    sec -= h * 3600
    m = int(sec // 60)
    sec -= m * 60
    s = int(sec)
    cs = int((sec - s) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def make_ass(segments, clip_start, clip_end, out_path: Path):
    header = """[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,DejaVu Sans,76,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,1,2,70,70,260,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"""
    lines = [header]
    for s in segments:
        if s["end"] < clip_start or s["start"] > clip_end:
            continue
        st = max(s["start"], clip_start) - clip_start
        en = min(s["end"], clip_end) - clip_start
        text = s["text"].replace("{", "(").replace("}", ")").replace("\n", " ")
        words = text.split()
        if len(words) > 8:
            mid = len(words) // 2
            text = " ".join(words[:mid]) + r"\N" + " ".join(words[mid:])
        lines.append(f"Dialogue: 0,{fmt_ass_time(st)},{fmt_ass_time(en)},Default,,0,0,0,,{text}\n")
    out_path.write_text("".join(lines), encoding="utf-8")


def render_clip(video: Path, start: float, end: float, ass_path: Path, out_path: Path):
    dur = max(1.0, end - start)
    ass_filter_path = str(ass_path).replace("\\", "/").replace(":", r"\:")
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        f"ass='{ass_filter_path}'"
    )
    cmd = [
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{dur:.3f}",
        "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_path),
    ]
    run(cmd)


def job_worker(job_id: str):
    job = load_job(job_id)
    if not job:
        return
    settings = job.get("settings", {})
    clip_count = int(settings.get("clip_count", 3))
    clip_length = int(settings.get("clip_length", 35))
    whisper_model = settings.get("whisper_model", "small")

    try:
        source_type = job.get("source_type")
        if source_type == "url":
            set_progress(job_id, 5, "download", "Connecting to source…")
            in_path = download_video(job["source_url"], job_id)
        else:
            in_path = Path(job["source_path"])
            set_progress(job_id, 24, "download", "Upload complete. Preparing audio…")

        update_job(job_id, source_path=str(in_path))
        segments = transcribe(in_path, whisper_model, job_id)
        if not segments:
            raise RuntimeError("Videost ei leitud kõnet, mida transkribeerida.")

        job_dir = OUTPUTS / job_id
        (job_dir / "transcript.json").write_text(
            json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with JOBS_LOCK:
            JOBS[job_id]["segments"] = segments

        set_progress(job_id, 68, "highlights", "Ranking moments for short-form potential…")
        picks = choose_clips(segments, clip_count, clip_length)
        if not picks:
            raise RuntimeError("Sobivaid klippe ei leitud.")

        clips = []
        total = len(picks)
        for idx, pick in enumerate(picks, 1):
            set_progress(
                job_id,
                70 + round(((idx - 1) / max(1, total)) * 27),
                "render",
                f"Rendering clip {idx} of {total}…",
            )
            ass = job_dir / f"clip_{idx}.ass"
            out = job_dir / f"clip_{idx}.mp4"
            make_ass(segments, pick["start"], pick["end"], ass)
            render_clip(in_path, pick["start"], pick["end"], ass, out)
            clips.append({
                "index": idx,
                "name": out.name,
                "url": f"/download/{job_id}/{out.name}",
                "start": round(pick["start"], 1),
                "end": round(pick["end"], 1),
                "score": round(pick["score"], 1),
                "title": make_title(pick["text"], idx),
                "text": pick["text"][:320],
                "deleted": False,
            })
            update_job(job_id, clips=clips)

        update_job(
            job_id,
            status="done",
            progress=100,
            stage="done",
            message=f"{len(clips)} clips ready.",
            clips=clips,
        )
    except Exception as e:
        update_job(
            job_id,
            status="error",
            stage="error",
            message="Processing failed.",
            error=str(e),
        )


def read_segments(job_id: str) -> list[dict]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job and job.get("segments"):
            return job["segments"]
    path = OUTPUTS / job_id / "transcript.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/jobs")
def create_job(
    video: UploadFile | None = File(None),
    video_url: str = Form(""),
    clip_count: int = Form(3),
    clip_length: int = Form(35),
    whisper_model: str = Form("small"),
):
    if clip_count < 1 or clip_count > 8:
        raise HTTPException(400, "Klippide arv peab olema 1–8.")
    if clip_length < 15 or clip_length > 90:
        raise HTTPException(400, "Klipi pikkus peab olema 15–90 sekundit.")
    if whisper_model not in {"tiny", "base", "small", "medium"}:
        raise HTTPException(400, "Tundmatu Whisperi mudel.")

    clean_url = validate_video_url(video_url)
    if not clean_url and not (video and video.filename):
        raise HTTPException(400, "Lisa video fail või YouTube/Twitchi link.")

    job_id = uuid.uuid4().hex[:10]
    job_dir = OUTPUTS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    job = {
        "id": job_id,
        "status": "queued",
        "progress": 3,
        "stage": "queued",
        "message": "Job created. Starting processor…",
        "created_at": time.time(),
        "updated_at": time.time(),
        "clips": [],
        "settings": {
            "clip_count": clip_count,
            "clip_length": clip_length,
            "whisper_model": whisper_model,
        },
    }

    if clean_url:
        job.update({"source_type": "url", "source_url": clean_url, "source_path": ""})
    else:
        in_path = UPLOADS / f"{job_id}_{safe_name(video.filename)}"
        with in_path.open("wb") as f:
            shutil.copyfileobj(video.file, f)
        job.update({"source_type": "upload", "source_url": "", "source_path": str(in_path)})

    with JOBS_LOCK:
        JOBS[job_id] = job
    save_job(job_id)

    thread = threading.Thread(target=job_worker, args=(job_id,), daemon=True)
    thread.start()

    return JSONResponse({"job": job_id, "status_url": f"/api/jobs/{job_id}"})


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = load_job(safe_name(job_id))
    if not job:
        raise HTTPException(404, "Jobi ei leitud.")
    return JSONResponse(public_job(job))


@app.get("/results/{job_id}", response_class=HTMLResponse)
def results(request: Request, job_id: str):
    job = load_job(safe_name(job_id))
    if not job:
        raise HTTPException(404, "Jobi ei leitud.")
    if job.get("status") == "error":
        return templates.TemplateResponse(
            "error.html", {"request": request, "error": job.get("error", "Unknown error")}, status_code=500
        )
    if job.get("status") != "done":
        return templates.TemplateResponse(
            "error.html", {"request": request, "error": "See töö ei ole veel valmis."}, status_code=409
        )
    clips = [c for c in job.get("clips", []) if not c.get("deleted")]
    return templates.TemplateResponse(
        "result.html", {"request": request, "job": job_id, "clips": clips}
    )


@app.delete("/api/jobs/{job_id}/clips/{clip_index}")
def delete_clip(job_id: str, clip_index: int):
    job = load_job(safe_name(job_id))
    if not job:
        raise HTTPException(404, "Jobi ei leitud.")
    clips = job.get("clips", [])
    match = next((c for c in clips if int(c.get("index", -1)) == clip_index), None)
    if not match:
        raise HTTPException(404, "Klippi ei leitud.")
    path = OUTPUTS / safe_name(job_id) / safe_name(match["name"])
    if path.exists():
        path.unlink()
    match["deleted"] = True
    update_job(job_id, clips=clips)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/clips/{clip_index}/regenerate")
def regenerate_clip(job_id: str, clip_index: int):
    job = load_job(safe_name(job_id))
    if not job:
        raise HTTPException(404, "Jobi ei leitud.")
    source_path = Path(job.get("source_path", ""))
    if not source_path.exists():
        raise HTTPException(410, "Algne video ei ole enam serveris saadaval.")

    segments = read_segments(job_id)
    if not segments:
        raise HTTPException(410, "Transkripti ei leitud.")

    settings = job.get("settings", {})
    target_len = int(settings.get("clip_length", 35))
    clips = job.get("clips", [])
    current = next((c for c in clips if int(c.get("index", -1)) == clip_index), None)
    if not current:
        raise HTTPException(404, "Klippi ei leitud.")

    other_starts = [float(c["start"]) for c in clips if int(c.get("index", -1)) != clip_index and not c.get("deleted")]
    candidates = rank_candidates(segments, target_len)
    replacement = None
    for c in candidates:
        if abs(c["start"] - float(current["start"])) < target_len * 0.55:
            continue
        if all(abs(c["start"] - s) > target_len * 0.65 for s in other_starts):
            replacement = c
            break
    if not replacement:
        raise HTTPException(409, "Teist piisavalt erinevat highlight'i ei leitud.")

    job_dir = OUTPUTS / job_id
    ass = job_dir / f"clip_{clip_index}.ass"
    out = job_dir / f"clip_{clip_index}.mp4"
    make_ass(segments, replacement["start"], replacement["end"], ass)
    render_clip(source_path, replacement["start"], replacement["end"], ass, out)

    current.update({
        "name": out.name,
        "url": f"/download/{job_id}/{out.name}?v={int(time.time())}",
        "start": round(replacement["start"], 1),
        "end": round(replacement["end"], 1),
        "score": round(replacement["score"], 1),
        "title": make_title(replacement["text"], clip_index),
        "text": replacement["text"][:320],
        "deleted": False,
    })
    update_job(job_id, clips=clips)
    return current


@app.get("/download/{job}/{filename}")
def download(job: str, filename: str):
    path = OUTPUTS / safe_name(job) / safe_name(filename)
    if not path.exists():
        raise HTTPException(404, "Faili ei leitud.")
    return FileResponse(path, filename=path.name, media_type="video/mp4")
