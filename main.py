import asyncio
import hashlib
import hmac
import html
import io
import json
import os
import threading
import time
import uuid
from urllib.parse import parse_qsl

from flask import Flask, jsonify, request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

# ═══════════════ CONFIG ═══════════════
BOT_TOKEN          = os.environ.get("BOT_TOKEN", "")
PUBLIC_CHANNEL_ID  = int(os.environ.get("PUBLIC_CHANNEL_ID", "0"))
PRIVATE_CHANNEL_ID = int(os.environ.get("PRIVATE_CHANNEL_ID", "0"))
BOT_USERNAME       = os.environ.get("BOT_USERNAME", "").lstrip("@")
BRAND              = os.environ.get("BRAND", "KNL Movies")
WEB_APP_URL        = (os.environ.get("WEB_APP_URL", "") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
LOG_ENTRIES        = os.environ.get("LOG_ENTRIES", "1") == "1"
INITDATA_TTL       = int(os.environ.get("INITDATA_TTL", "86400"))
KEEP_IDS           = int(os.environ.get("KEEP_IDS", "12"))
BACKUP_EVERY       = int(os.environ.get("BACKUP_EVERY", "5"))
ADMIN_LOGIN_ID     = os.environ.get("ADMIN_LOGIN_ID", "knl_admin")
ADMIN_LOGIN_PASS   = os.environ.get("ADMIN_LOGIN_PASS", "Knl#2026@Studio")
ADMIN_TOKEN_TTL    = int(os.environ.get("ADMIN_TOKEN_TTL", "43200"))


def _admin_ids():
    raw = os.environ.get("ADMIN_IDS", "") or os.environ.get("ADMIN_ID", "")
    return {int(p.strip()) for p in raw.replace(";", ",").split(",") if p.strip().isdigit()}


ADMIN_IDS = _admin_ids()
DIV, WIDTH = "━" * 22, "ㅤ" * 28
SNAP_MARK, SNAP_VER = "KNLDB", 6


def h(t): return html.escape(str(t if t is not None else ""), quote=False)
def frame(t): return f"{t}\n{WIDTH}"
def now(): return int(time.time())
def day_key(ts=None): return time.strftime("%Y-%m-%d", time.gmtime(ts or now()))


DB = {"movies": {}, "users": {}, "requests": [], "settings": {}}
SETTINGS_DEFAULT = {"hero_mode": "auto", "hero_manual": [], "top_mode": "auto",
                    "top_manual": [], "announcement": "", "requests_open": True}
LOAD = {"state": "boot", "baseline": 0, "authorized_loss": 0, "source": "none",
        "error": "", "msg_id": None, "saved_at": 0, "last_backup": 0}
BOT = MAIN_LOOP = _save_lock = None
DIRTY = False
THROTTLE = {}
LOGIN_TRIES = {}


def settings():
    for k, v in SETTINGS_DEFAULT.items():
        DB["settings"].setdefault(k, v() if callable(v) else (list(v) if isinstance(v, list) else v))
    return DB["settings"]


def writable():
    if LOAD["state"] in ("fresh", "ok"):
        return True, ""
    return False, "Database load nahi hua — save BLOCKED (data protection). /recover chalao ya purana library.json forward karo."


def shrink_ok(n):
    floor = max(0, LOAD["baseline"] - LOAD["authorized_loss"])
    if n >= floor:
        return True, ""
    return False, f"Save BLOCKED: {n} titles vs expected {floor}. /recover chalao ya /save force."


def mark_dirty():
    global DIRTY
    DIRTY = True


# ═══════════════ SNAPSHOT ═══════════════
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
            kv[k] = v
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
    for dest in [PRIVATE_CHANNEL_ID] + sorted(ADMIN_IDS):
        fwd = None
        try:
            fwd = await bot.forward_message(dest, PRIVATE_CHANNEL_ID, mid)
            if fwd.document:
                return await _download_doc(bot, fwd.document)
        except Exception as exc:
            print(f"[recover] {mid}@{dest}: {exc}")
        finally:
            if fwd:
                try:
                    await bot.delete_message(dest, fwd.message_id)
                except Exception:
                    pass
    return None


DEFAULTS = {"name": "Untitled", "link": "", "size": "-", "quality": "-", "poster": "",
            "backdrop": "", "type": "movie", "genres": list, "year": 0, "rating": 0.0,
            "age": "", "duration": "", "language": "", "description": "", "cast": list,
            "clicks": dict, "clicks_total": 0}


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


def _load_collections(payload):
    users = payload.get("users")
    DB["users"] = users if isinstance(users, dict) else {}
    reqs = payload.get("requests")
    DB["requests"] = reqs if isinstance(reqs, list) else []
    st = payload.get("settings")
    DB["settings"] = st if isinstance(st, dict) else {}
    settings()


async def load_snapshot(bot, announce=True):
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
            print(f"[load] getChat {attempt+1}: {exc}")
            await asyncio.sleep(1.5 * (attempt + 1))
    if chat is None:
        LOAD.update({"state": "failed", "error": "Channel reachable nahi. Save blocked."})
        return dict(LOAD)

    candidates = []
    pm = chat.pinned_message
    if pm and pm.document and (pm.caption or "").strip().startswith(SNAP_MARK):
        candidates.append(("pinned", pm.message_id, pm.document))
    di = _parse_desc(chat.description or "")
    if di:
        for mid in di["ids"]:
            if pm and mid == pm.message_id:
                continue
            candidates.append(("description", mid, None))

    if not candidates:
        DB["movies"] = {}
        _load_collections({})
        LOAD.update({"state": "fresh", "baseline": 0, "authorized_loss": 0,
                     "source": "fresh", "msg_id": None, "saved_at": 0})
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
            _load_collections(payload)
            LOAD.update({"state": "ok", "baseline": len(movies), "authorized_loss": 0,
                         "source": source, "msg_id": mid,
                         "saved_at": int(payload.get("saved_at") or 0), "error": ""})
            print(f"[load] {len(movies)} titles via {source}")
            if source != "pinned":
                await _repoint(bot, mid, len(movies))
            return dict(LOAD)
        except Exception as exc:
            print(f"[load] {source} {mid}: {exc}")

    LOAD.update({"state": "failed", "error": "Pointers mile lekin koi snapshot padha nahi gaya. Save blocked."})
    if announce:
        await alert_admins(f"🚨 <b>DATA PROTECTION ACTIVE</b>\n{DIV}\n{h(LOAD['error'])}\n\n"
                           f"<blockquote>/recover chalao ya purana library.json forward karo.</blockquote>")
    return dict(LOAD)


async def _repoint(bot, mid, count):
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
        print("[repoint] desc:", exc)


def _prune_clicks():
    keep = {day_key(now() - i * 86400) for i in range(7)}
    for m in DB["movies"].values():
        c = m.get("clicks") or {}
        m["clicks"] = {k: v for k, v in c.items() if k in keep}


async def save_snapshot(bot, force=False):
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
        _prune_clicks()
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

        payload = {"v": SNAP_VER, "saved_at": now(), "count": len(DB["movies"]),
                   "prev": prev_ids[:KEEP_IDS], "movies": DB["movies"],
                   "users": DB["users"], "requests": DB["requests"], "settings": DB["settings"]}
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        bio = io.BytesIO(raw)
        bio.name = "library.json"
        caption = (f"{SNAP_MARK} v{SNAP_VER}\nTitles: {len(DB['movies'])}\n"
                   f"Saved: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC")
        try:
            msg = await bot.send_document(PRIVATE_CHANNEL_ID, document=bio,
                                          filename="library.json", caption=caption,
                                          write_timeout=90, read_timeout=90)
        except Exception as exc:
            LOAD["error"] = f"Upload fail: {exc}"
            return {"ok": False, "error": LOAD["error"]}

        verified = False
        try:
            back = await _download_doc(bot, msg.document)
            verified = len(_normalize(back) or {}) == len(DB["movies"])
        except Exception as exc:
            print("[save] verify:", exc)
        if not verified:
            LOAD["error"] = "Verify fail — purana snapshot safe hai"
            await alert_admins(f"⚠️ <b>SAVE VERIFY FAILED</b>\n{DIV}\nPurana data untouched.")
            return {"ok": False, "error": LOAD["error"]}

        ids = [msg.message_id] + [i for i in prev_ids if i != msg.message_id]
        pinned = desc_ok = False
        try:
            await bot.pin_chat_message(PRIVATE_CHANNEL_ID, msg.message_id, disable_notification=True)
            pinned = True
        except Exception as exc:
            print("[save] pin:", exc)
        try:
            await bot.set_chat_description(PRIVATE_CHANNEL_ID, _desc_string(ids, len(DB["movies"])))
            desc_ok = True
        except Exception as exc:
            print("[save] desc:", exc)

        LOAD.update({"state": "ok", "baseline": len(DB["movies"]), "authorized_loss": 0,
                     "source": "pinned-snapshot", "msg_id": msg.message_id,
                     "saved_at": payload["saved_at"], "error": ""})
        if not pinned and not desc_ok:
            LOAD["error"] = "Pin + description dono fail — bot ko 'Pin Messages' + 'Change Channel Info' rights do"
            await alert_admins(f"🚨 <b>POINTER FAIL</b>\n{DIV}\n{h(LOAD['error'])}")
        global DIRTY
        DIRTY = False
        print(f"[save] {len(DB['movies'])} titles | pin={pinned} desc={desc_ok}")
        if len(DB["movies"]) - LOAD["last_backup"] >= BACKUP_EVERY:
            await dm_backup(bot)
            LOAD["last_backup"] = len(DB["movies"])
        return {"ok": True, "pinned": pinned, "description": desc_ok,
                "msg_id": msg.message_id, "error": LOAD["error"]}


async def dm_backup(bot):
    if not ADMIN_IDS:
        return
    raw = json.dumps({"v": SNAP_VER, "saved_at": now(), "movies": DB["movies"],
                      "users": DB["users"], "requests": DB["requests"],
                      "settings": DB["settings"]}, ensure_ascii=False, indent=1).encode("utf-8")
    for aid in ADMIN_IDS:
        try:
            bio = io.BytesIO(raw)
            bio.name = "library.json"
            await bot.send_document(aid, document=bio, filename="library.json",
                                    caption=f"{SNAP_MARK} backup\nTitles: {len(DB['movies'])}\nRestore: forward kar do.",
                                    write_timeout=90, read_timeout=90)
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


async def restore_payload(bot, payload, label="file"):
    movies = _normalize(payload)
    if movies is None:
        raise ValueError("Valid 'movies' data nahi hai")
    before = len(DB["movies"])
    DB["movies"] = movies
    _load_collections(payload)
    LOAD.update({"state": "ok", "baseline": len(movies), "authorized_loss": 0})
    res = await save_snapshot(bot, force=True)
    return {"restored": len(movies), "before": before, "save": res}


async def autosave_loop():
    while True:
        await asyncio.sleep(75)
        if DIRTY and writable()[0]:
            try:
                await save_snapshot(BOT)
            except Exception as exc:
                print("[autosave]", exc)


# ═══════════════ SCHEMA / OPS ═══════════════
def _as_list(val):
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        return [p.strip() for p in val.replace(";", ",").split(",") if p.strip()]
    return []


def clean_fields(data):
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
    return {"name": name, "link": link,
            "size": str(data.get("size", "-")).strip() or "-",
            "quality": str(data.get("quality", "-")).strip() or "-",
            "poster": poster, "backdrop": backdrop, "type": mtype,
            "genres": _as_list(data.get("genres")), "year": year, "rating": rating,
            "age": str(data.get("age", "")).strip(),
            "duration": str(data.get("duration", "")).strip(),
            "language": str(data.get("language", "")).strip(),
            "description": str(data.get("description", "")).strip(),
            "cast": _as_list(data.get("cast"))}, None


def public_view(m):
    return {"id": m["id"], "name": m.get("name", "Untitled"),
            "quality": m.get("quality", "-"), "poster": m.get("poster", ""),
            "backdrop": m.get("backdrop", ""), "type": m.get("type", "movie"),
            "genres": m.get("genres", []), "year": m.get("year", 0),
            "rating": m.get("rating", 0.0), "age": m.get("age", ""),
            "duration": m.get("duration", ""), "language": m.get("language", ""),
            "description": m.get("description", ""), "cast": m.get("cast", []),
            "created": m.get("created", 0)}


def top_today_ids():
    st = settings()
    if st["top_mode"] == "manual":
        return [i for i in st["top_manual"] if i in DB["movies"]][:10]
    today = day_key()
    ranked = sorted(DB["movies"].values(),
                    key=lambda m: (-(m.get("clicks", {}).get(today, 0)),
                                   -(m.get("clicks_total", 0)),
                                   -(m.get("rating", 0)), -(m.get("created", 0))))
    return [m["id"] for m in ranked[:10]]


def hero_ids():
    st = settings()
    if st["hero_mode"] == "manual":
        return [i for i in st["hero_manual"] if i in DB["movies"]][:6]
    ranked = sorted(DB["movies"].values(),
                    key=lambda m: (-(m.get("rating", 0)), -(m.get("created", 0))))
    withimg = [m["id"] for m in ranked if (m.get("backdrop") or m.get("poster"))]
    return (withimg or [m["id"] for m in ranked])[:5]


def guard_or_raise():
    ok, why = writable()
    if not ok:
        raise ValueError(why)


def movie_meta_line(m):
    bits = []
    if m.get("year"):
        bits.append(f"📅 {m['year']}")
    if m.get("rating"):
        bits.append(f"⭐ {m['rating']}")
    if m.get("duration"):
        bits.append(f"⏱ {h(m['duration'])}")
    return "  •  ".join(bits)


async def post_public(bot, movie):
    deep = f"https://t.me/{BOT_USERNAME}?start=movie_{movie['id']}" if BOT_USERNAME else ""
    meta = movie_meta_line(movie)
    caption = (f"🎬 <b>{h(movie['name'])}</b>\n{DIV}\n"
               f"📦 <b>Size:</b> <code>{h(movie['size'])}</code>\n"
               f"🎞 <b>Quality:</b> <code>{h(movie['quality'])}</code>\n")
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


async def log_entry(bot, movie):
    text = ("#MOVIE v2\n"
            f"ID: {movie['id']}\nNAME: {movie['name']}\nLINK: {movie['link']}\n"
            f"SIZE: {movie['size']}\nQUALITY: {movie['quality']}\nPOSTER: {movie['poster'] or '-'}")
    m = await bot.send_message(PRIVATE_CHANNEL_ID, text)
    return m.message_id


async def op_add(data):
    guard_or_raise()
    fields, err = clean_fields(data)
    if err:
        raise ValueError(err)
    mid = uuid.uuid4().hex[:8]
    movie = {"id": mid, **fields, "created": now(), "clicks": {}, "clicks_total": 0,
             "log_msg_id": None, "public_msg_id": None}
    DB["movies"][mid] = movie
    warns = []
    if LOG_ENTRIES and PRIVATE_CHANNEL_ID:
        try:
            movie["log_msg_id"] = await log_entry(BOT, movie)
        except Exception as exc:
            warns.append(f"Log: {exc}")
    if data.get("post") and PUBLIC_CHANNEL_ID:
        try:
            movie["public_msg_id"] = await post_public(BOT, movie)
        except Exception as exc:
            warns.append(f"Channel post: {exc}")
    st = await save_snapshot(BOT)
    if not st.get("ok"):
        warns.append(st.get("error") or "Save issue")
    return {"movie": movie, "warnings": warns}


async def op_update(data):
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
    return {"movie": movie, "warnings": [] if st.get("ok") else [st.get("error")]}


async def op_delete(data):
    guard_or_raise()
    mid = str(data.get("id", "")).strip()
    movie = DB["movies"].pop(mid, None)
    if not movie:
        raise ValueError("ID nahi mili")
    LOAD["authorized_loss"] += 1
    st = settings()
    st["hero_manual"] = [i for i in st["hero_manual"] if i != mid]
    st["top_manual"] = [i for i in st["top_manual"] if i != mid]
    warns = []
    for key, cid in (("log_msg_id", PRIVATE_CHANNEL_ID), ("public_msg_id", PUBLIC_CHANNEL_ID)):
        if movie.get(key) and cid:
            try:
                await BOT.delete_message(cid, movie[key])
            except Exception as exc:
                warns.append(f"{key}: {exc}")
    stt = await save_snapshot(BOT)
    if not stt.get("ok"):
        warns.append(stt.get("error"))
    return {"id": mid, "warnings": warns}


async def op_repost(data):
    guard_or_raise()
    movie = DB["movies"].get(str(data.get("id", "")).strip())
    if not movie:
        raise ValueError("ID nahi mili")
    if not PUBLIC_CHANNEL_ID:
        raise ValueError("PUBLIC_CHANNEL_ID set nahi hai")
    if movie.get("public_msg_id"):
        try:
            await BOT.delete_message(PUBLIC_CHANNEL_ID, movie["public_msg_id"])
        except Exception:
            pass
    movie["public_msg_id"] = await post_public(BOT, movie)
    await save_snapshot(BOT)
    return {"movie": movie, "warnings": []}


async def op_import(text):
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
        movie = {"id": mid, "created": now(), "clicks": {}, "clicks_total": 0,
                 "log_msg_id": None, "public_msg_id": None}
        for k, v in DEFAULTS.items():
            movie.setdefault(k, v() if callable(v) else v)
    movie.update({"name": fields.get("NAME", "Untitled"), "link": link,
                  "size": fields.get("SIZE", "-"), "quality": fields.get("QUALITY", "-"),
                  "poster": "" if poster in ("-", "") else poster})
    DB["movies"][mid] = movie
    if LOAD["state"] == "failed":
        LOAD.update({"state": "ok", "baseline": len(DB["movies"]), "authorized_loss": 0})
    await save_snapshot(BOT)
    return {"id": mid, "updated": existed}


# ═══════════════ USER DATA ═══════════════
def get_user(uid):
    u = DB["users"].get(str(uid))
    if not u:
        u = {"likes": [], "history": []}
        DB["users"][str(uid)] = u
    u.setdefault("likes", [])
    u.setdefault("history", [])
    return u


def track_click(uid, mid):
    m = DB["movies"].get(mid)
    if not m:
        return False
    key = (uid, mid)
    ts = now()
    if ts - THROTTLE.get(key, 0) < 45:
        return False
    THROTTLE[key] = ts
    if len(THROTTLE) > 8000:
        cutoff = ts - 300
        for k in list(THROTTLE):
            if THROTTLE[k] < cutoff:
                del THROTTLE[k]
    d = day_key()
    m.setdefault("clicks", {})
    m["clicks"][d] = m["clicks"].get(d, 0) + 1
    m["clicks_total"] = m.get("clicks_total", 0) + 1
    u = get_user(uid)
    hist = u["history"]
    if not (hist and hist[0]["id"] == mid and ts - hist[0]["ts"] < 300):
        hist.insert(0, {"id": mid, "ts": ts})
        del hist[60:]
    mark_dirty()
    return True


# ═══════════════ AUTH ═══════════════
def validate_init_data(init_data):
    if not init_data:
        return None, "initData missing — app Telegram ke andar kholen"
    if not BOT_TOKEN:
        return None, "BOT_TOKEN set nahi hai"
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None, "initData parse fail"
    recv = pairs.pop("hash", None)
    if not recv:
        return None, "hash missing"
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv):
        return None, "signature invalid"
    try:
        ad = int(pairs.get("auth_date", "0"))
    except ValueError:
        ad = 0
    if INITDATA_TTL and (time.time() - ad) > INITDATA_TTL:
        return None, "session expire — app dobara kholen"
    user = {}
    if pairs.get("user"):
        try:
            user = json.loads(pairs["user"])
        except ValueError:
            user = {}
    if not user.get("id"):
        return None, "user id missing"
    return {"user": user, "auth_date": ad}, None


def req_auth():
    init = request.headers.get("X-Init-Data", "")
    if not init:
        init = (request.get_json(silent=True) or {}).get("initData", "")
    return validate_init_data(init)


def make_admin_token(uid):
    exp = now() + ADMIN_TOKEN_TTL
    sig = hmac.new(BOT_TOKEN.encode(), f"adm|{uid}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{uid}.{exp}.{sig}"


def check_admin_token(token, uid):
    try:
        tuid, exp, sig = token.split(".")
        if int(tuid) != uid or int(exp) < now():
            return False
        want = hmac.new(BOT_TOKEN.encode(), f"adm|{tuid}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(want, sig)
    except Exception:
        return False


def bot_call(coro, timeout=120):
    if MAIN_LOOP is None:
        raise RuntimeError("Bot loop ready nahi")
    return asyncio.run_coroutine_threadsafe(coro, MAIN_LOOP).result(timeout)


async def check_membership(bot, uid):
    if not PUBLIC_CHANNEL_ID:
        return True
    try:
        m = await bot.get_chat_member(PUBLIC_CHANNEL_ID, uid)
        return m.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception as exc:
        print("[membership]", exc)
        return False


async def get_channel_link(bot):
    try:
        chat = await bot.get_chat(PUBLIC_CHANNEL_ID)
        if chat.username:
            return f"https://t.me/{chat.username}"
        if chat.invite_link:
            return chat.invite_link
        return await bot.export_chat_invite_link(PUBLIC_CHANNEL_ID)
    except Exception:
        return "https://t.me/"


# ═══════════════ FLASK ═══════════════
app = Flask(__name__)


@app.route("/")
def mini_app():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            body = f.read()
    except OSError:
        return "app.html missing", 500
    return body, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}


@app.route("/health")
def health():
    return f"OK movies={len(DB['movies'])} load={LOAD['state']}", 200


@app.route("/api/movies")
def api_movies():
    items = [public_view(m) for m in DB["movies"].values()]
    items.sort(key=lambda x: x.get("created", 0), reverse=True)
    st = settings()
    return jsonify({"ok": True, "brand": BRAND, "bot": BOT_USERNAME, "count": len(items),
                    "genres": sorted({g for m in items for g in m.get("genres", [])}),
                    "movies": items, "top10": top_today_ids(), "hero": hero_ids(),
                    "announcement": st["announcement"], "requests_open": st["requests_open"],
                    "load_state": LOAD["state"]})


@app.route("/api/session", methods=["POST"])
def api_session():
    auth, err = req_auth()
    if err:
        return jsonify({"ok": True, "authed": False, "is_admin": False, "reason": err,
                        "brand": BRAND, "bot": BOT_USERNAME})
    uid = int(auth["user"]["id"])
    return jsonify({"ok": True, "authed": True, "is_admin": uid in ADMIN_IDS,
                    "user": {"id": uid, "name": auth["user"].get("first_name", ""),
                             "username": auth["user"].get("username", ""),
                             "photo": auth["user"].get("photo_url", "")},
                    "brand": BRAND, "bot": BOT_USERNAME})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    auth, err = req_auth()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    uid = int(auth["user"]["id"])
    u = get_user(uid)
    mine = [r for r in DB["requests"] if r.get("user_id") == uid]
    return jsonify({"ok": True, "likes": u["likes"], "history": u["history"],
                    "requests": mine[:30]})


@app.route("/api/track", methods=["POST"])
def api_track():
    auth, err = req_auth()
    if err:
        return jsonify({"ok": False}), 401
    body = request.get_json(silent=True) or {}
    mid = str(body.get("id", "")).strip()
    counted = track_click(int(auth["user"]["id"]), mid)
    return jsonify({"ok": True, "counted": counted})


@app.route("/api/like", methods=["POST"])
def api_like():
    auth, err = req_auth()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    body = request.get_json(silent=True) or {}
    mid = str(body.get("id", "")).strip()
    if mid not in DB["movies"]:
        return jsonify({"ok": False, "error": "Title nahi mila"}), 404
    u = get_user(int(auth["user"]["id"]))
    if mid in u["likes"]:
        u["likes"].remove(mid)
        liked = False
    else:
        u["likes"].insert(0, mid)
        del u["likes"][200:]
        liked = True
    mark_dirty()
    return jsonify({"ok": True, "liked": liked, "likes": u["likes"]})


@app.route("/api/history/clear", methods=["POST"])
def api_history_clear():
    auth, err = req_auth()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    get_user(int(auth["user"]["id"]))["history"] = []
    mark_dirty()
    return jsonify({"ok": True})


@app.route("/api/request", methods=["POST"])
def api_request():
    auth, err = req_auth()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    st = settings()
    if not st["requests_open"]:
        return jsonify({"ok": False, "error": "Requests abhi band hain — baad mein try karein."}), 403
    body = request.get_json(silent=True) or {}
    title = str(body.get("title", "")).strip()[:90]
    if len(title) < 2:
        return jsonify({"ok": False, "error": "Movie ka naam likhein (2+ characters)."}), 400
    uid = int(auth["user"]["id"])
    today = day_key()
    mine_today = sum(1 for r in DB["requests"]
                     if r.get("user_id") == uid and day_key(r.get("created", 0)) == today)
    if mine_today >= 5:
        return jsonify({"ok": False, "error": "Aaj ki limit (5 requests) poori ho gayi. Kal phir try karein 🙏"}), 429
    dup = any(r.get("title", "").lower() == title.lower() and r.get("status") == "pending"
              for r in DB["requests"])
    rid = uuid.uuid4().hex[:8]
    DB["requests"].insert(0, {"id": rid, "title": title,
                              "year": str(body.get("year", "")).strip()[:4],
                              "note": str(body.get("note", "")).strip()[:200],
                              "user_id": uid,
                              "user_name": auth["user"].get("first_name", "User"),
                              "status": "pending", "created": now(),
                              "duplicate": dup})
    del DB["requests"][500:]

    async def notify():
        for aid in ADMIN_IDS:
            try:
                await BOT.send_message(aid, frame(
                    f"📥 <b>NEW MOVIE REQUEST</b>\n{DIV}\n"
                    f"🎬 <b>{h(title)}</b> {h('(' + body.get('year', '') + ')') if body.get('year') else ''}\n"
                    f"👤 {h(auth['user'].get('first_name', 'User'))}\n"
                    + (f"📝 {h(str(body.get('note', ''))[:200])}\n" if body.get("note") else "")
                    + f"<blockquote>Admin panel → Requests mein review karein.</blockquote>"),
                    parse_mode=ParseMode.HTML)
            except Exception:
                pass
    if MAIN_LOOP:
        MAIN_LOOP.create_task(notify())
    try:
        bot_call(save_snapshot(BOT), timeout=90)
    except Exception:
        mark_dirty()
    return jsonify({"ok": True, "id": rid, "duplicate": dup})


def _admin_guard():
    auth, err = req_auth()
    if err:
        return None, (jsonify({"ok": False, "error": err, "locked": True}), 401)
    uid = int(auth["user"]["id"])
    if uid not in ADMIN_IDS:
        return None, (jsonify({"ok": False, "error": "Admin only"}), 403)
    token = request.headers.get("X-Admin-Token", "")
    if not check_admin_token(token, uid):
        return None, (jsonify({"ok": False, "error": "Admin login required", "locked": True}), 401)
    return auth, None


@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    auth, err = req_auth()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    uid = int(auth["user"]["id"])
    if uid not in ADMIN_IDS:
        return jsonify({"ok": False, "error": "Admin only"}), 403
    tries = [t for t in LOGIN_TRIES.get(uid, []) if now() - t < 600]
    if len(tries) >= 6:
        return jsonify({"ok": False, "error": "Bahut zyada attempts — 10 min baad try karein."}), 429
    body = request.get_json(silent=True) or {}
    lid = str(body.get("login_id", "")).strip()
    pw = str(body.get("password", ""))
    ok = hmac.compare_digest(lid, ADMIN_LOGIN_ID) and hmac.compare_digest(pw, ADMIN_LOGIN_PASS)
    if not ok:
        tries.append(now())
        LOGIN_TRIES[uid] = tries
        return jsonify({"ok": False, "error": "Galat ID ya password."}), 403
    LOGIN_TRIES.pop(uid, None)
    return jsonify({"ok": True, "token": make_admin_token(uid), "expires": now() + ADMIN_TOKEN_TTL})


@app.route("/api/admin/status", methods=["POST"])
def api_admin_status():
    _, er = _admin_guard()
    if er:
        return er
    today = day_key()
    clicks_today = sum(m.get("clicks", {}).get(today, 0) for m in DB["movies"].values())
    clicks_total = sum(m.get("clicks_total", 0) for m in DB["movies"].values())
    top = sorted(DB["movies"].values(),
                 key=lambda m: (-m.get("clicks", {}).get(today, 0), -m.get("clicks_total", 0)))[:8]
    no_poster = sum(1 for m in DB["movies"].values() if not m.get("poster"))
    no_backdrop = sum(1 for m in DB["movies"].values() if not m.get("backdrop"))
    return jsonify({
        "ok": True, "titles": len(DB["movies"]),
        "movies_count": sum(1 for m in DB["movies"].values() if m.get("type") == "movie"),
        "series_count": sum(1 for m in DB["movies"].values() if m.get("type") == "series"),
        "clicks_today": clicks_today, "clicks_total": clicks_total,
        "requests_pending": sum(1 for r in DB["requests"] if r["status"] == "pending"),
        "requests_total": len(DB["requests"]),
        "users_active": len(DB["users"]),
        "no_poster": no_poster, "no_backdrop": no_backdrop,
        "top_today": [{"id": m["id"], "name": m["name"],
                       "today": m.get("clicks", {}).get(today, 0),
                       "total": m.get("clicks_total", 0)} for m in top],
        "storage": {"ok": LOAD["state"] == "ok" and not LOAD["error"],
                    "state": LOAD["state"], "source": LOAD["source"],
                    "saved_at": LOAD["saved_at"], "error": LOAD["error"],
                    "baseline": LOAD["baseline"]},
        "settings": settings()})


@app.route("/api/admin/movies", methods=["POST"])
def api_admin_movies():
    _, er = _admin_guard()
    if er:
        return er
    today = day_key()
    items = []
    for m in sorted(DB["movies"].values(), key=lambda x: x.get("created", 0), reverse=True):
        d = dict(m)
        d["today"] = m.get("clicks", {}).get(today, 0)
        items.append(d)
    return jsonify({"ok": True, "count": len(items), "movies": items})


def _mutate(op):
    _, er = _admin_guard()
    if er:
        return er
    body = request.get_json(silent=True) or {}
    try:
        res = bot_call(op(body), timeout=120)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, **res})


@app.route("/api/admin/add", methods=["POST"])
def admin_add():
    return _mutate(op_add)


@app.route("/api/admin/update", methods=["POST"])
def admin_update():
    return _mutate(op_update)


@app.route("/api/admin/delete", methods=["POST"])
def admin_delete():
    return _mutate(op_delete)


@app.route("/api/admin/repost", methods=["POST"])
def admin_repost():
    return _mutate(op_repost)


@app.route("/api/admin/curation", methods=["POST"])
def admin_curation():
    _, er = _admin_guard()
    if er:
        return er
    body = request.get_json(silent=True) or {}
    st = settings()
    if "hero_mode" in body:
        st["hero_mode"] = "manual" if body["hero_mode"] == "manual" else "auto"
    if "hero_manual" in body:
        st["hero_manual"] = [i for i in _as_list(body["hero_manual"]) if i in DB["movies"]][:6]
    if "top_mode" in body:
        st["top_mode"] = "manual" if body["top_mode"] == "manual" else "auto"
    if "top_manual" in body:
        st["top_manual"] = [i for i in _as_list(body["top_manual"]) if i in DB["movies"]][:10]
    mark_dirty()
    try:
        bot_call(save_snapshot(BOT), timeout=90)
    except Exception:
        pass
    return jsonify({"ok": True, "settings": st, "hero": hero_ids(), "top10": top_today_ids()})


@app.route("/api/admin/settings", methods=["POST"])
def admin_settings():
    _, er = _admin_guard()
    if er:
        return er
    body = request.get_json(silent=True) or {}
    st = settings()
    if "announcement" in body:
        st["announcement"] = str(body["announcement"]).strip()[:220]
    if "requests_open" in body:
        st["requests_open"] = bool(body["requests_open"])
    mark_dirty()
    try:
        bot_call(save_snapshot(BOT), timeout=90)
    except Exception:
        pass
    return jsonify({"ok": True, "settings": st})


@app.route("/api/admin/requests", methods=["POST"])
def admin_requests():
    _, er = _admin_guard()
    if er:
        return er
    return jsonify({"ok": True, "requests": DB["requests"][:200]})


@app.route("/api/admin/request_action", methods=["POST"])
def admin_request_action():
    _, er = _admin_guard()
    if er:
        return er
    body = request.get_json(silent=True) or {}
    rid = str(body.get("id", ""))
    action = str(body.get("action", ""))
    req = next((r for r in DB["requests"] if r["id"] == rid), None)
    if not req:
        return jsonify({"ok": False, "error": "Request nahi mili"}), 404
    if action == "delete":
        DB["requests"] = [r for r in DB["requests"] if r["id"] != rid]
    elif action in ("pending", "added", "rejected"):
        req["status"] = action
        req["acted"] = now()
        if action == "added" and BOT and MAIN_LOOP:
            async def tell():
                try:
                    await BOT.send_message(req["user_id"], frame(
                        f"🎉 <b>REQUEST PURI HUI!</b>\n{DIV}\n"
                        f"🎬 <b>{h(req['title'])}</b> ab library mein available hai.\n\n"
                        f"<blockquote>App kholo aur download karo ⚡</blockquote>"),
                        parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            MAIN_LOOP.create_task(tell())
    else:
        return jsonify({"ok": False, "error": "Unknown action"}), 400
    mark_dirty()
    try:
        bot_call(save_snapshot(BOT), timeout=90)
    except Exception:
        pass
    return jsonify({"ok": True})


def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")),
            debug=False, use_reloader=False, threaded=True)


# ═══════════════ BOT ═══════════════
def welcome_text(name):
    return (f"🎬 <b>{h(BRAND.upper())}</b>\n{DIV}\n👋 Welcome, <b>{h(name)}</b>!\n\n"
            f"Aapki premium movie library — <b>movies, series aur direct download links</b>.\n\n"
            f"<blockquote>⚡ Instant downloads\n🎞 HD • 4K quality\n🆕 Regular updates\n"
            f"🔒 Members-only access</blockquote>\n\n👇 App kholen ya library browse karein.")


def welcome_kb(link):
    rows = []
    if WEB_APP_URL:
        rows.append([InlineKeyboardButton("🎬 Open KNL Movies App", web_app=WebAppInfo(url=WEB_APP_URL))])
    rows.append([InlineKeyboardButton("📚 Browse Movies Library", url=link)])
    return InlineKeyboardMarkup(rows)


def join_gate_text(movie=None):
    head = f"🔒 <b>MEMBERS ONLY ACCESS</b>\n{DIV}\n"
    if movie:
        head += f"🎬 <b>{h(movie['name'])}</b> aapke liye ready hai.\n\n"
    return (head + f"Library sirf channel members ke liye unlock hoti hai.\n\n"
            f"<b>Unlock kaise karein:</b>\n1️⃣ <b>Join Channel</b> dabayein\n"
            f"2️⃣ Channel join karein\n3️⃣ Wapas aakar <b>✅ I Joined</b> dabayein\n\n"
            f"<blockquote>⚡ Join karte hi poori library instant unlock</blockquote>")


def join_gate_kb(link, ctx):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=link)],
        [InlineKeyboardButton("✅ I Joined", callback_data=f"joinok_{ctx}")]])


def download_card(m):
    meta = movie_meta_line(m)
    t = (f"🎬 <b>{h(m['name'])}</b>\n{DIV}\n"
         f"📦 <b>Size:</b> <code>{h(m.get('size', '-'))}</code>\n"
         f"🎞 <b>Quality:</b> <code>{h(m.get('quality', '-'))}</code>\n")
    if meta:
        t += f"{meta}\n"
    if m.get("genres"):
        t += f"🏷 <i>{h(', '.join(m['genres']))}</i>\n"
    t += f"{DIV}\n✅ <b>Access granted</b>\n\n<blockquote>⬇️ Neeche button se download karein</blockquote>"
    return t


def unavailable_text():
    return (f"⚠️ <b>CONTENT UNAVAILABLE</b>\n{DIV}\nYeh title library mein nahi mila.\n\n"
            f"<blockquote>📚 Channel mein latest titles dekhein</blockquote>")


async def reply(u, t, kb=None):
    await u.effective_message.reply_text(frame(t), parse_mode=ParseMode.HTML,
                                         reply_markup=kb, disable_web_page_preview=True)


async def edit(u, t, kb=None):
    await u.callback_query.edit_message_text(frame(t), parse_mode=ParseMode.HTML,
                                             reply_markup=kb, disable_web_page_preview=True)


async def show_welcome(u, c, via_edit=False):
    link = await get_channel_link(c.bot)
    t, kb = welcome_text(u.effective_user.first_name or "Member"), welcome_kb(link)
    await (edit(u, t, kb) if via_edit else reply(u, t, kb))


async def show_download(u, c, m, via_edit=False):
    link = await get_channel_link(c.bot)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download Now", url=m["link"])],
        [InlineKeyboardButton("📚 Browse Movies Library", url=link)]])
    await (edit(u, download_card(m), kb) if via_edit else reply(u, download_card(m), kb))


async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.effective_message:
        return
    uid = u.effective_user.id
    member = await check_membership(c.bot, uid)
    if u.effective_message.text and "/start" in u.effective_message.text:
        pass
    if c.args:
        arg = c.args[0]
        if arg.startswith("movie_"):
            m = DB["movies"].get(arg[6:])
            if not m:
                link = await get_channel_link(c.bot)
                await reply(u, unavailable_text(), InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📚 Browse Movies Library", url=link)]]))
                return
            if not member:
                link = await get_channel_link(c.bot)
                await reply(u, join_gate_text(m), join_gate_kb(link, m["id"]))
                return
            await show_download(u, c, m)
            return
    if not member:
        link = await get_channel_link(c.bot)
        await reply(u, join_gate_text(), join_gate_kb(link, "home"))
        return
    await show_welcome(u, c)


async def help_cmd(u, c):
    if u.effective_message:
        await reply(u, f"❓ <b>HELP — {h(BRAND.upper())}</b>\n{DIV}\n"
                       f"1️⃣ App ya channel se title chunein\n2️⃣ Channel join karein\n"
                       f"3️⃣ Download link turant milega\n\n<blockquote>📚 /movies • 🎬 /start</blockquote>")


async def render_list(u, c, via_edit=False):
    movies = DB["movies"]
    link = await get_channel_link(c.bot)
    if not movies:
        t = f"📭 <b>LIBRARY EMPTY</b>\n{DIV}\nAbhi koi title nahi."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📚 Browse Movies Library", url=link)]])
        await (edit(u, t, kb) if via_edit else reply(u, t, kb))
        return
    items = sorted(movies.values(), key=lambda x: x.get("created", 0), reverse=True)
    btns = [[InlineKeyboardButton(f"🎬 {m['name'][:38]}", callback_data=f"open_{m['id']}")] for m in items[:40]]
    btns.append([InlineKeyboardButton("📚 Browse Movies Library", url=link)])
    t = (f"📚 <b>{h(BRAND.upper())} — LIBRARY</b>\n{DIV}\n"
         f"<blockquote>🎞 {len(movies)} titles</blockquote>\n👇 Select karein")
    await (edit(u, t, InlineKeyboardMarkup(btns)) if via_edit else reply(u, t, InlineKeyboardMarkup(btns)))


async def button_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    if not q or not q.data:
        return
    uid = u.effective_user.id
    if q.data.startswith("joinok_"):
        ctx = q.data[7:]
        if not await check_membership(c.bot, uid):
            await q.answer("❌ Abhi join nahi hua — pehle Join Channel dabayein", show_alert=True)
            return
        await q.answer("✅ Membership confirmed")
        if ctx == "home":
            await show_welcome(u, c, True)
        else:
            m = DB["movies"].get(ctx)
            if m:
                await show_download(u, c, m, True)
            else:
                await edit(u, unavailable_text())
        return
    if q.data.startswith("open_"):
        m = DB["movies"].get(q.data[5:])
        if not m:
            await q.answer("⚠️ Title nahi mila", show_alert=True)
            return
        if not await check_membership(c.bot, uid):
            link = await get_channel_link(c.bot)
            await q.answer()
            await edit(u, join_gate_text(m), join_gate_kb(link, m["id"]))
            return
        await q.answer("✅ Access granted")
        await show_download(u, c, m, True)
        return
    await q.answer()


PIPE_FIELDS = ["name", "link", "size", "quality", "poster", "backdrop", "type", "genres",
               "year", "rating", "duration", "language", "age", "cast", "description"]


def is_admin(u):
    return u.effective_user and u.effective_user.id in ADMIN_IDS


async def add_movie_cmd(u, c):
    if not u.effective_message:
        return
    if not is_admin(u):
        await reply(u, f"⛔ <b>ADMIN ONLY</b>\n{DIV}")
        return
    raw = (u.effective_message.text or "").split(maxsplit=1)
    if len(raw) < 2 or "|" not in raw[1]:
        await reply(u, f"➕ <b>ADD TITLE</b>\n{DIV}\n<code>/addmovie Name | link | size | quality | poster</code>\n\n"
                       f"<blockquote>Full control: mini app → Admin panel</blockquote>")
        return
    parts = [p.strip() for p in raw[1].split("|")]
    if len(parts) < 4:
        await reply(u, f"⚠️ Name | link | size | quality chahiye")
        return
    data = {k: parts[i] for i, k in enumerate(PIPE_FIELDS) if i < len(parts)}
    data["post"] = True
    try:
        res = await op_add(data)
    except Exception as exc:
        await reply(u, f"🛑 <b>ERROR</b>\n{DIV}\n<code>{h(exc)}</code>")
        return
    m = res["movie"]
    warn = ("\n\n<blockquote>⚠️ " + h("\n⚠️ ".join(res["warnings"])) + "</blockquote>") if res["warnings"] else ""
    await reply(u, f"✅ <b>ADDED</b>\n{DIV}\n🎬 <b>{h(m['name'])}</b>\n🔖 <code>{m['id']}</code>{warn}")


async def list_cmd(u, c):
    if not u.effective_message or not is_admin(u):
        return
    if not DB["movies"]:
        await reply(u, f"📭 <b>EMPTY</b>\n{DIV}\nLoad: <code>{LOAD['state']}</code>")
        return
    lines = [f"🗂 <b>{len(DB['movies'])} TITLES</b>\n{DIV}"]
    for m in sorted(DB["movies"].values(), key=lambda x: x.get("created", 0), reverse=True)[:60]:
        lines.append(f"🔖 <code>{m['id']}</code> 🎬 {h(m['name'])}")
    await reply(u, "\n".join(lines))


async def del_cmd(u, c):
    if not u.effective_message or not is_admin(u):
        return
    if not c.args:
        await reply(u, f"🗑 <code>/delmovie &lt;id&gt;</code>")
        return
    try:
        res = await op_delete({"id": c.args[0]})
    except Exception as exc:
        await reply(u, f"🛑 <code>{h(exc)}</code>")
        return
    await reply(u, f"🗑 <b>REMOVED</b>\n{DIV}\n<code>{res['id']}</code>")


async def status_cmd(u, c):
    if not u.effective_message or not is_admin(u):
        return
    saved = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(LOAD["saved_at"])) if LOAD["saved_at"] else "—"
    icon = {"ok": "✅", "fresh": "🆕", "failed": "🚨", "boot": "⏳"}.get(LOAD["state"], "❓")
    await reply(u, (f"📊 <b>STATUS</b>\n{DIV}\n🎬 Titles: <code>{len(DB['movies'])}</code>\n"
                    f"🛡 Load: {icon} <code>{LOAD['state']}</code>\n"
                    f"✍️ Writes: <code>{'allowed' if writable()[0] else 'BLOCKED'}</code>\n"
                    f"🕒 Save: <code>{saved}</code>\n📥 Requests: <code>{len(DB['requests'])}</code>\n"
                    f"👥 Users: <code>{len(DB['users'])}</code>"
                    + (f"\n<blockquote>⚠️ {h(LOAD['error'])}</blockquote>" if LOAD["error"] else "")))


async def save_cmd(u, c):
    if not u.effective_message or not is_admin(u):
        return
    force = bool(c.args and c.args[0].lower() == "force")
    st = await save_snapshot(c.bot, force=force)
    if st.get("ok"):
        await reply(u, f"✅ <b>SAVED</b>\n{DIV}\n🎬 <code>{len(DB['movies'])}</code>\n"
                       f"📌 {'✅' if st.get('pinned') else '❌'} 📝 {'✅' if st.get('description') else '❌'}")
    else:
        await reply(u, f"🛑 <b>BLOCKED</b>\n{DIV}\n{h(st.get('error', ''))}\n\n"
                       f"<blockquote>/recover ya /save force</blockquote>")


async def recover_cmd(u, c):
    if not u.effective_message or not is_admin(u):
        return
    await reply(u, f"🔎 <b>RECOVERING…</b>\n{DIV}")
    st = await load_snapshot(c.bot, announce=False)
    if st["state"] == "ok":
        await reply(u, f"✅ <b>RECOVERED</b>\n{DIV}\n🎬 <code>{len(DB['movies'])}</code>\n"
                       f"📌 <code>{st['source']}</code>")
    else:
        await reply(u, f"🚨 <b>FAIL</b>\n{DIV}\n{h(st['error'])}\n\n"
                       f"<blockquote>Purana library.json bot mein forward karo — full restore ho jayega.</blockquote>")


async def backup_cmd(u, c):
    if not u.effective_message or not is_admin(u):
        return
    if not DB["movies"]:
        await reply(u, f"📭 <b>NOTHING TO BACKUP</b>")
        return
    await dm_backup(c.bot)
    LOAD["last_backup"] = len(DB["movies"])
    await reply(u, f"📦 <b>BACKUP SENT</b>\n{DIV}\n🎬 {len(DB['movies'])} titles")


async def import_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = u.effective_message
    if not msg or not is_admin(u) or u.effective_chat.type != ChatType.PRIVATE:
        return
    if msg.document:
        fname = (msg.document.file_name or "").lower()
        if fname.endswith(".json") or (msg.caption or "").strip().startswith(SNAP_MARK):
            try:
                payload = await _download_doc(c.bot, msg.document)
                res = await restore_payload(c.bot, payload)
            except Exception as exc:
                await reply(u, f"⚠️ <b>RESTORE FAIL</b>\n{DIV}\n<code>{h(exc)}</code>")
                return
            sv = res["save"]
            await reply(u, f"♻️ <b>RESTORED</b>\n{DIV}\n🎬 <code>{res['restored']}</code> (pehle {res['before']})\n"
                           f"💾 {'✅ verified' if sv.get('ok') else '⚠️ ' + h(sv.get('error', ''))}")
            return
    text = (msg.text or "").strip()
    if not text.startswith("#MOVIE"):
        return
    try:
        res = await op_import(text)
    except Exception as exc:
        await reply(u, f"⚠️ <code>{h(exc)}</code>")
        return
    await reply(u, f"{'🔄' if res['updated'] else '✅'} <code>{res['id']}</code>")


async def error_handler(u, c):
    print("[error]", c.error)
    for aid in ADMIN_IDS:
        try:
            await c.bot.send_message(aid, frame(f"⚠️ <b>BOT ERROR</b>\n{DIV}\n<code>{h(c.error)}</code>"),
                                     parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def post_init(application):
    global BOT, MAIN_LOOP, _save_lock
    BOT = application.bot
    MAIN_LOOP = asyncio.get_running_loop()
    _save_lock = asyncio.Lock()
    st = await load_snapshot(application.bot, announce=False)
    print(f"[boot] load={st['state']} titles={len(DB['movies'])}")
    MAIN_LOOP.create_task(autosave_loop())
    if st["state"] == "failed":
        await alert_admins(f"🚨 <b>DATA PROTECTION ACTIVE</b>\n{DIV}\n{h(st['error'])}\n\n"
                           f"<blockquote>/recover ya purana library.json forward karo.</blockquote>")
    elif st["state"] == "ok":
        for aid in ADMIN_IDS:
            try:
                await application.bot.send_message(aid, frame(
                    f"✅ <b>{h(BRAND.upper())} ONLINE</b>\n{DIV}\n"
                    f"🎬 Titles: <code>{len(DB['movies'])}</code>\n"
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
    print(f"[boot] flask | admins={sorted(ADMIN_IDS)}")
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("movies", render_list))
    application.add_handler(CommandHandler("addmovie", add_movie_cmd))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("delmovie", del_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("save", save_cmd))
    application.add_handler(CommandHandler("recover", recover_cmd))
    application.add_handler(CommandHandler("backup", backup_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, import_handler))
    application.add_error_handler(error_handler)
    print("[boot] polling started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
