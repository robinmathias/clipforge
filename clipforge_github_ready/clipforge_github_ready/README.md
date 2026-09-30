# ClipForge MVP

FastAPI rakendus, mis:
- võtab vastu videofaili või YouTube/Twitchi URL-i;
- transkribeerib video `faster-whisper` abil;
- valib lihtsa heuristika abil potentsiaalsed highlight'id;
- renderdab FFmpegiga 9:16 MP4 klipid;
- põletab captionid videole.

## Kohalik käivitus

Vajalik on Python ja FFmpeg.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Ava `http://127.0.0.1:8000`.

## Railway / muu Docker hosting

Repo sisaldab `Dockerfile` faili. Railway puhul:
1. Lae selle kausta **sisu** GitHubi repo juurkausta.
2. Railway → New Project → Deploy from GitHub Repo.
3. Vali repo. Railway kasutab `Dockerfile` faili automaatselt.
4. Settings → Networking → Generate Domain.

`/health` tagastab `{"status":"ok"}`.

## Tähtis MVP piirang

Praegu töödeldakse video ühe HTTP päringu sees. Väga pikk Twitch VOD võib odavas/free hostingus timeout'i või mälu/CPU piiranguid tabada. Järgmine tootmisversioon peaks kasutama background job queue'd ning object storage'it (nt R2/S3).

`data/uploads` ja `data/outputs` on ajutine lokaalne storage. Pilveserveri restart võib failid kustutada.

URL sisestus on turvalisuse mõttes piiratud YouTube'i ja Twitchi domeenidele. Töötle ainult sisu, mille kasutamiseks sul on õigus.
