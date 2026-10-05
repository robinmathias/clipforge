# ClipForge Autopilot

Automaatse YouTube Shortside tootmise MVP.

## Funktsioonid
- jalgpall, uudised, naljakas, faktid
- 5 ajastatud videot päevas
- AI skript
- eestikeelne sünteeshääl
- 1080x1920 video
- caption-tüüpi tekstikaardid
- YouTube OAuth
- automaatne YouTube üleslaadimine
- SQLite ajalugu
- Docker / Railway valmis

## Käivitamine Windowsis

1. Paigalda Python 3.12 ja FFmpeg.
2. Ava projekti kaustas terminal.
3. Käivita:

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn app.main:app --reload

Seejärel ava:
http://127.0.0.1:8000

## AI seadistamine

`.env` faili:
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-6-luna

Kui võtit pole, töötab rakendus demorežiimis näidisskriptidega.

## Värsked uudised

Valikuline:
NEWSAPI_KEY=...

Ilma võtmeta ei hakka äpp värskeid sündmusi välja mõtlema, vaid teeb uudiste ja jalgpalli puhul ajatut sisu.

## YouTube ühendamine

Google Cloudis:
1. loo projekt
2. lülita sisse YouTube Data API v3
3. loo OAuth 2.0 Web Application
4. lisa redirect URI

Kohalik:
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
YOUTUBE_REDIRECT_URI=http://127.0.0.1:8000/auth/youtube/callback

Railway:
YOUTUBE_REDIRECT_URI=https://SINU-DOMEEN.up.railway.app/auth/youtube/callback

## Railway

Soovituslikud Variables:
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-6-luna
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
YOUTUBE_REDIRECT_URI=https://SINU-DOMEEN.up.railway.app/auth/youtube/callback
DATA_DIR=/data
NEWSAPI_KEY=...

Lisa Railway Volume mount pathiga `/data`.
See hoiab alles andmebaasi, YouTube tokeni ja loodud videod pärast restarti/deployd.

## Autopiloot

Juhtpaneelis:
- lülita Autopiloot sisse
- määra teemade osakaalud
- määra 5 kellaaega
- salvesta

Ajavöönd on Europe/Tallinn.

## Autoriõigused

Rakendus ei kopeeri automaatselt teiste loojate YouTube/TikTok videoid.
Naljakas kategooria loob originaalseid tekste ja video visuaal genereeritakse programmi enda poolt.

## MVP piirangud

- visuaal on hetkel abstraktne animeeritud/vahetuv taust + tekst, mitte päris AI-video mudel
- scheduler on mõeldud ühe serveri-instantsi jaoks
- mitme kasutaja SaaS vajaks eraldi kontosid, turvalist tokenihoidlat ja tööjärjekorda
