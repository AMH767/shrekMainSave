import asyncio
import logging
import os
from telethon import TelegramClient, events
from telethon.tl.types import User

# ============================================================
#  НАСТРОЙКИ — заполни перед запуском
# ============================================================
API_ID      = int(os.getenv("API_ID", "38427116"))        # твой api_id
API_HASH    = os.getenv("API_HASH", "4a2603cca91ad7ee84ec10cce984206b")             # твой api_hash
BOT_TOKEN   = os.getenv("BOT_TOKEN", "8600045569:AAHQiy8xYvhbO1GbxfnmL_0yNqflTJCoa9w")            # токен твоего бота (@BotFather)
SESSION_FILE = os.getenv("SESSION_FILE", "my_account")  # имя .session файла (без расширения)

SAVEAS_BOT  = "SaveAsBot"   # юзернейм бота-получателя
WAIT_TIMEOUT = 120          # секунд ожидания ответа от SaveAsBot
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# Клиент-аккаунт (userbot) — пересылает сообщения в SaveAsBot
userbot = TelegramClient(SESSION_FILE, API_ID, API_HASH)

# Клиент-бот — общается с пользователями
bot = TelegramClient("bot_session", API_ID, API_HASH)

# Словарь: saveas_msg_id → (user_chat_id, event_loop_future)
pending: dict[int, asyncio.Future] = {}

# Словарь: user_chat_id → True (флаг «ждём ответа»)
user_waiting: dict[int, bool] = {}


# ─────────────────────────────────────────────
#  БОТ: обработка входящих сообщений от юзеров
# ─────────────────────────────────────────────
@bot.on(events.NewMessage(incoming=True))
async def on_user_message(event):
    user_id = event.chat_id
    text    = event.raw_text.strip()

    if not text:
        await event.reply("Отправь любое сообщение или ссылку, и я передам в SaveAsBot 📨")
        return

    if user_id in user_waiting:
        await event.reply("⏳ Уже обрабатываю твой предыдущий запрос, подожди...")
        return

    await event.reply("📨 Отправляю в SaveAsBot, жди ответа...")

    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    user_waiting[user_id] = True

    try:
        # Пересылаем сообщение через userbot в SaveAsBot
        sent = await userbot.send_message(SAVEAS_BOT, text)
        log.info(f"Forwarded to SaveAsBot, msg_id={sent.id}, from user={user_id}")

        # Регистрируем ожидание ответа
        pending[sent.id] = (user_id, future)

        # Ждём ответа от SaveAsBot
        result = await asyncio.wait_for(future, timeout=WAIT_TIMEOUT)

        # Пересылаем результат пользователю через бот
        if isinstance(result, list):
            for msg in result:
                await bot.forward_messages(user_id, msg.id, SAVEAS_BOT)
        else:
            await bot.forward_messages(user_id, result.id, SAVEAS_BOT)

        log.info(f"Result delivered to user={user_id}")

    except asyncio.TimeoutError:
        await bot.send_message(user_id, "⚠️ SaveAsBot не ответил за отведённое время. Попробуй позже.")
        log.warning(f"Timeout waiting SaveAsBot for user={user_id}")
        # Убираем из pending
        pending_keys = [k for k, v in pending.items() if v[0] == user_id]
        for k in pending_keys:
            pending.pop(k, None)

    except Exception as e:
        await bot.send_message(user_id, f"❌ Ошибка: {e}")
        log.error(f"Error for user={user_id}: {e}", exc_info=True)

    finally:
        user_waiting.pop(user_id, None)


# ─────────────────────────────────────────────
#  USERBOT: ловим ответы от SaveAsBot
# ─────────────────────────────────────────────
@userbot.on(events.NewMessage(incoming=True, from_users=SAVEAS_BOT))
async def on_saveas_reply(event):
    msg = event.message
    log.info(f"Got reply from SaveAsBot: msg_id={msg.id}, reply_to={msg.reply_to_msg_id}")

    # Определяем к какому нашему сообщению это ответ
    origin_id = msg.reply_to_msg_id if msg.reply_to_msg_id else None

    matched_future: asyncio.Future | None = None
    matched_user_id: int | None = None

    if origin_id and origin_id in pending:
        matched_user_id, matched_future = pending.pop(origin_id)
    else:
        # Если SaveAsBot не цитирует — берём последнего ожидающего
        if pending:
            key = next(iter(pending))
            matched_user_id, matched_future = pending.pop(key)

    if matched_future and not matched_future.done():
        matched_future.set_result(msg)
        log.info(f"Future resolved for user={matched_user_id}")


# ─────────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────────
async def main():
    log.info("Starting userbot...")
    await userbot.start()
    log.info("Userbot started ✅")

    log.info("Starting bot...")
    await bot.start(bot_token=BOT_TOKEN)
    bot_me = await bot.get_me()
    log.info(f"Bot started ✅ @{bot_me.username}")

    log.info("🚀 Proxy bot is running! Press Ctrl+C to stop.")
    await asyncio.gather(
        userbot.run_until_disconnected(),
        bot.run_until_disconnected(),
    )

if __name__ == "__main__":
    asyncio.run(main())
