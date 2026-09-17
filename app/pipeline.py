"""Обработка событий LINE: сохранение, перевод, пересылка вложений, срочные пуши."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from .ai import AIClient
from .config import Settings
from .db import Database
from .line_api import LineClient
from .telegram import TelegramClient, esc

log = logging.getLogger(__name__)

MEDIA_TYPES = {"image", "video", "audio", "file"}

TYPE_LABEL = {
    "image": "🖼 фото",
    "video": "🎥 видео",
    "audio": "🎙 голосовое",
    "file": "📎 файл",
    "sticker": "🙂 стикер",
}


class Pipeline:
    def __init__(self, settings: Settings, db: Database, line: LineClient,
                 ai: AIClient, tg: TelegramClient) -> None:
        self.s = settings
        self.db = db
        self.line = line
        self.ai = ai
        self.tg = tg
        # буфер вложений: group_id -> список (путь, подпись, row_id)
        self._media_buffer: dict[str, list[tuple[str, str, int]]] = {}
        self._flush_tasks: dict[str, asyncio.Task] = {}

    # ---------------- вспомогательное ----------------

    async def _display_name(self, group_id: str, user_id: str | None) -> str:
        if not user_id:
            return "неизвестный участник"
        cached = self.db.get_profile(group_id, user_id)
        if cached:
            return cached
        profile = await self.line.member_profile(group_id, user_id)
        name = profile.get("displayName") or f"участник {user_id[-4:]}"
        self.db.save_profile(group_id, user_id, name)
        return name

    async def _group_name(self, group_id: str) -> str:
        summary = await self.line.group_summary(group_id)
        return summary.get("groupName") or ""

    def _is_muted(self) -> bool:
        """Пауза срочных уведомлений, выставленная командой /mute."""
        raw = self.db.get_state("mute_until")
        if not raw:
            return False
        try:
            return datetime.fromisoformat(raw) > datetime.now(self.s.tz)
        except ValueError:
            return False

    def _allowed(self, group_id: str) -> bool:
        if not self.s.line_allowed_group_ids:
            return True
        return group_id in self.s.line_allowed_group_ids

    # ---------------- обработка события ----------------

    async def handle_event(self, event: dict) -> None:
        etype = event.get("type")
        source = event.get("source") or {}
        group_id = source.get("groupId") or source.get("roomId")

        if etype == "join" and group_id:
            await self.tg.send_text(
                f"✅ Бот добавлен в чат LINE.\n<code>groupId: {esc(group_id)}</code>\n"
                f"Сводка будет приходить каждый день в {self.s.summary_time} "
                f"({self.s.timezone})."
            )
            return

        if etype == "leave" and group_id:
            await self.tg.send_text(f"⚠️ Бота удалили из чата LINE <code>{esc(group_id)}</code>.")
            return

        if etype != "message" or not group_id:
            return

        if not self._allowed(group_id):
            log.info("Пропускаю сообщение из неразрешённой группы %s", group_id)
            return

        message = event.get("message") or {}
        msg_type = message.get("type")
        line_message_id = message.get("id")
        user_id = source.get("userId")

        ts_ms = event.get("timestamp") or int(datetime.now().timestamp() * 1000)
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=self.s.tz)
        day = ts.strftime("%Y-%m-%d")

        display_name = await self._display_name(group_id, user_id)
        group_name = await self._group_name(group_id)

        record: dict = {
            "line_message_id": line_message_id,
            "group_id": group_id,
            "group_name": group_name,
            "user_id": user_id,
            "display_name": display_name,
            "ts": ts.isoformat(),
            "day": day,
            "msg_type": msg_type,
            "text_original": None,
            "lang": None,
            "text_translated": None,
            "is_urgent": 0,
            "urgent_reason": None,
            "file_path": None,
            "file_name": None,
        }

        if msg_type == "text":
            text = message.get("text") or ""
            record["text_original"] = text
            result = await self.ai.translate(text, display_name, group_name)
            record["lang"] = result["lang"]
            record["text_translated"] = result["translation"]
            record["is_urgent"] = int(result["urgent"])
            record["urgent_reason"] = result["reason"]

        elif msg_type == "sticker":
            keywords = message.get("keywords") or []
            record["text_original"] = f"[стикер: {', '.join(keywords[:3])}]" if keywords else "[стикер]"
            record["text_translated"] = record["text_original"]

        elif msg_type in MEDIA_TYPES:
            hint = (message.get("fileName") if msg_type == "file" else None)
            downloaded = await self.line.download_content(line_message_id, day, hint)
            if downloaded:
                record["file_path"], record["file_name"] = downloaded
            label = TYPE_LABEL.get(msg_type, msg_type)
            record["text_original"] = f"[{label}{': ' + hint if hint else ''}]"
            record["text_translated"] = record["text_original"]

            # фото часто содержат объявления — читаем текст с картинки
            if msg_type == "image" and record["file_path"]:
                description = await self._describe(record["file_path"], group_name)
                if description:
                    record["text_translated"] = f"[🖼 фото] {description}"
                    urgency = await self.ai.translate(description, display_name, group_name)
                    record["is_urgent"] = int(urgency["urgent"])
                    record["urgent_reason"] = urgency["reason"]
        else:
            record["text_original"] = f"[{msg_type}]"
            record["text_translated"] = record["text_original"]

        row_id = self.db.save_message(**record)
        if row_id is None:
            log.info("Дубль вебхука %s — пропускаю", line_message_id)
            return

        if record["is_urgent"]:
            await self._send_urgent(record)
        elif self.s.forward_every_message and msg_type == "text":
            await self._send_plain(record)

        if record["file_path"] and self.s.media_delivery == "instant":
            await self._queue_media(group_id, record, row_id)

    async def _describe(self, path: str, group_name: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                      "webp": "image/webp", "gif": "image/gif"}.get(ext.lstrip("."), "")
        if not media_type:
            return ""
        try:
            if os.path.getsize(path) > 5 * 1024 * 1024:
                return ""  # слишком большая картинка для API
            with open(path, "rb") as fh:
                return await self.ai.describe_image(fh.read(), media_type, group_name)
        except Exception as exc:  # noqa: BLE001
            log.error("Не удалось описать изображение %s: %s", path, exc)
            return ""

    # ---------------- отправка ----------------

    def _header(self, record: dict) -> str:
        time_str = datetime.fromisoformat(record["ts"]).strftime("%H:%M")
        group = record.get("group_name") or "LINE"
        return f"<b>{esc(record['display_name'])}</b> · {esc(group)} · {time_str}"

    async def _send_urgent(self, record: dict) -> None:
        muted = self._is_muted()

        mention = ""
        if self.s.telegram_owner_username and not muted:
            handles = [h.strip().lstrip("@") for h in self.s.telegram_owner_username.split(",")]
            handles = [h for h in handles if h]
            if handles:
                mention = " ".join(f"@{h}" for h in handles) + " "

        body = [
            f"{'🔇' if muted else '🔴'} {mention}<b>СРОЧНОЕ</b>",
            "",
            self._header(record),
        ]
        if record.get("urgent_reason"):
            body.append(f"<i>{esc(record['urgent_reason'])}</i>")
        body += ["", esc(record["text_translated"])]
        if record.get("text_original") and record["text_original"] != record["text_translated"]:
            body += ["", f"<i>Оригинал:</i> {esc(record['text_original'])}"]

        await self.tg.send_text("\n".join(body), silent=muted)

    async def _send_plain(self, record: dict) -> None:
        text = f"{self._header(record)}\n{esc(record['text_translated'])}"
        await self.tg.send_text(text, silent=True)

    async def _queue_media(self, group_id: str, record: dict, row_id: int) -> None:
        """Копим вложения несколько секунд, чтобы серия фото ушла одним альбомом."""
        caption = self._header(record)
        if record["msg_type"] == "image" and record["text_translated"].startswith("[🖼 фото] "):
            note = record["text_translated"].removeprefix("[🖼 фото] ")
            caption = f"{caption}\n{esc(note)}"
        elif record.get("file_name"):
            caption = f"{caption}\n📎 {esc(record['file_name'])}"

        self._media_buffer.setdefault(group_id, []).append((record["file_path"], caption, row_id))

        task = self._flush_tasks.get(group_id)
        if task and not task.done():
            task.cancel()
        self._flush_tasks[group_id] = asyncio.create_task(self._flush_after_delay(group_id))

    async def _flush_after_delay(self, group_id: str) -> None:
        try:
            await asyncio.sleep(self.s.media_batch_seconds)
        except asyncio.CancelledError:
            return
        await self.flush_media(group_id)

    async def flush_media(self, group_id: str) -> None:
        items = self._media_buffer.pop(group_id, [])
        if not items:
            return
        try:
            await self.tg.send_album([(path, caption) for path, caption, _ in items])
            self.db.mark_media_delivered([row_id for _, _, row_id in items])
        except Exception as exc:  # noqa: BLE001
            log.error("Не удалось отправить вложения: %s", exc)

    async def flush_all_media(self) -> None:
        for group_id in list(self._media_buffer.keys()):
            await self.flush_media(group_id)
