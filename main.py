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


def _admin_ids():
    raw = os.environ.get("ADMIN_IDS", "") or os.environ.get("ADMIN_ID", "")
    out = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


ADMIN_IDS = _admin_ids()

# ── Message styling ──
DIV   = "━" * 22
WIDTH = "ㅤ" * 28          # invisible width-line: har bubble same width


def h(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def frame(text: str) -> str:
    return f"{text}\n{WIDTH}"


SNAP_MARK = "KNLDB"
SNAP_VER = 4

DB = {"movies": {}}
STORAGE = {"ok": False, "source": "none", "saved_at": 0, "error": ""}
BOT = None
MAIN_LOOP = None
_snap_msg_id = None
_save_lock = None
_save_task = None


def now() -> int:
    return int(time.time())


# ═══════════════════════════════════════════════════════════════
#  STORAGE — pinned JSON snapshot in private channel
# ═══════════════════════════════════════════════════════════════

async def load_snapshot(bot) -> None:
    global _snap_msg_id
    STORAGE.update({"ok": False, "source": "none", "error": ""})

    if not PRIVATE_CHANNEL_ID:
        STORAGE["error"] = "PRIVATE_CHANNEL_ID set nahi hai"
        print("[storage]", STORAGE["error"])
        return

    try:
        chat = await bot.get_chat(PRIVATE_CHANNEL_ID)
    except Exception as exc:
        STORAGE["error"] = f"getChat fail: {exc}"
        print("[storage]", STORAGE["error"])
        return

    pm = chat.pinned_message
    if not pm:
        STORAGE["error"] = "Koi pinned message nahi — pehli movie add karte hi ban jayega"
        print("[storage]", STORAGE["error"])
        return

    cap = (pm.caption or pm.text or "").strip()
    if not pm.document or not cap.startswith(SNAP_MARK):
        STORAGE["error"] = "Pinned message database snapshot nahi hai"
        print("[storage]", STORAGE["error"])
        return

    try:
        tg_file = await bot.get_file(pm.document.file_id)
        raw = await tg_file.download_as_bytearray()
        payload = json.loads(bytes(raw).decode("utf-8"))
    except Exception as exc:
        STORAGE["error"] = f"Snapshot read fail: {exc}"
        print("[storage]", STORAGE["error"])
        return

    movies = payload.get("movies") or {}
    if not isinstance(movies, dict):
        STORAGE["error"] = "Snapshot corrupt"
        return

    for mid, m in movies.items():
        m.setdefault("id", mid)
        for k, v in DEFAULTS.items():
            m.setdefault(k, v() if callable(v) else v)

    DB["movies"] = movies
    _snap_msg_id = pm.message_id
    STORAGE.update({
        "ok": True, "source": "pinned-snapshot",
        "saved_at": int(payload.get("saved_at") or 0), "error": "",
    })
    print(f"[storage] {len(movies)} titles restored from pinned snapshot")


async def save_snapshot(bot) -> dict:
    global _snap_msg_id
    if not PRIVATE_CHANNEL_ID:
        STORAGE["error"] = "PRIVATE_CHANNEL_ID set nahi hai"
        return dict(STORAGE)

    async with _save_lock:
        payload = {"v": SNAP_VER, "saved_at": now(), "movies": DB["movies"]}
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
                write_timeout=60, read_timeout=60,
            )
        except Exception as exc:
            STORAGE["error"] = f"Snapshot upload fail: {exc}"
            print("[storage]", STORAGE["error"])
            return dict(STORAGE)

        pinned = False
        try:
            await bot.pin_chat_message(
                PRIVATE_CHANNEL_ID, msg.message_id, disable_notification=True
            )
            pinned = True
        except Exception as exc:
            STORAGE["error"] = f"Pin fail (bot ko 'Pin Messages' right do): {exc}"
            print("[storage]", STORAGE["error"])

        old = _snap_msg_id
        _snap_msg_id = msg.message_id
        if pinned and old and old != msg.message_id:
            try:
                await bot.delete_message(PRIVATE_CHANNEL_ID, old)
            except Exception as exc:
                print("[storage] old snapshot delete:", exc)

        STORAGE.update({
            "ok": pinned, "source": "pinned-snapshot",
            "saved_at": payload["saved_at"],
            "error": "" if pinned else STORAGE["error"],
        })
        print(f"[storage] saved {len(DB['movies'])} titles | pinned={pinned}")
        return dict(STORAGE)


def schedule_save(delay: float = 3.0) -> None:
    global _save_task
    if MAIN_LOOP is None:
        return
    if _save_task and not _save_task.done():
        _save_task.cancel()
    _save_task = MAIN_LOOP.create_task(_delayed_save(delay))


async def _delayed_save(delay: float):
    try:
        await asyncio.sleep(delay)
        await save_snapshot(BOT)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print("[storage] delayed save:", exc)


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
    deep = (
        f"https://t.me/{BOT_USERNAME}?start=movie_{movie['id']}"
        if BOT_USERNAME else ""
    )
    meta = movie_meta_line(movie)
    caption = (
        f"🎬 <b>{h(movie['name'])}</b>\n"
        f"{DIV}\n"
        f"📦 <b>Size:</b> <code>{h(movie['size'])}</code>\n"
        f"🎞 <b>Quality:</b> <code>{h(movie['quality'])}</code>\n"
    )
    if meta:
        caption += f"{meta}\n"
    if movie.get("genres"):
        caption += f"🏷 <i>{h(', '.join(movie['genres']))}</i>\n"
    caption += (
        f"{DIV}\n"
        f"⬇️ <b>Download</b> — button dabayein, link bot mein milega."
    )
    kb = (
        InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ DOWNLOAD", url=deep)]])
        if deep else None
    )
    image = movie.get("backdrop") or movie.get("poster")
    if image:
        m = await bot.send_photo(
            PUBLIC_CHANNEL_ID, photo=image, caption=frame(caption),
            parse_mode=ParseMode.HTML, reply_markup=kb,
        )
    else:
        m = await bot.send_message(
            PUBLIC_CHANNEL_ID, frame(caption),
            parse_mode=ParseMode.HTML, reply_markup=kb,
        )
    return m.message_id


async def log_entry(bot, movie: dict) -> int:
    text = (
        "#MOVIE v2\n"
        f"ID: {movie['id']}\n"
        f"NAME: {movie['name']}\n"
        f"LINK: {movie['link']}\n"
        f"SIZE: {movie['size']}\n"
        f"QUALITY: {movie['quality']}\n"
        f"POSTER: {movie['poster'] or '-'}"
    )
    m = await bot.send_message(PRIVATE_CHANNEL_ID, text)
    return m.message_id


# ═══════════════════════════════════════════════════════════════
#  OPS
# ═══════════════════════════════════════════════════════════════

async def op_add(data: dict) -> dict:
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
        warns.append(st.get("error") or "Snapshot pin nahi hua")
    return {"movie": movie, "warnings": warns}


async def op_update(data: dict) -> dict:
    mid = str(data.get("id", "")).strip()
    movie = DB["movies"].get(mid)
    if not movie:
        raise ValueError("ID nahi mili")
    fields, err = clean_fields(data)
    if err:
        raise ValueError(err)
    movie.update(fields)
    movie["updated"] = now()
    warns = []
    st = await save_snapshot(BOT)
    if not st.get("ok"):
        warns.append(st.get("error") or "Snapshot pin nahi hua")
    return {"movie": movie, "warnings": warns}


async def op_delete(data: dict) -> dict:
    mid = str(data.get("id", "")).strip()
    movie = DB["movies"].pop(mid, None)
    if not movie:
        raise ValueError("ID nahi mili")
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
        warns.append(st.get("error") or "Snapshot pin nahi hua")
    return {"id": mid, "warnings": warns}


async def op_repost(data: dict) -> dict:
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
        movie = {"id": mid, "created": now(),
                 "log_msg_id": None, "public_msg_id": None}
        for k, v in DEFAULTS.items():
            movie.setdefault(k, v() if callable(v) else v)
    movie.update({
        "name": fields.get("NAME", "Untitled"), "link": link,
        "size": fields.get("SIZE", "-"), "quality": fields.get("QUALITY", "-"),
        "poster": "" if poster in ("-", "") else poster,
    })
    DB["movies"][mid] = movie
    schedule_save()
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


def bot_call(coro, timeout=60):
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
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
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
#  FLASK — MINI APP + API (unchanged)
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
    return f"OK movies={len(DB['movies'])} storage={'ok' if STORAGE['ok'] else 'warn'}", 200


@app.route("/api/movies")
def api_movies():
    items = [public_view(m) for m in DB["movies"].values()]
    items.sort(key=lambda x: x.get("created", 0), reverse=True)
    genres = sorted({g for m in items for g in m.get("genres", [])})
    return jsonify({"ok": True, "brand": BRAND, "bot": BOT_USERNAME,
                    "count": len(items), "genres": genres, "movies": items})


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
    mid = str(body.get("id", "")).strip()
    movie = DB["movies"].get(mid)
    if not movie:
        return jsonify({"ok": False, "error": "Title library mein nahi mila"}), 404
    uid = int(auth["user"]["id"])
    try:
        is_member = bot_call(check_membership(BOT, uid), timeout=25)
        join_url = "" if is_member else bot_call(get_channel_link(BOT), timeout=25)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Membership check fail: {exc}"}), 503
    if not is_member:
        return jsonify({"ok": True, "granted": False,
                        "need_join": True, "join_url": join_url})
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
        "ok": True, "titles": len(DB["movies"]), "storage": STORAGE,
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
        result = bot_call(op(body), timeout=90)
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
#  BOT — MESSAGE TEMPLATES
# ═══════════════════════════════════════════════════════════════

def welcome_text(first_name: str) -> str:
    return (
        f"🎬 <b>{h(BRAND.upper())}</b>\n"
        f"{DIV}\n"
        f"👋 Welcome, <b>{h(first_name)}</b>!\n\n"
        f"Aapki premium movie library — <b>movies, series aur direct "
        f"download links</b>, bina kisi wait ke.\n\n"
        f"<blockquote>⚡ Instant downloads\n"
        f"🎞 HD • 4K quality\n"
        f"🆕 Regular updates\n"
        f"🔒 Members-only access</blockquote>\n\n"
        f"👇 App kholen ya channel library browse karein."
    )


def welcome_kb(channel_link: str) -> InlineKeyboardMarkup:
    rows = []
    if WEB_APP_URL:
        rows.append([InlineKeyboardButton(
            "🎬 Open KNL Movies App", web_app=WebAppInfo(url=WEB_APP_URL))])
    rows.append([InlineKeyboardButton(
        "📚 Browse Movies Library", url=channel_link)])
    return InlineKeyboardMarkup(rows)


def join_gate_text(movie: dict = None) -> str:
    head = (
        f"🔒 <b>MEMBERS ONLY ACCESS</b>\n"
        f"{DIV}\n"
    )
    if movie:
        head += f"🎬 <b>{h(movie['name'])}</b> aapke liye ready hai.\n\n"
    return (
        head +
        f"Library sirf channel members ke liye unlock hoti hai.\n\n"
        f"<b>Unlock kaise karein:</b>\n"
        f"1️⃣ <b>Join Channel</b> dabayein\n"
        f"2️⃣ Channel join karein\n"
        f"3️⃣ Wapas aakar <b>✅ I Joined</b> dabayein\n\n"
        f"<blockquote>⚡ Join karte hi poori library instant unlock</blockquote>"
    )


def join_gate_kb(channel_link: str, ctx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=channel_link)],
        [InlineKeyboardButton("✅ I Joined", callback_data=f"joinok_{ctx}")],
    ])


def download_card(movie: dict) -> str:
    meta = movie_meta_line(movie)
    text = (
        f"🎬 <b>{h(movie['name'])}</b>\n"
        f"{DIV}\n"
        f"📦 <b>Size:</b> <code>{h(movie.get('size', '-'))}</code>\n"
        f"🎞 <b>Quality:</b> <code>{h(movie.get('quality', '-'))}</code>\n"
    )
    if meta:
        text += f"{meta}\n"
    if movie.get("genres"):
        text += f"🏷 <i>{h(', '.join(movie['genres']))}</i>\n"
    text += (
        f"{DIV}\n"
        f"✅ <b>Access granted</b> — download link ready hai.\n\n"
        f"<blockquote>⬇️ Neeche button se download shuru karein</blockquote>"
    )
    return text


def unavailable_text() -> str:
    return (
        f"⚠️ <b>CONTENT UNAVAILABLE</b>\n"
        f"{DIV}\n"
        f"Yeh title abhi library mein nahi mila — ho sakta hai remove ho gaya ho.\n\n"
        f"<blockquote>📚 Channel mein latest titles dekhein</blockquote>"
    )


# ═══════════════════════════════════════════════════════════════
#  BOT — SEND HELPERS
# ═══════════════════════════════════════════════════════════════

async def reply(update: Update, text: str, kb=None):
    await update.effective_message.reply_text(
        frame(text), parse_mode=ParseMode.HTML, reply_markup=kb,
        disable_web_page_preview=True,
    )


async def edit(update: Update, text: str, kb=None):
    await update.callback_query.edit_message_text(
        frame(text), parse_mode=ParseMode.HTML, reply_markup=kb,
        disable_web_page_preview=True,
    )


async def show_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE, via_edit=False):
    link = await get_channel_link(context.bot)
    text = welcome_text(update.effective_user.first_name or "Member")
    kb = welcome_kb(link)
    if via_edit:
        await edit(update, text, kb)
    else:
        await reply(update, text, kb)


async def show_download(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        movie: dict, via_edit=False):
    link = await get_channel_link(context.bot)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download Now", url=movie["link"])],
        [InlineKeyboardButton("📚 Browse Movies Library", url=link)],
    ])
    if via_edit:
        await edit(update, download_card(movie), kb)
    else:
        await reply(update, download_card(movie), kb)


# ═══════════════════════════════════════════════════════════════
#  BOT — USER FLOW
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
                await reply(update, unavailable_text(), InlineKeyboardMarkup([
                    [InlineKeyboardButton("📚 Browse Movies Library", url=link)]]))
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

    # Normal /start
    if not member:
        link = await get_channel_link(context.bot)
        await reply(update, join_gate_text(), join_gate_kb(link, "home"))
        return
    await show_welcome(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    await reply(update, (
        f"❓ <b>HELP — {h(BRAND.upper())}</b>\n"
        f"{DIV}\n"
        f"<b>Download kaise karein:</b>\n"
        f"1️⃣ App ya channel se title chunein\n"
        f"2️⃣ Channel join karein (agar nahi kiya)\n"
        f"3️⃣ Download link turant mil jayega\n\n"
        f"<blockquote>📚 Library: /movies\n"
        f"🎬 App: /start ke baad button</blockquote>"
    ))


async def render_list(update: Update, context: ContextTypes.DEFAULT_TYPE, via_edit=False):
    movies = DB["movies"]
    link = await get_channel_link(context.bot)
    if not movies:
        text = (
            f"📭 <b>LIBRARY EMPTY</b>\n{DIV}\n"
            f"Abhi koi title available nahi — admin jald hi add karega."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📚 Browse Movies Library", url=link)]])
        if via_edit:
            await edit(update, text, kb)
        else:
            await reply(update, text, kb)
        return

    items = sorted(movies.values(), key=lambda x: x.get("created", 0), reverse=True)
    buttons = [[InlineKeyboardButton(f"🎬 {m['name'][:38]}", callback_data=f"open_{m['id']}")]
               for m in items[:40]]
    buttons.append([InlineKeyboardButton("📚 Browse Movies Library", url=link)])
    text = (
        f"📚 <b>{h(BRAND.upper())} — LIBRARY</b>\n{DIV}\n"
        f"<blockquote>🎞 {len(movies)} titles available</blockquote>\n"
        f"👇 Title select karein"
    )
    kb = InlineKeyboardMarkup(buttons)
    if via_edit:
        await edit(update, text, kb)
    else:
        await reply(update, text, kb)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    uid = update.effective_user.id

    if data.startswith("joinok_"):
        ctx = data[len("joinok_"):]
        if not await check_membership(context.bot, uid):
            await query.answer("❌ Abhi join nahi hua — pehle Join Channel dabayein", show_alert=True)
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
        mid = data[len("open_"):]
        movie = DB["movies"].get(mid)
        if not movie:
            await query.answer("⚠️ Title nahi mila", show_alert=True)
            return
        if not await check_membership(context.bot, uid):
            link = await get_channel_link(context.bot)
            await query.answer()
            await edit(update, join_gate_text(movie), join_gate_kb(link, mid))
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
#  BOT — ADMIN
# ═══════════════════════════════════════════════════════════════

PIPE_FIELDS = ["name", "link", "size", "quality", "poster", "backdrop",
               "type", "genres", "year", "rating", "duration",
               "language", "age", "cast", "description"]


async def add_movie_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    if update.effective_user.id not in ADMIN_IDS:
        await reply(update, "⛔ <b>ADMIN ONLY</b>\n" + DIV + "\nYeh command sirf admin ke liye hai.")
        return

    raw = (update.effective_message.text or "").split(maxsplit=1)
    if len(raw) < 2 or "|" not in raw[1]:
        await reply(update, (
            f"➕ <b>ADD TITLE</b>\n{DIV}\n"
            f"<b>Quick add:</b>\n"
            f"<code>/addmovie Name | link | size | quality | poster</code>\n\n"
            f"<blockquote>Full metadata (genre, rating, cast, backdrop) ke liye "
            f"mini app ka Admin panel use karein — ya /addjson.</blockquote>"
        ))
        return

    parts = [p.strip() for p in raw[1].split("|")]
    if len(parts) < 4:
        await reply(update, f"⚠️ <b>INVALID FORMAT</b>\n{DIV}\n4 fields chahiye: Name | link | size | quality")
        return

    data = {k: parts[i] for i, k in enumerate(PIPE_FIELDS) if i < len(parts)}
    data["post"] = True
    try:
        res = await op_add(data)
    except Exception as exc:
        await reply(update, f"⚠️ <b>ERROR</b>\n{DIV}\n<code>{h(exc)}</code>")
        return

    m = res["movie"]
    warn = ""
    if res["warnings"]:
        warn = "\n\n<blockquote>⚠️ " + h("\n⚠️ ".join(res["warnings"])) + "</blockquote>"
    await reply(update, (
        f"✅ <b>TITLE ADDED</b>\n{DIV}\n"
        f"🎬 <b>{h(m['name'])}</b>\n"
        f"🔖 ID: <code>{m['id']}</code>\n"
        f"<blockquote>💾 Database saved\n📢 Channel post done</blockquote>{warn}"
    ))


async def add_json_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or update.effective_user.id not in ADMIN_IDS:
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
        await reply(update, f"⚠️ <b>ERROR</b>\n{DIV}\n<code>{h(exc)}</code>")
        return
    await reply(update, f"✅ <b>ADDED</b>\n{DIV}\n🔖 <code>{res['movie']['id']}</code>")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or update.effective_user.id not in ADMIN_IDS:
        return
    movies = DB["movies"]
    if not movies:
        await reply(update, f"📭 <b>LIBRARY EMPTY</b>\n{DIV}\nKoi title nahi.")
        return
    lines = [f"🗂 <b>DATABASE — {len(movies)} TITLES</b>\n{DIV}"]
    for m in sorted(movies.values(), key=lambda x: x.get("created", 0), reverse=True)[:60]:
        lines.append(f"🔖 <code>{m['id']}</code>  🎬 {h(m['name'])}")
    await reply(update, "\n".join(lines))


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await reply(update, f"🗑 <b>DELETE</b>\n{DIV}\n<code>/delmovie &lt;id&gt;</code>")
        return
    try:
        res = await op_delete({"id": context.args[0]})
    except Exception as exc:
        await reply(update, f"⚠️ <b>ERROR</b>\n{DIV}\n<code>{h(exc)}</code>")
        return
    await reply(update, f"🗑 <b>REMOVED</b>\n{DIV}\n🔖 <code>{res['id']}</code>")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or update.effective_user.id not in ADMIN_IDS:
        return
    saved = (time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(STORAGE["saved_at"]))
             if STORAGE["saved_at"] else "—")
    await reply(update, (
        f"📊 <b>SYSTEM STATUS</b>\n{DIV}\n"
        f"🎬 Titles: <code>{len(DB['movies'])}</code>\n"
        f"💾 Storage: {'✅ OK' if STORAGE['ok'] else '⚠️ Problem'}\n"
        f"🕒 Last save: <code>{saved}</code>\n"
        f"📌 Source: <code>{STORAGE['source']}</code>\n"
        f"🌐 App URL: <code>{h(WEB_APP_URL or 'set nahi')}</code>"
        + (f"\n<blockquote>⚠️ {h(STORAGE['error'])}</blockquote>" if STORAGE["error"] else "")
    ))


async def save_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or update.effective_user.id not in ADMIN_IDS:
        return
    st = await save_snapshot(context.bot)
    await reply(update, (
        f"{'✅' if st['ok'] else '⚠️'} <b>SNAPSHOT SAVE</b>\n{DIV}\n"
        f"🎬 Titles: <code>{len(DB['movies'])}</code>\n"
        f"<blockquote>{h(st.get('error') or 'Pinned + saved')}</blockquote>"
    ))


async def import_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    if update.effective_user.id not in ADMIN_IDS:
        return
    text = (msg.text or "").strip()
    if not text.startswith("#MOVIE"):
        return
    try:
        res = await op_import(text)
    except Exception as exc:
        await reply(update, f"⚠️ <b>IMPORT FAIL</b>\n{DIV}\n<code>{h(exc)}</code>")
        return
    await reply(update, (
        f"{'🔄' if res['updated'] else '✅'} <b>{'UPDATED' if res['updated'] else 'IMPORTED'}</b>\n"
        f"{DIV}\n🔖 <code>{res['id']}</code>"
    ))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("[error]", context.error)
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
    await load_snapshot(application.bot)

    if not STORAGE["ok"]:
        for aid in ADMIN_IDS:
            try:
                await application.bot.send_message(
                    aid,
                    frame(
                        f"⚠️ <b>STORAGE WARNING</b>\n{DIV}\n{h(STORAGE['error'])}\n\n"
                        f"<blockquote>Fix: bot ko private channel mein Pin Messages aur "
                        f"Delete Messages rights do, phir /save bhejo.\n\n"
                        f"Purani movies wapas laane ke liye unke #MOVIE messages "
                        f"yahan forward kar do.</blockquote>"
                    ), parse_mode=ParseMode.HTML)
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
    print(f"[boot] flask port {os.environ.get('PORT', '5000')} | admins={sorted(ADMIN_IDS)} | app={WEB_APP_URL or 'none'}")

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
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, import_handler))
    application.add_error_handler(error_handler)

    print("[boot] polling started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
