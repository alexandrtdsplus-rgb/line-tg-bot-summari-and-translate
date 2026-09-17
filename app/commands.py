"""Команды управления ботом из Telegram-чата."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

from .ai import AIClient
from .config import Settings
from .db import Database
from .telegram import TelegramClient, esc

log = logging.getLogger(__name__)

MUTE_KEY = "mute_until"
OFFSET_KEY = "tg_offset"

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

# Список для меню Telegram и для /help
COMMANDS: list[tuple[str, str]] = [
    ("summary", "Сводка за сегодня на текущий момент"),
    ("allevent", "Предстоящие события начиная с завтра"),
    ("todo", "Что нужно сделать начиная с завтра"),
    ("money", "Все денежные сборы"),
    ("ask", "Вопрос по переписке: /ask когда экскурсия?"),
    ("search", "Поиск по сообщениям: /search автобус"),
    ("today", "Полный лог за сегодня с переводом"),
    ("day", "Сводка за дату: /day 2026-09-15"),
    ("media", "Переслать вложения за сегодня"),
    ("mute", "Пауза срочных: /mute 8 (часов)"),
    ("unmute", "Снять паузу срочных"),
    ("status", "Состояние бота"),
    ("cleanhistory", "Удалить историю (нужно подтверждение)"),
    ("help", "Список команд"),
]


def ru_date(d: datetime) -> str:
    return f"{d.strftime('%d.%m')}, {WEEKDAYS[d.weekday()]}"


def build_history_transcript(rows: list, limit_chars: int = 120_000) -> str:
    """Транскрипт за несколько дней — с датой в каждой строке.

    При переполнении обрезает СТАРЫЕ сообщения: свежие важнее.
    """
    lines = []
    for row in rows:
        ts = datetime.fromisoformat(row["ts"])
        text = row["text_translated"] or row["text_original"] or ""
        marker = " [СРОЧНОЕ]" if row["is_urgent"] else ""
        lines.append(f"{ts.strftime('%d.%m.%Y %H:%M')} — {row['display_name']}{marker}: {text}")

    out = "\n".join(lines)
    if len(out) <= limit_chars:
        return out
    while lines and len("\n".join(lines)) > limit_chars:
        lines.pop(0)
    return "[начало переписки обрезано]\n" + "\n".join(lines)


class CommandHandler:
    def __init__(self, settings: Settings, db: Database, ai: AIClient,
                 tg: TelegramClient, summary_service, pipeline) -> None:
        self.s = settings
        self.db = db
        self.ai = ai
        self.tg = tg
        self.summary = summary_service
        self.pipeline = pipeline
        self._stopping = False

    # ---------------- пауза уведомлений ----------------

    def muted_until(self) -> datetime | None:
        raw = self.db.get_state(MUTE_KEY)
        if not raw:
            return None
        try:
            until = datetime.fromisoformat(raw)
        except ValueError:
            self.db.set_state(MUTE_KEY, None)
            return None
        if until <= datetime.now(self.s.tz):
            self.db.set_state(MUTE_KEY, None)
            return None
        return until

    # ---------------- цикл получения команд ----------------

    async def run_polling(self) -> None:
        await self.tg.delete_webhook()
        await self.tg.set_my_commands(COMMANDS)

        offset = await self._initial_offset()
        log.info("Приём команд запущен, offset=%s", offset)

        while not self._stopping:
            try:
                updates = await self.tg.get_updates(offset, timeout=30)
                for update in updates:
                    offset = max(offset, update.get("update_id", 0) + 1)
                    self.db.set_state(OFFSET_KEY, str(offset))
                    message = update.get("message")
                    if message:
                        await self._handle_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Сбой в цикле команд: %s", exc)
                await asyncio.sleep(5)

    async def _initial_offset(self) -> int:
        """Сохранённый offset, иначе пропускаем всё накопленное до старта."""
        stored = self.db.get_state(OFFSET_KEY)
        if stored and stored.isdigit():
            return int(stored)
        updates = await self.tg.get_updates(-1, timeout=0)
        offset = (updates[-1]["update_id"] + 1) if updates else 0
        self.db.set_state(OFFSET_KEY, str(offset))
        return offset

    def stop(self) -> None:
        self._stopping = True

    # ---------------- разбор сообщения ----------------

    async def _handle_message(self, message: dict) -> None:
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if chat_id != str(self.s.telegram_chat_id):
            log.info("Команда из постороннего чата %s — игнорирую", chat_id)
            return

        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return

        head, _, rest = text.partition(" ")
        command = head[1:].split("@")[0].lower()
        args = rest.strip()

        author = (message.get("from") or {}).get("first_name") or "кто-то"
        log.info("Команда /%s от %s, аргументы: %r", command, author, args)

        handlers = {
            "summary": self._cmd_summary,
            "allevent": self._cmd_allevent,
            "todo": self._cmd_todo,
            "money": self._cmd_money,
            "ask": self._cmd_ask,
            "search": self._cmd_search,
            "today": self._cmd_today,
            "day": self._cmd_day,
            "media": self._cmd_media,
            "mute": self._cmd_mute,
            "unmute": self._cmd_unmute,
            "status": self._cmd_status,
            "cleanhistory": self._cmd_cleanhistory,
            "help": self._cmd_help,
            "start": self._cmd_help,
        }
        handler = handlers.get(command)
        if not handler:
            await self.tg.send_text(
                f"Не знаю команду <code>/{esc(command)}</code>. "
                f"Список — /help", silent=True)
            return

        try:
            await handler(args)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка обработки /%s: %s", command, exc)
            await self.tg.send_text(
                f"⚠️ Команда <code>/{esc(command)}</code> сломалась: {esc(str(exc)[:200])}")

    # ---------------- вспомогательное ----------------

    def _today(self) -> datetime:
        return datetime.now(self.s.tz)

    async def _history_answer(self, kind: str, question: str = "") -> None:
        """Общий путь для /allevent, /todo, /money, /ask."""
        today = self._today()
        day_from = (today - timedelta(days=self.s.history_days)).strftime("%Y-%m-%d")
        rows = self.db.messages_since(day_from)

        if not rows:
            await self.tg.send_text(
                f"В базе пока нет сообщений за последние {self.s.history_days} дней.",
                silent=True)
            return

        await self.tg.send_chat_action("typing")
        transcript = build_history_transcript(rows)
        answer = await self.ai.history_query(
            kind, transcript,
            today=f"{today.strftime('%Y-%m-%d')} ({ru_date(today)})",
            tomorrow=(today + timedelta(days=1)).strftime("%d.%m"),
            question=question,
        )
        footer = (f"\n\n<i>По {len(rows)} сообщ. за {self.s.history_days} дн.</i>")
        await self.tg.send_text(answer + footer)

    # ---------------- команды ----------------

    async def _cmd_summary(self, args: str) -> None:
        await self.tg.send_chat_action("typing")
        await self.summary.run_for_day(force=True)

    async def _cmd_allevent(self, args: str) -> None:
        await self._history_answer("events")

    async def _cmd_todo(self, args: str) -> None:
        await self._history_answer("todo")

    async def _cmd_money(self, args: str) -> None:
        await self._history_answer("money")

    async def _cmd_ask(self, args: str) -> None:
        if not args:
            await self.tg.send_text(
                "После команды нужен вопрос.\n\n"
                "Например:\n"
                "<code>/ask что нужно принести на этой неделе?</code>\n"
                "<code>/ask когда родительское собрание?</code>\n"
                "<code>/ask сколько всего сдали за месяц?</code>",
                silent=True)
            return
        await self._history_answer("ask", question=args)

    async def _cmd_search(self, args: str) -> None:
        if len(args) < 2:
            await self.tg.send_text(
                "Нужно слово для поиска: <code>/search автобус</code>", silent=True)
            return

        rows = self.db.search_messages(args, limit=25)
        if not rows:
            await self.tg.send_text(
                f"По запросу «{esc(args)}» ничего не нашлось.", silent=True)
            return

        parts = [f"🔍 <b>Поиск: {esc(args)}</b> — найдено {len(rows)}", ""]
        for row in rows:
            ts = datetime.fromisoformat(row["ts"])
            text = (row["text_translated"] or row["text_original"] or "")[:300]
            mark = "🔴 " if row["is_urgent"] else ""
            parts.append(
                f"{mark}<b>{esc(row['display_name'])}</b> · {ts.strftime('%d.%m %H:%M')}")
            parts.append(esc(text))
            parts.append("")
        await self.tg.send_text("\n".join(parts), silent=True)

    async def _cmd_today(self, args: str) -> None:
        from .summary import build_full_log
        day = self._today().strftime("%Y-%m-%d")
        rows = self.db.messages_for_day(day)
        if not rows:
            await self.tg.send_text("Сегодня в чате пока тихо.", silent=True)
            return
        await self.tg.send_text(build_full_log(rows), silent=True)

    async def _cmd_day(self, args: str) -> None:
        if not args:
            await self.tg.send_text(
                "Нужна дата: <code>/day 2026-09-15</code>", silent=True)
            return
        try:
            datetime.strptime(args, "%Y-%m-%d")
        except ValueError:
            await self.tg.send_text(
                f"Не понял дату «{esc(args)}». Формат: <code>2026-09-15</code>", silent=True)
            return

        if not self.db.messages_for_day(args):
            await self.tg.send_text(
                f"За {esc(args)} сообщений в базе нет.", silent=True)
            return

        await self.tg.send_chat_action("typing")
        await self.summary.run_for_day(day=args, force=True)

    async def _cmd_media(self, args: str) -> None:
        day = args.strip() or self._today().strftime("%Y-%m-%d")
        rows = [r for r in self.db.messages_for_day(day)
                if r["file_path"] and os.path.exists(r["file_path"])]
        if not rows:
            await self.tg.send_text(f"Вложений за {esc(day)} нет.", silent=True)
            return

        items = []
        for row in rows:
            ts = datetime.fromisoformat(row["ts"])
            items.append((row["file_path"],
                          f"<b>{esc(row['display_name'])}</b> · {ts.strftime('%d.%m %H:%M')}"))
        await self.tg.send_text(
            f"📎 <b>Вложения за {esc(day)}</b> — {len(items)} шт.", silent=True)
        await self.tg.send_album(items, silent=True)

    async def _cmd_mute(self, args: str) -> None:
        hours = 8
        if args:
            try:
                hours = max(1, min(168, int(args.split()[0])))
            except ValueError:
                await self.tg.send_text(
                    "Нужно число часов: <code>/mute 12</code>", silent=True)
                return

        until = self._today() + timedelta(hours=hours)
        self.db.set_state(MUTE_KEY, until.isoformat())
        await self.tg.send_text(
            f"🔇 Срочные уведомления приглушены на {hours} ч — "
            f"до {until.strftime('%d.%m %H:%M')}.\n"
            f"<i>Сообщения продолжают собираться и попадут в сводку. "
            f"Срочное будет приходить без звука и без упоминаний.</i>\n"
            f"Снять — /unmute")

    async def _cmd_unmute(self, args: str) -> None:
        was = self.muted_until()
        self.db.set_state(MUTE_KEY, None)
        if was:
            await self.tg.send_text("🔔 Срочные уведомления снова включены.")
        else:
            await self.tg.send_text("Уведомления и так были включены.", silent=True)

    async def _cmd_status(self, args: str) -> None:
        today = self._today()
        day = today.strftime("%Y-%m-%d")
        counts = self.db.counts_for_day(day)
        first_day = self.db.first_message_day()

        next_summary = today.replace(hour=self.s.summary_hour,
                                     minute=self.s.summary_minute,
                                     second=0, microsecond=0)
        if next_summary <= today:
            next_summary += timedelta(days=1)

        mute = self.muted_until()
        groups = self.db.groups_active_on(day)

        lines = [
            "✅ <b>Бот работает</b>",
            "",
            f"<b>Сегодня, {ru_date(today)}</b>",
            f"• сообщений: {counts['total']}",
            f"• срочных: {counts['urgent']}",
            f"• вложений: {counts['media']}",
            "",
            f"Следующая сводка: <b>{next_summary.strftime('%d.%m в %H:%M')}</b>",
            f"Время на сервере: {today.strftime('%H:%M')} ({self.s.timezone})",
        ]
        if mute:
            lines.append(f"🔇 Уведомления приглушены до {mute.strftime('%d.%m %H:%M')}")
        else:
            lines.append("🔔 Уведомления включены")
        if first_day:
            lines.append(f"История ведётся с {first_day}")
        if groups:
            names = ", ".join(n or g[:8] for g, n in groups)
            lines.append(f"Активные чаты сегодня: {esc(names)}")

        await self.tg.send_text("\n".join(lines), silent=True)

    async def _cleanup_files(self, paths: list[str]) -> int:
        removed = 0
        for path in paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
                    removed += 1
            except OSError as exc:
                log.warning("Не удалось удалить %s: %s", path, exc)

        # пустые папки по дням
        media_dir = self.s.media_dir
        if os.path.isdir(media_dir):
            for name in os.listdir(media_dir):
                day_dir = os.path.join(media_dir, name)
                try:
                    if os.path.isdir(day_dir) and not os.listdir(day_dir):
                        os.rmdir(day_dir)
                except OSError:
                    pass
        return removed

    async def _cmd_cleanhistory(self, args: str) -> None:
        """/cleanhistory [дней] [confirm] — удаление истории.

        Без confirm только показывает, что будет удалено.
        """
        parts = args.split()
        confirmed = "confirm" in [p.lower() for p in parts]
        numbers = [p for p in parts if p.isdigit()]

        before_day: str | None = None
        scope = "всю историю"
        if numbers:
            days = int(numbers[0])
            if days < 1:
                await self.tg.send_text(
                    "Число дней должно быть больше нуля.", silent=True)
                return
            cutoff = self._today() - timedelta(days=days)
            before_day = cutoff.strftime("%Y-%m-%d")
            scope = f"всё старше {days} дн. (до {cutoff.strftime('%d.%m.%Y')})"

        preview = self.db.purge_preview(before_day)

        if preview["messages"] == 0:
            await self.tg.send_text(
                f"Удалять нечего — под «{esc(scope)}» ничего не подпадает.", silent=True)
            return

        if not confirmed:
            lines = [
                "⚠️ <b>Удаление истории</b>",
                "",
                f"Под удаление подпадает <b>{scope}</b>:",
                f"• сообщений: <b>{preview['messages']}</b>",
                f"• вложений на диске: <b>{preview['files']}</b>",
                f"• сохранённых сводок: <b>{preview['summaries']}</b>",
            ]
            if preview["first_day"]:
                lines.append(f"• период: {preview['first_day']} — {preview['last_day']}")
            lines += [
                "",
                "<b>Восстановить будет нельзя.</b> Уже отправленные в этот чат "
                "сводки, срочные и фото останутся — удаляется только база на сервере.",
                "",
                "Если согласны, повторите команду со словом <code>confirm</code>:",
                f"<code>/cleanhistory{' ' + numbers[0] if numbers else ''} confirm</code>",
            ]
            await self.tg.send_text("\n".join(lines))
            return

        paths = self.db.purge_file_paths(before_day)
        result = self.db.purge_messages(before_day)
        removed_files = await self._cleanup_files(paths)
        left = self.db.purge_preview()["messages"]

        await self.tg.send_text(
            "🗑 <b>История удалена</b>\n\n"
            f"• сообщений: {result['messages']}\n"
            f"• файлов с диска: {removed_files}\n"
            f"• сводок: {result['summaries']}\n\n"
            f"Осталось в базе: <b>{left}</b>\n"
            f"<i>Кеш имён участников сохранён, чтобы не запрашивать их у LINE заново. "
            f"Бот продолжает работать — новые сообщения пишутся как обычно.</i>")

    async def _cmd_help(self, args: str) -> None:
        lines = ["🤖 <b>Команды</b>", ""]
        for name, description in COMMANDS:
            lines.append(f"/{name} — {esc(description)}")
        lines += [
            "",
            "<i>Сводка приходит автоматически каждый день "
            f"в {self.s.summary_time}. Срочное — сразу, как появится. "
            "Фото и файлы пересылаются по мере поступления.</i>",
        ]
        await self.tg.send_text("\n".join(lines), silent=True)
