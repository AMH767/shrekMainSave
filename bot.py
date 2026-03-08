

"""
Proxy-bot: User → Bot → Userbot → @SaveAsBot → Userbot → Bot → User
"""

import asyncio
import logging
import os
import shutil
import tempfile

from telethon import TelegramClient, events

# ══════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════
API_ID      = int(os.getenv("API_ID", "38427116"))        # твой api_id
API_HASH    = os.getenv("API_HASH", "4a2603cca91ad7ee84ec10cce984206b")             # твой api_hash
BOT_TOKEN   = os.getenv("BOT_TOKEN", "8600045569:AAHQiy8xYvhbO1GbxfnmL_0yNqflTJCoa9w")            # токен твоего бота (@BotFather)
SESSION_FILE = os.getenv("SESSION_FILE", "my_account")  # имя .session файла (без расширения)


SAVEAS_BOT    = "SaveAsBot"
WAIT_TIMEOUT  = 120   # сек
BUFFER_DELAY  = 3.0   # сек
# ══════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

userbot = TelegramClient(SESSION_FILE, API_ID, API_HASH)
bot     = TelegramClient("bot_session", API_ID, API_HASH)

# pending[sent_msg_id] = {"user_id": int, "future": Future, "msgs": []}
pending: dict[int, dict] = {}
timers: dict[int, asyncio.TimerHandle] = {}
processing: set[int] = set()


# ══════════════════════════════════════════════
#  Доставка одного сообщения юзеру
# ══════════════════════════════════════════════
async def deliver(user_id: int, msg) -> None:
    try:
        if msg.media:
            # mkdtemp НЕ удаляется автоматически — удалим вручную после отправки
            tmp_dir = tempfile.mkdtemp()
            try:
                path = await userbot.download_media(msg, file=tmp_dir)
                log.info(f"Downloaded: {path}")
                if path and os.path.isfile(path):
                    caption = (msg.text or "").strip()
                    await bot.send_file(user_id, path, caption=caption)
                    log.info(f"→ user={user_id} file={os.path.basename(path)}")
                    return
                # Файл не скачался — fallback на текст
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        # Нет медиа или скачать не удалось
        text = (msg.text or msg.raw_text or "").strip()
        if text:
            await bot.send_message(user_id, text)
            log.info(f"→ user={user_id} text len={len(text)}")

    except Exception as e:
        log.error(f"deliver error user={user_id}: {e}", exc_info=True)
        try:
            await bot.send_message(user_id, f"⚠️ Не удалось доставить файл: {e}")
        except Exception:
            pass


# ══════════════════════════════════════════════
#  Сброс буфера
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
#  /start
# ══════════════════════════════════════════════
@bot.on(events.NewMessage(pattern=r"^/start$"))
async def cmd_start(event):
    await event.respond(
        "👋 Привет!\n\n"
        "📌 Отправь мне ссылку или текст — "
        "и я верну тебе результат 🚀"
    )
    raise events.StopPropagation


# ══════════════════════════════════════════════
#  Входящее сообщение от юзера
# ══════════════════════════════════════════════
@bot.on(events.NewMessage(incoming=True))
async def on_message(event):
    user_id = event.chat_id
    text    = (event.raw_text or "").strip()

    if text.startswith("/"):
        return

    if not text:
        await event.reply("Отправь ссылку или текст 📨")
        return

    if user_id in processing:
        await event.reply("⏳ Уже обрабатываю твой предыдущий запрос, подожди...")
        return

    status = await event.reply("⏳ Обрабатываю запрос…")
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
        for k in [k for k, v in list(pending.items()) if v["user_id"] == user_id]:
            pending.pop(k, None)
            t = timers.pop(k, None)
            if t:
                t.cancel()
        try:
            await status.delete()
        except Exception:
            pass
        await bot.send_message(user_id, "⚠️ Не удалось получить результат. Попробуй позже.")

    except Exception as e:
        log.error(f"on_message error user={user_id}: {e}", exc_info=True)
        try:
            await status.delete()
        except Exception:
            pass
        await bot.send_message(user_id, f"❌ Внутренняя ошибка: {e}")

    finally:
        processing.discard(user_id)


# ══════════════════════════════════════════════
#  Ответы от SaveAsBot (через userbot)
# ══════════════════════════════════════════════
@userbot.on(events.NewMessage(incoming=True, from_users=SAVEAS_BOT))
async def on_saveas(event):
    msg = event.message
    log.info(f"SaveAsBot → msg_id={msg.id}, reply_to={msg.reply_to_msg_id}")

    # Находим нужный pending
    origin_id: int | None = None
    if msg.reply_to_msg_id and msg.reply_to_msg_id in pending:
        origin_id = msg.reply_to_msg_id
    elif pending:
        origin_id = next(iter(pending))
    else:
        log.warning("No pending entry, skip")
        return

    pending[origin_id]["msgs"].append(msg)

    # Перезапускаем таймер
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

    log.info("🚀 Running.")
    await asyncio.gather(
        userbot.run_until_disconnected(),
        bot.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())



