import asyncio
import os
import threading
import uuid

from flask import Flask, jsonify
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
BOT_TOKEN          = os.environ.get("BOT_TOKEN", "")
PUBLIC_CHANNEL_ID  = int(os.environ.get("PUBLIC_CHANNEL_ID", "0"))
PRIVATE_CHANNEL_ID = int(os.environ.get("PRIVATE_CHANNEL_ID", "0"))
ADMIN_ID           = int(os.environ.get("ADMIN_ID", "0"))
BOT_USERNAME       = os.environ.get("BOT_USERNAME", "").lstrip("@")
BRAND              = os.environ.get("BRAND", "🎬 Movie Hub")

DB = {"movies": {}}
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
    movies = {}
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
                    entry["msg_id"] = msg.message_id
                    movies[entry["id"]] = entry
        offset = batch[-1].message_id - 1
        pages += 1

    DB["movies"] = movies
    print(f"[rebuild] {len(movies)} titles restored")


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
        "quality": quality, "poster": poster_url, "msg_id": msg_id,
    }
    return movie_id


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


@app.route("/api/movies")
def api_movies():
    """Catalog metadata. Download links yahan expose nahi hote."""
    items = []
    for mid, m in DB["movies"].items():
        items.append({
            "id": mid,
            "name": m.get("name", "Untitled"),
            "size": m.get("size", "-"),
            "quality": m.get("quality", "-"),
            "poster": m.get("poster", ""),
            "_seq": m.get("msg_id") or 0,
        })
    items.sort(key=lambda x: x["_seq"], reverse=True)
    for it in items:
        it.pop("_seq", None)
    return jsonify({
        "ok": True,
        "brand": BRAND,
        "bot": BOT_USERNAME,
        "count": len(items),
        "movies": items,
    })


@app.route("/health")
def health():
    return f"OK movies={len(DB['movies'])}", 200


def run_flask():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


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
        "Channel mein kisi bhi movie ka *DOWNLOAD*\n"
        "button dabayein — link yahin mil jayega.\n\n"
        "⚡ Members-only • Instant • No extra steps\n"
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
        "2️⃣ Channel join karein (agar nahi kiya)\n"
        "3️⃣ Download link turant mil jayega\n\n"
        "📚 Library dekhne ke liye /movies",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_movie_request(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: str):
    user_id = update.effective_user.id
    movie = get_movie(movie_id)

    if not movie:
        await update.effective_message.reply_text(
            f"⚠️ *Content unavailable*\n{LINE}\n"
            "Yeh title abhi library mein nahi mila.\n"
            "Channel mein nayi posts dekhein.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not await check_membership(context.bot, user_id):
        channel_link = await get_channel_link(context.bot)
        text = (
            "🔒 *MEMBERS ONLY*\n"
            f"{LINE}\n"
            f"🎬 {md(movie['name'])}\n\n"
            "Download sirf channel members ke liye hai.\n\n"
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

    await show_download(update, movie)


async def show_download(update: Update, movie: dict):
    text = movie_card(movie, "✅ *Access granted* — download ready")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download Now", url=movie["link"])],
        [InlineKeyboardButton("📚 More Movies", callback_data="browse")],
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
        await render_list(update, edit=True)
        return

    if data == "helpcb":
        await query.answer()
        await query.edit_message_text(
            f"*{md(BRAND)} — Help*\n{LINE}\n"
            "1️⃣ Channel se DOWNLOAD button dabayein\n"
            "2️⃣ Channel join karein\n"
            "3️⃣ Link turant mil jayega\n\n"
            "⚡ Koi extra step nahi.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📚 Browse Movies", callback_data="browse")],
            ]),
        )
        return

    if data.startswith("join_"):
        movie_id = data[len("join_"):]
        if await check_membership(context.bot, int(user_id)):
            await query.answer("✅ Membership confirmed")
            movie = get_movie(movie_id)
            if movie:
                await show_download(update, movie)
            else:
                await query.edit_message_text("⚠️ Content nahi mila.")
        else:
            await query.answer(
                "❌ Abhi join nahi hua — Join Channel dabayein", show_alert=True
            )
        return

    if data.startswith("open_"):
        movie_id = data[len("open_"):]
        movie = get_movie(movie_id)
        if not movie:
            await query.answer("⚠️ Content nahi mila", show_alert=True)
            return
        if await check_membership(context.bot, int(user_id)):
            await query.answer()
            await show_download(update, movie)
        else:
            await query.answer("❌ Pehle channel join karein", show_alert=True)
        return

    await query.answer()


# ═══════════════════════════════════════════════════════════════
#  LIBRARY LIST
# ═══════════════════════════════════════════════════════════════

async def render_list(update: Update, edit=False):
    movies = DB["movies"]
    if not movies:
        text = f"📭 *Library khali hai*\n{LINE}\nAbhi koi title available nahi."
        if edit:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    buttons = []
    for mid, m in list(movies.items())[:40]:
        buttons.append([
            InlineKeyboardButton(f"🎬 {m['name'][:38]}", callback_data=f"open_{mid}")
        ])

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
        await render_list(update, edit=False)


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
        "• Database ✔\n• Public post ✔",
        parse_mode=ParseMode.MARKDOWN,
    )


async def list_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or update.effective_user.id != ADMIN_ID:
        return
    movies = DB["movies"]
    if not movies:
        await update.effective_message.reply_text("Database khali hai.")
        return
    lines = [f"*Database — {len(movies)} titles*\n{LINE}"]
    for mid, m in movies.items():
        lines.append(f"🔖 `{mid}`  🎬 {md(m['name'])}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def del_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or update.effective_user.id != ADMIN_ID:
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
    await update.effective_message.reply_text(f"🗑 `{mid}` remove ho gaya.", parse_mode=ParseMode.MARKDOWN)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or update.effective_user.id != ADMIN_ID:
        return
    await update.effective_message.reply_text(
        f"📊 *Stats*\n{LINE}\n🎬 Titles : {len(DB['movies'])}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ═══════════════════════════════════════════════════════════════
#  BOOT
# ═══════════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("[error]", context.error)
    if ADMIN_ID:
        try:
            await context.bot.send_message(ADMIN_ID, f"⚠️ Bot error:\n{context.error}")
        except Exception:
            pass


async def post_init(application: Application):
    if PRIVATE_CHANNEL_ID:
        await rebuild_db(application.bot)
    else:
        print("[boot] PRIVATE_CHANNEL_ID not set")


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN set nahi hai")

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    threading.Thread(target=run_flask, daemon=True).start()
    print(f"[boot] flask port {os.environ.get('PORT', '5000')}")

    application = (
        Application.builder().token(BOT_TOKEN).post_init(post_init).build()
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
