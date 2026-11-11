# Saver Bot

Telegram-бот, который скачивает видео и аудио по ссылке: TikTok, X/Twitter, Threads, Bluesky, YT Shorts, YT Music, Twitch Clips, SoundCloud, Instagram Reels.

## Возможности
- видео без водяного знака (TikTok)
- отдельная аудиодорожка mp3 (включается в /settings)
- отправка без сжатия, файлом
- подпись со ссылкой на бота
- работа в группах (настройки меняют только админы)
- /help, /settings, /feedback, /privacy

## Переменные окружения
| Имя | Обязательно | Что это |
|---|---|---|
| `BOT_TOKEN` | да | токен от @BotFather |
| `ADMIN_ID` | нет | твой Telegram ID, сюда приходят /feedback |
| `DONATE_URL` | нет | ссылка на банку Monobank |
| `CHANNEL` | нет | @канал с новостями |

## Деплой на Render
1. Залей папку в новый репозиторий на GitHub.
2. Render → New → Web Service → выбери репозиторий. Runtime: **Docker**. Dockerfile ставит ffmpeg сам.
3. В Environment добавь `BOT_TOKEN` (и остальные, если нужны).
4. Deploy.

## Чтобы бот видел ссылки в группах
@BotFather → /mybots → бот → Bot Settings → Group Privacy → **Turn off**.

## Локальный запуск
```
pip install -r requirements.txt
# нужен ffmpeg в системе
set BOT_TOKEN=123:ABC   (Windows)
python main.py
```
