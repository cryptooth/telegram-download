"""
TG Album DL — Telegram kanallarındaki müzik paylaşımlarını albüm albüm indiren küçük yerel arayüz.
Çalıştır:  python app.py   →  http://127.0.0.1:8765
"""
import asyncio
import json
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from telethon import TelegramClient, utils
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

BASE = Path(__file__).parent
CFG_PATH = BASE / "config.json"
cfg = json.loads(CFG_PATH.read_text(encoding="utf-8")) if CFG_PATH.exists() else {}

API_ID = int(os.getenv("TG_API_ID", cfg.get("api_id", 0)) or 0)
API_HASH = os.getenv("TG_API_HASH", cfg.get("api_hash", ""))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", cfg.get("download_dir", str(BASE / "downloads")))).expanduser()
SESSION = os.getenv("SESSION_PATH", cfg.get("session_path", str(BASE / "data" / "tg")))
HOST = os.getenv("HOST", cfg.get("host", "127.0.0.1"))
PORT = int(os.getenv("PORT", cfg.get("port", 8765)))
PARALLEL_JOBS = int(cfg.get("parallel_jobs", 2))

AUDIO_EXT = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".alac",
             ".ape", ".wv", ".dsf", ".dff", ".aiff", ".aif"}
# Aynı albüme ait sayılması için ardışık iki ses mesajı arasındaki en fazla süre
GROUP_GAP_SEC = int(cfg.get("group_gap_minutes", 15)) * 60

client: Optional[TelegramClient] = None
login_state: dict = {}
jobs: dict = {}
tasks: dict = {}
cover_cache: dict = {}
dl_sem = asyncio.Semaphore(PARALLEL_JOBS)


@asynccontextmanager
async def lifespan(_app):
    global client
    if not API_ID or not API_HASH:
        raise RuntimeError("config.json içinde api_id ve api_hash gerekli (my.telegram.org → API development tools)")
    Path(SESSION).parent.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    yield
    await client.disconnect()


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------- yardımcılar

def safe(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s or "").strip(" .")
    return s[:150] or "untitled"


def is_audio(m) -> bool:
    if not m or not m.file or m.voice:
        return False
    name_ext = Path(m.file.name or "").suffix.lower()
    ext = name_ext or (m.file.ext or "").lower()
    return bool(m.audio) or ext in AUDIO_EXT or (m.file.mime_type or "").startswith("audio/")


def media_kind(m) -> Optional[str]:
    """'audio' | 'file' | None. Fotoğraf, sticker, GIF, sesli/görüntülü not sayılmaz."""
    if not m or not m.file or m.photo or m.voice or m.video_note or m.sticker or m.gif:
        return None
    if is_audio(m):
        return "audio"
    return "file" if m.document else None


# film.part01.rar / film.r00 / film.7z.001 / film.zip.002 / film.z01
MULTIPART_RE = re.compile(
    r"^(?P<base>.+?)(?:\.part(?P<p1>\d+)\.rar|\.r(?P<p2>\d{2,3})|\.(?:7z|zip|rar|tar)\.(?P<p3>\d{3})|\.z(?P<p4>\d{2}))$",
    re.I,
)


def multipart_info(name: str):
    """(ortak isim, parça no) ya da None"""
    m = MULTIPART_RE.match(name or "")
    if not m:
        # eski stil setlerde ilk parça düz "film.rar" olur (.r00, .r01 … onu izler)
        if re.search(r"\.rar$", name or "", re.I):
            return name[:-4], 1
        return None
    if m["p1"]:
        n = int(m["p1"])
    elif m["p2"]:
        n = int(m["p2"]) + 2          # .rar = 1, .r00 = 2 …
    elif m["p3"]:
        n = int(m["p3"])
    else:
        n = int(m["p4"])
    return m["base"], n


def archive_base(name: str) -> Optional[str]:
    mp = multipart_info(name)
    if mp:
        return mp[0]
    if re.search(r"\.(rar|zip|7z)$", name or "", re.I):
        return re.sub(r"\.(rar|zip|7z)$", "", name, flags=re.I)
    return None


def file_ext(m) -> str:
    f = m.file
    suf = Path(f.name or "").suffix
    return (suf or f.ext or ".bin").lower()


def track_info(m) -> dict:
    f = m.file
    return {
        "id": m.id,
        "title": f.title or Path(f.name or "").stem or f"Parça {m.id}",
        "performer": f.performer or "",
        "duration": int(f.duration or 0),
        "size": f.size or 0,
        "ext": file_ext(m).lstrip("."),
        "name": f.name or "",
    }


def clean_title(text: str) -> str:
    for line in (text or "").splitlines():
        line = re.sub(r"#\S+", "", line).strip(" -–—|•*_")
        if line:
            return line[:120]
    return ""


def _belongs(cur, m, kind) -> bool:
    """m mesajı açık olan gruba eklenmeli mi?"""
    if not cur or cur["kind"] != kind:
        return False
    if m.grouped_id and m.grouped_id == cur["_gid"]:
        return True
    if (m.date - cur["_last"]).total_seconds() > GROUP_GAP_SEC:
        return False
    if kind == "file":
        # Dosyalarda sadece aynı arşivin parçalarını birleştir; alakasız dosyalar ayrı kart olsun
        base = archive_base(m.file.name or "")
        return bool(base) and base == cur["_base"]
    return True


def build_albums(msgs) -> list:
    """Ardışık ses mesajlarını albüm, aynı arşivin parçalarını (part1.rar, .r00, .001 …)
    tek paket olarak gruplar. Hemen öncesindeki fotoğraf/metin mesajı (kapak + açıklama)
    gruba bağlanır."""
    msgs = sorted((m for m in msgs if m), key=lambda m: m.id)
    albums, cur, last_ctx = [], None, None

    for m in msgs:
        kind = media_kind(m)
        if kind:
            if _belongs(cur, m, kind):
                cur["tracks"].append(track_info(m))
                cur["_last"] = m.date
            else:
                if cur:
                    albums.append(cur)
                ctx = last_ctx if last_ctx and (m.date - last_ctx["date"]).total_seconds() <= GROUP_GAP_SEC else None
                last_ctx = None   # kapak/açıklama sadece hemen arkasındaki gruba ait
                cur = {"kind": kind, "tracks": [track_info(m)], "_last": m.date, "_gid": m.grouped_id,
                       "_base": archive_base(m.file.name or "") if kind == "file" else None,
                       "date": m.date.isoformat(), "_ctx": ctx, "_caption": ""}
            if m.message and not cur["_caption"]:
                cur["_caption"] = m.message
        else:
            if cur:
                albums.append(cur)
                cur = None
            if m.message or m.photo:
                last_ctx = {"date": m.date, "text": m.message or "", "photo_id": m.id if m.photo else None}
    if cur:
        albums.append(cur)

    for a in albums:
        ctx = a.pop("_ctx")
        caption = a.pop("_caption")
        a.pop("_last"); a.pop("_gid")
        base = a.pop("_base")
        perf = next((t["performer"] for t in a["tracks"] if t["performer"]), "")
        title = (clean_title(ctx["text"]) if ctx else "") or clean_title(caption)
        if a["kind"] == "file":
            # Parça sırasına göre diz (kanallar bazen part4 → part1 sırasıyla atıyor)
            a["tracks"].sort(key=lambda t: (multipart_info(t["name"]) or ("", 0))[1] or t["id"])
            title = title or base or Path(a["tracks"][0]["name"] or "").stem
            nums = sorted(mp[1] for t in a["tracks"] if (mp := multipart_info(t["name"])))
            missing = [n for n in range(1, max(nums) + 1) if n not in nums] if nums else []
            a["archive"] = {"parts": len(nums), "missing": missing} if nums or base else None
        a["title"] = title or f"{perf or 'Albüm'} — {a['date'][:10]}"
        a["performer"] = perf
        a["description"] = ((ctx["text"] if ctx else "") or caption)[:600]
        a["cover_id"] = ctx["photo_id"] if ctx else None
        a["total_size"] = sum(t["size"] for t in a["tracks"])
        a["total_duration"] = sum(t["duration"] for t in a["tracks"])
        a["key"] = str(min(t["id"] for t in a["tracks"]))
    return albums


def target_name(m, i: int, n: int) -> str:
    f = m.file
    if media_kind(m) != "audio":
        # Arşiv parçaları ve diğer dosyalar orijinal adını korumalı (yoksa açılmaz)
        if not f.name:
            return f"file_{m.id}{file_ext(m)}"
        mp = multipart_info(f.name)
        suffix = f.name[len(mp[0]):] if mp else Path(f.name).suffix
        stem = f.name[: len(f.name) - len(suffix)] if suffix else f.name
        suffix = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", suffix)
        return safe(stem)[:150] + suffix   # uzantı (.part1.rar) asla kırpılmasın
    if f.name:
        stem = Path(f.name).stem
    elif f.title:
        stem = f"{f.performer} - {f.title}" if f.performer else f.title
    else:
        stem = f"track_{m.id}"
    if not re.match(r"^\d{1,3}[\s._-]", stem):
        stem = f"{i:0{max(2, len(str(n)))}d} - {stem}"
    return safe(stem) + file_ext(m)


# ---------------------------------------------------------------- giriş

class PhoneIn(BaseModel):
    phone: str


class CodeIn(BaseModel):
    code: str


class PasswordIn(BaseModel):
    password: str


@app.get("/api/status")
async def status():
    if await client.is_user_authorized():
        me = await client.get_me()
        return {"authorized": True, "name": utils.get_display_name(me), "download_dir": str(DOWNLOAD_DIR)}
    return {"authorized": False, "step": login_state.get("step", "phone")}


@app.post("/api/login/phone")
async def login_phone(p: PhoneIn):
    r = await client.send_code_request(p.phone.strip())
    login_state.update(step="code", phone=p.phone.strip(), hash=r.phone_code_hash)
    return {"step": "code"}


@app.post("/api/login/code")
async def login_code(c: CodeIn):
    if "phone" not in login_state:
        raise HTTPException(400, "Önce telefon numarası gir")
    try:
        await client.sign_in(login_state["phone"], c.code.strip(), phone_code_hash=login_state["hash"])
    except SessionPasswordNeededError:
        login_state["step"] = "password"
        return {"step": "password"}
    except PhoneCodeInvalidError:
        raise HTTPException(400, "Kod hatalı")
    except PhoneCodeExpiredError:
        login_state.clear()
        raise HTTPException(400, "Kodun süresi doldu, baştan dene")
    login_state.clear()
    return {"step": "done"}


@app.post("/api/login/password")
async def login_password(p: PasswordIn):
    try:
        await client.sign_in(password=p.password)
    except PasswordHashInvalidError:
        raise HTTPException(400, "İki adımlı doğrulama şifresi hatalı")
    login_state.clear()
    return {"step": "done"}


@app.post("/api/logout")
async def logout():
    await client.log_out()
    await client.connect()
    return {"ok": True}


# ---------------------------------------------------------------- kanallar / albümler

@app.get("/api/dialogs")
async def dialogs():
    out = []
    async for d in client.iter_dialogs():
        if d.is_channel or d.is_group:
            out.append({"id": d.id, "name": d.name, "unread": d.unread_count})
    return out


@app.get("/api/albums")
async def albums(chat: int, offset_id: int = 0, limit: int = 300):
    ent = await client.get_entity(chat)
    msgs = await client.get_messages(ent, limit=min(limit, 1000), offset_id=offset_id)
    res = build_albums(msgs)
    res.reverse()
    return {
        "chat_id": utils.get_peer_id(ent),
        "chat_title": utils.get_display_name(ent),
        "albums": res,
        "next_offset": min((m.id for m in msgs), default=0),
        "scanned": len(msgs),
    }


@app.get("/api/link")
async def from_link(url: str):
    m = re.search(r"t\.me/(?:c/(\d+)|([A-Za-z0-9_]{4,}))/(?:\d+/)?(\d+)", url)
    if not m:
        raise HTTPException(400, "Mesaj linki tanınmadı (örn. https://t.me/c/123456/789)")
    chat = int("-100" + m[1]) if m[1] else m[2]
    mid = int(m[3])
    try:
        ent = await client.get_entity(chat)
    except Exception:
        raise HTTPException(404, "Kanal bulunamadı — üye olduğundan emin ol")
    msgs = await client.get_messages(ent, min_id=max(mid - 60, 0), max_id=mid + 60, limit=120)
    res = build_albums(msgs)
    hit = next((a for a in res if any(t["id"] == mid for t in a["tracks"]) or a["cover_id"] == mid), None)
    if not hit:
        hit = next((a for a in res if a["tracks"][0]["id"] > mid), None)
    res.reverse()
    return {
        "chat_id": utils.get_peer_id(ent),
        "chat_title": utils.get_display_name(ent),
        "albums": res,
        "highlight": hit["key"] if hit else None,
        "next_offset": 0,
    }


@app.get("/api/cover")
async def cover(chat: int, id: int):
    k = (chat, id)
    if k not in cover_cache:
        msg = await client.get_messages(chat, ids=id)
        data = await client.download_media(msg, bytes) if msg and msg.photo else None
        if not data:
            raise HTTPException(404)
        cover_cache[k] = data
    return Response(cover_cache[k], media_type="image/jpeg",
                    headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------- indirme

class DownloadIn(BaseModel):
    chat: int
    ids: list[int]
    folder: str
    cover_id: Optional[int] = None
    extract: bool = False


CHUNK = 1024 * 1024  # 1 MB, Telethon'un istediği hizalamaya uygun


def find_extractor():
    """macOS'ta `brew install unar` ya da `brew install sevenzip` yeterli."""
    for tool in ("unar", "7zz", "7z", "unrar"):
        p = shutil.which(tool)
        if p:
            return tool, p
    return None


async def fetch_file(m, dest: Path, on_progress):
    """Kaldığı yerden devam eden indirme (.part dosyası korunur)."""
    size = m.file.size or 0
    tmp = dest.with_name(dest.name + ".part")
    have = tmp.stat().st_size if tmp.exists() else 0
    have -= have % CHUNK                        # yarım kalan son bloğu at
    attempts = 0
    while True:
        try:
            with open(tmp, "r+b" if tmp.exists() else "wb") as fh:
                fh.truncate(have)
                fh.seek(have)
                on_progress(have)
                async for chunk in client.iter_download(m.media, offset=have, request_size=CHUNK, file_size=size):
                    fh.write(chunk)
                    have += len(chunk)
                    on_progress(have)
            break
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            attempts += 1
            if attempts > 5:
                raise
            await asyncio.sleep(3 * attempts)
            have = tmp.stat().st_size - tmp.stat().st_size % CHUNK
    os.replace(tmp, dest)


async def extract_archive(target: Path, names: list[str]) -> str:
    ex = find_extractor()
    if not ex:
        return "Arşiv açıcı bulunamadı (brew install unar)"
    # İlk parça: part1 / part01 / .rar / .001 / tek arşiv
    first = min((n for n in names if archive_base(n)),
                key=lambda n: (multipart_info(n) or ("", 1))[1], default=None)
    if not first:
        return ""
    tool, path = ex
    out = target / safe(archive_base(first) or "extracted")
    out.mkdir(exist_ok=True)
    args = {
        "unar": [path, "-q", "-f", "-o", str(out), str(target / first)],
        "7zz": [path, "x", "-y", f"-o{out}", str(target / first)],
        "7z": [path, "x", "-y", f"-o{out}", str(target / first)],
        "unrar": [path, "x", "-o+", str(target / first), str(out) + "/"],
    }[tool]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        return f"Açılamadı ({tool}): {err.decode(errors='ignore').strip()[:200] or 'şifreli ya da eksik parça olabilir'}"
    return ""


@app.get("/api/capabilities")
async def capabilities():
    ex = find_extractor()
    return {"extractor": ex[0] if ex else None}


@app.post("/api/download")
async def download(d: DownloadIn):
    if not d.ids:
        raise HTTPException(400, "Parça seçilmedi")
    jid = uuid.uuid4().hex[:8]
    jobs[jid] = {"id": jid, "title": d.folder, "status": "queued", "files": len(d.ids),
                 "done_files": 0, "bytes": 0, "total": 0, "current": "", "error": None, "path": ""}
    tasks[jid] = asyncio.create_task(run_job(jid, d))
    return jobs[jid]


async def run_job(jid: str, d: DownloadIn):
    j = jobs[jid]
    try:
        async with dl_sem:
            j["status"] = "running"
            ent = await client.get_entity(d.chat)
            msgs = [m for m in await client.get_messages(ent, ids=d.ids) if m and m.file]
            msgs.sort(key=lambda m: m.id)
            j["files"] = len(msgs)
            j["total"] = sum(m.file.size or 0 for m in msgs)

            target = DOWNLOAD_DIR / safe(utils.get_display_name(ent)) / safe(d.folder)
            target.mkdir(parents=True, exist_ok=True)
            j["path"] = str(target)

            if d.cover_id and not (target / "cover.jpg").exists():
                cm = await client.get_messages(ent, ids=d.cover_id)
                if cm and cm.photo:
                    await client.download_media(cm, str(target / "cover.jpg"))

            done, names = 0, []
            for i, m in enumerate(msgs, 1):
                name = target_name(m, i, len(msgs))
                dest = target / name
                j["current"] = name
                size = m.file.size or 0
                if dest.exists() and dest.stat().st_size == size:
                    done += size
                else:
                    def prog(cur, base=done):
                        j["bytes"] = base + cur

                    await fetch_file(m, dest, prog)
                    done += size
                j["bytes"] = done
                j["done_files"] += 1
                names.append(name)

            if d.extract and any(archive_base(n) for n in names):
                j["status"] = "extracting"
                j["current"] = "Arşiv açılıyor…"
                warn = await extract_archive(target, names)
                if warn:
                    j["error"] = warn
            j["status"] = "done"
            j["current"] = ""
    except asyncio.CancelledError:
        j["status"] = "cancelled"
    except Exception as e:
        j["status"] = "error"
        j["error"] = str(e)
    finally:
        tasks.pop(jid, None)


@app.get("/api/jobs")
async def get_jobs():
    return list(jobs.values())[::-1]


@app.post("/api/jobs/{jid}/cancel")
async def cancel_job(jid: str):
    t = tasks.get(jid)
    if t:
        t.cancel()
    return {"ok": True}


@app.post("/api/jobs/clear")
async def clear_jobs():
    for k in [k for k, v in jobs.items() if v["status"] in ("done", "error", "cancelled")]:
        jobs.pop(k)
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn
    print(f"→ http://{HOST}:{PORT}   (indirme klasörü: {DOWNLOAD_DIR})")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
