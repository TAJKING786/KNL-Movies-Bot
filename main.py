import asyncio
import html
import json
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
#                    CONFIGURATION
# ═══════════════════════════════════════════════════════════════

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
AROLINKS_API = os.environ.get("AROLINKS_API", "")
AROLINKS_ENDPOINT = os.environ.get("AROLINKS_ENDPOINT", "https://arolinks.com/api")
AROLINK_TIMEOUT = int(os.environ.get("AROLINK_TIMEOUT", "20"))

# rstrip("/") se trailing slash ka "//verify" problem fix hota hai
WEB_SERVER_URL = os.environ.get("WEB_SERVER_URL", "http://localhost:5000").rstrip("/")
PUBLIC_CHANNEL_ID = int(os.environ.get("PUBLIC_CHANNEL_ID", "0"))
PRIVATE_CHANNEL_ID = int(os.environ.get("PRIVATE_CHANNEL_ID", "0"))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").lstrip("@")

DB_FILE = "movies_db.json"


def md(text) -> str:
    """
    Telegram legacy Markdown ke special characters escape karo.
    Movie name mein '_' ya '*' ho to parse error se bachata hai.
    """
    if text is None:
        return ""
    out = str(text)
    for ch in ("\\", "_", "*", "[", "`"):
        out = out.replace(ch, "\\" + ch)
    return out


# ═══════════════════════════════════════════════════════════════
#                    DATABASE HANDLING
# ═══════════════════════════════════════════════════════════════

def load_db() -> dict:
    if not os.path.exists(DB_FILE):
        return {"movies": {}}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "movies" not in data:
            return {"movies": {}}
        return data
    except (json.JSONDecodeError, OSError):
        return {"movies": {}}


def save_db(data: dict) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_movie(movie_id: str):
    return load_db()["movies"].get(movie_id)


def add_movie_to_db(name, filespay_link, size, quality, poster_url="") -> str:
    db = load_db()
    movie_id = uuid.uuid4().hex[:8]
    db["movies"][movie_id] = {
        "name": name,
        "filespay_link": filespay_link,
        "size": size,
        "quality": quality,
        "poster_url": poster_url,
        "verified_users": [],
        "public_channel_msg_id": None,
    }
    save_db(db)
    return movie_id


def mark_user_verified(movie_id: str, user_id: str) -> bool:
    db = load_db()
    movie = db["movies"].get(movie_id)
    if not movie:
        return False
    if user_id not in movie.setdefault("verified_users", []):
        movie["verified_users"].append(user_id)
        save_db(db)
    return True


def is_user_verified(movie_id: str, user_id: str) -> bool:
    movie = get_movie(movie_id)
    return bool(movie) and user_id in movie.get("verified_users", [])


def set_public_msg_id(movie_id: str, msg_id: int) -> None:
    db = load_db()
    if movie_id in db["movies"]:
        db["movies"][movie_id]["public_channel_msg_id"] = msg_id
        save_db(db)


# ═══════════════════════════════════════════════════════════════
#                    FLASK WEB SERVER (verification webhook)
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__)

_SUCCESS_PAGE = """<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ad Complete</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh;
display:flex;align-items:center;justify-content:center;margin:0;padding:20px;box-sizing:border-box}}
.card{{background:#fff;border-radius:20px;padding:36px 28px;text-align:center;
box-shadow:0 20px 60px rgba(0,0,0,.3);max-width:400px;width:100%}}
.icon{{font-size:72px;line-height:1;margin-bottom:16px}}
h1{{color:#2d3748;margin:0 0 8px;font-size:24px}}
p{{color:#718096;line-height:1.6;margin:10px 0}}
.highlight{{background:#f7fafc;padding:14px;border-radius:10px;margin:18px 0;
font-weight:700;color:#667eea;word-break:break-word}}
.btn{{display:inline-block;background:#667eea;color:#fff;padding:14px 28px;
border-radius:10px;text-decoration:none;font-weight:700;margin-top:16px}}
</style>
</head>
<body>
<div class="card">
<div class="icon">✅</div>
<h1>Ad Complete!</h1>
<p>Aapka verification successful raha.</p>
<div class="highlight">🎬 {name}</div>
<p>Ab <strong>Telegram Bot</strong> mein wapas jao aur<br>
<strong>"🔓 Unlock Step 2"</strong> button dabao.</p>
{button}
</div>
</body>
</html>"""

_FAIL_PAGE = """<!DOCTYPE html>
<html lang="hi"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Verification Failed</title></head>
<body style="text-align:center;padding:50px 20px;font-family:sans-serif;background:#f0f2f5">
<h1>❌ Verification Failed</h1>
<p>Link invalid ya expire ho gaya hai.<br>
Telegram bot mein wapas jao aur Step 1 dobara karo.</p>
</body></html>"""


@app.route("/")
def home():
    return "Movie Bot Server is Running", 200


@app.route("/health")
def health():
    return "OK", 200


@app.route("/verify")
def verify_user():
    """Arolinks ad complete hone ke baad user yahan aata hai."""
    user_id = (request.args.get("u") or "").strip()
    movie_id = (request.args.get("m") or "").strip()

    movie_name = "Unknown"
    ok = False

    if user_id and movie_id:
        movie = get_movie(movie_id)
        if movie:
            movie_name = movie["name"]
            ok = mark_user_verified(movie_id, user_id)

    if not ok:
        return _FAIL_PAGE, 400

    button = (
        f'<a href="https://t.me/{html.escape(BOT_USERNAME)}" class="btn">Open Telegram Bot →</a>'
        if BOT_USERNAME
        else ""
    )
    return _SUCCESS_PAGE.format(name=html.escape(str(movie_name)), button=button), 200


def run_flask() -> None:
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ═══════════════════════════════════════════════════════════════
#                    AROLINKS API
# ═══════════════════════════════════════════════════════════════

async def build_arolink(verify_url: str):
    """
    Arolinks API call. requests.params use kiya hai taaki URL
    properly encode ho — warna '&m=movie_id' kat jaata tha.
    Returns (short_url, error_message).
    """
    if not AROLINKS_API:
        return None, "AROLINKS_API set nahi hai (Render env variable)"

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
        if text.startswith("http"):
            return text, None
        return None, f"Invalid response (HTTP {resp.status_code})"

    if isinstance(data, dict):
        status = str(data.get("status", "")).lower()
        if status and status not in ("success", "ok", "true"):
            return None, str(
                data.get("message") or data.get("error") or "API ne error diya"
            )
        for key in ("shortenedUrl", "shortened_url", "shortUrl", "short_url",
                    "result", "url", "link"):
            val = data.get(key)
            if val:
                return str(val), None
        return None, "Response mein short link nahi mila"

    if isinstance(data, str) and data.startswith("http"):
        return data, None

    return None, "Unexpected API response"


# ═══════════════════════════════════════════════════════════════
#                    TELEGRAM HELPERS
# ═══════════════════════════════════════════════════════════════

async def check_membership(bot, user_id: int) -> bool:
    """User public channel ka member hai ya nahi."""
    if not PUBLIC_CHANNEL_ID:
        return True  # Channel configure nahi hua, block mat karo
    try:
        member = await bot.get_chat_member(PUBLIC_CHANNEL_ID, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except Exception as exc:
        print(f"[membership] error: {exc}")
        return False


async def get_channel_link(bot) -> str:
    """Public channel ka join link."""
    try:
        chat = await bot.get_chat(PUBLIC_CHANNEL_ID)
        if chat.username:
            return f"https://t.me/{chat.username}"
        if chat.invite_link:
            return chat.invite_link
        inv = await bot.export_chat_invite_link(PUBLIC_CHANNEL_ID)
        return inv
    except Exception as exc:
        print(f"[channel link] error: {exc}")
        return "https://t.me/"


async def _send_or_edit(update: Update, text: str, keyboard):
    """Callback se aaya ho to edit karo, warna naya message bhejo."""
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )


# ═══════════════════════════════════════════════════════════════
#                    USER FLOW
# ═══════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return

    if context.args and context.args[0].startswith("movie_"):
        movie_id = context.args[0][len("movie_"):]
        await handle_movie_request(update, context, movie_id)
        return

    text = (
        "🎬 *Movie Downloader Bot*\n\n"
        "Channel mein se kisi bhi movie ka *DOWNLOAD* button dabao, "
        "link yahin mil jayega.\n\n"
        "*Commands*\n"
        "/movies — available movies\n"
        "/help — kaise use karein"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    text = (
        "*Kaise download karein?*\n\n"
        "1️⃣ Channel mein movie ka DOWNLOAD dabao\n"
        "2️⃣ Channel join karo (agar nahi kiya)\n"
        "3️⃣ Step 1 ka link kholo aur ad complete karo\n"
        "4️⃣ Browser mein '✅ Ad Complete' dikhe\n"
        "5️⃣ Bot mein wapas aake Unlock Step 2 dabao\n"
        "6️⃣ Filespay se movie download karo"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def handle_movie_request(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: str):
    """Download button se aaya hua request. context ab properly pass hota hai."""
    user_id = update.effective_user.id
    bot = context.bot

    movie = get_movie(movie_id)
    if not movie:
        await update.effective_message.reply_text(
            "❌ Movie database mein nahi mili. Admin se bolo dobara add kare."
        )
        return

    if not await check_membership(bot, user_id):
        channel_link = await get_channel_link(bot)
        text = (
            "⚠️ *Channel Join Required*\n\n"
            "Movie download karne se pehle channel join karna zaroori hai.\n\n"
            "1️⃣ Neeche *Join Channel* dabao\n"
            "2️⃣ Channel join karo\n"
            "3️⃣ Wapas aake *I Joined* dabao\n\n"
            f"🎬 {md(movie['name'])}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Channel", url=channel_link)],
            [InlineKeyboardButton("✅ I Joined — Check Now",
                                  callback_data=f"checkjoin_{movie_id}")],
        ])
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
        return

    await show_step1(update, movie_id, movie)


async def show_step1(update: Update, movie_id: str, movie: dict):
    user_id = str(update.effective_user.id)

    if is_user_verified(movie_id, user_id):
        await show_step2(update, movie_id, movie)
        return

    text = (
        f"🎬 *{md(movie['name'])}*\n"
        f"📦 Size: {md(movie.get('size', '-'))}\n"
        f"🎞 Quality: {md(movie.get('quality', '-'))}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📎 *STEP 1 — Watch Ad to Unlock*\n\n"
        "Pehle button se ad link lo, browser mein complete karo.\n"
        "'✅ Ad Complete' page dikhne ke baad wapas aake "
        "*Unlock Step 2* dabao."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Get Step 1 Link (Watch Ad)",
                              callback_data=f"getarolink_{movie_id}")],
        [InlineKeyboardButton("🔓 Unlock Step 2",
                              callback_data=f"unlock_{movie_id}")],
    ])
    await _send_or_edit(update, text, keyboard)


async def show_step2(update: Update, movie_id: str, movie: dict):
    text = (
        "✅ *Verification Successful*\n\n"
        f"🎬 *{md(movie['name'])}*\n"
        f"📦 {md(movie.get('size', '-'))} | 🎞 {md(movie.get('quality', '-'))}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📥 *STEP 2 — Download Link Ready*\n\n"
        "Neeche button se Filespay par jao aur movie download karo."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download Now", url=movie["filespay_link"])]
    ])
    await _send_or_edit(update, text, keyboard)


# ═══════════════════════════════════════════════════════════════
#                    CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════════

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    user_id = str(update.effective_user.id)

    # ── JOIN CHECK ──
    if data.startswith("checkjoin_"):
        movie_id = data[len("checkjoin_"):]
        if await check_membership(context.bot, int(user_id)):
            await query.answer("✅ Join ho gaya!", show_alert=False)
            movie = get_movie(movie_id)
            if movie:
                await show_step1(update, movie_id, movie)
        else:
            await query.answer("❌ Pehle channel join karo!", show_alert=True)
        return

    # ── AROLINKS GENERATE ──
    if data.startswith("getarolink_"):
        movie_id = data[len("getarolink_"):]
        await query.answer()  # spinner turant band, API slow ho sakti hai

        movie = get_movie(movie_id)
        if not movie:
            await query.edit_message_text("❌ Movie nahi mili.")
            return

        await query.edit_message_text("⏳ Ad link ban raha hai, 2 second...")

        verify_url = f"{WEB_SERVER_URL}/verify?u={user_id}&m={movie_id}"
        aro_link, error = await build_arolink(verify_url)

        if not aro_link:
            await query.edit_message_text(
                f"❌ Link generate nahi hua.\n`{error}`\n\nThodi der baad dobara try karo.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        text = (
            "🔗 *Step 1 Link Ready*\n\n"
            f"`{aro_link}`\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*Instructions*\n"
            "1️⃣ Neeche button se browser mein kholo\n"
            "2️⃣ Ad complete karo\n"
            "3️⃣ '✅ Ad Complete' page dekho\n"
            "4️⃣ Bot mein wapas aake *Unlock Step 2* dabao"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Open Ad Link", url=aro_link)],
            [InlineKeyboardButton("🔓 Unlock Step 2", callback_data=f"unlock_{movie_id}")],
            [InlineKeyboardButton("↩️ Back", callback_data=f"step1back_{movie_id}")],
        ])
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
        return

    # ── BACK TO STEP 1 ──
    if data.startswith("step1back_"):
        await query.answer()
        movie_id = data[len("step1back_"):]
        movie = get_movie(movie_id)
        if movie:
            await show_step1(update, movie_id, movie)
        else:
            await query.edit_message_text("❌ Movie nahi mili.")
        return

    # ── UNLOCK STEP 2 ──
    if data.startswith("unlock_"):
        movie_id = data[len("unlock_"):]
        movie = get_movie(movie_id)

        if not movie:
            await query.answer("❌ Movie nahi mili", show_alert=True)
            return

        if is_user_verified(movie_id, user_id):
            await query.answer("✅ Verified!", show_alert=False)
            await show_step2(update, movie_id, movie)
        else:
            await query.answer(
                "❌ Pehle Step 1 ka ad complete karo!\n"
                "Browser mein '✅ Ad Complete' dikhne ke baad hi yeh button kaam karega.",
                show_alert=True,
            )
        return

    await query.answer()


# ═══════════════════════════════════════════════════════════════
#                    ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════

async def add_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    if update.effective_user.id != ADMIN_ID:
        await update.effective_message.reply_text("❌ Sirf admin movie add kar sakta hai.")
        return

    raw = (update.effective_message.text or "").split(maxsplit=1)
    if len(raw) < 2 or "|" not in raw[1]:
        await update.effective_message.reply_text(
            "*Galat format*\n\n"
            "`/addmovie Name | filespay_link | size | quality | poster_url`\n\n"
            "*Example:*\n"
            "`/addmovie Avengers Endgame | https://filespay.net/abc | 2.1GB | 1080p | https://img.com/poster.jpg`\n\n"
            "Poster URL optional hai.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    parts = [p.strip() for p in raw[1].split("|")]
    if len(parts) < 4:
        await update.effective_message.reply_text("❌ Kam se kam 4 fields chahiye (Name | link | size | quality).")
        return

    name, filespay_link, size, quality = parts[0], parts[1], parts[2], parts[3]
    poster_url = parts[4] if len(parts) > 4 else ""

    movie_id = add_movie_to_db(name, filespay_link, size, quality, poster_url)

    # Private channel = database log
    if PRIVATE_CHANNEL_ID:
        try:
            await context.bot.send_message(
                PRIVATE_CHANNEL_ID,
                f"📦 *MOVIE ENTRY*\n\n"
                f"🆔 ID: `{movie_id}`\n"
                f"🎬 Name: {md(name)}\n"
                f"📦 Size: {md(size)}\n"
                f"🎞 Quality: {md(quality)}\n"
                f"🔗 Filespay: `{filespay_link}`\n"
                f"🖼 Poster: {md(poster_url or 'None')}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            await update.effective_message.reply_text(f"⚠️ Private channel error: {exc}")

    # Public channel = user-facing post
    if PUBLIC_CHANNEL_ID:
        try:
            deep_link = f"https://t.me/{BOT_USERNAME}?start=movie_{movie_id}" if BOT_USERNAME else ""
            caption = (
                f"🎬 *{md(name)}*\n\n"
                f"📦 Size: {md(size)}\n"
                f"🎞 Quality: {md(quality)}\n\n"
                "⬇️ Download ke liye neeche button dabao"
            )
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬇️ DOWNLOAD NOW", url=deep_link)]]
            ) if deep_link else None

            if poster_url:
                sent = await context.bot.send_photo(
                    PUBLIC_CHANNEL_ID, photo=poster_url, caption=caption,
                    parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard,
                )
            else:
                sent = await context.bot.send_message(
                    PUBLIC_CHANNEL_ID, caption,
                    parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard,
                )
            set_public_msg_id(movie_id, sent.message_id)
        except Exception as exc:
            await update.effective_message.reply_text(f"⚠️ Public channel error: {exc}")

    await update.effective_message.reply_text(
        f"✅ *Movie added*\n\n🎬 {md(name)}\n🆔 `{movie_id}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def list_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin ke liye detailed list."""
    if not update.effective_message:
        return
    if update.effective_user.id != ADMIN_ID:
        await update.effective_message.reply_text("❌ Sirf admin ke liye.")
        return

    movies = load_db()["movies"]
    if not movies:
        await update.effective_message.reply_text("Database khali hai.")
        return

    lines = ["🎬 *Database:*\n"]
    for mid, d in movies.items():
        lines.append(
            f"🆔 `{mid}`\n🎬 {md(d['name'])}\n"
            f"📊 Verified users: {len(d.get('verified_users', []))}\n"
            "━━━━━━━━━━"
        )
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


async def show_all_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Users ke liye movie list."""
    if not update.effective_message:
        return

    movies = load_db()["movies"]
    if not movies:
        await update.effective_message.reply_text("Abhi koi movie available nahi hai.")
        return

    base = f"https://t.me/{BOT_USERNAME}?start=movie_" if BOT_USERNAME else ""
    buttons = []
    for mid, d in movies.items():
        if not base:
            buttons.append([InlineKeyboardButton(f"🎬 {md(d['name'])}", callback_data=f"step1back_{mid}")])
        else:
            buttons.append([InlineKeyboardButton(f"🎬 {d['name'][:40]}", url=f"{base}{mid}")])

    await update.effective_message.reply_text(
        "🎬 *Available Movies*\n\nMovie select karo:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin ke liye quick stats."""
    if not update.effective_message:
        return
    if update.effective_user.id != ADMIN_ID:
        return
    movies = load_db()["movies"]
    total_verif = sum(len(m.get("verified_users", [])) for m in movies.values())
    await update.effective_message.reply_text(
        f"📊 *Stats*\n\nMovies: {len(movies)}\nTotal verified users: {total_verif}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ═══════════════════════════════════════════════════════════════
#                    ERROR HANDLER
# ═══════════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"[error] {context.error}")
    if context.error and ADMIN_ID:
        try:
            await context.bot.send_message(ADMIN_ID, f"⚠️ Bot error:\n{context.error}")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#                    MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable set nahi hai")

    threading.Thread(target=run_flask, daemon=True).start()
    print(f"[boot] flask on port {os.environ.get('PORT', '5000')}")
    print(f"[boot] web_server_url = {WEB_SERVER_URL}")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("movies", show_all_movies))
    application.add_handler(CommandHandler("addmovie", add_movie))
    application.add_handler(CommandHandler("list", list_movies))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(error_handler)

    print("[boot] bot polling started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
