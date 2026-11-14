"""
Бот-загрузчик: TikTok, X/Twitter, Threads, Bluesky, YouTube, YT Music,
Twitch Clips, SoundCloud, Instagram Reels.
Для YouTube — меню выбора качества, MP3, превью и обрезка.
Стек: aiogram 3 + yt-dlp + ffmpeg + SQLite.
"""
import asyncio
import html
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
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
MAX_CUT = 5 * 60                                    # максимальная длина обрезки, сек

URL_RE = re.compile(r"https?://\S+")
YOUTUBE = ("youtube.com", "youtu.be")
SUPPORTED = (
    "tiktok.com", "twitter.com", "x.com", "threads.net", "threads.com",
    "bsky.app", "clips.twitch.tv", "twitch.tv", "soundcloud.com",
    "instagram.com",
) + YOUTUBE
AUDIO_EXT = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".flac"}
TRASH_EXT = {".part", ".ytdl", ".json", ".temp"}
HEIGHTS = [144, 240, 360, 480, 720, 1080]

logging.basicConfig(level=logging.INFO)
router = Router()
sem = asyncio.Semaphore(MAX_PARALLEL)
bot_username = ""

# YouTube-сессии: token -> данные о видео (живут 1 час)
SESS: dict[str, dict] = {}
# Кто сейчас вводит промежуток для обрезки: (chat_id, user_id) -> token
PENDING_CUT: dict[tuple[int, int], str] = {}

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
        "YouTube, YT Music, Twitch Clips, SoundCloud — надішли мені посилання 😉\n\n"
        "<b>Можливості:</b>\n"
        "🔹 <i>Завантаження відео без водяного знака;</i>\n"
        "🔹 <i>Вибір якості для YouTube;</i>\n"
        "🔹 <i>Окрема аудіодоріжка та MP3;</i>\n"
        "🔹 <i>Обрізка відео;</i>\n"
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
    "<b>YouTube:</b> бот покаже меню — обери якість, MP3, превʼю або обрізку.\n\n"
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

TOO_BIG = "Файл більший за 50 МБ — Telegram не дозволяє ботам надсилати такі 😔"


def settings_kb(s: dict) -> InlineKeyboardMarkup:
    mark = lambda v: "✅" if v else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Без стиснення {mark(s['as_doc'])}", callback_data="set:as_doc")],
        [
            InlineKeyboardButton(text=f"Аудіо {mark(s['audio'])}", callback_data="set:audio"),
            InlineKeyboardButton(text=f"Підпис {mark(s['caption'])}", callback_data="set:caption"),
        ],
    ])


# ---------- ошибки и повторы ----------
NO_RETRY = ("private", "unsupported url", "no video formats", "unavailable",
            "not available", "removed", "sign in to confirm", "copyright", "404")


def explain_error(err: Exception, url: str) -> str:
    m = str(err).lower()
    if "sign in to confirm" in m:
        return "YouTube тимчасово блокує завантаження з нашого сервера 😔 Спробуй пізніше."
    if "private" in m:
        return "Це приватне відео — його неможливо завантажити 🔒"
    if "tiktok" in url and ("/photo/" in url or "unsupported url" in m or "no video formats" in m):
        return "Схоже, це слайдшоу з фото — такі пости поки не підтримуються 🖼"
    if any(k in m for k in ("unavailable", "not available", "removed", "404", "copyright")):
        return "Відео недоступне: його видалили або воно заблоковане в регіоні 🌍"
    if "429" in m or "too many requests" in m:
        return "Сайт тимчасово обмежив запити. Спробуй за кілька хвилин ⏳"
    return "Не вдалося завантажити 😔 Перевір посилання або спробуй пізніше."


async def with_retry(func, *args, attempts: int = 3):
    """Запускает загрузку в отдельном потоке, при временной ошибке повторяет."""
    for i in range(attempts):
        try:
            return await asyncio.to_thread(func, *args)
        except Exception as e:
            msg = str(e).lower()
            if i == attempts - 1 or any(k in msg for k in NO_RETRY):
                raise
            logging.info("Retry %s after error: %s", i + 1, e)
            await asyncio.sleep(2 * (i + 1))


# ---------- yt-dlp ----------
BASE_OPTS = {
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "merge_output_format": "mp4",
}


def _download(url: str, folder: str, extra: dict | None = None):
    opts = dict(BASE_OPTS)
    opts["outtmpl"] = f"{folder}/%(id)s.%(ext)s"
    # по умолчанию: стараемся уложиться в 50 МБ, иначе берём лучшее
    opts["format"] = "bv*[filesize<?45M]+ba/b[filesize<?50M]/bv*+ba/b"
    if extra:
        opts.update(extra)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = [p for p in Path(folder).iterdir()
             if p.is_file() and p.suffix.lower() not in TRASH_EXT]
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return info, files


def _info(url: str) -> dict:
    with yt_dlp.YoutubeDL(dict(BASE_OPTS)) as ydl:
        return ydl.extract_info(url, download=False)


async def _probe_duration(path: Path) -> int | None:
    """Реальная длительность файла (а не та, что заявлена на сайте)."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        return int(float(out.decode().strip()))
    except ValueError:
        return None


async def _extract_mp3(video: Path) -> Path | None:
    out = video.with_suffix(".mp3")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
        "-vn", "-c:a", "libmp3lame", "-q:a", "4", str(out),
    )
    await proc.wait()
    return out if proc.returncode == 0 and out.exists() and out.stat().st_size > 0 else None


def caption_for(chat_id: int) -> str | None:
    if get_settings(chat_id)["caption"]:
        return f'<a href="https://t.me/{bot_username}">@{bot_username}</a>'
    return None


async def send_media(target: Message, media: Path, info: dict, *, extra_mp3: bool = False):
    """Отправляет файл в чат target. Возвращает False, если файл слишком большой."""
    if media.stat().st_size > MAX_SIZE:
        return False
    s = get_settings(target.chat.id)
    caption = caption_for(target.chat.id)
    title = (info.get("title") or "")[:64] or None
    performer = info.get("uploader") or info.get("artist")
    duration = await _probe_duration(media)

    if media.suffix.lower() in AUDIO_EXT:
        await target.answer_audio(FSInputFile(media), caption=caption, title=title,
                                  performer=performer, duration=duration)
        return True
    if s["as_doc"]:
        await target.answer_document(FSInputFile(media), caption=caption)
    else:
        await target.answer_video(
            FSInputFile(media), caption=caption, supports_streaming=True,
            width=info.get("width"), height=info.get("height"), duration=duration,
        )
    if extra_mp3 and s["audio"]:
        mp3 = await _extract_mp3(media)
        if mp3 and mp3.stat().st_size <= MAX_SIZE:
            await target.answer_audio(FSInputFile(mp3), title=title,
                                      performer=performer, duration=duration)
    return True


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


# ---------- YouTube: меню форматов ----------
def fmt_size(b: int | None) -> str:
    return f"{b / 1024 / 1024:.1f} МБ" if b else "? МБ"


def fmt_dur(sec) -> str:
    if not sec:
        return "?"
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def build_yt_options(info: dict) -> dict:
    """Для каждого качества (по короткой стороне кадра) — формат и примерный размер."""
    formats = info.get("formats") or []
    audio = [f for f in formats
             if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    m4a = [f for f in audio if f.get("ext") == "m4a"]
    best_audio = max(m4a or audio, key=lambda f: f.get("abr") or 0, default=None)
    a_size = 0
    if best_audio:
        a_size = best_audio.get("filesize") or best_audio.get("filesize_approx") or 0

    result = {}
    for h in HEIGHTS:
        cands = []
        for f in formats:
            if f.get("vcodec") in (None, "none"):
                continue
            w, hh = f.get("width") or 0, f.get("height") or 0
            p = min(w, hh) if w and hh else hh
            if p == h:
                cands.append(f)
        if not cands:
            continue
        avc = [f for f in cands if str(f.get("vcodec", "")).startswith("avc")]
        f = max(avc or cands, key=lambda f: f.get("tbr") or 0)
        has_audio = f.get("acodec") not in (None, "none")
        vsize = f.get("filesize") or f.get("filesize_approx")
        size = (vsize + (0 if has_audio else a_size)) if vsize else None
        if has_audio or not best_audio:
            sel = f["format_id"]
        else:
            sel = f"{f['format_id']}+{best_audio['format_id']}"
        result[h] = {"sel": sel, "size": size}
    return result


def pick_thumb(info: dict) -> str | None:
    thumbs = [t.get("url") for t in (info.get("thumbnails") or []) if t.get("url")]
    jpgs = [u for u in thumbs if ".jpg" in u]
    for u in reversed(jpgs):
        if "maxresdefault" in u or "hqdefault" in u:
            return u
    return (jpgs or thumbs or [info.get("thumbnail")])[-1]


def yt_keyboard(token: str, sess: dict) -> InlineKeyboardMarkup:
    rows, row = [], []
    for h, o in sess["opts"].items():
        big = o["size"] and o["size"] > MAX_SIZE
        row.append(InlineKeyboardButton(
            text=f"🚫 {h}p" if big else f"{'⚡' if h <= 360 else '🎬'} {h}p",
            callback_data=f"yt:{token}:big" if big else f"yt:{token}:{h}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    mp3_big = sess["mp3_size"] > MAX_SIZE
    rows.append([
        InlineKeyboardButton(text="🚫 MP3" if mp3_big else "🎵 MP3",
                             callback_data=f"yt:{token}:big" if mp3_big else f"yt:{token}:mp3"),
        InlineKeyboardButton(text="🖼 Превʼю", callback_data=f"yt:{token}:thumb"),
    ])
    rows.append([InlineKeyboardButton(text="✂️ Обрізка", callback_data=f"yt:{token}:cut")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def youtube_menu(message: Message, url: str):
    status = await message.reply("🔎 Отримую інформацію про відео...")
    try:
        async with sem:
            info = await with_retry(_info, url)
    except Exception as e:
        logging.warning("YT info failed for %s: %s", url, e)
        await status.edit_text(explain_error(e, url))
        return

    # чистим старые сессии
    now = time.time()
    for k in [k for k, v in SESS.items() if now - v["ts"] > 3600]:
        SESS.pop(k, None)

    duration = info.get("duration") or 0
    token = secrets.token_hex(4)
    sess = {
        "url": info.get("webpage_url") or url,
        "title": info.get("title") or "Відео",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": duration,
        "thumb": pick_thumb(info),
        "opts": build_yt_options(info),
        "mp3_size": int(duration * 192_000 / 8),
        "ts": now,
    }
    SESS[token] = sess

    lines = [f"🎬 <b>{html.escape(sess['title'][:200])}</b>",
             f"👤 {html.escape(sess['uploader'])} · ⏱ {fmt_dur(duration)}", ""]
    for h, o in sess["opts"].items():
        icon = "🚫" if o["size"] and o["size"] > MAX_SIZE else ("⚡" if h <= 360 else "🎬")
        lines.append(f"{icon} <code>{h}p: {fmt_size(o['size'])}</code>")
    lines.append(f"🎵 <code>MP3: ~{fmt_size(sess['mp3_size'])}</code>")
    lines += ["", "Обери формат для завантаження 👇"]
    text = "\n".join(lines)
    kb = yt_keyboard(token, sess)

    await status.delete()
    try:
        if sess["thumb"]:
            await message.answer_photo(sess["thumb"], caption=text, reply_markup=kb)
            return
    except Exception as e:
        logging.info("Thumb send failed: %s", e)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("yt:"))
async def cb_youtube(cb: CallbackQuery):
    _, token, action = cb.data.split(":", 2)
    sess = SESS.get(token)
    if not sess:
        await cb.answer("Посилання застаріло — надішли його ще раз 🙏", show_alert=True)
        return
    if action == "big":
        await cb.answer(TOO_BIG, show_alert=True)
        return
    if action == "thumb":
        await cb.answer()
        if sess["thumb"]:
            await cb.message.answer_photo(sess["thumb"], caption=html.escape(sess["title"][:200]))
        else:
            await cb.message.answer("У цього відео немає превʼю 🤷")
        return
    if action == "cut":
        PENDING_CUT[(cb.message.chat.id, cb.from_user.id)] = token
        await cb.answer()
        await cb.message.answer(
            "✂️ Надішли проміжок, який вирізати, у форматі <code>0:10-0:45</code>\n"
            f"Тривалість відео: {fmt_dur(sess['duration'])}. Максимум — {MAX_CUT // 60} хв."
        )
        return

    await cb.answer()
    if action == "mp3":
        label, extra = "MP3", {
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3", "preferredquality": "192"}],
        }
    else:
        opt = sess["opts"].get(int(action))
        if not opt:
            await cb.message.answer("Цей формат недоступний 😔")
            return
        label, extra = f"{action}p", {"format": opt["sel"]}
    await download_and_send(cb.message, sess["url"], extra, f"⏳ Завантажую {label}...")


# ---------- обрезка ----------
def parse_ts(s: str) -> float:
    secs = 0.0
    for part in s.strip().split(":"):
        secs = secs * 60 + float(part)
    return secs


def has_pending_cut(message: Message) -> bool:
    if not message.from_user or not message.text:
        return False
    if URL_RE.search(message.text):      # прислал новую ссылку — обрезку отменяем
        return False
    return (message.chat.id, message.from_user.id) in PENDING_CUT


@router.message(has_pending_cut)
async def on_cut_range(message: Message):
    key = (message.chat.id, message.from_user.id)
    sess = SESS.get(PENDING_CUT.get(key, ""))
    m = re.match(r"^\s*([\d:.]+)\s*[-–—]\s*([\d:.]+)\s*$", message.text)
    if not sess:
        PENDING_CUT.pop(key, None)
        await message.reply("Посилання застаріло — надішли відео ще раз 🙏")
        return
    if not m:
        await message.reply("Не зрозумів 🤔 Напиши так: <code>0:10-0:45</code>")
        return
    try:
        start, end = parse_ts(m.group(1)), parse_ts(m.group(2))
    except ValueError:
        await message.reply("Не зрозумів 🤔 Напиши так: <code>0:10-0:45</code>")
        return
    dur = sess["duration"] or 0
    if end <= start:
        await message.reply("Кінець має бути пізніше за початок 🙂")
        return
    if dur and end > dur + 1:
        await message.reply(f"Відео триває лише {fmt_dur(dur)} — вкажи менший проміжок.")
        return
    if end - start > MAX_CUT:
        await message.reply(f"Максимальна довжина обрізки — {MAX_CUT // 60} хв.")
        return

    PENDING_CUT.pop(key, None)
    extra = {
        "format": "bv*[height<=720]+ba/bv*[width<=720]+ba/b",
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
    }
    await download_and_send(message, sess["url"], extra,
                            f"✂️ Вирізаю {fmt_dur(start)}–{fmt_dur(end)}...")


# ---------- общая загрузка ----------
async def download_and_send(target: Message, url: str, extra: dict | None,
                            status_text: str, extra_mp3: bool = False):
    status = await target.answer(status_text)
    tmp = tempfile.mkdtemp()
    try:
        async with sem:
            info, files = await with_retry(_download, url, tmp, extra)
        if not files:
            raise RuntimeError("no files")
        if not await send_media(target, files[0], info, extra_mp3=extra_mp3):
            await status.edit_text(TOO_BIG)
            return
        await status.delete()
    except TelegramEntityTooLarge:
        await status.edit_text(TOO_BIG)
    except Exception as e:
        logging.warning("Download failed for %s: %s", url, e)
        await status.edit_text(explain_error(e, url))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@router.message(F.text.regexp(URL_RE))
async def on_link(message: Message):
    url = URL_RE.search(message.text).group(0)
    if message.from_user:
        PENDING_CUT.pop((message.chat.id, message.from_user.id), None)

    if not any(domain in url for domain in SUPPORTED):
        if message.chat.type == ChatType.PRIVATE:
            await message.reply("Цей сайт поки не підтримується 😔")
        return

    if any(domain in url for domain in YOUTUBE):
        await youtube_menu(message, url)
    else:
        await download_and_send(message, url, None, "⏳ Завантажую...", extra_mp3=True)


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
    logging.info("yt-dlp version: %s", yt_dlp.version.__version__)
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
