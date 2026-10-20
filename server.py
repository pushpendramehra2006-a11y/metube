import os, shutil, tempfile, subprocess, threading, time, uuid, urllib.request
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
import yt_dlp
from yt_dlp.utils import DownloadCancelled

app = FastAPI(title="MeTube")
IMG = {"jpg", "jpeg", "png", "webp", "gif"}
JOBS = {}


class Link(BaseModel):
    url: str


class Start(BaseModel):
    url: str
    kind: str = "mp4"
    height: int = 0


def first_entry(i):
    if i.get("_type") == "playlist" and i.get("entries"):
        return i["entries"][0]
    return i


def get_info(url):
    with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as y:
        return first_entry(y.extract_info(url, download=False))


@app.post("/api/info")
def info(l: Link):
    try:
        i = get_info(l.url)
    except Exception as e:
        raise HTTPException(400, "Link read nahi ho paya: " + str(e)[:200])
    fm = i.get("formats") or []
    dur = i.get("duration") or 0

    def fsize(f):
        s = f.get("filesize") or f.get("filesize_approx") or 0
        if not s and f.get("tbr") and dur:
            s = f["tbr"] * 1000 / 8 * dur
        return s

    audio = max([fsize(f) for f in fm if f.get("vcodec") == "none" and f.get("acodec") != "none"] or [0])
    best = {}
    for f in fm:
        h = f.get("height")
        if h and f.get("vcodec") != "none":
            q = {"h": h, "fps": f.get("fps") or 0, "size": fsize(f), "tbr": f.get("tbr") or 0}
            c = best.get(h)
            if not c or (q["fps"], q["tbr"]) > (c["fps"], c["tbr"]):
                best[h] = q
    for q in best.values():
        q["size"] = q["size"] + audio if q["size"] else 0
    heights = [best[k] for k in sorted(best, reverse=True)]
    is_image = i.get("ext") in IMG or (not fm and bool(i.get("thumbnail")))
    return {"title": i.get("title") or "Untitled", "thumbnail": i.get("thumbnail"),
            "heights": heights, "is_image": is_image, "has_video": bool(heights)}


def cleanup(d):
    shutil.rmtree(d, ignore_errors=True)


def gc():
    now = time.time()
    for k, j in list(JOBS.items()):
        if now - j["t"] > 3600 and j["status"] not in ("downloading", "merging", "converting"):
            cleanup(j["dir"])
            JOBS.pop(k, None)


def new_job():
    gc()
    jid = uuid.uuid4().hex[:12]
    j = {"dir": tempfile.mkdtemp(), "status": "starting", "pct": 0, "cancel": False, "t": time.time(),
         "cur": None, "stage": -1, "speed": 0, "eta": None, "proc": None}
    JOBS[jid] = j
    return jid, j


def fail(j, e):
    if j["cancel"]:
        j["status"] = "cancelled"
    else:
        j.update(status="error", error=str(e)[:200])
    cleanup(j["dir"])


def work(j, url, kind, height):
    d = j["dir"]
    try:
        if kind == "image":
            j["status"] = "downloading"
            i = get_info(url)
            src = i["url"] if i.get("ext") in IMG and i.get("url") else i.get("thumbnail")
            if not src:
                raise ValueError("Image nahi mili")
            path = os.path.join(d, "image.jpg")
            req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req) as r, open(path, "wb") as f:
                shutil.copyfileobj(r, f)
            j.update(path=path, name="image.jpg", status="ready", pct=100)
            return

        def hook(x):
            if j["cancel"]:
                raise DownloadCancelled()
            if x["status"] != "downloading":
                return
            j["status"] = "downloading"
            if x.get("filename") != j["cur"]:
                j["cur"] = x.get("filename")
                j["stage"] += 1
            tot = x.get("total_bytes") or x.get("total_bytes_estimate") or 0
            p = x.get("downloaded_bytes", 0) / tot * 100 if tot else 0
            if kind == "mp4":
                j["pct"] = round(p * 0.92 if j["stage"] == 0 else 92 + p * 0.08, 1)
            else:
                j["pct"] = round(p, 1)
            j["speed"] = x.get("speed") or 0
            j["eta"] = x.get("eta")

        def pp(x):
            if j["cancel"]:
                raise DownloadCancelled()
            if x["status"] == "started":
                j["status"] = "merging"

        opts = {"quiet": True, "noplaylist": True, "outtmpl": d + "/%(title).80s.%(ext)s",
                "progress_hooks": [hook], "postprocessor_hooks": [pp]}
        if kind == "mp3":
            opts.update(format="bestaudio/best", postprocessors=[
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}])
        else:
            h = f"[height<={height}]" if height else ""
            opts.update(format=f"bestvideo{h}+bestaudio[ext=m4a]/bestvideo{h}+bestaudio/best{h}/best",
                        format_sort=["res", "fps", "br"], merge_output_format="mp4")
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([url])
        files = [os.path.join(d, f) for f in os.listdir(d)]
        files = [f for f in files if os.path.isfile(f) and not f.endswith((".part", ".ytdl"))]
        if not files:
            raise ValueError("File nahi bani")
        p = max(files, key=os.path.getmtime)
        j.update(path=p, name=os.path.basename(p), status="ready", pct=100)
    except Exception as e:
        fail(j, e)


@app.post("/api/start")
def start(s: Start):
    jid, j = new_job()
    threading.Thread(target=work, args=(j, s.url, s.kind, s.height), daemon=True).start()
    return {"id": jid}


@app.post("/api/convert")
def convert(file: UploadFile = File(...)):
    jid, j = new_job()
    d = j["dir"]
    base = os.path.basename(file.filename or "video")
    src = os.path.join(d, "in_" + base)
    with open(src, "wb") as f:
        shutil.copyfileobj(file.file, f)
    out = os.path.join(d, os.path.splitext(base)[0] + ".mp3")

    def run():
        j["status"] = "converting"
        try:
            j["proc"] = subprocess.Popen(["ffmpeg", "-y", "-i", src, "-vn", "-b:a", "192k", out],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if j["proc"].wait() != 0 or not os.path.exists(out):
                raise ValueError("Convert fail hua")
            os.remove(src)
            j.update(path=out, name=os.path.basename(out), status="ready", pct=100)
        except Exception as e:
            fail(j, e)

    threading.Thread(target=run, daemon=True).start()
    return {"id": jid}


@app.get("/api/status/{jid}")
def status(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "Job nahi mila")
    r = {k: j.get(k) for k in ("status", "pct", "speed", "eta", "name", "error")}
    if j["status"] == "ready":
        r["size"] = os.path.getsize(j["path"])
    return r


@app.post("/api/cancel/{jid}")
def cancel(jid: str):
    j = JOBS.get(jid)
    if j:
        j["cancel"] = True
        if j.get("proc"):
            j["proc"].kill()
        if j["status"] in ("ready", "error", "cancelled"):
            cleanup(j["dir"])
            JOBS.pop(jid, None)
    return {"ok": True}


@app.get("/api/file/{jid}")
def get_file(jid: str):
    j = JOBS.get(jid)
    if not j or j["status"] != "ready":
        raise HTTPException(404, "File ready nahi hai")

    def done():
        cleanup(j["dir"])
        JOBS.pop(jid, None)

    return FileResponse(j["path"], filename=j["name"], background=BackgroundTask(done))


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True))
