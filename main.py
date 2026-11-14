import asyncio
import hashlib
import hmac
import io
import json
import os
import threading
import time
import uuid
from urllib.parse import parse_qsl

from flask import Flask, jsonify, request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
BRAND              = os.environ.get("BRAND", "🎬 Movie Hub")
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
LINE = "──────────────────────"
SNAP_MARK = "KNLDB"
SNAP_VER = 3

DB = {"movies": {}}
STORAGE = {"ok": False, "source": "none", "saved_at": 0, "error": ""}
BOT = None
MAIN_LOOP = None
_snap_msg_id = None
_save_lock = None
_save_task = None


def md(text) -> str:
    if text is None:
        return ""
    out = str(text)
    for ch in ("\\", "_", "*", "[", "`"):
        out = out.replace(ch, "\\" + ch)
    return out


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

    DB["movies"] = movies
    _snap_msg_id = pm.message_id
    STORAGE.update({
        "ok": True,
        "source": "pinned-snapshot",
        "saved_at": int(payload.get("saved_at") or 0),
        "error": "",
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
                chat_id=PRIVATE_CHANNEL_ID,
                document=bio,
                filename="library.json",
                caption=caption,
                write_timeout=60,
                read_timeout=60,
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
            "ok": pinned,
            "source": "pinned-snapshot",
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
#  MOVIE OPS
# ═══════════════════════════════════════════════════════════════

def clean_fields(data: dict):
    name = str(data.get("name", "")).strip()
    link = str(data.get("link", "")).strip()
    size = str(data.get("size", "-")).strip() or "-"
    quality = str(data.get("quality", "-")).strip() or "-"
    poster = str(data.get("poster", "")).strip()
    if not name:
        return None, "Name khali hai"
    if not link.startswith(("http://", "https://", "tg://")):
        return None, "Link http(s) se shuru hona chahiye"
    if poster and not poster.startswith(("http://", "https://")):
        return None, "Poster URL http(s) hona chahiye"
    return {"name": name, "link": link, "size": size,
            "quality": quality, "poster": poster}, None


async def post_public(bot, movie: dict) -> int:
    deep = (
        f"https://t.me/{BOT_USERNAME}?start=movie_{movie['id']}"
        if BOT_USERNAME else ""
    )
    caption = (
        f"🎬 *{md(movie['name'])}*\n"
        f"{LINE}\n"
        f"📦 Size : {md(movie['size'])}\n"
        f"🎞 Quality : {md(movie['quality'])}\n"
        f"{LINE}\n"
        "⬇️ Download ke liye button dabayein"
    )
    kb = (
        InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ DOWNLOAD", url=deep)]])
        if deep else None
    )
    if movie.get("poster"):
        m = await bot.send_photo(
            PUBLIC_CHANNEL_ID, photo=movie["poster"], caption=caption,
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        )
    else:
        m = await bot.send_message(
            PUBLIC_CHANNEL_ID, caption,
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
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
    """Purane #MOVIE messages se restore — original ID preserve hoti hai."""
    fields = {}
    for line in text.splitlines()[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip().upper()] = v.strip()

    mid = fields.get("ID")
    link = fields.get("LINK")
    if not mid or not link:
        raise ValueError("ID / LINK missing")

    poster = fields.get("POSTER", "-")
    existed = mid in DB["movies"]
    movie = DB["movies"].get(mid, {"id": mid, "created": now()})
    movie.update({
        "name": fields.get("NAME", "Untitled"),
        "link": link,
        "size": fields.get("SIZE", "-"),
        "quality": fields.get("QUALITY", "-"),
        "poster": "" if poster in ("-", "") else poster,
    })
    movie.setdefault("log_msg_id", None)
    movie.setdefault("public_msg_id", None)
    DB["movies"][mid] = movie
    schedule_save()
    return {"id": mid, "updated": existed}


# ═══════════════════════════════════════════════════════════════
#  TELEGRAM initData VALIDATION
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
#  FLASK — MINI APP + API
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
    return body, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
    }


@app.route("/health")
def health():
    return f"OK movies={len(DB['movies'])} storage={'ok' if STORAGE['ok'] else 'warn'}", 200


@app.route("/api/movies")
def api_movies():
    items = [
        {
            "id": mid,
            "name": m.get("name", "Untitled"),
            "size": m.get("size", "-"),
            "quality": m.get("quality", "-"),
            "poster": m.get("poster", ""),
            "created": m.get("created", 0),
        }
        for mid, m in DB["movies"].items()
    ]
    items.sort(key=lambda x: x.get("created", 0), reverse=True)
    return jsonify({"ok": True, "brand": BRAND, "bot": BOT_USERNAME,
                    "count": len(items), "movies": items})


@app.route("/api/session", methods=["POST"])
def api_session():
    auth, err = req_auth()
    if err:
        return jsonify({"ok": True, "authed": False, "is_admin": False,
                        "reason": err, "brand": BRAND, "bot": BOT_USERNAME})
    uid = int(auth["user"]["id"])
    return jsonify({
        "ok": True, "authed": True, "is_admin": uid in ADMIN_IDS,
        "user": {"id": uid,
                 "name": auth["user"].get("first_name", ""),
                 "username": auth["user"].get("username", "")},
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
        return jsonify({"ok": False, "need_join": True, "join_url": join_url})

    return jsonify({"ok": True, "link": movie["link"],
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
        "public_channel": bool(PUBLIC_CHANNEL_ID),
        "private_channel": bool(PRIVATE_CHANNEL_ID),
        "bot_username": BOT_USERNAME,
    })


@app.route("/api/admin/movies", methods=["POST"])
def api_admin_movies():
    _, err_resp = _admin_guard()
    if err_resp:
        return err_resp
    items = list(DB["movies"].values())
    items.sort(key=lambda x: x.get("created", 0), reverse=True)
    return jsonify({"ok": True, "count": len(items), "movies": items})


@app.route("/api/admin/add", methods=["POST"])
def api_admin_add():
    _, err_resp = _admin_guard()
    if err_resp:
        return err_resp
    body = request.get_json(silent=True) or {}
    try:
        result = bot_call(op_add(body), timeout=90)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, **result})


@app.route("/api/admin/update", methods=["POST"])
def api_admin_update():
    _, err_resp = _admin_guard()
    if err_resp:
        return err_resp
    body = request.get_json(silent=True) or {}
    try:
        result = bot_call(op_update(body), timeout=90)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, **result})


@app.route("/api/admin/delete", methods=["POST"])
def api_admin_delete():
    _, err_resp = _admin_guard()
    if err_resp:
        return err_resp
    body = request.get_json(silent=True) or {}
    try:
        result = bot_call(op_delete(body), timeout=90)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, **result})


@app.route("/api/admin/repost", methods=["POST"])
def api_admin_repost():
    _, err_resp = _admin_guard()
    if err_resp:
        return err_resp
    body = request.get_json(silent=True) or {}
    try:
        result = bot_call(op_repost(body), timeout=90)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, **result})


def run_flask():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


# ═══════════════════════════════════════════════════════════════
#  BOT — USER FLOW
# ═══════════════════════════════════════════════════════════════

def movie_card(movie, status):
    return (
        f"🎬 *{md(movie['name'])}*\n"
        f"{LINE}\n"
        f"📦 Size : {md(movie.get('size', '-'))}\n"
        f"🎞 Quality : {md(movie.get('quality', '-'))}\n"
        f"{LINE}\n"
        f"{status}"
    )


async def _send_or_edit(update: Update, text: str, keyboard):
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )


async def show_download(update: Update, movie: dict):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download Now", url=movie["link"])],
        [InlineKeyboardButton("📚 More Movies", callback_data="browse")],
    ])
    await _send_or_edit(update, movie_card(movie, "✅ *Access granted*"), keyboard)


async def handle_movie_request(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: str):
    movie = DB["movies"].get(movie_id)
    if not movie:
        await update.effective_message.reply_text(
            f"⚠️ *Content unavailable*\n{LINE}\n"
            "Yeh title library mein nahi mila.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    uid = update.effective_user.id
    if not await check_membership(context.bot, uid):
        link = await get_channel_link(context.bot)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Channel", url=link)],
            [InlineKeyboardButton("✅ I Joined", callback_data=f"join_{movie_id}")],
        ])
        await update.effective_message.reply_text(
            "🔒 *MEMBERS ONLY*\n"
            f"{LINE}\n"
            f"🎬 {md(movie['name'])}\n\n"
            "1️⃣ *Join Channel* dabayein\n"
            "2️⃣ Channel join karein\n"
            "3️⃣ *I Joined* se confirm karein",
            parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard,
        )
        return

    await show_download(update, movie)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    if context.args:
        arg = context.args[0]
        if arg.startswith("movie_"):
            await handle_movie_request(update, context, arg[len("movie_"):])
            return
        if arg in ("movies", "list"):
            await render_list(update, edit=False)
            return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Browse Movies", callback_data="browse")],
        [InlineKeyboardButton("❓ Help", callback_data="helpcb")],
    ])
    await update.effective_message.reply_text(
        f"*{md(BRAND)}*\n{LINE}\n"
        f"Welcome, {md(update.effective_user.first_name or '')} 👋\n\n"
        "Channel mein movie ka *DOWNLOAD* button dabayein —\n"
        "join check ke baad link turant mil jayega.\n\n"
        "⚡ Members-only • Instant • No extra steps",
        parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        f"*{md(BRAND)} — Help*\n{LINE}\n"
        "1️⃣ Channel se DOWNLOAD dabayein\n"
        "2️⃣ Channel join karein\n"
        "3️⃣ Link turant mil jayega\n\n"
        "📚 Library: /movies",
        parse_mode=ParseMode.MARKDOWN,
    )


async def render_list(update: Update, edit=False):
    movies = DB["movies"]
    if not movies:
        text = f"📭 *Library khali hai*\n{LINE}\nAbhi koi title nahi."
        if edit:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    items = sorted(movies.values(), key=lambda x: x.get("created", 0), reverse=True)
    buttons = [
        [InlineKeyboardButton(f"🎬 {m['name'][:38]}", callback_data=f"open_{m['id']}")]
        for m in items[:40]
    ]
    text = f"*{md(BRAND)} — Library*\n{LINE}\n📚 {len(movies)} titles\nSelect karein 👇"
    if edit:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    uid = update.effective_user.id

    if data == "browse":
        await query.answer()
        await render_list(update, edit=True)
        return

    if data == "helpcb":
        await query.answer()
        await query.edit_message_text(
            f"*{md(BRAND)} — Help*\n{LINE}\n"
            "1️⃣ Channel se DOWNLOAD dabayein\n"
            "2️⃣ Channel join karein\n"
            "3️⃣ Link turant mil jayega",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📚 Browse Movies", callback_data="browse")]
            ]),
        )
        return

    if data.startswith(("join_", "open_")):
        prefix = "join_" if data.startswith("join_") else "open_"
        mid = data[len(prefix):]
        movie = DB["movies"].get(mid)
        if not movie:
            await query.answer("⚠️ Content nahi mila", show_alert=True)
            return
        if await check_membership(context.bot, uid):
            await query.answer("✅ Access granted")
            await show_download(update, movie)
        else:
            await query.answer("❌ Pehle channel join karein", show_alert=True)
        return

    await query.answer()


# ═══════════════════════════════════════════════════════════════
#  BOT — ADMIN
# ═══════════════════════════════════════════════════════════════

async def add_movie_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Admin-only.")
        return

    raw = (update.effective_message.text or "").split(maxsplit=1)
    if len(raw) < 2 or "|" not in raw[1]:
        await update.effective_message.reply_text(
            "*Usage*\n`/addmovie Name | link | size | quality | poster_url`\n\n"
            "Poster optional hai.\n\n💡 Mini app ka Admin panel zyada aasan hai.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    parts = [p.strip() for p in raw[1].split("|")]
    if len(parts) < 4:
        await update.effective_message.reply_text("⚠️ 4 fields chahiye: Name | link | size | quality")
        return

    data = {"name": parts[0], "link": parts[1], "size": parts[2],
            "quality": parts[3], "poster": parts[4] if len(parts) > 4 else "",
            "post": True}
    try:
        res = await op_add(data)
    except Exception as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}")
        return

    m = res["movie"]
    warn = ("\n\n⚠️ " + "\n⚠️ ".join(res["warnings"])) if res["warnings"] else ""
    await update.effective_message.reply_text(
        f"✅ *Added*\n{LINE}\n🎬 {md(m['name'])}\n🔖 `{m['id']}`{md(warn)}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or update.effective_user.id not in ADMIN_IDS:
        return
    movies = DB["movies"]
    if not movies:
        await update.effective_message.reply_text("Library khali hai.")
        return
    lines = [f"*Library — {len(movies)} titles*\n{LINE}"]
    for m in sorted(movies.values(), key=lambda x: x.get("created", 0), reverse=True)[:60]:
        lines.append(f"🔖 `{m['id']}`  🎬 {md(m['name'])}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: `/delmovie <id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        res = await op_delete({"id": context.args[0]})
    except Exception as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}")
        return
    await update.effective_message.reply_text(f"🗑 `{res['id']}` removed.", parse_mode=ParseMode.MARKDOWN)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or update.effective_user.id not in ADMIN_IDS:
        return
    saved = (
        time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(STORAGE["saved_at"]))
        if STORAGE["saved_at"] else "—"
    )
    await update.effective_message.reply_text(
        f"📊 *Status*\n{LINE}\n"
        f"🎬 Titles : {len(DB['movies'])}\n"
        f"💾 Storage : {'✅ OK' if STORAGE['ok'] else '⚠️ Problem'}\n"
        f"🕒 Last save : {saved}\n"
        f"📌 Source : {STORAGE['source']}\n"
        + (f"\n⚠️ {md(STORAGE['error'])}" if STORAGE["error"] else ""),
        parse_mode=ParseMode.MARKDOWN,
    )


async def save_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or update.effective_user.id not in ADMIN_IDS:
        return
    st = await save_snapshot(context.bot)
    await update.effective_message.reply_text(
        f"{'✅' if st['ok'] else '⚠️'} Snapshot save\n{LINE}\n"
        f"Titles: {len(DB['movies'])}\n{md(st.get('error') or 'Pinned + saved')}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def import_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin dwara forward/paste kiye gaye #MOVIE entries import karo."""
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
        await msg.reply_text(f"⚠️ Import fail: {exc}")
        return
    await msg.reply_text(
        f"{'🔄 Updated' if res['updated'] else '✅ Imported'} `{res['id']}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("[error]", context.error)
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"⚠️ Bot error:\n{context.error}")
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
                    f"⚠️ *Storage warning*\n{LINE}\n{md(STORAGE['error'])}\n\n"
                    "Fix: bot ko private channel mein *Pin Messages* aur "
                    "*Delete Messages* rights do, phir /save bhejo.\n\n"
                    "Purani movies wapas laane ke liye unke `#MOVIE` messages "
                    "yahan forward kar do.",
                    parse_mode=ParseMode.MARKDOWN,
                )
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
    application.add_handler(CommandHandler("movies", lambda u, c: render_list(u, False)))
    application.add_handler(CommandHandler("addmovie", add_movie_cmd))
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
