# ping_pong_async.py  – минимальный ping/pong, совместим с PTB 22.x
import os, asyncio
from telegram import Update
from telegram.ext import (
    Application, CommandHandler,
    MessageHandler, ContextTypes, filters
)

TOKEN = os.getenv("SCANNER_BOT_TOKEN")          # добавь токен в ENV

async def ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def main():
    if not TOKEN:
        raise RuntimeError("SCANNER_BOT_TOKEN not set")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^ping$"), ping))

    # — ручной запуск вместо run_polling() —
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    print("Bot is running. Press Ctrl-C to stop.")

    # держим процесс живым
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
