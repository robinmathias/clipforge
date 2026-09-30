# ClipForge

FastAPI MVP that turns long videos into vertical short-form clips with burned-in captions.

## Included

- Upload a local video or paste a supported YouTube/Twitch URL
- Real background processing job with backend progress
- Real progress stages: source/download, transcription, highlight ranking, rendering
- Faster-Whisper speech transcription
- Improved local highlight scoring (hooks, emotional language, speech density, punctuation, completeness and overlap avoidance)
- 9:16 FFmpeg rendering with captions
- Result page with in-browser video previews
- Clip title, source timestamp and highlight score
- Download, regenerate and delete controls for each generated clip
- Dockerfile for Railway-style deployment

## Local Windows start

1. Install Python 3.12 and FFmpeg.
2. Open the project folder in a terminal.
3. Create/activate a virtual environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

4. Start the app:

```powershell
python -m uvicorn app.main:app --reload
```

5. Open http://127.0.0.1:8000

`start_local.cmd` can also be used after dependencies are installed.

## Railway

The included Dockerfile installs FFmpeg and starts Uvicorn on Railway's `$PORT`. Push this folder to GitHub and deploy the repository/directory that contains `Dockerfile` and `requirements.txt`.

## Notes

- The job state is kept in process memory and mirrored to `data/outputs/<job>/status.json`. This is suitable for an MVP running one application instance. A production multi-instance version should use Redis/database-backed jobs and object storage.
- Generated source/output files use local/ephemeral server storage. Production should move them to object storage such as S3/R2.
- Highlight ranking is local and needs no paid API key. A later version can add an optional LLM reranker for more semantic judgement.
- Use only video content you have the right to process.
