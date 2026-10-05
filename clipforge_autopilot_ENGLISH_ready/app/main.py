
from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

load_dotenv()

BASE = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("DATA_DIR", str(BASE / "data"))).resolve()
VIDEOS = DATA / "videos"
DB_PATH = DATA / "clipforge.db"
TOKEN_PATH = DATA / "youtube_token.json"

DATA.mkdir(parents=True, exist_ok=True)
VIDEOS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ClipForge Autopilot")
app.mount("/static", StaticFiles(directory=BASE / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "app" / "templates")

scheduler = BackgroundScheduler(timezone="Europe/Tallinn")
JOBS: dict[str, dict] = {}

DEFAULT_SETTINGS = {
    "autopilot": "0",
    "videos_per_day": "5",
    "times": "10:00,13:00,16:00,19:00,22:00",
    "categories": json.dumps(
        {"Football": 2, "News": 1, "Funny": 1, "Facts": 1}
    ),
    "privacy": "private",
}

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                script TEXT NOT NULL,
                status TEXT NOT NULL,
                file_path TEXT,
                youtube_id TEXT,
                error TEXT
            )
            '''
        )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                (key, value),
            )

def get_setting(key: str) -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else DEFAULT_SETTINGS.get(key, "")

def set_setting(key: str, value: str):
    with db() as conn:
        conn.execute(
            '''
            INSERT INTO settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            ''',
            (key, value),
        )

def save_video(rec: dict):
    with db() as conn:
        conn.execute(
            '''
            INSERT OR REPLACE INTO videos
            (id,created_at,category,title,script,status,file_path,youtube_id,error)
            VALUES(?,?,?,?,?,?,?,?,?)
            ''',
            (
                rec["id"],
                rec["created_at"],
                rec["category"],
                rec["title"],
                rec["script"],
                rec["status"],
                rec.get("file_path"),
                rec.get("youtube_id"),
                rec.get("error"),
            ),
        )

def update_video(video_id: str, **kwargs):
    if not kwargs:
        return
    columns = ",".join(f"{key}=?" for key in kwargs)
    values = list(kwargs.values()) + [video_id]
    with db() as conn:
        conn.execute(f"UPDATE videos SET {columns} WHERE id=?", values)

def list_videos(limit: int = 30):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM videos ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

def pick_category() -> str:
    categories = json.loads(get_setting("categories"))
    pool = []
    for name, amount in categories.items():
        pool.extend([name] * max(0, int(amount)))
    return random.choice(pool or ["Facts"])

def get_fresh_context(category: str) -> str:
    api_key = os.getenv("NEWSAPI_KEY", "").strip()
    if not api_key or category not in {"News", "Football"}:
        return ""

    query = "football OR soccer" if category == "Football" else "top news"

    try:
        response = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query,
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 6,
                "apiKey": api_key,
            },
            timeout=15,
        )
        articles = response.json().get("articles", [])
        return "\n".join(
            f"- {article.get('title', '')} — {article.get('description') or ''}"
            for article in articles[:6]
        )
    except Exception:
        return ""

def fallback_script(category: str) -> dict:
    samples = {
        "Football": {
            "title": "Why the final 10 minutes can change everything",
            "script": (
                "The final minutes of a football match can become complete chaos. "
                "Tired players make slower decisions, teams take bigger risks, and one set piece can flip the entire result. "
                "That is why so many late goals happen when the match already feels decided."
            ),
        },
        "News": {
            "title": "Three things to check before you trust a headline",
            "script": (
                "Before you trust a big headline, check the date first. "
                "Then look for the original source and see whether other reliable outlets are reporting the same thing. "
                "Those three quick checks can save you from sharing a story that sounds convincing but is completely wrong."
            ),
        },
        "Funny": {
            "title": "Every friend group has this one person",
            "script": (
                "Every friend group has that one person who says they will be there in five minutes. "
                "Thirty minutes later, you get a message saying, I am leaving now. "
                "The funniest part is nobody is even surprised. Everyone knew exactly how this was going to end."
            ),
        },
        "Facts": {
            "title": "Your brain is hiding this from you all day",
            "script": (
                "Your brain constantly fills in information your eyes never actually see. "
                "Each eye has a blind spot, but you usually never notice it because your brain automatically guesses what should be there. "
                "So what you see is not quite as direct as it feels."
            ),
        },
    }
    item = samples.get(category, samples["Facts"])
    return {
        "title": item["title"],
        "script": item["script"],
        "description": item["title"] + " #shorts",
    }

def generate_script(category: str) -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return fallback_script(category)

    context = get_fresh_context(category)
    client = OpenAI(api_key=api_key)
    model = os.getenv("OPENAI_MODEL", "gpt-6-luna")

    if category == "Funny":
        rule = (
            "Create an original funny short-form scenario or observation. "
            "Do not copy existing creators or viral clips."
        )
    elif category in {"News", "Football"} and context:
        rule = (
            "Use only the fresh context below for factual claims. "
            "Do not invent facts. If the context is insufficient, create evergreen educational content instead.\n\n"
            "CONTEXT:\n" + context
        )
    else:
        rule = (
            "Create an evergreen, self-contained video. "
            "Do not invent specific current events."
        )

    prompt = f'''
You create an English YouTube Short in the category "{category}".

{rule}

Requirements:
- 30 to 45 seconds of spoken content.
- The first sentence must be a strong hook.
- Do not rely on copyrighted clips.
- The script must work with abstract visuals and large on-screen captions.
- News content must stay neutral and factual.
- Use clear, natural English.
- Return ONLY JSON in exactly this shape:
{{"title":"...","script":"...","description":"... #shorts"}}
'''

    response = client.responses.create(model=model, input=prompt)
    output = response.output_text.strip()
    output = output.removeprefix("```json").removesuffix("```").strip()

    try:
        return json.loads(output)
    except Exception:
        return fallback_script(category)

def get_font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]

    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)

    return ImageFont.load_default()

def wrap_text(draw, text, font_obj, max_width):
    words = text.split()
    lines = []
    line = ""

    for word in words:
        test = (line + " " + word).strip()
        width = draw.textbbox((0, 0), test, font=font_obj)[2]
        if width <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = word

    if line:
        lines.append(line)

    return lines

def make_card(text: str, category: str, path: Path, index: int):
    image = Image.new("RGB", (1080, 1920), (18, 18, 20))
    draw = ImageDraw.Draw(image)

    random.seed(index * 19 + len(text))

    for _ in range(9):
        x = random.randint(-150, 950)
        y = random.randint(-100, 1820)
        radius = random.randint(130, 420)
        color = random.choice(
            [(45, 52, 66), (58, 42, 48), (35, 56, 50), (54, 47, 35)]
        )
        draw.ellipse((x, y, x + radius, y + radius), fill=color)

    label_font = get_font(38, True)
    body_font = get_font(72, True)

    draw.rounded_rectangle((70, 100, 450, 178), radius=38, fill=(245, 245, 240))
    draw.text((100, 122), category.upper(), font=label_font, fill=(20, 20, 22))

    lines = wrap_text(draw, text, body_font, 900)
    y = max(620, 960 - len(lines) * 46)

    for line in lines[:8]:
        box = draw.textbbox((0, 0), line, font=body_font)
        x = (1080 - (box[2] - box[0])) / 2
        draw.text((x + 3, y + 3), line, font=body_font, fill=(0, 0, 0))
        draw.text((x, y), line, font=body_font, fill=(255, 255, 255))
        y += 92

    draw.text(
        (70, 1800),
        "CLIPFORGE AUTOPILOT",
        font=get_font(30, True),
        fill=(190, 190, 190),
    )

    image.save(path, quality=92)

def split_sentences(script: str):
    import re
    parts = re.split(r"(?<=[.!?])\s+", script.strip())
    return [part.strip() for part in parts if part.strip()]

def make_voice(text: str, path: Path):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is required for English AI voice generation."
        )

    client = OpenAI(api_key=api_key)

    with client.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice="marin",
        input=text,
        instructions=(
            "Speak in natural, energetic English suitable for a YouTube Short. "
            "Keep the pacing punchy, clear and conversational."
        ),
    ) as response:
        response.stream_to_file(path)

def probe_duration(path: Path) -> float:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return max(1.0, float(process.stdout.strip()))

def render_video(video_id: str, category: str, script: str) -> Path:
    work = VIDEOS / video_id
    work.mkdir(parents=True, exist_ok=True)

    voice_path = work / "voice.mp3"
    make_voice(script, voice_path)
    duration = probe_duration(voice_path)

    sentences = split_sentences(script) or [script]
    seconds_per_card = duration / len(sentences)

    concat_lines = []

    for index, sentence in enumerate(sentences):
        image_path = work / f"card_{index:02}.jpg"
        make_card(sentence, category, image_path, index)

        concat_lines.append(f"file '{image_path.as_posix()}'")
        concat_lines.append(f"duration {seconds_per_card:.3f}")

    concat_lines.append(
        f"file '{(work / f'card_{len(sentences) - 1:02}.jpg').as_posix()}'"
    )

    concat_file = work / "images.txt"
    concat_file.write_text("\n".join(concat_lines), encoding="utf-8")

    output = VIDEOS / f"{video_id}.mp4"

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-i",
            str(voice_path),
            "-vf",
            "scale=1080:1920,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
        capture_output=True,
    )

    return output

def youtube_credentials() -> Optional[Credentials]:
    if not TOKEN_PATH.exists():
        return None

    data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    credentials = Credentials.from_authorized_user_info(
        data,
        ["https://www.googleapis.com/auth/youtube.upload"],
    )

    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleRequest())
        TOKEN_PATH.write_text(credentials.to_json(), encoding="utf-8")

    return credentials if credentials.valid else None

def youtube_connected() -> bool:
    try:
        return youtube_credentials() is not None
    except Exception:
        return False

def upload_youtube(path: Path, title: str, description: str) -> str:
    credentials = youtube_credentials()

    if not credentials:
        raise RuntimeError("YouTube is not connected.")

    youtube = build("youtube", "v3", credentials=credentials)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": get_setting("privacy"),
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(path),
        mimetype="video/mp4",
        resumable=True,
    )

    response = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    ).execute()

    return response["id"]

def create_one(category: Optional[str] = None, auto_post: bool = True):
    video_id = uuid.uuid4().hex[:10]
    category = category or pick_category()

    save_video(
        {
            "id": video_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "category": category,
            "title": "Generating...",
            "script": "",
            "status": "Creating idea",
        }
    )

    JOBS[video_id] = {"progress": 5, "stage": "Creating idea"}

    try:
        content = generate_script(category)

        update_video(
            video_id,
            title=content["title"],
            script=content["script"],
            status="Generating voice",
        )
        JOBS[video_id] = {"progress": 30, "stage": "Generating voice"}

        output = render_video(
            video_id,
            category,
            content["script"],
        )

        update_video(
            video_id,
            file_path=str(output),
            status="Video ready",
        )
        JOBS[video_id] = {"progress": 80, "stage": "Video ready"}

        if auto_post and youtube_connected():
            update_video(video_id, status="Uploading to YouTube")
            JOBS[video_id] = {
                "progress": 90,
                "stage": "Uploading to YouTube",
            }

            youtube_id = upload_youtube(
                output,
                content["title"],
                content.get(
                    "description",
                    content["title"] + " #shorts",
                ),
            )

            update_video(
                video_id,
                youtube_id=youtube_id,
                status="Posted",
            )
            JOBS[video_id] = {
                "progress": 100,
                "stage": "Posted",
            }

        else:
            update_video(video_id, status="Ready")
            JOBS[video_id] = {
                "progress": 100,
                "stage": "Ready",
            }

    except Exception as error:
        update_video(
            video_id,
            status="Error",
            error=str(error),
        )
        JOBS[video_id] = {
            "progress": 100,
            "stage": "Error",
            "error": str(error),
        }

    return video_id

def scheduled_generate():
    if get_setting("autopilot") == "1":
        create_one(auto_post=True)

def rebuild_schedule():
    for job in scheduler.get_jobs():
        if job.id.startswith("slot_"):
            scheduler.remove_job(job.id)

    times = [
        value.strip()
        for value in get_setting("times").split(",")
        if value.strip()
    ]

    max_videos = int(get_setting("videos_per_day"))

    for index, time_value in enumerate(times[:max_videos]):
        try:
            hour, minute = map(int, time_value.split(":"))

            scheduler.add_job(
                scheduled_generate,
                CronTrigger(
                    hour=hour,
                    minute=minute,
                    timezone="Europe/Tallinn",
                ),
                id=f"slot_{index}",
                replace_existing=True,
            )
        except Exception:
            pass

@app.on_event("startup")
def startup():
    init_db()

    if not scheduler.running:
        scheduler.start()

    rebuild_schedule()

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "videos": list_videos(),
            "categories": json.loads(get_setting("categories")),
            "times": get_setting("times").split(","),
            "autopilot": get_setting("autopilot") == "1",
            "youtube": youtube_connected(),
            "ai": bool(os.getenv("OPENAI_API_KEY")),
            "privacy": get_setting("privacy"),
        },
    )

@app.post("/settings")
def settings(
    autopilot: Optional[str] = Form(None),
    football: int = Form(2),
    news: int = Form(1),
    funny: int = Form(1),
    facts: int = Form(1),
    time1: str = Form("10:00"),
    time2: str = Form("13:00"),
    time3: str = Form("16:00"),
    time4: str = Form("19:00"),
    time5: str = Form("22:00"),
    privacy: str = Form("private"),
):
    set_setting(
        "categories",
        json.dumps(
            {
                "Football": football,
                "News": news,
                "Funny": funny,
                "Facts": facts,
            }
        ),
    )

    set_setting(
        "times",
        ",".join([time1, time2, time3, time4, time5]),
    )

    set_setting("videos_per_day", "5")
    set_setting(
        "autopilot",
        "1" if autopilot else "0",
    )

    set_setting(
        "privacy",
        privacy
        if privacy in {"private", "unlisted", "public"}
        else "private",
    )

    rebuild_schedule()

    return RedirectResponse("/", status_code=303)

@app.post("/generate")
def generate(category: str = Form("Facts")):
    import threading

    threading.Thread(
        target=create_one,
        args=(category, False),
        daemon=True,
    ).start()

    return RedirectResponse("/", status_code=303)

@app.post("/post/{video_id}")
def post_video(video_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE id=?",
            (video_id,),
        ).fetchone()

    if not row or not row["file_path"]:
        raise HTTPException(404, "Video not found.")

    youtube_id = upload_youtube(
        Path(row["file_path"]),
        row["title"],
        row["title"] + " #shorts",
    )

    update_video(
        video_id,
        youtube_id=youtube_id,
        status="Posted",
    )

    return RedirectResponse("/", status_code=303)

@app.get("/video/{video_id}")
def video_file(video_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT file_path FROM videos WHERE id=?",
            (video_id,),
        ).fetchone()

    if (
        not row
        or not row["file_path"]
        or not Path(row["file_path"]).exists()
    ):
        raise HTTPException(404)

    return FileResponse(
        row["file_path"],
        media_type="video/mp4",
        filename=f"{video_id}.mp4",
    )

@app.get("/api/status")
def status():
    return JSONResponse(
        {
            "jobs": JOBS,
            "videos": [
                dict(video)
                for video in list_videos(10)
            ],
        }
    )

def oauth_config():
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    redirect_uri = os.getenv("YOUTUBE_REDIRECT_URI")

    if not all(
        [client_id, client_secret, redirect_uri]
    ):
        raise HTTPException(
            400,
            "Google OAuth variables are not configured.",
        )

    config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }

    return config, redirect_uri

@app.get("/auth/youtube")
def auth_youtube():
    config, redirect_uri = oauth_config()

    flow = Flow.from_client_config(
        config,
        scopes=[
            "https://www.googleapis.com/auth/youtube.upload"
        ],
        redirect_uri=redirect_uri,
    )

    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    set_setting("oauth_state", state)

    return RedirectResponse(url)

@app.get("/auth/youtube/callback")
def auth_youtube_callback(request: Request):
    config, redirect_uri = oauth_config()

    flow = Flow.from_client_config(
        config,
        scopes=[
            "https://www.googleapis.com/auth/youtube.upload"
        ],
        state=get_setting("oauth_state"),
        redirect_uri=redirect_uri,
    )

    flow.fetch_token(
        authorization_response=str(request.url)
    )

    TOKEN_PATH.write_text(
        flow.credentials.to_json(),
        encoding="utf-8",
    )

    return RedirectResponse("/", status_code=303)
