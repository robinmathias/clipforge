from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from urllib.parse import urlparse
import json
import re
import shutil
import subprocess
import uuid

import yt_dlp

BASE = Path(__file__).resolve().parent.parent
UPLOADS = BASE / "data" / "uploads"
OUTPUTS = BASE / "data" / "outputs"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ClipForge MVP")
app.mount("/static", StaticFiles(directory=BASE / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "app" / "templates")


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-5000:])
    return p.stdout


def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "video.mp4")
    return name[:120]


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


def download_video(url: str, job: str) -> Path:
    output_template = str(UPLOADS / f"{job}_downloaded.%(ext)s")
    options = {
        "format": "bv*+ba/b",
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        prepared = Path(ydl.prepare_filename(info))

    mp4_path = UPLOADS / f"{job}_downloaded.mp4"
    if mp4_path.exists():
        return mp4_path
    if prepared.exists():
        return prepared

    # yt-dlp may choose another merged extension; locate it by job prefix.
    matches = sorted(UPLOADS.glob(f"{job}_downloaded.*"))
    if matches:
        return matches[0]
    raise RuntimeError("Videot ei õnnestunud lingilt alla laadida.")


def transcribe(video_path: Path, model_size: str = "small"):
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(video_path), vad_filter=True)
    rows = []
    for s in segments:
        text = (s.text or "").strip()
        if text:
            rows.append({"start": float(s.start), "end": float(s.end), "text": text})
    return rows


def highlight_score(text: str) -> float:
    t = text.lower()
    score = 0.0
    keywords = [
        "wow", "insane", "crazy", "no way", "what", "omg", "damn", "bro", "dude",
        "haha", "lol", "funny", "võimatu", "appi", "issand", "mida", "päriselt",
        "ha ha", "naer", "uskumatu", "hull", "oi", "jessas",
    ]
    score += sum(2.5 for k in keywords if k in t)
    score += min(len(text) / 120, 2.0)
    score += text.count("!") * 1.8 + text.count("?") * 0.8
    if any(c.isupper() for c in text) and len(text) > 15:
        score += 0.5
    return score


def choose_clips(segments, count=3, target_len=35):
    if not segments:
        return []
    candidates = []
    video_end = segments[-1]["end"]
    for seg in segments:
        start = max(0.0, seg["start"] - 6)
        end = min(video_end, start + target_len)
        text_parts = [
            s["text"] for s in segments
            if s["end"] >= start and s["start"] <= end
        ]
        full = " ".join(text_parts)
        candidates.append({
            "start": start,
            "end": end,
            "score": highlight_score(full),
            "text": full,
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    picked = []
    for c in candidates:
        if all(abs(c["start"] - p["start"]) > target_len * 0.7 for p in picked):
            picked.append(c)
        if len(picked) >= count:
            break
    return sorted(picked or candidates[:count], key=lambda x: x["start"])


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
        lines.append(
            f"Dialogue: 0,{fmt_ass_time(st)},{fmt_ass_time(en)},Default,,0,0,0,,{text}\n"
        )
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
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(video),
        "-t", f"{dur:.3f}",
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "21",
        "-c:a", "aac",
        "-b:a", "160k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    run(cmd)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/process", response_class=HTMLResponse)
def process_video(
    request: Request,
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

    job = uuid.uuid4().hex[:10]
    job_dir = OUTPUTS / job
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        clean_url = validate_video_url(video_url)
        if clean_url:
            in_path = download_video(clean_url, job)
        elif video and video.filename:
            in_path = UPLOADS / f"{job}_{safe_name(video.filename)}"
            with in_path.open("wb") as f:
                shutil.copyfileobj(video.file, f)
        else:
            raise RuntimeError("Lisa video fail või YouTube/Twitchi link.")

        segments = transcribe(in_path, whisper_model)
        if not segments:
            raise RuntimeError("Videost ei leitud kõnet, mida transkribeerida.")

        (job_dir / "transcript.json").write_text(
            json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        picks = choose_clips(segments, clip_count, clip_length)
        clips = []
        for idx, pick in enumerate(picks, 1):
            ass = job_dir / f"clip_{idx}.ass"
            out = job_dir / f"clip_{idx}.mp4"
            make_ass(segments, pick["start"], pick["end"], ass)
            render_clip(in_path, pick["start"], pick["end"], ass, out)
            clips.append({
                "name": out.name,
                "url": f"/download/{job}/{out.name}",
                "start": round(pick["start"], 1),
                "end": round(pick["end"], 1),
                "text": pick["text"][:220],
            })

        return templates.TemplateResponse(
            "result.html", {"request": request, "job": job, "clips": clips}
        )
    except Exception as e:
        return templates.TemplateResponse(
            "error.html", {"request": request, "error": str(e)}, status_code=500
        )


@app.get("/download/{job}/{filename}")
def download(job: str, filename: str):
    path = OUTPUTS / safe_name(job) / safe_name(filename)
    if not path.exists():
        raise HTTPException(404, "Faili ei leitud.")
    return FileResponse(path, filename=path.name, media_type="video/mp4")
