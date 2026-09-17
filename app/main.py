"""FastAPI-приложение: приём вебхуков LINE + планировщик сводок."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .ai import AIClient
from .commands import CommandHandler
from .config import get_settings
from .db import Database
from .line_api import LineClient, verify_signature
from .pipeline import Pipeline
from .summary import SummaryService
from .telegram import TelegramClient

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("line-bot")

os.makedirs(settings.data_dir, exist_ok=True)
os.makedirs(settings.media_dir, exist_ok=True)

db = Database(settings.db_path)
line = LineClient(settings.line_channel_access_token, settings.media_dir)
ai = AIClient(settings.anthropic_api_key, settings.model_fast,
              settings.model_summary, settings.target_language)
tg = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
pipeline = Pipeline(settings, db, line, ai, tg)
summary_service = SummaryService(settings, db, ai, tg, pipeline)
commands = CommandHandler(settings, db, ai, tg, summary_service, pipeline)

scheduler = AsyncIOScheduler(timezone=settings.tz)
_command_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(
        summary_service.run_for_day,
        CronTrigger(hour=settings.summary_hour, minute=settings.summary_minute,
                    timezone=settings.tz),
        id="daily_summary",
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.add_job(
        summary_service.cleanup_media,
        CronTrigger(hour=4, minute=30, timezone=settings.tz),
        id="cleanup_media",
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.start()

    global _command_task
    if settings.enable_commands:
        _command_task = asyncio.create_task(commands.run_polling())
        log.info("Команды из Telegram включены")

    log.info("Запущен. Сводка ежедневно в %s (%s)", settings.summary_time, settings.timezone)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        commands.stop()
        if _command_task:
            _command_task.cancel()
            try:
                await _command_task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        await pipeline.flush_all_media()
        await line.aclose()
        await tg.aclose()


app = FastAPI(title="LINE → Telegram summary bot", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    mute = commands.muted_until()
    return {
        "status": "ok",
        "time": datetime.now(settings.tz).isoformat(),
        "summary_at": settings.summary_time,
        "timezone": settings.timezone,
        "commands": settings.enable_commands,
        "muted_until": mute.isoformat() if mute else None,
    }


@app.post("/webhook")
async def webhook(
    request: Request,
    background: BackgroundTasks,
    x_line_signature: str = Header(default=""),
) -> PlainTextResponse:
    body = await request.body()

    if not verify_signature(settings.line_channel_secret, body, x_line_signature):
        log.warning("Неверная подпись вебхука")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return PlainTextResponse("OK")

    events = payload.get("events") or []
    # LINE ждёт ответ в течение секунд — обрабатываем в фоне
    background.add_task(process_events, events)
    return PlainTextResponse("OK")


async def process_events(events: list[dict]) -> None:
    for event in events:
        try:
            await pipeline.handle_event(event)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка обработки события: %s", exc)


def _check_admin(token: str) -> None:
    if not settings.admin_token:
        raise HTTPException(status_code=404, detail="Not found")
    if token != settings.admin_token:
        raise HTTPException(status_code=403, detail="Forbidden")


@app.get("/admin/summary")
async def admin_summary(token: str = "", day: str | None = None, force: bool = True) -> JSONResponse:
    """Ручной запуск сводки: /admin/summary?token=...&day=2026-09-17"""
    _check_admin(token)
    asyncio.create_task(summary_service.run_for_day(day=day, force=force))
    return JSONResponse({"status": "started", "day": day or "today"})


@app.get("/admin/test")
async def admin_test(token: str = "") -> JSONResponse:
    """Проверка связки с Telegram."""
    _check_admin(token)
    await tg.send_text("✅ Тест: бот жив, Telegram подключён.")
    return JSONResponse({"status": "sent"})


@app.get("/admin/stats")
async def admin_stats(token: str = "", day: str | None = None) -> JSONResponse:
    _check_admin(token)
    day = day or datetime.now(settings.tz).strftime("%Y-%m-%d")
    rows = db.messages_for_day(day)
    return JSONResponse({
        "day": day,
        "messages": len(rows),
        "urgent": sum(1 for r in rows if r["is_urgent"]),
        "media": sum(1 for r in rows if r["file_path"]),
        "groups": [{"id": g, "name": n} for g, n in db.groups_active_on(day)],
    })
