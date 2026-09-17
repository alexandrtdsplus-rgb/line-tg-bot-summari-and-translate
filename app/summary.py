"""Ежедневная сводка и обслуживание хранилища."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from .ai import AIClient
from .config import Settings
from .db import Database
from .telegram import TelegramClient, esc

log = logging.getLogger(__name__)


def build_transcript(rows: list) -> str:
    lines = []
    for row in rows:
        time_str = datetime.fromisoformat(row["ts"]).strftime("%H:%M")
        text = row["text_translated"] or row["text_original"] or ""
        marker = " [СРОЧНОЕ]" if row["is_urgent"] else ""
        lines.append(f"{time_str} — {row['display_name']}{marker}: {text}")
    return "\n".join(lines)


def build_full_log(rows: list) -> str:
    lines = ["<b>📄 Полный лог за день</b>", ""]
    for row in rows:
        time_str = datetime.fromisoformat(row["ts"]).strftime("%H:%M")
        translated = row["text_translated"] or row["text_original"] or ""
        lines.append(f"<b>{esc(row['display_name'])}</b> · {time_str}")
        lines.append(esc(translated))
        original = row["text_original"]
        if original and original != translated:
            lines.append(f"<i>{esc(original)}</i>")
        lines.append("")
    return "\n".join(lines)


class SummaryService:
    def __init__(self, settings: Settings, db: Database, ai: AIClient,
                 tg: TelegramClient, pipeline=None) -> None:
        self.s = settings
        self.db = db
        self.ai = ai
        self.tg = tg
        self.pipeline = pipeline

    async def run_for_day(self, day: str | None = None, force: bool = False) -> None:
        day = day or datetime.now(self.s.tz).strftime("%Y-%m-%d")
        log.info("Готовлю сводку за %s", day)

        if self.pipeline:
            await self.pipeline.flush_all_media()

        groups = self.db.groups_active_on(day)
        if not groups:
            await self.tg.send_text(
                f"🌙 <b>{day}</b>\nВ родительском чате сегодня тихо — новых сообщений не было.",
                silent=True,
            )
            return

        for group_id, group_name in groups:
            if not force and self.db.summary_exists(day, group_id):
                log.info("Сводка за %s для %s уже отправлена", day, group_id)
                continue

            rows = self.db.messages_for_day(day, group_id)
            if not rows:
                continue

            transcript = build_transcript(rows)
            body = await self.ai.daily_summary(transcript, group_name, day)

            urgent_count = sum(1 for r in rows if r["is_urgent"])
            media_count = sum(1 for r in rows if r["file_path"])
            header = (
                f"🗒 <b>Сводка за {day}</b>\n"
                f"<i>{esc(group_name or 'родительский чат')} · "
                f"{len(rows)} сообщ."
                f"{f' · {urgent_count} срочных' if urgent_count else ''}"
                f"{f' · {media_count} вложений' if media_count else ''}</i>"
            )
            await self.tg.send_text(f"{header}\n\n{body}")

            if self.s.include_full_log:
                await self.tg.send_text(build_full_log(rows), silent=True)

            # вложения, которые ещё не улетели (режим daily или сбой отправки)
            pending = self.db.undelivered_media(day, group_id)
            if pending:
                items = []
                for row in pending:
                    if not row["file_path"] or not os.path.exists(row["file_path"]):
                        continue
                    time_str = datetime.fromisoformat(row["ts"]).strftime("%H:%M")
                    caption = f"<b>{esc(row['display_name'])}</b> · {time_str}"
                    items.append((row["file_path"], caption))
                if items:
                    await self.tg.send_text(
                        f"📎 <b>Вложения за {day}</b> — {len(items)} шт.", silent=True
                    )
                    await self.tg.send_album(items, silent=True)
                self.db.mark_media_delivered([r["id"] for r in pending])

            self.db.save_summary(day, group_id, body)

    async def cleanup_media(self) -> None:
        """Удаляет старые скачанные файлы, чтобы не забить диск."""
        if self.s.media_retention_days <= 0:
            return
        today = datetime.now(self.s.tz)
        removed = 0
        for message_id, path in self.db.old_media_paths(self.s.media_retention_days, today):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
                    removed += 1
                self.db.clear_media_path(message_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось удалить %s: %s", path, exc)
        # подчищаем пустые папки по дням
        if os.path.isdir(self.s.media_dir):
            for name in os.listdir(self.s.media_dir):
                day_dir = os.path.join(self.s.media_dir, name)
                if os.path.isdir(day_dir) and not os.listdir(day_dir):
                    os.rmdir(day_dir)
        if removed:
            log.info("Удалено старых вложений: %s", removed)

    async def yesterday(self) -> str:
        return (datetime.now(self.s.tz) - timedelta(days=1)).strftime("%Y-%m-%d")
