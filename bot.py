

"""
Proxy-bot: User → Bot → Userbot → @SaveAsBot → Userbot → Bot → User
Никаких forward_messages — только download + send_file/send_message
"""

import asyncio
import logging
import os
import tempfile

from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetHistoryRequest

# ══════════════════════════════════════════════
#  НАСТРОЙКИ (через переменные окружения)

API_ID      = int(os.getenv("API_ID", "38427116"))        # твой api_id
API_HASH    = os.getenv("API_HASH", "4a2603cca91ad7ee84ec10cce984206b")             # твой api_hash
BOT_TOKEN   = os.getenv("BOT_TOKEN", "8600045569:AAHQiy8xYvhbO1GbxfnmL_0yNqflTJCoa9w")            # токен твоего бота (@BotFather)
SESSION_FILE = os.getenv("SESSION_FILE", "my_account")  # имя .session файла (без расширения)



SAVEAS_BOT    = "SaveAsBot"
WAIT_TIMEOUT  = 120   # сек — сколько ждать ответа от SaveAsBot
BUFFER_DELAY  = 3.0   # сек — ждём доп. сообщения от SaveAsBot перед отправкой
# ══════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# Два клиента
userbot = TelegramClient(SESSION_FILE, API_ID, API_HASH)
bot     = TelegramClient("bot_session",  API_ID, API_HASH)

# ── Состояние ──────────────────────────────────
# pending[sent_msg_id] = {"user_id": int, "future": Future, "msgs": []}
pending: dict[int, dict] = {}

# Таймеры сброса буфера
timers: dict[int, asyncio.TimerHandle] = {}

# Юзеры в процессе обработки
processing: set[int] = set()
# ───────────────────────────────────────────────


# ══════════════════════════════════════════════
#  Утилита: доставить одно сообщение юзеру
# ══════════════════════════════════════════════
async def deliver(user_id: int, msg) -> None:
    try:
        if msg.media:
            with tempfile.TemporaryDirectory() as tmp:
                path = await userbot.download_media(msg, file=tmp)
            if path and os.path.exists(path):
                caption = (msg.text or "").strip()
                await bot.send_file(user_id, path, caption=caption)
                log.info(f"→ user={user_id} file={os.path.basename(path)}")
                return
        # Нет медиа или не скачалось
        text = (msg.text or msg.raw_text or "").strip()
        if text:
            await bot.send_message(user_id, text)
            log.info(f"→ user={user_id} text len={len(text)}")
    except Exception as e:
        log.error(f"deliver error user={user_id}: {e}", exc_info=True)
        try:
            await bot.send_message(user_id, f"⚠️ Ошибка при доставке файла: {e}")
        except Exception:
            pass


# ══════════════════════════════════════════════
#  Сброс буфера — вызывается через BUFFER_DELAY
# ══════════════════════════════════════════════
async def flush(origin_id: int) -> None:
    timers.pop(origin_id, None)
    entry = pending.pop(origin_id, None)
    if not entry:
        return

    user_id = entry["user_id"]
    msgs    = entry["msgs"]
    future  = entry["future"]

    log.info(f"flush: {len(msgs)} msg(s) → user={user_id}")
    for msg in msgs:
        await deliver(user_id, msg)

    if not future.done():
        future.set_result(True)


# ══════════════════════════════════════════════
#  БОТ — /start
# ══════════════════════════════════════════════
@bot.on(events.NewMessage(pattern=r"^/start$"))
async def cmd_start(event):
    await event.respond(
        "👋 Привет! Я бот‑посредник для @SaveAsBot.\n\n"
        "📌 Просто отправь мне ссылку или любой текст — "
        "я передам его в SaveAsBot и верну тебе результат 🚀"
    )
    raise events.StopPropagation   # не пускаем дальше в on_message


# ══════════════════════════════════════════════
#  БОТ — любое входящее сообщение
# ══════════════════════════════════════════════
@bot.on(events.NewMessage(incoming=True))
async def on_message(event):
    user_id = event.chat_id
    text    = (event.raw_text or "").strip()

    # Игнорируем команды (на случай если StopPropagation не сработал)
    if text.startswith("/"):
        return

    if not text:
        await event.reply("Отправь ссылку или текст 📨")
        return

    if user_id in processing:
        await event.reply("⏳ Уже обрабатываю твой предыдущий запрос, подожди...")
        return

    status = await event.reply("📨 Отправляю в SaveAsBot, жди ответа…")
    processing.add(user_id)
    loop   = asyncio.get_event_loop()
    future = loop.create_future()

    try:
        sent = await userbot.send_message(SAVEAS_BOT, text)
        log.info(f"sent to SaveAsBot: msg_id={sent.id}, user={user_id}")

        pending[sent.id] = {
            "user_id": user_id,
            "future":  future,
            "msgs":    [],
        }

        await asyncio.wait_for(future, timeout=WAIT_TIMEOUT)

        try:
            await status.delete()
        except Exception:
            pass

    except asyncio.TimeoutError:
        log.warning(f"timeout user={user_id}")
        # Чистим всё что осталось от этого юзера
        for k in [k for k, v in list(pending.items()) if v["user_id"] == user_id]:
            pending.pop(k, None)
            t = timers.pop(k, None)
            if t:
                t.cancel()
        await bot.send_message(user_id, "⚠️ SaveAsBot не ответил. Попробуй позже.")

    except Exception as e:
        log.error(f"on_message error user={user_id}: {e}", exc_info=True)
        await bot.send_message(user_id, f"❌ Внутренняя ошибка: {e}")

    finally:
        processing.discard(user_id)


# ══════════════════════════════════════════════
#  USERBOT — ответы от SaveAsBot
# ══════════════════════════════════════════════
@userbot.on(events.NewMessage(incoming=True, from_users=SAVEAS_BOT))
async def on_saveas(event):
    msg = event.message
    log.info(f"SaveAsBot → msg_id={msg.id}, reply_to={msg.reply_to_msg_id}")

    # Определяем origin
    origin_id: int | None = None
    if msg.reply_to_msg_id and msg.reply_to_msg_id in pending:
        origin_id = msg.reply_to_msg_id
    elif pending:
        origin_id = next(iter(pending))   # берём первый в очереди
    else:
        log.warning("No pending entry for SaveAsBot message, skip")
        return

    # Кладём сообщение в буфер
    pending[origin_id]["msgs"].append(msg)

    # Сбрасываем таймер — ждём ещё BUFFER_DELAY секунд
    old = timers.pop(origin_id, None)
    if old:
        old.cancel()

    loop = asyncio.get_event_loop()
    handle = loop.call_later(
        BUFFER_DELAY,
        lambda oid=origin_id: asyncio.ensure_future(flush(oid))
    )
    timers[origin_id] = handle


# ══════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════
async def main():
    await userbot.start()
    me = await userbot.get_me()
    log.info(f"Userbot ✅ @{me.username}")

    await bot.start(bot_token=BOT_TOKEN)
    bot_me = await bot.get_me()
    log.info(f"Bot ✅ @{bot_me.username}")

    log.info("🚀 Running. Ctrl+C to stop.")
    await asyncio.gather(
        userbot.run_until_disconnected(),
        bot.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
