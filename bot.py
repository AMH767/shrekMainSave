import asyncio
import logging
import os
import tempfile
from telethon import TelegramClient, events

# ============================================================
#  НАСТРОЙКИ
# ============================================================
API_ID       = int(os.getenv("API_ID", "0"))
API_HASH     = os.getenv("API_HASH", "")
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
SESSION_FILE = os.getenv("SESSION_FILE", "my_account")

SAVEAS_BOT   = "SaveAsBot"
WAIT_TIMEOUT = 120  # секунд ожидания ответа
BUFFER_DELAY = 2.5  # секунд ожидания доп. сообщений от SaveAsBot
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

userbot = TelegramClient(SESSION_FILE, API_ID, API_HASH)
bot     = TelegramClient("bot_session", API_ID, API_HASH)

# pending[sent_msg_id] = (user_id, future)
pending: dict[int, tuple[int, asyncio.Future]] = {}

# Буфер сообщений от SaveAsBot
pending_buffer: dict[int, list] = {}
pending_timers: dict[int, asyncio.TimerHandle] = {}

# Занятые юзеры
user_waiting: dict[int, bool] = {}


# ─────────────────────────────────────────────
#  Доставка одного сообщения юзеру
# ─────────────────────────────────────────────
async def deliver_message(user_id: int, msg):
    try:
        if msg.media:
            with tempfile.TemporaryDirectory() as tmpdir:
                path = await userbot.download_media(msg, file=tmpdir)
                if path:
                    caption = msg.text or msg.raw_text or ""
                    await bot.send_file(user_id, path, caption=caption)
                    log.info(f"Sent file to user={user_id}: {os.path.basename(path)}")
                    return
        # Нет медиа или не скачалось — отправляем текст
        text = msg.text or msg.raw_text or ""
        if text:
            await bot.send_message(user_id, text)
    except Exception as e:
        log.error(f"deliver_message error for user={user_id}: {e}", exc_info=True)
        try:
            await bot.send_message(user_id, f"⚠️ Не удалось доставить файл: {e}")
        except Exception:
            pass


# ─────────────────────────────────────────────
#  Сброс буфера после задержки
# ─────────────────────────────────────────────
async def flush_buffer(origin_id: int, user_id: int, future: asyncio.Future):
    msgs = pending_buffer.pop(origin_id, [])
    pending_timers.pop(origin_id, None)

    for msg in msgs:
        await deliver_message(user_id, msg)

    if not future.done():
        future.set_result(True)

    log.info(f"Flushed {len(msgs)} msg(s) to user={user_id}")


# ─────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="/start"))
async def on_start(event):
    await event.respond(
        "👋 Привет! Я бот-посредник для **SaveAsBot**.\n\n"
        "📌 Отправь мне:\n"
        "• Ссылку на пост/канал/видео\n"
        "• Любое текстовое сообщение\n\n"
        "Я передам его в @SaveAsBot и верну тебе результат 🚀"
    )


# ─────────────────────────────────────────────
#  Входящее сообщение от юзера
# ─────────────────────────────────────────────
@bot.on(events.NewMessage(incoming=True))
async def on_user_message(event):
    if event.raw_text.startswith("/"):
        return

    user_id = event.chat_id
    text    = event.raw_text.strip()

    if not text:
        await event.reply("Отправь ссылку или текст 📨")
        return

    if user_id in user_waiting:
        await event.reply("⏳ Уже обрабатываю твой запрос, подожди...")
        return

    status_msg = await event.reply("📨 Отправляю в SaveAsBot, жди ответа...")
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    user_waiting[user_id] = True

    try:
        sent = await userbot.send_message(SAVEAS_BOT, text)
        log.info(f"Sent to SaveAsBot msg_id={sent.id} from user={user_id}")

        pending[sent.id] = (user_id, future)
        pending_buffer[sent.id] = []

        await asyncio.wait_for(future, timeout=WAIT_TIMEOUT)

        try:
            await status_msg.delete()
        except Exception:
            pass

    except asyncio.TimeoutError:
        await bot.send_message(user_id, "⚠️ SaveAsBot не ответил. Попробуй позже.")
        log.warning(f"Timeout for user={user_id}")
        for k in [k for k, v in pending.items() if v[0] == user_id]:
            pending.pop(k, None)
            pending_buffer.pop(k, None)
            t = pending_timers.pop(k, None)
            if t:
                t.cancel()

    except Exception as e:
        await bot.send_message(user_id, f"❌ Ошибка: {e}")
        log.error(f"Error for user={user_id}: {e}", exc_info=True)

    finally:
        user_waiting.pop(user_id, None)


# ─────────────────────────────────────────────
#  Ответы от SaveAsBot (через userbot)
# ─────────────────────────────────────────────
@userbot.on(events.NewMessage(incoming=True, from_users=SAVEAS_BOT))
async def on_saveas_reply(event):
    msg = event.message
    log.info(f"Got reply from SaveAsBot: msg_id={msg.id}, reply_to={msg.reply_to_msg_id}")

    origin_id = msg.reply_to_msg_id

    # Ищем нужный pending
    matched_origin = None
    if origin_id and origin_id in pending:
        matched_origin = origin_id
    elif pending:
        matched_origin = next(iter(pending))

    if matched_origin is None:
        log.warning("No pending request for this SaveAsBot reply, ignoring.")
        return

    user_id, future = pending[matched_origin]

    # Добавляем в буфер
    pending_buffer.setdefault(matched_origin, []).append(msg)

    # Сбрасываем таймер
    if matched_origin in pending_timers:
        pending_timers[matched_origin].cancel()

    loop = asyncio.get_event_loop()
    handle = loop.call_later(
        BUFFER_DELAY,
        lambda oid=matched_origin, uid=user_id, f=future: asyncio.ensure_future(
            flush_buffer(oid, uid, f)
        )
    )
    pending_timers[matched_origin] = handle

    # Убираем из pending чтобы не дублировать
    pending.pop(matched_origin, None)


# ─────────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────────
async def main():
    log.info("Starting userbot...")
    await userbot.start()
    me = await userbot.get_me()
    log.info(f"Userbot ✅ @{me.username}")

    log.info("Starting bot...")
    await bot.start(bot_token=BOT_TOKEN)
    bot_me = await bot.get_me()
    log.info(f"Bot ✅ @{bot_me.username}")

    log.info("🚀 Proxy bot is running!")
    await asyncio.gather(
        userbot.run_until_disconnected(),
        bot.run_until_disconnected(),
    )

if __name__ == "__main__":
    asyncio.run(main())
