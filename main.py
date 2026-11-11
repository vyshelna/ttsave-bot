"""
Бот-загрузчик: TikTok, X/Twitter, Threads, Bluesky, YT Shorts, YT Music,
Twitch Clips, SoundCloud, Instagram Reels.
Стек: aiogram 3 + yt-dlp + ffmpeg + SQLite.
"""
import asyncio
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramEntityTooLarge
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ---------- настройки ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))          # твой Telegram ID для /feedback
DONATE_URL = os.getenv("DONATE_URL", "")            # ссылка на банку (необязательно)
CHANNEL = os.getenv("CHANNEL", "")                  # @канал с новостями (необязательно)
DB_PATH = os.getenv("DB_PATH", "bot.db")
MAX_SIZE = 50 * 1024 * 1024                         # лимит Bot API на отправку файла
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "3"))  # одновременных загрузок

URL_RE = re.compile(r"https?://\S+")
SUPPORTED = (
    "tiktok.com", "twitter.com", "x.com", "threads.net", "threads.com",
    "bsky.app", "youtube.com/shorts", "youtu.be", "music.youtube.com",
    "clips.twitch.tv", "twitch.tv", "soundcloud.com", "instagram.com",
)
AUDIO_EXT = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".flac"}

logging.basicConfig(level=logging.INFO)
router = Router()
sem = asyncio.Semaphore(MAX_PARALLEL)
bot_username = ""

# ---------- база (настройки чатов) ----------
FIELDS = ("audio", "caption", "as_doc")
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    "CREATE TABLE IF NOT EXISTS chats ("
    "chat_id INTEGER PRIMARY KEY, audio INTEGER DEFAULT 1, "
    "caption INTEGER DEFAULT 1, as_doc INTEGER DEFAULT 0)"
)
db.commit()


def get_settings(chat_id: int) -> dict:
    row = db.execute(
        "SELECT audio, caption, as_doc FROM chats WHERE chat_id=?", (chat_id,)
    ).fetchone()
    if row is None:
        db.execute("INSERT INTO chats(chat_id) VALUES(?)", (chat_id,))
        db.commit()
        row = (1, 1, 0)
    return dict(zip(FIELDS, row))


def toggle_setting(chat_id: int, key: str) -> dict:
    if key not in FIELDS:
        raise ValueError(key)
    get_settings(chat_id)
    db.execute(f"UPDATE chats SET {key} = 1 - {key} WHERE chat_id=?", (chat_id,))
    db.commit()
    return get_settings(chat_id)


# ---------- тексты ----------
def start_text() -> str:
    text = (
        "Привіт!\n"
        "Щоб завантажити відео або звук з TikTok, Twitter, Threads, Bluesky, "
        "YT Shorts, YT Music, Twitch Clips, SoundCloud — надішли мені посилання 😉\n\n"
        "<b>Можливості:</b>\n"
        "🔹 <i>Завантаження відео без водяного знака;</i>\n"
        "🔹 <i>Окрема аудіодоріжка;</i>\n"
        "🔹 <i>Відправка без стиснення;</i>\n"
        "🔹 <i>Підтримка груп;</i>\n"
        "🔹 <i>Налаштування — /settings.</i>"
    )
    if DONATE_URL:
        text += f"\n\n🔔 Підтримати: {DONATE_URL}"
    if CHANNEL:
        text += f"\n✨ Новини бота: {CHANNEL}"
    return text


HELP_TEXT = (
    "<b>Як користуватись</b>\n\n"
    "1. Скопіюй посилання на відео чи трек.\n"
    "2. Надішли його мені — і отримаєш файл.\n\n"
    "<b>У групі:</b> додай бота в групу, і він автоматично завантажуватиме "
    "всі посилання, які туди скидають.\n\n"
    "/settings — налаштування\n/feedback текст — написати автору\n/privacy — приватність"
)

PRIVACY_TEXT = (
    "🔐 <b>Приватність</b>\n\n"
    "<b>Що ми зберігаємо:</b> лише ID чату та його налаштування.\n"
    "<b>Що НЕ зберігаємо:</b> історію повідомлень, посилання, завантажені файли "
    "(вони видаляються одразу після відправки).\n\n"
    "<b>Як видалити дані:</b> для групи — видаліть з неї бота; "
    "для особистого чату — заблокуйте бота."
)


def settings_kb(s: dict) -> InlineKeyboardMarkup:
    mark = lambda v: "✅" if v else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Без стиснення {mark(s['as_doc'])}", callback_data="set:as_doc")],
        [
            InlineKeyboardButton(text=f"Аудіо {mark(s['audio'])}", callback_data="set:audio"),
            InlineKeyboardButton(text=f"Підпис {mark(s['caption'])}", callback_data="set:caption"),
        ],
    ])


# ---------- команды ----------
@router.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 Додати до групи",
                             url=f"https://t.me/{bot_username}?startgroup=true")
    ]])
    await message.answer(start_text(), reply_markup=kb, disable_web_page_preview=True)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


@router.message(Command("privacy"))
async def cmd_privacy(message: Message):
    await message.answer(PRIVACY_TEXT)


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    await message.answer("Налаштування чату:", reply_markup=settings_kb(get_settings(message.chat.id)))


@router.callback_query(F.data.startswith("set:"))
async def cb_settings(cb: CallbackQuery):
    chat = cb.message.chat
    if chat.type != ChatType.PRIVATE:
        member = await cb.bot.get_chat_member(chat.id, cb.from_user.id)
        if member.status not in ("administrator", "creator"):
            await cb.answer("Змінювати налаштування можуть лише адміни групи", show_alert=True)
            return
    s = toggle_setting(chat.id, cb.data.split(":", 1)[1])
    await cb.message.edit_reply_markup(reply_markup=settings_kb(s))
    await cb.answer()


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Напиши так: <code>/feedback твоє повідомлення</code>")
        return
    if ADMIN_ID:
        user = message.from_user
        await message.bot.send_message(
            ADMIN_ID,
            f"📩 Відгук від {user.full_name} (@{user.username}, id {user.id}):\n\n{command.args}",
            parse_mode=None,
        )
    await message.answer("Дякую! Повідомлення надіслано 🙌")


# ---------- загрузка ----------
def _download(url: str, folder: str):
    opts = {
        "outtmpl": f"{folder}/%(id)s.%(ext)s",
        # сначала пробуем уложиться в 50 МБ, иначе берём лучшее
        "format": "bv*[filesize<?45M]+ba/b[filesize<?50M]/bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = sorted(p for p in Path(folder).iterdir() if p.is_file())
    return info, files


async def _extract_mp3(video: Path) -> Path | None:
    out = video.with_suffix(".mp3")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
        "-vn", "-c:a", "libmp3lame", "-q:a", "4", str(out),
    )
    await proc.wait()
    return out if proc.returncode == 0 and out.exists() and out.stat().st_size > 0 else None


@router.message(F.text.regexp(URL_RE))
async def on_link(message: Message):
    url = URL_RE.search(message.text).group(0)
    private = message.chat.type == ChatType.PRIVATE

    if not any(domain in url for domain in SUPPORTED):
        if private:
            await message.reply("Цей сайт поки не підтримується 😔")
        return

    s = get_settings(message.chat.id)
    caption = f'<a href="https://t.me/{bot_username}">@{bot_username}</a>' if s["caption"] else None
    status = await message.reply("⏳ Завантажую...")
    tmp = tempfile.mkdtemp()
    try:
        async with sem:
            info, files = await asyncio.to_thread(_download, url, tmp)
        if not files:
            raise RuntimeError("no files")

        media = files[0]
        if media.stat().st_size > MAX_SIZE:
            await status.edit_text("Файл більший за 50 МБ — Telegram не дозволяє ботам надсилати такі 😔")
            return

        title = (info.get("title") or "")[:64] or None
        performer = info.get("uploader") or info.get("artist")

        if media.suffix.lower() in AUDIO_EXT:
            await message.answer_audio(FSInputFile(media), caption=caption,
                                       title=title, performer=performer)
        else:
            if s["as_doc"]:
                await message.answer_document(FSInputFile(media), caption=caption)
            else:
                await message.answer_video(
                    FSInputFile(media), caption=caption, supports_streaming=True,
                    width=info.get("width"), height=info.get("height"),
                    duration=int(info["duration"]) if info.get("duration") else None,
                )
            if s["audio"]:
                mp3 = await _extract_mp3(media)
                if mp3 and mp3.stat().st_size <= MAX_SIZE:
                    await message.answer_audio(FSInputFile(mp3), title=title, performer=performer)

        await status.delete()
    except TelegramEntityTooLarge:
        await status.edit_text("Файл завеликий для Telegram 😔")
    except Exception as e:
        logging.warning("Download failed for %s: %s", url, e)
        await status.edit_text("Не вдалося завантажити 😔 Перевір посилання або спробуй пізніше.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- запуск ----------
async def start_health_server():
    """Render (Web Service) требует открытый порт — отдаём простой 'ok'."""
    port = os.getenv("PORT")
    if not port:
        return
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()


async def main():
    global bot_username
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    bot_username = (await bot.get_me()).username
    await bot.set_my_commands([
        BotCommand(command="help", description="Як користуватись"),
        BotCommand(command="settings", description="Налаштування"),
        BotCommand(command="feedback", description="Зворотний зв'язок"),
        BotCommand(command="privacy", description="Політика приватності"),
    ])
    await start_health_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
