import asyncio
import hashlib
import hmac
import html
import io
import json
import os
import re
import threading
import time
import uuid
from urllib.parse import parse_qsl

from flask import Flask, jsonify, request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
BOT_TOKEN          = os.environ.get("BOT_TOKEN", "")
PUBLIC_CHANNEL_ID  = int(os.environ.get("PUBLIC_CHANNEL_ID", "0"))
PRIVATE_CHANNEL_ID = int(os.environ.get("PRIVATE_CHANNEL_ID", "0"))
BOT_USERNAME       = os.environ.get("BOT_USERNAME", "").lstrip("@")
BRAND              = os.environ.get("BRAND", "KNL Movies")
WEB_APP_URL        = (os.environ.get("WEB_APP_URL", "")
                      or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
LOG_ENTRIES        = os.environ.get("LOG_ENTRIES", "1") == "1"
INITDATA_TTL       = int(os.environ.get("INITDATA_TTL", "86400"))
KEEP_IDS           = int(os.environ.get("KEEP_IDS", "12"))
BACKUP_EVERY       = int(os.environ.get("BACKUP_EVERY", "5"))


def _admin_ids():
    raw = os.environ.get("ADMIN_IDS", "") or os.environ.get("ADMIN_ID", "")
    out = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


ADMIN_IDS = _admin_ids()

DIV   = "━" * 22
WIDTH = "ㅤ" * 28


def h(t) -> str:
    return html.escape(str(t if t is not None else ""), quote=False)


def frame(t: str) -> str:
    return f"{t}\n{WIDTH}"


SNAP_MARK = "KNLDB"
SNAP_VER  = 5

DB = {"movies": {}}

# ── Data-safety state ──
LOAD = {
    "state": "boot",        # boot | fresh | ok | failed
    "baseline": 0,          # titles at successful load
    "authorized_loss": 0,   # legit deletions since load
    "source": "none",
    "error": "",
    "msg_id": None,
    "saved_at": 0,
    "last_backup": 0,
}

BOT = None
MAIN_LOOP = None
_save_lock = None


def now() -> int:
    return int(time.time())


def writable():
    """Load fail hua hai toh likhna MANA hai — yahi data bachata hai."""
    if LOAD["state"] in ("fresh", "ok"):
        return True, ""
    return False, (
        "Database load nahi hua, isliye save BLOCKED hai (data protection). "
        "Pehle /recover chalao ya purana library.json forward karo."
    )


def shrink_ok(new_count: int):
    floor = max(0, LOAD["baseline"] - LOAD["authorized_loss"])
    if new_count >= floor:
        return True, ""
    return False, (
        f"Save BLOCKED: {new_count} titles likhne ja rahe the, lekin "
        f"{floor} hone chahiye. Data loss rok diya gaya. "
        f"/recover chalao, ya janbujh kar delete kiya hai to /save force."
    )


# ═══════════════════════════════════════════════════════════════
#  SNAPSHOT PLUMBING
# ═══════════════════════════════════════════════════════════════

def _desc_string(ids, count):
    ids = [str(i) for i in ids if i][:KEEP_IDS]
    return f"{SNAP_MARK} v{SNAP_VER} n={count} t={now()} ids={','.join(ids)}"


def _parse_desc(desc):
    if not desc or not desc.strip().startswith(SNAP_MARK):
        return None
    kv = {}
    for tok in desc.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            kv[k.strip()] = v.strip()
    ids = [int(x) for x in kv.get("ids", "").split(",") if x.strip().isdigit()]
    try:
        n = int(kv.get("n", "0") or 0)
    except ValueError:
        n = 0
    return {"ids": ids, "n": n}


async def _download_doc(bot, document):
    f = await bot.get_file(document.file_id)
    raw = await f.download_as_bytearray()
    return json.loads(bytes(raw).decode("utf-8"))


async def _read_by_id(bot, mid):
    """Channel ki purani message se document padho (forward trick)."""
    targets = [PRIVATE_CHANNEL_ID] + sorted(ADMIN_IDS)
    for dest in targets:
        fwd = None
        try:
            fwd = await bot.forward_message(dest, PRIVATE_CHANNEL_ID, mid)
            if not fwd.document:
                continue
            return await _download_doc(bot, fwd.document)
        except Exception as exc:
            print(f"[recover] id={mid} dest={dest}: {exc}")
        finally:
            if fwd is not None:
                try:
                    await bot.delete_message(dest, fwd.message_id)
                except Exception:
                    pass
    return None


def _normalize(payload):
    movies = payload.get("movies")
    if not isinstance(movies, dict):
        return None
    for mid, m in movies.items():
        if not isinstance(m, dict):
            continue
        m.setdefault("id", mid)
        for k, v in DEFAULTS.items():
            m.setdefault(k, v() if callable(v) else v)
    return movies


async def load_snapshot(bot, announce=True) -> dict:
    """Pin → description chain → fail. Fail par DB ko chhua nahi jata."""
    LOAD.update({"state": "boot", "error": "", "source": "none"})

    if not PRIVATE_CHANNEL_ID:
        LOAD.update({"state": "failed", "error": "PRIVATE_CHANNEL_ID set nahi hai"})
        return dict(LOAD)

    chat = None
    for attempt in range(3):
        try:
            chat = await bot.get_chat(PRIVATE_CHANNEL_ID)
            break
        except Exception as exc:
            print(f"[load] getChat attempt {attempt+1}: {exc}")
            await asyncio.sleep(1.5 * (attempt + 1))

    if chat is None:
        LOAD.update({"state": "failed",
                     "error": "Channel reachable nahi (getChat fail). Save blocked."})
        return dict(LOAD)

    candidates = []

    pm = chat.pinned_message
    if pm and pm.document and (pm.caption or "").strip().startswith(SNAP_MARK):
        candidates.append(("pinned", pm.message_id, pm.document))

    desc_info = _parse_desc(chat.description or "")
    if desc_info:
        for mid in desc_info["ids"]:
            if pm and mid == pm.message_id:
                continue
            candidates.append(("description", mid, None))

    if not candidates:
        DB["movies"] = {}
        LOAD.update({"state": "fresh", "baseline": 0, "authorized_loss": 0,
                     "source": "fresh", "msg_id": None, "saved_at": 0})
        print("[load] fresh database")
        return dict(LOAD)

    for source, mid, doc in candidates:
        try:
            payload = await _download_doc(bot, doc) if doc else await _read_by_id(bot, mid)
            if not payload:
                continue
            movies = _normalize(payload)
            if movies is None:
                continue
            DB["movies"] = movies
            LOAD.update({
                "state": "ok", "baseline": len(movies), "authorized_loss": 0,
                "source": source, "msg_id": mid,
                "saved_at": int(payload.get("saved_at") or 0), "error": "",
            })
            print(f"[load] {len(movies)} titles via {source} (msg {mid})")
            if source != "pinned":
                await _repoint(bot, mid, len(movies))
            return dict(LOAD)
        except Exception as exc:
            print(f"[load] {source} msg={mid}: {exc}")

    LOAD.update({
        "state": "failed",
        "error": "Snapshot pointers mile lekin koi padha nahi gaya. Save blocked.",
    })
    if announce:
        await alert_admins(
            f"🚨 <b>DATA PROTECTION ACTIVE</b>\n{DIV}\n"
            f"{h(LOAD['error'])}\n\n"
            f"<blockquote>Purana data safe hai — bot kuch overwrite nahi karega.\n\n"
            f"Fix:\n"
            f"1️⃣ /recover chalao\n"
            f"2️⃣ Ya private channel se koi purana <code>library.json</code> "
            f"yahan forward kar do</blockquote>"
        )
    return dict(LOAD)


async def _repoint(bot, mid, count):
    """Pin + description dono ko ek valid snapshot par le jao."""
    try:
        await bot.pin_chat_message(PRIVATE_CHANNEL_ID, mid, disable_notification=True)
    except Exception as exc:
        print("[repoint] pin:", exc)
    try:
        chat = await bot.get_chat(PRIVATE_CHANNEL_ID)
        info = _parse_desc(chat.description or "") or {"ids": []}
        ids = [mid] + [i for i in info["ids"] if i != mid]
        await bot.set_chat_description(PRIVATE_CHANNEL_ID, _desc_string(ids, count))
    except Exception as exc:
        print("[repoint] description:", exc)


async def save_snapshot(bot, force=False) -> dict:
    ok, why = writable()
    if not ok and not force:
        LOAD["error"] = why
        return {"ok": False, "error": why, "blocked": True}

    ok, why = shrink_ok(len(DB["movies"]))
    if not ok and not force:
        LOAD["error"] = why
        await alert_admins(f"🛑 <b>SAVE BLOCKED</b>\n{DIV}\n{h(why)}")
        return {"ok": False, "error": why, "blocked": True}

    if not PRIVATE_CHANNEL_ID:
        return {"ok": False, "error": "PRIVATE_CHANNEL_ID set nahi hai"}

    async with _save_lock:
        prev_ids = []
        try:
            chat = await bot.get_chat(PRIVATE_CHANNEL_ID)
            info = _parse_desc(chat.description or "")
            if info:
                prev_ids = info["ids"]
        except Exception:
            pass
        if LOAD.get("msg_id") and LOAD["msg_id"] not in prev_ids:
            prev_ids.insert(0, LOAD["msg_id"])

        payload = {
            "v": SNAP_VER, "saved_at": now(),
            "count": len(DB["movies"]),
            "prev": prev_ids[:KEEP_IDS],
            "movies": DB["movies"],
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        bio = io.BytesIO(raw)
        bio.name = "library.json"
        caption = (
            f"{SNAP_MARK} v{SNAP_VER}\n"
            f"Titles: {len(DB['movies'])}\n"
            f"Saved: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC"
        )

        try:
            msg = await bot.send_document(
                chat_id=PRIVATE_CHANNEL_ID, document=bio,
                filename="library.json", caption=caption,
                write_timeout=90, read_timeout=90,
            )
        except Exception as exc:
            LOAD["error"] = f"Upload fail: {exc}"
            return {"ok": False, "error": LOAD["error"]}

        # Verify: jo likha wahi padh paa rahe hain?
        verified = False
        try:
            back = await _download_doc(bot, msg.document)
            verified = len(_normalize(back) or {}) == len(DB["movies"])
        except Exception as exc:
            print("[save] verify:", exc)

        if not verified:
            LOAD["error"] = "Verify fail — pin nahi kiya, purana snapshot safe hai"
            await alert_admins(f"⚠️ <b>SAVE VERIFY FAILED</b>\n{DIV}\n"
                               f"Naya file corrupt tha. Purana data untouched.")
            return {"ok": False, "error": LOAD["error"]}

        ids = [msg.message_id] + [i for i in prev_ids if i != msg.message_id]
        pinned, desc_ok = False, False
        try:
            await bot.pin_chat_message(PRIVATE_CHANNEL_ID, msg.message_id,
                                       disable_notification=True)
            pinned = True
        except Exception as exc:
            print("[save] pin:", exc)
        try:
            await bot.set_chat_description(PRIVATE_CHANNEL_ID,
                                           _desc_string(ids, len(DB["movies"])))
            desc_ok = True
        except Exception as exc:
            print("[save] description:", exc)

        # Purane snapshots DELETE NAHI hote — archive rehta hai
        LOAD.update({
            "state": "ok", "baseline": len(DB["movies"]), "authorized_loss": 0,
            "source": "pinned-snapshot", "msg_id": msg.message_id,
            "saved_at": payload["saved_at"], "error": "",
        })
        if not pinned and not desc_ok:
            LOAD["error"] = ("Pin aur description dono fail — bot ko 'Pin Messages' + "
                             "'Change Channel Info' rights do")
            await alert_admins(f"🚨 <b>POINTER FAIL</b>\n{DIV}\n{h(LOAD['error'])}\n\n"
                               f"<blockquote>Data channel mein safe hai "
                               f"(msg <code>{msg.message_id}</code>), lekin agle boot par "
                               f"auto-load nahi hoga. Rights fix karo phir /save.</blockquote>")

        print(f"[save] {len(DB['movies'])} titles | pin={pinned} desc={desc_ok}")

        if len(DB["movies"]) - LOAD["last_backup"] >= BACKUP_EVERY:
            await dm_backup(bot)
            LOAD["last_backup"] = len(DB["movies"])

        return {"ok": True, "pinned": pinned, "description": desc_ok,
                "msg_id": msg.message_id, "error": LOAD["error"]}


async def dm_backup(bot):
    if not ADMIN_IDS:
        return
    raw = json.dumps({"v": SNAP_VER, "saved_at": now(), "movies": DB["movies"]},
                     ensure_ascii=False, indent=1).encode("utf-8")
    for aid in ADMIN_IDS:
        try:
            bio = io.BytesIO(raw)
            bio.name = "library.json"
            await bot.send_document(
                aid, document=bio, filename="library.json",
                caption=(f"{SNAP_MARK} backup\nTitles: {len(DB['movies'])}\n"
                         f"Restore: is file ko bot mein forward kar do."),
                write_timeout=90, read_timeout=90,
            )
        except Exception as exc:
            print("[backup]", aid, exc)


async def alert_admins(text):
    if BOT is None:
        return
    for aid in ADMIN_IDS:
        try:
            await BOT.send_message(aid, frame(text), parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)
        except Exception:
            pass


async def restore_payload(bot, payload, label="forwarded file"):
    movies = _normalize(payload)
    if movies is None:
        raise ValueError("File mein valid 'movies' data nahi hai")
    before = len(DB["movies"])
    DB["movies"] = movies
    LOAD.update({"state": "ok", "baseline": len(movies), "authorized_loss": 0})
    res = await save_snapshot(bot, force=True)
    return {"restored": len(movies), "before": before, "save": res, "label": label}


# ═══════════════════════════════════════════════════════════════
#  SCHEMA
# ═══════════════════════════════════════════════════════════════

DEFAULTS = {
    "name": "Untitled", "link": "", "size": "-", "quality": "-",
    "poster": "", "backdrop": "", "type": "movie",
    "genres": list, "year": 0, "rating": 0.0, "age": "",
    "duration": "", "language": "", "description": "", "cast": list,
}


def _as_list(val):
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        return [p.strip() for p in val.replace(";", ",").split(",") if p.strip()]
    return []


def clean_fields(data: dict):
    name = str(data.get("name", "")).strip()
    link = str(data.get("link", "")).strip()
    if not name:
        return None, "Name khali hai"
    if not link.startswith(("http://", "https://", "tg://")):
        return None, "Link http(s) se shuru hona chahiye"
    poster = str(data.get("poster", "")).strip()
    backdrop = str(data.get("backdrop", "")).strip()
    for label, url in (("Poster", poster), ("Backdrop", backdrop)):
        if url and not url.startswith(("http://", "https://")):
            return None, f"{label} URL http(s) hona chahiye"
    try:
        year = int(str(data.get("year") or 0).strip() or 0)
    except ValueError:
        year = 0
    try:
        rating = round(float(str(data.get("rating") or 0).strip() or 0), 1)
    except ValueError:
        rating = 0.0
    rating = max(0.0, min(10.0, rating))
    mtype = str(data.get("type", "movie")).strip().lower()
    if mtype not in ("movie", "series"):
        mtype = "movie"
    return {
        "name": name, "link": link,
        "size": str(data.get("size", "-")).strip() or "-",
        "quality": str(data.get("quality", "-")).strip() or "-",
        "poster": poster, "backdrop": backdrop, "type": mtype,
        "genres": _as_list(data.get("genres")), "year": year, "rating": rating,
        "age": str(data.get("age", "")).strip(),
        "duration": str(data.get("duration", "")).strip(),
        "language": str(data.get("language", "")).strip(),
        "description": str(data.get("description", "")).strip(),
        "cast": _as_list(data.get("cast")),
    }, None


def public_view(m: dict) -> dict:
    return {
        "id": m["id"], "name": m.get("name", "Untitled"),
        "size": m.get("size", "-"), "quality": m.get("quality", "-"),
        "poster": m.get("poster", ""), "backdrop": m.get("backdrop", ""),
        "type": m.get("type", "movie"), "genres": m.get("genres", []),
        "year": m.get("year", 0), "rating": m.get("rating", 0.0),
        "age": m.get("age", ""), "duration": m.get("duration", ""),
        "language": m.get("language", ""), "description": m.get("description", ""),
        "cast": m.get("cast", []), "created": m.get("created", 0),
    }


def guard_or_raise():
    ok, why = writable()
    if not ok:
        raise ValueError(why)


# ═══════════════════════════════════════════════════════════════
#  CHANNEL POST
# ═══════════════════════════════════════════════════════════════

def movie_meta_line(m: dict) -> str:
    bits = []
    if m.get("year"):
        bits.append(f"📅 {m['year']}")
    if m.get("rating"):
        bits.append(f"⭐ {m['rating']}")
    if m.get("duration"):
        bits.append(f"⏱ {h(m['duration'])}")
    return "  •  ".join(bits)


async def post_public(bot, movie: dict) -> int:
    deep = f"https://t.me/{BOT_USERNAME}?start=movie_{movie['id']}" if BOT_USERNAME else ""
    meta = movie_meta_line(movie)
    caption = (
        f"🎬 <b>{h(movie['name'])}</b>\n{DIV}\n"
        f"📦 <b>Size:</b> <code>{h(movie['size'])}</code>\n"
        f"🎞 <b>Quality:</b> <code>{h(movie['quality'])}</code>\n"
    )
    if meta:
        caption += f"{meta}\n"
    if movie.get("genres"):
        caption += f"🏷 <i>{h(', '.join(movie['genres']))}</i>\n"
    caption += f"{DIV}\n⬇️ <b>Download</b> — button dabayein, link bot mein milega."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ DOWNLOAD", url=deep)]]) if deep else None
    image = movie.get("backdrop") or movie.get("poster")
    if image:
        m = await bot.send_photo(PUBLIC_CHANNEL_ID, photo=image, caption=frame(caption),
                                 parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        m = await bot.send_message(PUBLIC_CHANNEL_ID, frame(caption),
                                   parse_mode=ParseMode.HTML, reply_markup=kb)
    return m.message_id


async def log_entry(bot, movie: dict) -> int:
    text = ("#MOVIE v2\n"
            f"ID: {movie['id']}\nNAME: {movie['name']}\nLINK: {movie['link']}\n"
            f"SIZE: {movie['size']}\nQUALITY: {movie['quality']}\n"
            f"POSTER: {movie['poster'] or '-'}")
    m = await bot.send_message(PRIVATE_CHANNEL_ID, text)
    return m.message_id


# ═══════════════════════════════════════════════════════════════
#  OPS
# ═══════════════════════════════════════════════════════════════

async def op_add(data: dict) -> dict:
    guard_or_raise()
    fields, err = clean_fields(data)
    if err:
        raise ValueError(err)
    mid = uuid.uuid4().hex[:8]
    movie = {"id": mid, **fields, "created": now(),
             "log_msg_id": None, "public_msg_id": None}
    DB["movies"][mid] = movie
    warns = []
    if LOG_ENTRIES and PRIVATE_CHANNEL_ID:
        try:
            movie["log_msg_id"] = await log_entry(BOT, movie)
        except Exception as exc:
            warns.append(f"Log entry: {exc}")
    if data.get("post") and PUBLIC_CHANNEL_ID:
        try:
            movie["public_msg_id"] = await post_public(BOT, movie)
        except Exception as exc:
            warns.append(f"Channel post: {exc}")
    st = await save_snapshot(BOT)
    if not st.get("ok"):
        warns.append(st.get("error") or "Save issue")
    return {"movie": movie, "warnings": warns}


async def op_update(data: dict) -> dict:
    guard_or_raise()
    mid = str(data.get("id", "")).strip()
    movie = DB["movies"].get(mid)
    if not movie:
        raise ValueError("ID nahi mili")
    fields, err = clean_fields(data)
    if err:
        raise ValueError(err)
    movie.update(fields)
    movie["updated"] = now()
    st = await save_snapshot(BOT)
    warns = [] if st.get("ok") else [st.get("error") or "Save issue"]
    return {"movie": movie, "warnings": warns}


async def op_delete(data: dict) -> dict:
    guard_or_raise()
    mid = str(data.get("id", "")).strip()
    movie = DB["movies"].pop(mid, None)
    if not movie:
        raise ValueError("ID nahi mili")
    LOAD["authorized_loss"] += 1
    warns = []
    for key, chat_id in (("log_msg_id", PRIVATE_CHANNEL_ID),
                         ("public_msg_id", PUBLIC_CHANNEL_ID)):
        msg_id = movie.get(key)
        if msg_id and chat_id:
            try:
                await BOT.delete_message(chat_id, msg_id)
            except Exception as exc:
                warns.append(f"{key} delete: {exc}")
    st = await save_snapshot(BOT)
    if not st.get("ok"):
        warns.append(st.get("error") or "Save issue")
    return {"id": mid, "warnings": warns}


async def op_repost(data: dict) -> dict:
    guard_or_raise()
    mid = str(data.get("id", "")).strip()
    movie = DB["movies"].get(mid)
    if not movie:
        raise ValueError("ID nahi mili")
    if not PUBLIC_CHANNEL_ID:
        raise ValueError("PUBLIC_CHANNEL_ID set nahi hai")
    old = movie.get("public_msg_id")
    if old:
        try:
            await BOT.delete_message(PUBLIC_CHANNEL_ID, old)
        except Exception:
            pass
    movie["public_msg_id"] = await post_public(BOT, movie)
    await save_snapshot(BOT)
    return {"movie": movie, "warnings": []}


async def op_import(text: str) -> dict:
    fields = {}
    for line in text.splitlines()[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip().upper()] = v.strip()
    mid, link = fields.get("ID"), fields.get("LINK")
    if not mid or not link:
        raise ValueError("ID / LINK missing")
    poster = fields.get("POSTER", "-")
    existed = mid in DB["movies"]
    movie = DB["movies"].get(mid)
    if not movie:
        movie = {"id": mid, "created": now(), "log_msg_id": None, "public_msg_id": None}
        for k, v in DEFAULTS.items():
            movie.setdefault(k, v() if callable(v) else v)
    movie.update({
        "name": fields.get("NAME", "Untitled"), "link": link,
        "size": fields.get("SIZE", "-"), "quality": fields.get("QUALITY", "-"),
        "poster": "" if poster in ("-", "") else poster,
    })
    DB["movies"][mid] = movie
    if LOAD["state"] == "failed":
        LOAD.update({"state": "ok", "baseline": len(DB["movies"]), "authorized_loss": 0})
    await save_snapshot(BOT)
    return {"id": mid, "updated": existed}


# ═══════════════════════════════════════════════════════════════
#  initData VALIDATION
# ═══════════════════════════════════════════════════════════════

def validate_init_data(init_data: str):
    if not init_data:
        return None, "initData missing — app Telegram ke andar kholen"
    if not BOT_TOKEN:
        return None, "BOT_TOKEN set nahi hai"
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None, "initData parse fail"
    recv_hash = pairs.pop("hash", None)
    if not recv_hash:
        return None, "hash missing"
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calc = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        return None, "signature invalid"
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if INITDATA_TTL and (time.time() - auth_date) > INITDATA_TTL:
        return None, "session expire — app dobara kholen"
    user = {}
    if pairs.get("user"):
        try:
            user = json.loads(pairs["user"])
        except ValueError:
            user = {}
    if not user.get("id"):
        return None, "user id missing"
    return {"user": user, "auth_date": auth_date}, None


def req_auth():
    init_data = request.headers.get("X-Init-Data", "")
    if not init_data:
        body = request.get_json(silent=True) or {}
        init_data = body.get("initData", "")
    return validate_init_data(init_data)


def bot_call(coro, timeout=90):
    if MAIN_LOOP is None:
        raise RuntimeError("Bot loop ready nahi hai")
    return asyncio.run_coroutine_threadsafe(coro, MAIN_LOOP).result(timeout)


# ═══════════════════════════════════════════════════════════════
#  MEMBERSHIP
# ═══════════════════════════════════════════════════════════════

async def check_membership(bot, user_id: int) -> bool:
    if not PUBLIC_CHANNEL_ID:
        return True
    try:
        member = await bot.get_chat_member(PUBLIC_CHANNEL_ID, user_id)
        return member.status in (ChatMemberStatus.MEMBER,
                                 ChatMemberStatus.ADMINISTRATOR,
                                 ChatMemberStatus.OWNER)
    except Exception as exc:
        print("[membership]", exc)
        return False


async def get_channel_link(bot) -> str:
    try:
        chat = await bot.get_chat(PUBLIC_CHANNEL_ID)
        if chat.username:
            return f"https://t.me/{chat.username}"
        if chat.invite_link:
            return chat.invite_link
        return await bot.export_chat_invite_link(PUBLIC_CHANNEL_ID)
    except Exception:
        return "https://t.me/"


# ═══════════════════════════════════════════════════════════════
#  FLASK
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__)


@app.route("/")
def mini_app():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            body = f.read()
    except OSError:
        return "app.html missing", 500
    return body, 200, {"Content-Type": "text/html; charset=utf-8",
                       "Cache-Control": "no-store"}


@app.route("/health")
def health():
    return (f"OK movies={len(DB['movies'])} load={LOAD['state']} "
            f"baseline={LOAD['baseline']}"), 200


@app.route("/api/movies")
def api_movies():
    items = [public_view(m) for m in DB["movies"].values()]
    items.sort(key=lambda x: x.get("created", 0), reverse=True)
    genres = sorted({g for m in items for g in m.get("genres", [])})
    return jsonify({"ok": True, "brand": BRAND, "bot": BOT_USERNAME,
                    "count": len(items), "genres": genres, "movies": items,
                    "load_state": LOAD["state"]})


@app.route("/api/session", methods=["POST"])
def api_session():
    auth, err = req_auth()
    if err:
        return jsonify({"ok": True, "authed": False, "is_admin": False,
                        "reason": err, "brand": BRAND, "bot": BOT_USERNAME})
    uid = int(auth["user"]["id"])
    return jsonify({
        "ok": True, "authed": True, "is_admin": uid in ADMIN_IDS,
        "user": {"id": uid, "name": auth["user"].get("first_name", ""),
                 "username": auth["user"].get("username", ""),
                 "photo": auth["user"].get("photo_url", "")},
        "brand": BRAND, "bot": BOT_USERNAME,
    })


@app.route("/api/access", methods=["POST"])
def api_access():
    auth, err = req_auth()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    body = request.get_json(silent=True) or {}
    movie = DB["movies"].get(str(body.get("id", "")).strip())
    if not movie:
        return jsonify({"ok": False, "error": "Title library mein nahi mila"}), 404
    uid = int(auth["user"]["id"])
    try:
        is_member = bot_call(check_membership(BOT, uid), timeout=25)
        join_url = "" if is_member else bot_call(get_channel_link(BOT), timeout=25)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Membership check fail: {exc}"}), 503
    if not is_member:
        return jsonify({"ok": True, "granted": False, "need_join": True,
                        "join_url": join_url})
    return jsonify({"ok": True, "granted": True, "link": movie["link"],
                    "name": movie["name"], "size": movie["size"],
                    "quality": movie["quality"]})


def _admin_guard():
    auth, err = req_auth()
    if err:
        return None, (jsonify({"ok": False, "error": err}), 401)
    if int(auth["user"]["id"]) not in ADMIN_IDS:
        return None, (jsonify({"ok": False, "error": "Admin only"}), 403)
    return auth, None


@app.route("/api/admin/status", methods=["POST"])
def api_admin_status():
    _, err_resp = _admin_guard()
    if err_resp:
        return err_resp
    return jsonify({
        "ok": True, "titles": len(DB["movies"]),
        "storage": {"ok": LOAD["state"] == "ok" and not LOAD["error"],
                    "source": LOAD["source"], "saved_at": LOAD["saved_at"],
                    "error": LOAD["error"], "state": LOAD["state"],
                    "baseline": LOAD["baseline"]},
        "movies_count": sum(1 for m in DB["movies"].values() if m.get("type") == "movie"),
        "series_count": sum(1 for m in DB["movies"].values() if m.get("type") == "series"),
        "public_channel": bool(PUBLIC_CHANNEL_ID),
        "private_channel": bool(PRIVATE_CHANNEL_ID),
        "bot_username": BOT_USERNAME,
    })


@app.route("/api/admin/movies", methods=["POST"])
def api_admin_movies():
    _, err_resp = _admin_guard()
    if err_resp:
        return err_resp
    items = sorted(DB["movies"].values(), key=lambda x: x.get("created", 0), reverse=True)
    return jsonify({"ok": True, "count": len(items), "movies": items})


def _mutate(op):
    _, err_resp = _admin_guard()
    if err_resp:
        return err_resp
    body = request.get_json(silent=True) or {}
    try:
        result = bot_call(op(body), timeout=120)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, **result})


@app.route("/api/admin/add", methods=["POST"])
def api_admin_add():
    return _mutate(op_add)


@app.route("/api/admin/update", methods=["POST"])
def api_admin_update():
    return _mutate(op_update)


@app.route("/api/admin/delete", methods=["POST"])
def api_admin_delete():
    return _mutate(op_delete)


@app.route("/api/admin/repost", methods=["POST"])
def api_admin_repost():
    return _mutate(op_repost)


def run_flask():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


# ═══════════════════════════════════════════════════════════════
#  MESSAGE TEMPLATES
# ═══════════════════════════════════════════════════════════════

def welcome_text(first_name):
    return (
        f"🎬 <b>{h(BRAND.upper())}</b>\n{DIV}\n"
        f"👋 Welcome, <b>{h(first_name)}</b>!\n\n"
        f"Aapki premium movie library — <b>movies, series aur direct "
        f"download links</b>, bina kisi wait ke.\n\n"
        f"<blockquote>⚡ Instant downloads\n🎞 HD • 4K quality\n"
        f"🆕 Regular updates\n🔒 Members-only access</blockquote>\n\n"
        f"👇 App kholen ya channel library browse karein."
    )


def welcome_kb(channel_link):
    rows = []
    if WEB_APP_URL:
        rows.append([InlineKeyboardButton("🎬 Open KNL Movies App",
                                          web_app=WebAppInfo(url=WEB_APP_URL))])
    rows.append([InlineKeyboardButton("📚 Browse Movies Library", url=channel_link)])
    return InlineKeyboardMarkup(rows)


def join_gate_text(movie=None):
    head = f"🔒 <b>MEMBERS ONLY ACCESS</b>\n{DIV}\n"
    if movie:
        head += f"🎬 <b>{h(movie['name'])}</b> aapke liye ready hai.\n\n"
    return (head +
            f"Library sirf channel members ke liye unlock hoti hai.\n\n"
            f"<b>Unlock kaise karein:</b>\n"
            f"1️⃣ <b>Join Channel</b> dabayein\n"
            f"2️⃣ Channel join karein\n"
            f"3️⃣ Wapas aakar <b>✅ I Joined</b> dabayein\n\n"
            f"<blockquote>⚡ Join karte hi poori library instant unlock</blockquote>")


def join_gate_kb(channel_link, ctx):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=channel_link)],
        [InlineKeyboardButton("✅ I Joined", callback_data=f"joinok_{ctx}")],
    ])


def download_card(movie):
    meta = movie_meta_line(movie)
    text = (f"🎬 <b>{h(movie['name'])}</b>\n{DIV}\n"
            f"📦 <b>Size:</b> <code>{h(movie.get('size', '-'))}</code>\n"
            f"🎞 <b>Quality:</b> <code>{h(movie.get('quality', '-'))}</code>\n")
    if meta:
        text += f"{meta}\n"
    if movie.get("genres"):
        text += f"🏷 <i>{h(', '.join(movie['genres']))}</i>\n"
    text += (f"{DIV}\n✅ <b>Access granted</b> — download link ready hai.\n\n"
             f"<blockquote>⬇️ Neeche button se download shuru karein</blockquote>")
    return text


def unavailable_text():
    return (f"⚠️ <b>CONTENT UNAVAILABLE</b>\n{DIV}\n"
            f"Yeh title abhi library mein nahi mila — ho sakta hai remove ho gaya ho.\n\n"
            f"<blockquote>📚 Channel mein latest titles dekhein</blockquote>")


async def reply(update, text, kb=None):
    await update.effective_message.reply_text(
        frame(text), parse_mode=ParseMode.HTML, reply_markup=kb,
        disable_web_page_preview=True)


async def edit(update, text, kb=None):
    await update.callback_query.edit_message_text(
        frame(text), parse_mode=ParseMode.HTML, reply_markup=kb,
        disable_web_page_preview=True)


async def show_welcome(update, context, via_edit=False):
    link = await get_channel_link(context.bot)
    text, kb = welcome_text(update.effective_user.first_name or "Member"), welcome_kb(link)
    await (edit(update, text, kb) if via_edit else reply(update, text, kb))


async def show_download(update, context, movie, via_edit=False):
    link = await get_channel_link(context.bot)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download Now", url=movie["link"])],
        [InlineKeyboardButton("📚 Browse Movies Library", url=link)],
    ])
    await (edit(update, download_card(movie), kb) if via_edit
           else reply(update, download_card(movie), kb))


# ═══════════════════════════════════════════════════════════════
#  USER FLOW
# ═══════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    uid = update.effective_user.id
    member = await check_membership(context.bot, uid)

    if context.args:
        arg = context.args[0]
        if arg.startswith("movie_"):
            mid = arg[len("movie_"):]
            movie = DB["movies"].get(mid)
            if not movie:
                link = await get_channel_link(context.bot)
                await reply(update, unavailable_text(), InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📚 Browse Movies Library", url=link)]]))
                return
            if not member:
                link = await get_channel_link(context.bot)
                await reply(update, join_gate_text(movie), join_gate_kb(link, mid))
                return
            await show_download(update, context, movie)
            return
        if arg in ("movies", "list"):
            await render_list(update, context)
            return

    if not member:
        link = await get_channel_link(context.bot)
        await reply(update, join_gate_text(), join_gate_kb(link, "home"))
        return
    await show_welcome(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    await reply(update, (
        f"❓ <b>HELP — {h(BRAND.upper())}</b>\n{DIV}\n"
        f"<b>Download kaise karein:</b>\n"
        f"1️⃣ App ya channel se title chunein\n"
        f"2️⃣ Channel join karein (agar nahi kiya)\n"
        f"3️⃣ Download link turant mil jayega\n\n"
        f"<blockquote>📚 Library: /movies</blockquote>"))


async def render_list(update: Update, context: ContextTypes.DEFAULT_TYPE, via_edit=False):
    movies = DB["movies"]
    link = await get_channel_link(context.bot)
    if not movies:
        text = (f"📭 <b>LIBRARY EMPTY</b>\n{DIV}\n"
                f"Abhi koi title available nahi.")
        if LOAD["state"] == "failed":
            text += f"\n\n<blockquote>🚨 Data load nahi hua — admin /recover chalaye</blockquote>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📚 Browse Movies Library", url=link)]])
        await (edit(update, text, kb) if via_edit else reply(update, text, kb))
        return
    items = sorted(movies.values(), key=lambda x: x.get("created", 0), reverse=True)
    buttons = [[InlineKeyboardButton(f"🎬 {m['name'][:38]}", callback_data=f"open_{m['id']}")]
               for m in items[:40]]
    buttons.append([InlineKeyboardButton("📚 Browse Movies Library", url=link)])
    text = (f"📚 <b>{h(BRAND.upper())} — LIBRARY</b>\n{DIV}\n"
            f"<blockquote>🎞 {len(movies)} titles available</blockquote>\n"
            f"👇 Title select karein")
    kb = InlineKeyboardMarkup(buttons)
    await (edit(update, text, kb) if via_edit else reply(update, text, kb))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    uid = update.effective_user.id

    if data.startswith("joinok_"):
        ctx = data[len("joinok_"):]
        if not await check_membership(context.bot, uid):
            await query.answer("❌ Abhi join nahi hua — pehle Join Channel dabayein",
                               show_alert=True)
            return
        await query.answer("✅ Membership confirmed")
        if ctx == "home":
            await show_welcome(update, context, via_edit=True)
        else:
            movie = DB["movies"].get(ctx)
            if movie:
                await show_download(update, context, movie, via_edit=True)
            else:
                await edit(update, unavailable_text())
        return

    if data.startswith("open_"):
        movie = DB["movies"].get(data[len("open_"):])
        if not movie:
            await query.answer("⚠️ Title nahi mila", show_alert=True)
            return
        if not await check_membership(context.bot, uid):
            link = await get_channel_link(context.bot)
            await query.answer()
            await edit(update, join_gate_text(movie), join_gate_kb(link, movie["id"]))
            return
        await query.answer("✅ Access granted")
        await show_download(update, context, movie, via_edit=True)
        return

    if data == "browse":
        await query.answer()
        await render_list(update, context, via_edit=True)
        return

    await query.answer()


# ═══════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════

PIPE_FIELDS = ["name", "link", "size", "quality", "poster", "backdrop",
               "type", "genres", "year", "rating", "duration",
               "language", "age", "cast", "description"]


def is_admin(update):
    return update.effective_user and update.effective_user.id in ADMIN_IDS


async def add_movie_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    if not is_admin(update):
        await reply(update, f"⛔ <b>ADMIN ONLY</b>\n{DIV}\nYeh command sirf admin ke liye hai.")
        return
    raw = (update.effective_message.text or "").split(maxsplit=1)
    if len(raw) < 2 or "|" not in raw[1]:
        await reply(update, (
            f"➕ <b>ADD TITLE</b>\n{DIV}\n"
            f"<code>/addmovie Name | link | size | quality | poster</code>\n\n"
            f"<blockquote>Full metadata ke liye mini app ka Admin panel use karein.</blockquote>"))
        return
    parts = [p.strip() for p in raw[1].split("|")]
    if len(parts) < 4:
        await reply(update, f"⚠️ <b>INVALID FORMAT</b>\n{DIV}\nName | link | size | quality")
        return
    data = {k: parts[i] for i, k in enumerate(PIPE_FIELDS) if i < len(parts)}
    data["post"] = True
    try:
        res = await op_add(data)
    except Exception as exc:
        await reply(update, f"🛑 <b>BLOCKED / ERROR</b>\n{DIV}\n<code>{h(exc)}</code>")
        return
    m = res["movie"]
    warn = ("\n\n<blockquote>⚠️ " + h("\n⚠️ ".join(res["warnings"])) + "</blockquote>") if res["warnings"] else ""
    await reply(update, (f"✅ <b>TITLE ADDED</b>\n{DIV}\n🎬 <b>{h(m['name'])}</b>\n"
                         f"🔖 ID: <code>{m['id']}</code>\n"
                         f"<blockquote>💾 Saved &amp; verified\n📢 Channel post done</blockquote>{warn}"))


async def add_json_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not is_admin(update):
        return
    raw = (update.effective_message.text or "").split(maxsplit=1)
    if len(raw) < 2:
        await reply(update, f"➕ <b>ADD JSON</b>\n{DIV}\n<code>/addjson {{\"name\":\"…\",\"link\":\"…\"}}</code>")
        return
    try:
        data = json.loads(raw[1])
    except ValueError as exc:
        await reply(update, f"⚠️ <b>JSON INVALID</b>\n{DIV}\n<code>{h(exc)}</code>")
        return
    data["post"] = data.get("post", True)
    try:
        res = await op_add(data)
    except Exception as exc:
        await reply(update, f"🛑 <b>BLOCKED / ERROR</b>\n{DIV}\n<code>{h(exc)}</code>")
        return
    await reply(update, f"✅ <b>ADDED</b>\n{DIV}\n🔖 <code>{res['movie']['id']}</code>")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not is_admin(update):
        return
    movies = DB["movies"]
    if not movies:
        await reply(update, f"📭 <b>LIBRARY EMPTY</b>\n{DIV}\nLoad state: <code>{LOAD['state']}</code>")
        return
    lines = [f"🗂 <b>DATABASE — {len(movies)} TITLES</b>\n{DIV}"]
    for m in sorted(movies.values(), key=lambda x: x.get("created", 0), reverse=True)[:60]:
        lines.append(f"🔖 <code>{m['id']}</code>  🎬 {h(m['name'])}")
    await reply(update, "\n".join(lines))


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not is_admin(update):
        return
    if not context.args:
        await reply(update, f"🗑 <b>DELETE</b>\n{DIV}\n<code>/delmovie &lt;id&gt;</code>")
        return
    try:
        res = await op_delete({"id": context.args[0]})
    except Exception as exc:
        await reply(update, f"🛑 <b>BLOCKED / ERROR</b>\n{DIV}\n<code>{h(exc)}</code>")
        return
    await reply(update, f"🗑 <b>REMOVED</b>\n{DIV}\n🔖 <code>{res['id']}</code>")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not is_admin(update):
        return
    saved = (time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(LOAD["saved_at"]))
             if LOAD["saved_at"] else "—")
    icon = {"ok": "✅", "fresh": "🆕", "failed": "🚨", "boot": "⏳"}.get(LOAD["state"], "❓")
    await reply(update, (
        f"📊 <b>SYSTEM STATUS</b>\n{DIV}\n"
        f"🎬 Titles: <code>{len(DB['movies'])}</code>\n"
        f"🛡 Load: {icon} <code>{LOAD['state']}</code>\n"
        f"📐 Baseline: <code>{LOAD['baseline']}</code>\n"
        f"✍️ Writes: <code>{'allowed' if writable()[0] else 'BLOCKED'}</code>\n"
        f"🕒 Last save: <code>{saved}</code>\n"
        f"📌 Pointer: <code>{LOAD['msg_id'] or '—'}</code>\n"
        f"🌐 App: <code>{h(WEB_APP_URL or 'set nahi')}</code>"
        + (f"\n<blockquote>⚠️ {h(LOAD['error'])}</blockquote>" if LOAD["error"] else "")))


async def save_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not is_admin(update):
        return
    force = bool(context.args and context.args[0].lower() == "force")
    st = await save_snapshot(context.bot, force=force)
    if st.get("ok"):
        await reply(update, (
            f"✅ <b>SNAPSHOT SAVED</b>\n{DIV}\n"
            f"🎬 Titles: <code>{len(DB['movies'])}</code>\n"
            f"📌 Pin: {'✅' if st.get('pinned') else '❌'}  "
            f"📝 Description: {'✅' if st.get('description') else '❌'}\n"
            f"<blockquote>Purane snapshots delete nahi hue — archive safe hai.</blockquote>"))
    else:
        await reply(update, (
            f"🛑 <b>SAVE BLOCKED</b>\n{DIV}\n{h(st.get('error', 'unknown'))}\n\n"
            f"<blockquote>Data loss rok diya gaya.\n"
            f"/recover chalao, ya janbujh kar kam karna hai to /save force</blockquote>"))


async def recover_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not is_admin(update):
        return
    await reply(update, f"🔎 <b>RECOVERING…</b>\n{DIV}\nPointers scan ho rahe hain…")
    before = len(DB["movies"])
    st = await load_snapshot(context.bot, announce=False)
    if st["state"] == "ok":
        await reply(update, (
            f"✅ <b>RECOVERED</b>\n{DIV}\n"
            f"🎬 Titles: <code>{len(DB['movies'])}</code> (pehle {before})\n"
            f"📌 Source: <code>{st['source']}</code>\n"
            f"<blockquote>Ab writes allowed hain. /save se confirm kar lo.</blockquote>"))
        return
    await reply(update, (
        f"🚨 <b>AUTO-RECOVER FAIL</b>\n{DIV}\n{h(st['error'])}\n\n"
        f"<b>Manual restore (100% kaam karta hai):</b>\n"
        f"1️⃣ Private database channel kholo\n"
        f"2️⃣ Koi bhi purana <code>library.json</code> dhoondo\n"
        f"3️⃣ Usko is bot mein <b>forward</b> kar do\n\n"
        f"<blockquote>Bot us file se poora data restore kar dega.\n"
        f"Backup DM mein bhi library.json milega.</blockquote>"))


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not is_admin(update):
        return
    if not DB["movies"]:
        await reply(update, f"📭 <b>NOTHING TO BACKUP</b>\n{DIV}\nLibrary khali hai.")
        return
    await dm_backup(context.bot)
    LOAD["last_backup"] = len(DB["movies"])
    await reply(update, (f"📦 <b>BACKUP SENT</b>\n{DIV}\n"
                         f"🎬 {len(DB['movies'])} titles\n"
                         f"<blockquote>File DM mein aa gayi. Isko safe rakho — "
                         f"kabhi bhi forward karke restore kar sakte ho.</blockquote>"))


async def import_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not is_admin(update):
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    # Forwarded library.json → full restore
    if msg.document:
        fname = (msg.document.file_name or "").lower()
        cap = (msg.caption or "").strip()
        if fname.endswith(".json") or cap.startswith(SNAP_MARK):
            try:
                payload = await _download_doc(context.bot, msg.document)
                res = await restore_payload(context.bot, payload, fname or "document")
            except Exception as exc:
                await reply(update, f"⚠️ <b>RESTORE FAIL</b>\n{DIV}\n<code>{h(exc)}</code>")
                return
            sv = res["save"]
            await reply(update, (
                f"♻️ <b>DATABASE RESTORED</b>\n{DIV}\n"
                f"🎬 Titles: <code>{res['restored']}</code> (pehle {res['before']})\n"
                f"💾 Save: {'✅ verified' if sv.get('ok') else '⚠️ ' + h(sv.get('error', ''))}\n"
                f"<blockquote>Ab /list se check karo. Data permanent save ho gaya.</blockquote>"))
            return

    text = (msg.text or "").strip()
    if not text.startswith("#MOVIE"):
        return
    try:
        res = await op_import(text)
    except Exception as exc:
        await reply(update, f"⚠️ <b>IMPORT FAIL</b>\n{DIV}\n<code>{h(exc)}</code>")
        return
    await reply(update, (f"{'🔄' if res['updated'] else '✅'} "
                         f"<b>{'UPDATED' if res['updated'] else 'IMPORTED'}</b>\n"
                         f"{DIV}\n🔖 <code>{res['id']}</code>"))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        # Same BOT_TOKEN se doosra process getUpdates kar raha hai (purana
        # deploy, duplicate service, ya local run). Bot khud retry karta
        # rehta hai, isliye admin ko har baar alert nahi bhejte.
        print("[poll] CONFLICT: same BOT_TOKEN se dusra bot instance chal raha "
              "hai — purana deploy/process band karein")
        return
    print("[error]", err)
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(
                aid, frame(f"⚠️ <b>BOT ERROR</b>\n{DIV}\n<code>{h(context.error)}</code>"),
                parse_mode=ParseMode.HTML)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  BOOT
# ═══════════════════════════════════════════════════════════════

async def post_init(application: Application):
    global BOT, MAIN_LOOP, _save_lock
    BOT = application.bot
    MAIN_LOOP = asyncio.get_running_loop()
    _save_lock = asyncio.Lock()

    # Polling se pehle koi webhook set ho to hata do, warna bot updates
    # receive nahi kar payega.
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception as exc:
        print("[boot] webhook clear fail:", exc)

    st = await load_snapshot(application.bot, announce=False)
    print(f"[boot] load={st['state']} titles={len(DB['movies'])}")

    if st["state"] == "failed":
        await alert_admins(
            f"🚨 <b>DATA PROTECTION ACTIVE</b>\n{DIV}\n{h(st['error'])}\n\n"
            f"<blockquote>Bot koi write nahi karega — purana data safe hai.\n\n"
            f"1️⃣ /recover chalao\n"
            f"2️⃣ Ya private channel se purana <code>library.json</code> "
            f"yahan forward kar do</blockquote>")
    elif st["state"] == "ok":
        for aid in ADMIN_IDS:
            try:
                await application.bot.send_message(
                    aid, frame(f"✅ <b>{h(BRAND.upper())} ONLINE</b>\n{DIV}\n"
                               f"🎬 Titles: <code>{len(DB['movies'])}</code>\n"
                               f"📌 Source: <code>{st['source']}</code>\n"
                               f"<blockquote>Data loaded &amp; write-protected.</blockquote>"),
                    parse_mode=ParseMode.HTML)
            except Exception:
                pass


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN set nahi hai")
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    threading.Thread(target=run_flask, daemon=True).start()
    print(f"[boot] flask port {os.environ.get('PORT', '5000')} | admins={sorted(ADMIN_IDS)}")

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("movies", render_list))
    application.add_handler(CommandHandler("addmovie", add_movie_cmd))
    application.add_handler(CommandHandler("addjson", add_json_cmd))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("delmovie", del_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("save", save_cmd))
    application.add_handler(CommandHandler("recover", recover_cmd))
    application.add_handler(CommandHandler("backup", backup_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(
        (filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, import_handler))
    application.add_error_handler(error_handler)

    print("[boot] polling started")
    application.run_polling(allowed_updates=Update.ALL_TYPES,
                            drop_pending_updates=True,
                            poll_interval=1.0, timeout=30)


if __name__ == "__main__":
    main()
