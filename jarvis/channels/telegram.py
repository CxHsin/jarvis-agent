from __future__ import annotations

import asyncio
import html
import logging
import re

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from jarvis.runtime.messages import InboundMessageFactory

logger = logging.getLogger(__name__)
_TELEGRAM_TEXT_LIMIT = 4000


class TelegramBotRuntime:
    def __init__(
        self,
        token: str,
        allowed_chat_ids: list[str],
        app_runtime,
    ) -> None:
        self._allowed = set(allowed_chat_ids)
        self._runtime = app_runtime
        self._inbound_factory = InboundMessageFactory()
        self._app = Application.builder().token(token).build()
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))

    async def run(self) -> None:
        logger.info("telegram bot starting")
        await self._app.initialize()
        await self._app.start()
        if self._app.updater is None:
            raise RuntimeError("Telegram updater is unavailable.")
        await self._app.updater.start_polling()
        logger.info("telegram bot polling")
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._app.updater is not None and self._app.updater.running:
            await self._app.updater.stop()
        if self._app.running:
            await self._app.stop()
        await self._app.shutdown()

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None or not message.text:
            return
        chat_id = str(chat.id)
        if self._allowed and chat_id not in self._allowed:
            logger.warning("rejected chat_id=%s", chat_id)
            return
        logger.info("chat_id=%s inbound=%r", chat_id, message.text[:120])
        try:
            await context.bot.send_chat_action(chat_id=chat.id, action="typing")
            inbound = self._inbound_factory.create(
                session_key=chat_id,
                text=message.text,
                channel="telegram",
                user_id=str(message.from_user.id) if message.from_user else None,
            )
            outbound = await self._runtime.process_inbound(inbound)
        except Exception as exc:
            logger.exception("chat_id=%s turn_failed", chat_id)
            outbound = None
            reply = f"Request failed: {exc}"
        else:
            reply = outbound.text
        await self._send_reply(message, reply)

    async def _send_reply(self, message, reply: str) -> None:
        rendered = _render_markdown_to_telegram_html(reply)
        html_chunks = _chunk_text(rendered, _TELEGRAM_TEXT_LIMIT)
        plain_chunks = _chunk_text(reply, _TELEGRAM_TEXT_LIMIT)
        try:
            for chunk in html_chunks:
                await message.reply_text(chunk, parse_mode=ParseMode.HTML)
        except BadRequest:
            logger.warning("telegram html render failed; falling back to plain text")
            for chunk in plain_chunks:
                await message.reply_text(chunk)


def _render_markdown_to_telegram_html(text: str) -> str:
    lines = text.splitlines()
    rendered_lines: list[str] = []
    in_code_block = False
    code_lines: list[str] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if in_code_block:
                rendered_lines.append(f"<pre>{html.escape(chr(10).join(code_lines))}</pre>")
                code_lines = []
                in_code_block = False
            else:
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(raw_line)
            continue

        rendered_lines.append(_render_line(raw_line))

    if code_lines:
        rendered_lines.append(f"<pre>{html.escape(chr(10).join(code_lines))}</pre>")

    return "\n".join(rendered_lines)


def _render_line(line: str) -> str:
    if not line.strip():
        return ""

    heading_match = re.match(r"^(#{1,6})\s+(.*)$", line)
    if heading_match:
        return f"<b>{_render_inline(heading_match.group(2))}</b>"

    bullet_match = re.match(r"^(\s*)[-*]\s+(.*)$", line)
    if bullet_match:
        return f"• {_render_inline(bullet_match.group(2))}"

    number_match = re.match(r"^(\s*)(\d+)\.\s+(.*)$", line)
    if number_match:
        return f"{number_match.group(2)}. {_render_inline(number_match.group(3))}"

    return _render_inline(line)


def _render_inline(text: str) -> str:
    placeholders: list[str] = []

    def store(value: str) -> str:
        token = f"@@TG_PLACEHOLDER_{len(placeholders)}@@"
        placeholders.append(value)
        return token

    text = re.sub(
        r"`([^`]+)`",
        lambda m: store(f"<code>{html.escape(m.group(1))}</code>"),
        text,
    )
    text = re.sub(
        r"\*\*(.+?)\*\*",
        lambda m: store(f"<b>{html.escape(m.group(1))}</b>"),
        text,
    )
    text = re.sub(
        r"\*(.+?)\*",
        lambda m: store(f"<i>{html.escape(m.group(1))}</i>"),
        text,
    )

    escaped = html.escape(text)
    for idx, value in enumerate(placeholders):
        escaped = escaped.replace(f"@@TG_PLACEHOLDER_{idx}@@", value)
    return escaped


def _chunk_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) <= limit:
            current += line
            continue
        if current:
            chunks.append(current.rstrip())
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit].rstrip())
            line = line[limit:]
        current = line
    if current:
        chunks.append(current.rstrip())
    return [chunk for chunk in chunks if chunk]
