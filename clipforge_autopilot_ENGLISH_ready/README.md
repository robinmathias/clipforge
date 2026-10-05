# ClipForge Autopilot — English version

This build creates English YouTube Shorts automatically.

## Included

- Football
- News
- Funny
- Facts
- 5 scheduled videos per day
- English AI scripts
- English AI voice
- 1080x1920 Shorts video generation
- on-screen text cards
- YouTube OAuth
- automatic YouTube uploads
- SQLite history
- Railway / Docker ready

## Important change

This version no longer uses edge-tts / Bing Speech.

Voice generation uses OpenAI's speech API with:

- model: `gpt-4o-mini-tts`
- voice: `marin`

Set `OPENAI_API_KEY` in Railway Variables.

## Railway Variables

```env
OPENAI_API_KEY=YOUR_KEY
OPENAI_MODEL=gpt-6-luna
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
YOUTUBE_REDIRECT_URI=https://YOUR-DOMAIN.up.railway.app/auth/youtube/callback
DATA_DIR=/data
NEWSAPI_KEY=
```

`NEWSAPI_KEY` is optional.

Without it:
- Football and News will use evergreen topics
- the app will not invent current breaking news

## Railway Volume

Create a Railway volume mounted at:

```text
/data
```

That keeps:
- SQLite database
- YouTube OAuth token
- generated videos

after redeploys/restarts.

## Local launch

Install Python 3.12 and FFmpeg.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn app.main:app --reload
```

Open:

```text
http://127.0.0.1:8000
```

## AI voice disclosure

Before using this publicly, clearly disclose to viewers that the narration is AI-generated.
