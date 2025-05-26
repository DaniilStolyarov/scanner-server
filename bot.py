"""
ScannerBot — Telegram-бот + WebSocket-сервер.
 • /scan или «📷 Скан» → фото + пустой description
 • любое другое сообщение → append в последний description
"""
from __future__ import annotations

import asyncio, io, logging, os, pathlib, textwrap
from datetime import datetime, timedelta, timezone
from typing import Final, Set

import websockets
from telegram import (InputFile, KeyboardButton, ReplyKeyboardMarkup, Update)
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

# ───────────── конфигурация ────────────────────────────────────────────────
class Config:
    TOKEN:   Final[str]          = os.getenv("SCANNER_BOT_TOKEN")
    WS_HOST: Final[str]          = "0.0.0.0"
    WS_PORT: Final[int]          = 8765
    TIMEOUT: Final[int]          = 15
    TZ:      Final[timezone]     = timezone(timedelta(hours=3))
    DIR_IMG: Final[pathlib.Path] = pathlib.Path("scans/images")
    DIR_DES: Final[pathlib.Path] = pathlib.Path("scans/descriptions")

# ───────────── логирование ────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("telegram.ext").setLevel(logging.DEBUG)
log = logging.getLogger("scanner")

# ───────────── основной класс ─────────────────────────────────────────────
class ScannerBot:
    def __init__(self, cfg: Config):
        self.cfg    = cfg
        self.app    = Application.builder().token(cfg.TOKEN).build()
        self.subs:  Set[int] = set()
        self._ws:   websockets.WebSocketServerProtocol | None = None
        self._img_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._last_file: pathlib.Path | None = None     # description file

    # ────────────────── публичный запуск ────────────────────────────────
    async def run(self):
        if not self.cfg.TOKEN:
            raise RuntimeError("SCANNER_BOT_TOKEN not set")

        self._ensure_dirs()
        self._restore_last_file()

        ws_srv = await websockets.serve(self._ws_handler,
                                        self.cfg.WS_HOST, self.cfg.WS_PORT)
        log.info("WS listening on ws://%s:%d", self.cfg.WS_HOST, self.cfg.WS_PORT)

        self._register_handlers()
        await self.app.initialize(), await self.app.start()
        await self.app.updater.start_polling()
        log.info("Bot polling…  Ctrl-C to stop")

        try:
            await asyncio.Event().wait()
        finally:
            await self.app.updater.stop(); await self.app.stop(); await self.app.shutdown()
            ws_srv.close(); await ws_srv.wait_closed()

    # ────────────────── Telegram handlers ───────────────────────────────
    async def _cmd_start(self, u: Update, _):
        self._subs_add(u.effective_chat.id)
        kb = ReplyKeyboardMarkup([[KeyboardButton("📷 Скан")]], resize_keyboard=True)
        await u.message.reply_text("Привет! Нажми «📷 Скан» или /scan.", reply_markup=kb)

    async def _cmd_scan(self, u: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat = u.effective_chat.id; self._subs_add(chat)
        note = await u.message.reply_text(f"📸 Ожидаем файл… (≤ {self.cfg.TIMEOUT}s)")
        try:
            if not await self._ws_send_scan():
                await note.edit_text("🚫 Сканер не подключён."); return
            img = await asyncio.wait_for(self._img_q.get(), timeout=self.cfg.TIMEOUT)
            img_path, des_path = self._save_files(img)
            await ctx.bot.send_photo(chat, InputFile(io.BytesIO(img), img_path.name),
                                     caption=f"`{img_path.name}`", parse_mode="Markdown")
            await note.delete()
        except asyncio.TimeoutError:
            await note.edit_text("⚠️ Фото не пришло.")
        except Exception as e:
            log.exception("scan error"); await note.edit_text(f"❗ Ошибка: {e}")

    async def _plain_text(self, u: Update, _):
        """Любое обычное сообщение → append в последний description + показать файл."""
        if not self._last_file:
            await u.message.reply_text("❔ Пока нет фотографий для описания.")
            return

        text = u.message.text.strip()
        if not text:
            return

        try:
            # append
            with self._last_file.open("a", encoding="utf-8") as f:
                f.write(text + "\n")

            # read full content
            full = self._last_file.read_text(encoding="utf-8")

            # Telegram hard-limit 4096; оставим небольшой запас
            MAX = 4000
            to_send = full[-MAX:] if len(full) > MAX else full
            await u.message.reply_text(
                f"✏️ Обновлено `{self._last_file.name}`:\n"
                f"{to_send}",
                parse_mode="Markdown",
            )
        except Exception as exc:
            log.exception("append description")
            await u.message.reply_text(f"❗ Не удалось записать файл: {exc}")

    async def _cmd_unknown(self, u: Update, _):
        await u.message.reply_text("Неизвестная команда.")

    # ────────────────── WebSocket part ─────────────────────────────────
    async def _ws_handler(self, ws: websockets.WebSocketServerProtocol):
        self._ws = ws; await self._notify_all("🟢 Сканер подключён")
        try:
            async for msg in ws:
                if isinstance(msg, bytes): await self._img_q.put(msg)
        finally:
            self._ws = None; await self._notify_all("🔴 Сканер отключён")

    async def _ws_send_scan(self) -> bool:
        if not self._ws: return False
        try: await self._ws.send('{"cmd":"scan"}'); return True
        except Exception as e: log.warning("WS send error %s", e); return False

    # ────────────────── utilities ──────────────────────────────────────
    def _save_files(self, img: bytes) -> tuple[pathlib.Path, pathlib.Path]:
        ts = int(datetime.now(self.cfg.TZ).timestamp() * 1000)
        img_path = self.cfg.DIR_IMG / f"{ts}.png"
        des_path = self.cfg.DIR_DES / f"{ts}.txt"
        img_path.write_bytes(img)
        des_path.touch()
        self._last_file = des_path
        return img_path, des_path

    def _restore_last_file(self):
        files = sorted(self.cfg.DIR_DES.glob("*.txt"))
        self._last_file = files[-1] if files else None
        if self._last_file:
            log.info("last description restored: %s", self._last_file.name)

    def _ensure_dirs(self):
        self.cfg.DIR_IMG.mkdir(parents=True, exist_ok=True)
        self.cfg.DIR_DES.mkdir(parents=True, exist_ok=True)

    async def _notify_all(self, txt: str):
        dead = []
        for cid in self.subs:
            try: await self.app.bot.send_message(cid, txt)
            except: dead.append(cid)
        for cid in dead: self.subs.discard(cid)

    def _subs_add(self, cid: int): self.subs.add(cid)

    def _register_handlers(self):
        ah = self.app.add_handler
        ah(CommandHandler("start", self._cmd_start))
        ah(CommandHandler("scan",  self._cmd_scan))
        ah(MessageHandler(filters.Regex("(?i)скан"), self._cmd_scan))
        ah(MessageHandler(filters.TEXT & ~filters.COMMAND, self._plain_text))
        ah(MessageHandler(filters.COMMAND, self._cmd_unknown))

# ───────────── entrypoint ────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(ScannerBot(Config).run())
