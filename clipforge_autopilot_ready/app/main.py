from __future__ import annotations
import asyncio, json, os, random, sqlite3, subprocess, uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
import edge_tts, requests
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
JOBS = {}

DEFAULT_SETTINGS = {
    "autopilot": "0",
    "videos_per_day": "5",
    "times": "10:00,13:00,16:00,19:00,22:00",
    "categories": json.dumps({"Jalgpall":2,"Uudised":1,"Naljakas":1,"Faktid":1}, ensure_ascii=False),
    "voice": "et-EE-KertNeural",
    "privacy": "private"
}

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        c.execute('''CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY,created_at TEXT NOT NULL,category TEXT NOT NULL,
            title TEXT NOT NULL,script TEXT NOT NULL,status TEXT NOT NULL,
            file_path TEXT,youtube_id TEXT,error TEXT)''')
        for k,v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))

def get_setting(k):
    with db() as c:
        r=c.execute("SELECT value FROM settings WHERE key=?",(k,)).fetchone()
        return r["value"] if r else DEFAULT_SETTINGS.get(k,"")

def set_setting(k,v):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,v))

def save_video(r):
    with db() as c:
        c.execute('''INSERT OR REPLACE INTO videos
        (id,created_at,category,title,script,status,file_path,youtube_id,error)
        VALUES(?,?,?,?,?,?,?,?,?)''',(r["id"],r["created_at"],r["category"],r["title"],r["script"],r["status"],r.get("file_path"),r.get("youtube_id"),r.get("error")))

def update_video(vid,**kw):
    if not kw:return
    with db() as c:
        cols=",".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE videos SET {cols} WHERE id=?",list(kw.values())+[vid])

def list_videos(limit=30):
    with db() as c:
        return c.execute("SELECT * FROM videos ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()

def pick_category():
    cats=json.loads(get_setting("categories"))
    pool=[]
    for k,v in cats.items(): pool += [k]*max(0,int(v))
    return random.choice(pool or ["Faktid"])

def fresh_context(category):
    key=os.getenv("NEWSAPI_KEY","").strip()
    if not key or category not in {"Uudised","Jalgpall"}: return ""
    q="football OR soccer" if category=="Jalgpall" else "top news"
    try:
        r=requests.get("https://newsapi.org/v2/everything",params={"q":q,"language":"en","sortBy":"publishedAt","pageSize":6,"apiKey":key},timeout=15)
        arts=r.json().get("articles",[])
        return "\\n".join(f"- {a.get('title','')} — {a.get('description','') or ''}" for a in arts[:6])
    except Exception:
        return ""

def fallback_script(category):
    data={
      "Jalgpall":("Miks mängu lõpp on tihti kõige ohtlikum","Jalgpallimängu lõpp võib olla täielik kaos. Väsimus teeb otsused aeglasemaks, meeskonnad riskivad rohkem ja üks standardolukord võib kogu mängu pöörata. Seepärast näeme nii palju hiliseid väravaid just siis, kui tundub, et kõik on juba otsustatud."),
      "Uudised":("Kolm asja, mida uudist lugedes kontrollida","Kui näed suurt pealkirja, vaata kõigepealt kuupäeva. Seejärel kontrolli algallikat ja vaata, kas sama infot kinnitavad ka teised usaldusväärsed väljaanded. Kolm lihtsat kontrolli võivad päästa sind väga veenvast, aga valest loost."),
      "Naljakas":("Kõige universaalsem sõprade grupi hetk","Igas sõprade grupis on see üks inimene, kes ütleb, et tuleb viie minuti pärast. Pool tundi hiljem saad sõnumi: ma kohe liigun. Kõige naljakam osa on see, et keegi ei ole isegi üllatunud. Kõik teadsid juba algusest peale, kuidas see lõppeb."),
      "Faktid":("Üks veider asi, mida aju sinu eest teeb","Sinu aju täidab pidevalt infot, mida silmad tegelikult ei näe. Igal silmal on pimeala, kuid tavaliselt sa seda ei märka, sest aju arvab puuduva osa ise juurde. Ehk sa ei näe maailma päris nii otse, nagu tundub.")
    }
    title,script=data.get(category,data["Faktid"])
    return {"title":title,"script":script,"description":title+" #shorts"}

def generate_script(category):
    key=os.getenv("OPENAI_API_KEY","").strip()
    if not key:return fallback_script(category)
    ctx=fresh_context(category)
    client=OpenAI(api_key=key)
    model=os.getenv("OPENAI_MODEL","gpt-6-luna")
    if category=="Naljakas":
        rule="Loo originaalne humoorikas lühistsenaarium või tähelepanek. Ära kopeeri teiste loojate videoid."
    elif category in {"Uudised","Jalgpall"} and ctx:
        rule="Kasuta ainult allolevat värsket konteksti. Ära mõtle fakte juurde. Kui kontekst ei piisa, tee ajatu õpetlik video.\\nKONTEKST:\\n"+ctx
    else:
        rule="Tee ajatu ja kontrollitav video. Ära mõtle konkreetseid värskeid sündmusi välja."
    prompt=f'''Sa lood eestikeelset YouTube Shortsi kategoorias "{category}".
{rule}
Nõuded:
- 30–45 sekundi kõnetekst.
- esimene lause on tugev konks;
- ära vaja autoriõigusega kaitstud klippe;
- tekst peab töötama abstraktse tausta ja suurte captionitega;
- uudistes neutraalne toon;
- tagasta AINULT JSON:
{{"title":"...","script":"...","description":"... #shorts"}}'''
    resp=client.responses.create(model=model,input=prompt)
    txt=resp.output_text.strip().removeprefix("```json").removesuffix("```").strip()
    try:return json.loads(txt)
    except:return fallback_script(category)

def font(size,bold=False):
    cand=[
      "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
      "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"
    ]
    for f in cand:
        if Path(f).exists(): return ImageFont.truetype(f,size)
    return ImageFont.load_default()

def wrap_text(draw,text,f,maxw):
    lines=[]; line=""
    for w in text.split():
        t=(line+" "+w).strip()
        if draw.textbbox((0,0),t,font=f)[2] <= maxw: line=t
        else:
            if line:lines.append(line)
            line=w
    if line:lines.append(line)
    return lines

def make_card(text,category,path,idx):
    img=Image.new("RGB",(1080,1920),(18,18,20)); d=ImageDraw.Draw(img)
    random.seed(idx*19+len(text))
    for _ in range(9):
        x=random.randint(-150,950); y=random.randint(-100,1820); r=random.randint(130,420)
        col=random.choice([(45,52,66),(58,42,48),(35,56,50),(54,47,35)])
        d.ellipse((x,y,x+r,y+r),fill=col)
    lf=font(38,True); bf=font(72,True)
    d.rounded_rectangle((70,100,440,178),radius=38,fill=(245,245,240))
    d.text((100,122),category.upper(),font=lf,fill=(20,20,22))
    lines=wrap_text(d,text,bf,900); y=max(620,960-len(lines)*46)
    for line in lines[:8]:
        bb=d.textbbox((0,0),line,font=bf); x=(1080-(bb[2]-bb[0]))/2
        d.text((x+3,y+3),line,font=bf,fill=(0,0,0)); d.text((x,y),line,font=bf,fill=(255,255,255)); y+=92
    d.text((70,1800),"CLIPFORGE AUTOPILOT",font=font(30,True),fill=(190,190,190))
    img.save(path,quality=92)

def split_sentences(s):
    import re
    return [x.strip() for x in re.split(r'(?<=[.!?])\\s+',s.strip()) if x.strip()]

async def make_voice(text,path):
    c=edge_tts.Communicate(text,voice=get_setting("voice"),rate="+5%")
    await c.save(str(path))

def duration(path):
    p=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=nw=1:nk=1",str(path)],capture_output=True,text=True,check=True)
    return max(1.0,float(p.stdout.strip()))

def render_video(vid,category,script):
    work=VIDEOS/vid; work.mkdir(parents=True,exist_ok=True)
    voice=work/"voice.mp3"; asyncio.run(make_voice(script,voice)); dur=duration(voice)
    sents=split_sentences(script) or [script]; each=dur/len(sents); lines=[]
    for i,s in enumerate(sents):
        img=work/f"card_{i:02}.jpg"; make_card(s,category,img,i)
        lines += [f"file '{img.as_posix()}'",f"duration {each:.3f}"]
    lines.append(f"file '{(work/f'card_{len(sents)-1:02}.jpg').as_posix()}'")
    concat=work/"images.txt"; concat.write_text("\\n".join(lines),encoding="utf-8")
    out=VIDEOS/f"{vid}.mp4"
    subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),"-i",str(voice),"-vf","scale=1080:1920,format=yuv420p","-c:v","libx264","-preset","veryfast","-r","30","-c:a","aac","-b:a","160k","-shortest","-movflags","+faststart",str(out)],check=True,capture_output=True)
    return out

def youtube_credentials():
    if not TOKEN_PATH.exists(): return None
    data=json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    creds=Credentials.from_authorized_user_info(data,["https://www.googleapis.com/auth/youtube.upload"])
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest()); TOKEN_PATH.write_text(creds.to_json(),encoding="utf-8")
    return creds if creds.valid else None

def youtube_connected():
    try:return youtube_credentials() is not None
    except:return False

def upload_youtube(path,title,description):
    creds=youtube_credentials()
    if not creds: raise RuntimeError("YouTube ei ole ühendatud.")
    yt=build("youtube","v3",credentials=creds)
    body={"snippet":{"title":title[:100],"description":description[:5000],"categoryId":"22"},
          "status":{"privacyStatus":get_setting("privacy"),"selfDeclaredMadeForKids":False}}
    media=MediaFileUpload(str(path),mimetype="video/mp4",resumable=True)
    return yt.videos().insert(part="snippet,status",body=body,media_body=media).execute()["id"]

def create_one(category=None,auto_post=True):
    vid=uuid.uuid4().hex[:10]; category=category or pick_category()
    save_video({"id":vid,"created_at":datetime.now().isoformat(timespec="seconds"),"category":category,"title":"Genereerin...","script":"","status":"Idee loomine"})
    JOBS[vid]={"progress":5,"stage":"Idee loomine"}
    try:
        content=generate_script(category)
        update_video(vid,title=content["title"],script=content["script"],status="Hääle loomine")
        JOBS[vid]={"progress":30,"stage":"Hääle loomine"}
        out=render_video(vid,category,content["script"])
        update_video(vid,file_path=str(out),status="Video valmis")
        JOBS[vid]={"progress":80,"stage":"Video valmis"}
        if auto_post and youtube_connected():
            update_video(vid,status="Postitan YouTube'i"); JOBS[vid]={"progress":90,"stage":"Postitan YouTube'i"}
            yid=upload_youtube(out,content["title"],content.get("description",content["title"]+" #shorts"))
            update_video(vid,youtube_id=yid,status="Postitatud"); JOBS[vid]={"progress":100,"stage":"Postitatud"}
        else:
            update_video(vid,status="Valmis"); JOBS[vid]={"progress":100,"stage":"Valmis"}
    except Exception as e:
        update_video(vid,status="Viga",error=str(e)); JOBS[vid]={"progress":100,"stage":"Viga","error":str(e)}
    return vid

def scheduled_generate():
    if get_setting("autopilot")=="1": create_one(auto_post=True)

def rebuild_schedule():
    for j in scheduler.get_jobs():
        if j.id.startswith("slot_"): scheduler.remove_job(j.id)
    times=[x.strip() for x in get_setting("times").split(",") if x.strip()]
    for i,t in enumerate(times[:int(get_setting("videos_per_day"))]):
        try:
            h,m=map(int,t.split(":"))
            scheduler.add_job(scheduled_generate,CronTrigger(hour=h,minute=m,timezone="Europe/Tallinn"),id=f"slot_{i}",replace_existing=True)
        except: pass

@app.on_event("startup")
def startup():
    init_db()
    if not scheduler.running:scheduler.start()
    rebuild_schedule()

@app.get("/",response_class=HTMLResponse)
def dashboard(request:Request):
    return templates.TemplateResponse("index.html",{"request":request,"videos":list_videos(),"categories":json.loads(get_setting("categories")),"times":get_setting("times").split(","),"autopilot":get_setting("autopilot")=="1","youtube":youtube_connected(),"ai":bool(os.getenv("OPENAI_API_KEY")),"privacy":get_setting("privacy")})

@app.post("/settings")
def settings(autopilot:Optional[str]=Form(None),football:int=Form(2),news:int=Form(1),funny:int=Form(1),facts:int=Form(1),time1:str=Form("10:00"),time2:str=Form("13:00"),time3:str=Form("16:00"),time4:str=Form("19:00"),time5:str=Form("22:00"),privacy:str=Form("private")):
    set_setting("categories",json.dumps({"Jalgpall":football,"Uudised":news,"Naljakas":funny,"Faktid":facts},ensure_ascii=False))
    set_setting("times",",".join([time1,time2,time3,time4,time5])); set_setting("videos_per_day","5")
    set_setting("autopilot","1" if autopilot else "0"); set_setting("privacy",privacy if privacy in {"private","unlisted","public"} else "private")
    rebuild_schedule(); return RedirectResponse("/",303)

@app.post("/generate")
def generate(category:str=Form("Faktid")):
    import threading
    threading.Thread(target=create_one,args=(category,False),daemon=True).start()
    return RedirectResponse("/",303)

@app.post("/post/{vid}")
def post(vid:str):
    with db() as c:r=c.execute("SELECT * FROM videos WHERE id=?",(vid,)).fetchone()
    if not r or not r["file_path"]:raise HTTPException(404)
    yid=upload_youtube(Path(r["file_path"]),r["title"],r["title"]+" #shorts")
    update_video(vid,youtube_id=yid,status="Postitatud"); return RedirectResponse("/",303)

@app.get("/video/{vid}")
def video(vid:str):
    with db() as c:r=c.execute("SELECT file_path FROM videos WHERE id=?",(vid,)).fetchone()
    if not r or not r["file_path"] or not Path(r["file_path"]).exists():raise HTTPException(404)
    return FileResponse(r["file_path"],media_type="video/mp4",filename=f"{vid}.mp4")

@app.get("/api/status")
def api_status():
    return JSONResponse({"jobs":JOBS,"videos":[dict(v) for v in list_videos(10)]})

def oauth_config():
    cid=os.getenv("GOOGLE_CLIENT_ID"); sec=os.getenv("GOOGLE_CLIENT_SECRET"); red=os.getenv("YOUTUBE_REDIRECT_URI")
    if not all([cid,sec,red]):raise HTTPException(400,"Google OAuth muutujad on seadistamata.")
    return {"web":{"client_id":cid,"client_secret":sec,"auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","redirect_uris":[red]}},red

@app.get("/auth/youtube")
def auth_youtube():
    cfg,red=oauth_config(); flow=Flow.from_client_config(cfg,scopes=["https://www.googleapis.com/auth/youtube.upload"],redirect_uri=red)
    url,state=flow.authorization_url(access_type="offline",include_granted_scopes="true",prompt="consent")
    set_setting("oauth_state",state); return RedirectResponse(url)

@app.get("/auth/youtube/callback")
def callback(request:Request):
    cfg,red=oauth_config(); flow=Flow.from_client_config(cfg,scopes=["https://www.googleapis.com/auth/youtube.upload"],state=get_setting("oauth_state"),redirect_uri=red)
    flow.fetch_token(authorization_response=str(request.url)); TOKEN_PATH.write_text(flow.credentials.to_json(),encoding="utf-8")
    return RedirectResponse("/",303)
