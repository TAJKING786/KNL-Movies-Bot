import asyncio
import html
import os
import threading
import uuid

import requests
from flask import Flask, request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
AROLINKS_API      = os.environ.get("AROLINKS_API", "")
AROLINKS_ENDPOINT = os.environ.get("AROLINKS_ENDPOINT", "https://arolinks.com/api")
AROLINK_TIMEOUT   = int(os.environ.get("AROLINK_TIMEOUT", "20"))

WEB_SERVER_URL     = os.environ.get("WEB_SERVER_URL", "http://localhost:5000").rstrip("/")
PUBLIC_CHANNEL_ID  = int(os.environ.get("PUBLIC_CHANNEL_ID", "0"))
PRIVATE_CHANNEL_ID = int(os.environ.get("PRIVATE_CHANNEL_ID", "0"))
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "0"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").lstrip("@")
BRAND        = os.environ.get("BRAND", "🎬 Movie Hub")

# In-memory cache. Source of truth = private channel.
DB = {"movies": {}}
BOT = None
MAIN_LOOP = None

LINE = "──────────────────────"


def md(text) -> str:
    if text is None:
        return ""
    out = str(text)
    for ch in ("\\", "_", "*", "[", "`"):
        out = out.replace(ch, "\\" + ch)
    return out


# ═══════════════════════════════════════════════════════════════
#  CHANNEL DATABASE (restart-proof)
# ═══════════════════════════════════════════════════════════════

def _parse_kv(text: str) -> dict:
    fields = {}
    for line in text.splitlines()[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip().upper()] = v.strip()
    return fields


def _parse_entry(text: str):
    if not text.startswith("#MOVIE"):
        return None
    f = _parse_kv(text)
    if not f.get("ID") or not f.get("LINK"):
        return None
    poster = f.get("POSTER", "-")
    return {
        "id": f["ID"],
        "name": f.get("NAME", "Untitled"),
        "link": f["LINK"],
        "size": f.get("SIZE", "-"),
        "quality": f.get("QUALITY", "-"),
        "poster": "" if poster in ("-", "") else poster,
    }


async def rebuild_db(bot) -> None:
    """Private channel ki history padh kar poora DB rebuild karo."""
    movies, verified = {}, {}
    offset, pages = 0, 0
    while pages < 30:
        try:
            batch = await bot.get_chat_history(
                PRIVATE_CHANNEL_ID, limit=100, offset=offset
            )
        except Exception as exc:
            print("[rebuild] history error:", exc)
            break
        if not batch:
            break
        for msg in batch:
            txt = (msg.text or msg.caption or "").strip()
            if txt.startswith("#MOVIE"):
                entry = _parse_entry(txt)
                if entry:
                    entry["verified"] = set()
                    entry["msg_id"] = msg.message_id
                    movies[entry["id"]] = entry
            elif txt.startswith("#VERIFY"):
                f = _parse_kv(txt)
                if f.get("ID") and f.get("USER"):
                    verified.setdefault(f["ID"], set()).add(f["USER"])
        offset = batch[-1].message_id - 1
        pages += 1

    for mid, users in verified.items():
        if mid in movies:
            movies[mid]["verified"] |= users

    DB["movies"] = movies
    total_v = sum(len(m["verified"]) for m in movies.values())
    print(f"[rebuild] {len(movies)} movies | {total_v} verifications restored")


def get_movie(movie_id: str):
    return DB["movies"].get(movie_id)


async def create_movie(bot, name, link, size, quality, poster_url) -> str:
    movie_id = uuid.uuid4().hex[:8]
    entry = (
        "#MOVIE v2\n"
        f"ID: {movie_id}\n"
        f"NAME: {name}\n"
        f"LINK: {link}\n"
        f"SIZE: {size}\n"
        f"QUALITY: {quality}\n"
        f"POSTER: {poster_url or '-'}"
    )
    msg_id = None
    if PRIVATE_CHANNEL_ID:
        sent = await bot.send_message(PRIVATE_CHANNEL_ID, entry)
        msg_id = sent.message_id

    DB["movies"][movie_id] = {
        "id": movie_id, "name": name, "link": link, "size": size,
        "quality": quality, "poster": poster_url,
        "verified": set(), "msg_id": msg_id,
    }
    return movie_id


def mark_user_verified(movie_id: str, user_id: str):
    movie = DB["movies"].get(movie_id)
    if not movie:
        return False, ""
    if user_id not in movie["verified"]:
        movie["verified"].add(user_id)
        _schedule_verify_log(movie_id, user_id)
    return True, movie["name"]


def is_user_verified(movie_id: str, user_id: str) -> bool:
    movie = DB["movies"].get(movie_id)
    return bool(movie) and user_id in movie["verified"]


async def _post_verify_log(movie_id: str, user_id: str):
    await BOT.send_message(
        PRIVATE_CHANNEL_ID,
        f"#VERIFY v2\nID: {movie_id}\nUSER: {user_id}",
    )


def _schedule_verify_log(movie_id: str, user_id: str):
    """Flask thread se bot loop mein coroutine bhejo."""
    if MAIN_LOOP is None or not PRIVATE_CHANNEL_ID:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _post_verify_log(movie_id, user_id), MAIN_LOOP
        )
    except Exception as exc:
        print("[verify-log]", exc)


# ═══════════════════════════════════════════════════════════════
#  FLASK SERVER (verification webhook)
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__)

_SUCCESS_PAGE = """<!DOCTYPE html>
<html lang="hi"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Verification Successful</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:linear-gradient(135deg,#0f2027 0%,#203a43 50%,#2c5364 100%);
min-height:100vh;display:flex;align-items:center;justify-content:center;
margin:0;padding:20px;box-sizing:border-box}}
.card{{background:#fff;border-radius:22px;padding:38px 26px;text-align:center;
box-shadow:0 24px 70px rgba(0,0,0,.45);max-width:400px;width:100%}}
.badge{{display:inline-block;background:#e6fffa;color:#047857;font-weight:700;
font-size:12px;letter-spacing:1px;padding:6px 14px;border-radius:999px;
margin-bottom:14px;text-transform:uppercase}}
.icon{{font-size:64px;line-height:1;margin-bottom:10px}}
h1{{color:#111827;margin:0 0 6px;font-size:23px}}
p{{color:#6b7280;line-height:1.6;margin:8px 0;font-size:15px}}
.title{{background:#f3f4f6;padding:12px;border-radius:12px;margin:16px 0;
font-weight:700;color:#1f2937;word-break:break-word}}
.btn{{display:inline-block;background:#111827;color:#fff;padding:14px 26px;
border-radius:12px;text-decoration:none;font-weight:700;margin-top:14px;font-size:15px}}
.foot{{margin-top:18px;font-size:12px;color:#9ca3af}}
</style></head><body>
<div class="card">
<div class="badge">Verified</div>
<div class="icon">✅</div>
<h1>Step 1 Complete</h1>
<p>Aapki verification successful rahi.</p>
<div class="title">🎬 {name}</div>
<p>Ab Telegram bot mein wapas jaiye aur<br><b>"2️⃣ Unlock Download"</b> dabaiye.</p>
{button}
<div class="foot">{brand}</div>
</div></body></html>"""

_FAIL_PAGE = """<!DOCTYPE html>
<html lang="hi"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Verification Failed</title>
<style>
body{{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#f3f4f6;
min-height:100vh;display:flex;align-items:center;justify-content:center;
margin:0;padding:20px}}
.card{{background:#fff;border-radius:20px;padding:36px 26px;text-align:center;
max-width:380px;box-shadow:0 16px 50px rgba(0,0,0,.12)}}
h1{{color:#111827;font-size:22px;margin:10px 0}}
p{{color:#6b7280;line-height:1.6;font-size:15px}}
</style></head><body>
<div class="card">
<div style="font-size:56px">⚠️</div>
<h1>Verification Failed</h1>
<p>Link invalid hai ya expire ho gaya hai.<br>
Telegram bot mein wapas jakar <b>Step 1</b> dobara karein.</p>
</div></body></html>"""


@app.route("/")
def home():
    return f"{BRAND} bot server online", 200


@app.route("/health")
def health():
    return f"OK movies={len(DB['movies'])}", 200


@app.route("/verify")
def verify_user():
    user_id = (request.args.get("u") or "").strip()
    movie_id = (request.args.get("m") or "").strip()

    if not (user_id and movie_id):
        return _FAIL_PAGE, 400

    ok, movie_name = mark_user_verified(movie_id, user_id)
    if not ok:
        return _FAIL_PAGE, 400

    button = (
        f'<a href="https://t.me/{html.escape(BOT_USERNAME)}" class="btn">'
        "Open Telegram Bot →</a>"
    ) if BOT_USERNAME else ""

    return _SUCCESS_PAGE.format(
        name=html.escape(str(movie_name)),
        button=button,
        brand=html.escape(BRAND),
    ), 200


def run_flask():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ═══════════════════════════════════════════════════════════════
#  SHORTENER API (generic)
# ═══════════════════════════════════════════════════════════════

async def build_short_link(verify_url: str):
    if not AROLINKS_API:
        return None, "Shortener API key set nahi hai"
    params = {"api": AROLINKS_API, "url": verify_url}
    try:
        resp = await asyncio.to_thread(
            requests.get, AROLINKS_ENDPOINT, params=params, timeout=AROLINK_TIMEOUT
        )
    except requests.RequestException as exc:
        return None, f"Network error: {exc}"
    try:
        data = resp.json()
    except ValueError:
        text = resp.text.strip()
        return (text, None) if text.startswith("http") else (None, f"HTTP {resp.status_code}")
    if isinstance(data, dict):
        status = str(data.get("status", "")).lower()
        if status and status not in ("success", "ok", "true"):
            return None, str(data.get("message") or data.get("error") or "API error")
        for key in ("shortenedUrl", "shortened_url", "shortUrl", "short_url",
                    "result", "url", "link"):
            if data.get(key):
                return str(data[key]), None
        return None, "Response mein link nahi mila"
    if isinstance(data, str) and data.startswith("http"):
        return data, None
    return None, "Unexpected response"


# ═══════════════════════════════════════════════════════════════
#  HELPERS
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


def movie_card(movie, status: str) -> str:
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


# ═══════════════════════════════════════════════════════════════
#  USER FLOW
# ═══════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return

    if context.args:
        arg = context.args[0]
        if arg.startswith("movie_"):
            await handle_movie_request(update, context, arg[len("movie_"):])
            return
        if arg in ("movies", "list"):
            await show_all_movies(update, context)
            return

    name = md(update.effective_user.first_name or "")
    text = (
        f"*{md(BRAND)}*\n"
        f"{LINE}\n"
        f"Welcome, {name} 👋\n\n"
        "*Download kaise karein?*\n"
        "1️⃣ Channel mein movie post ka *DOWNLOAD* button dabayein\n"
        "2️⃣ *Step 1* — short verification complete karein\n"
        "3️⃣ *Step 2* — download link unlock karein\n\n"
        "⚡ Members-only • Fast • Secure\n"
        f"{LINE}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Browse Movies", callback_data="browse")],
        [InlineKeyboardButton("❓ Help", callback_data="helpcb")],
    ])
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        f"*{md(BRAND)} — Help*\n{LINE}\n"
        "1️⃣ Channel se movie ka DOWNLOAD button dabayein\n"
        "2️⃣ Bot mein *Step 1* verification link kholen\n"
        "3️⃣ Browser mein verification complete karein\n"
        "4️⃣ ✅ success page ke baad bot mein *Unlock Download* dabayein\n"
        "5️⃣ Download link unlock ho jayega\n\n"
        "⚠️ Verification ke bina download possible nahi hai.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_movie_request(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: str):
    user_id = update.effective_user.id
    movie = get_movie(movie_id)

    if not movie:
        await update.effective_message.reply_text(
            f"⚠️ *Content unavailable*\n{LINE}\n"
            "Yeh title abhi database mein nahi mila.\n"
            "Ho sakta hai hataya gaya ho — channel mein nayi posts dekhein.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not await check_membership(context.bot, user_id):
        channel_link = await get_channel_link(context.bot)
        text = (
            "🔒 *MEMBERS ONLY*\n"
            f"{LINE}\n"
            "Downloads sirf channel members ke liye hain.\n\n"
            "1️⃣ *Join Channel* dabayein\n"
            "2️⃣ Channel join karein\n"
            "3️⃣ *I Joined* se confirm karein\n"
            f"{LINE}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Channel", url=channel_link)],
            [InlineKeyboardButton("✅ I Joined", callback_data=f"join_{movie_id}")],
        ])
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
        return

    await show_card(update, movie)


async def show_card(update: Update, movie: dict):
    user_id = str(update.effective_user.id)
    if is_user_verified(movie["id"], user_id):
        await show_step2(update, movie)
        return
    status = "🟡 Status : Step 1 pending"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ Step 1 — Verification Link", callback_data=f"s1_{movie['id']}")],
        [InlineKeyboardButton("2️⃣ Unlock Download", callback_data=f"unlock_{movie['id']}")],
    ])
    await _send_or_edit(update, movie_card(movie, status), keyboard)


async def show_step2(update: Update, movie: dict):
    text = (
        "✅ *VERIFIED — DOWNLOAD READY*\n"
        f"{LINE}\n"
        f"🎬 {md(movie['name'])}\n"
        f"📦 {md(movie.get('size', '-'))} • 🎞 {md(movie.get('quality', '-'))}\n"
        f"{LINE}\n"
        "Neeche button se download shuru karein."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download Movie", url=movie["link"])],
        [InlineKeyboardButton("↩️ Back", callback_data=f"back_{movie['id']}")],
    ])
    await _send_or_edit(update, text, keyboard)


# ═══════════════════════════════════════════════════════════════
#  CALLBACKS
# ═══════════════════════════════════════════════════════════════

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    user_id = str(update.effective_user.id)

    if data == "browse":
        await query.answer()
        await render_list(update, context, edit=True)
        return

    if data == "helpcb":
        await query.answer()
        await query.edit_message_text(
            f"*{md(BRAND)} — Help*\n{LINE}\n"
            "1️⃣ Channel se movie ka DOWNLOAD button dabayein\n"
            "2️⃣ Step 1 verification complete karein\n"
            "3️⃣ Unlock Download se link payein\n\n"
            "⚠️ Verification ke bina download possible nahi.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Back", callback_data="browse")],
            ]),
        )
        return

    if data.startswith("join_"):
        movie_id = data[len("join_"):]
        if await check_membership(context.bot, int(user_id)):
            await query.answer("✅ Membership confirmed")
            movie = get_movie(movie_id)
            if movie:
                await show_card(update, movie)
        else:
            await query.answer("❌ Abhi join nahi hua — Join Channel dabayein", show_alert=True)
        return

    if data.startswith("back_"):
        await query.answer()
        movie = get_movie(data[len("back_"):])
        if movie:
            await show_card(update, movie)
        else:
            await query.edit_message_text("⚠️ Content nahi mila.")
        return

    if data.startswith("s1_"):
        movie_id = data[len("s1_"):]
        await query.answer()
        movie = get_movie(movie_id)
        if not movie:
            await query.edit_message_text("⚠️ Content nahi mila.")
            return

        await query.edit_message_text("⏳ Verification link ban raha hai…")
        verify_url = f"{WEB_SERVER_URL}/verify?u={user_id}&m={movie_id}"
        link, error = await build_short_link(verify_url)

        if not link:
            await query.edit_message_text(
                f"⚠️ Link generate nahi hua\n`{error}`\n\nThodi der baad dobara try karein.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        text = (
            "🔐 *STEP 1 — VERIFICATION*\n"
            f"{LINE}\n"
            f"🎬 {md(movie['name'])}\n\n"
            "Neeche button se link kholen aur instructions\n"
            "complete karein (≈10 second).\n\n"
            "Complete hone par browser mein ✅ success\n"
            "page dikhega, phir yahan wapas aayein.\n"
            f"{LINE}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Open Verification Link", url=link)],
            [InlineKeyboardButton("2️⃣ Unlock Download", callback_data=f"unlock_{movie_id}")],
            [InlineKeyboardButton("↩️ Back", callback_data=f"back_{movie_id}")],
        ])
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
        return

    if data.startswith("unlock_"):
        movie_id = data[len("unlock_"):]
        movie = get_movie(movie_id)
        if not movie:
            await query.answer("⚠️ Content nahi mila", show_alert=True)
            return
        if is_user_verified(movie_id, user_id):
            await query.answer("✅ Verified")
            await show_step2(update, movie)
        else:
            await query.answer(
                "❌ Pehle Step 1 complete karein.\n"
                "Browser mein ✅ success page dikhne ke baad hi yeh button chalega.",
                show_alert=True,
            )
        return

    await query.answer()


# ═══════════════════════════════════════════════════════════════
#  MOVIE LIST
# ═══════════════════════════════════════════════════════════════

async def render_list(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    movies = DB["movies"]
    if not movies:
        text = f"📭 *Library khali hai*\n{LINE}\nAbhi koi title available nahi."
        if edit:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    base = f"https://t.me/{BOT_USERNAME}?start=movie_" if BOT_USERNAME else ""
    buttons = []
    for mid, m in list(movies.items())[:40]:
        label = f"🎬 {m['name'][:38]}"
        if base:
            buttons.append([InlineKeyboardButton(label, url=f"{base}{mid}")])
        else:
            buttons.append([InlineKeyboardButton(label, callback_data=f"back_{mid}")])

    text = (
        f"*{md(BRAND)} — Library*\n"
        f"{LINE}\n"
        f"📚 {len(movies)} titles available\n"
        "Title select karein 👇"
    )
    if edit:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )


async def show_all_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await render_list(update, context, edit=False)


# ═══════════════════════════════════════════════════════════════
#  ADMIN
# ═══════════════════════════════════════════════════════════════

async def add_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    if update.effective_user.id != ADMIN_ID:
        await update.effective_message.reply_text("⛔ Admin-only command.")
        return

    raw = (update.effective_message.text or "").split(maxsplit=1)
    if len(raw) < 2 or "|" not in raw[1]:
        await update.effective_message.reply_text(
            "*Usage*\n`/addmovie Name | download_link | size | quality | poster_url`\n\n"
            "*Example*\n`/addmovie Inception | https://host.example/abc | 2.1GB | 1080p | https://img.example/p.jpg`\n\n"
            "Poster optional hai.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    parts = [p.strip() for p in raw[1].split("|")]
    if len(parts) < 4:
        await update.effective_message.reply_text("⚠️ Kam se kam 4 fields: Name | link | size | quality")
        return

    name, link, size, quality = parts[0], parts[1], parts[2], parts[3]
    poster = parts[4] if len(parts) > 4 else ""

    try:
        movie_id = await create_movie(context.bot, name, link, size, quality, poster)
    except Exception as exc:
        await update.effective_message.reply_text(f"⚠️ DB entry error: {exc}")
        return

    # Public channel card
    if PUBLIC_CHANNEL_ID:
        try:
            deep = f"https://t.me/{BOT_USERNAME}?start=movie_{movie_id}" if BOT_USERNAME else ""
            caption = (
                f"🎬 *{md(name)}*\n"
                f"{LINE}\n"
                f"📦 Size : {md(size)}\n"
                f"🎞 Quality : {md(quality)}\n"
                f"{LINE}\n"
                "⬇️ Download ke liye button dabayein"
            )
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬇️ DOWNLOAD", url=deep)]]
            ) if deep else None
            if poster:
                await context.bot.send_photo(
                    PUBLIC_CHANNEL_ID, photo=poster, caption=caption,
                    parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
                )
            else:
                await context.bot.send_message(
                    PUBLIC_CHANNEL_ID, caption,
                    parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
                )
        except Exception as exc:
            await update.effective_message.reply_text(f"⚠️ Public post error: {exc}")

    await update.effective_message.reply_text(
        f"✅ *Title added*\n{LINE}\n🎬 {md(name)}\n🔖 `{movie_id}`\n\n"
        "• Database (private channel) ✔\n• Public post ✔",
        parse_mode=ParseMode.MARKDOWN,
    )


async def list_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    if update.effective_user.id != ADMIN_ID:
        return
    movies = DB["movies"]
    if not movies:
        await update.effective_message.reply_text("Database khali hai.")
        return
    lines = [f"*Database — {len(movies)} titles*\n{LINE}"]
    for mid, m in movies.items():
        lines.append(f"🔖 `{mid}`  🎬 {md(m['name'])}  ✅ {len(m['verified'])}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def del_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: `/delmovie <id>`", parse_mode=ParseMode.MARKDOWN)
        return
    mid = context.args[0]
    movie = DB["movies"].pop(mid, None)
    if not movie:
        await update.effective_message.reply_text("❌ ID nahi mili.")
        return
    if PRIVATE_CHANNEL_ID and movie.get("msg_id"):
        try:
            await context.bot.delete_message(PRIVATE_CHANNEL_ID, movie["msg_id"])
        except Exception as exc:
            print("[del]", exc)
    await update.effective_message.reply_text(f" `{mid}` remove ho gaya.", parse_mode=ParseMode.MARKDOWN)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or update.effective_user.id != ADMIN_ID:
        return
    movies = DB["movies"]
    total = sum(len(m["verified"]) for m in movies.values())
    await update.effective_message.reply_text(
        f"📊 *Stats*\n{LINE}\n🎬 Titles : {len(movies)}\n✅ Verifications : {total}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ═══════════════════════════════════════════════════════════════
#  ERROR HANDLER + BOOT
# ═══════════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("[error]", context.error)
    if ADMIN_ID:
        try:
            await context.bot.send_message(ADMIN_ID, f"⚠️ Bot error:\n{context.error}")
        except Exception:
            pass


async def post_init(application: Application):
    global BOT, MAIN_LOOP
    BOT = application.bot
    MAIN_LOOP = asyncio.get_running_loop()
    if PRIVATE_CHANNEL_ID:
        await rebuild_db(application.bot)
    else:
        print("[boot] PRIVATE_CHANNEL_ID not set")


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN set nahi hai")

    # Python 3.14 event-loop fix
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    threading.Thread(target=run_flask, daemon=True).start()
    print(f"[boot] flask port {os.environ.get('PORT', '5000')} | web={WEB_SERVER_URL}")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("movies", show_all_movies))
    application.add_handler(CommandHandler("addmovie", add_movie))
    application.add_handler(CommandHandler("list", list_movies))
    application.add_handler(CommandHandler("delmovie", del_movie))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(error_handler)

    print("[boot] polling started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
