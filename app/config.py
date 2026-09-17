"""Конфигурация из переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value or ""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    # --- LINE ---
    line_channel_secret: str = field(default_factory=lambda: _env("LINE_CHANNEL_SECRET", required=True))
    line_channel_access_token: str = field(
        default_factory=lambda: _env("LINE_CHANNEL_ACCESS_TOKEN", required=True)
    )
    # Если указан — принимаем сообщения только из этих групп (через запятую).
    line_allowed_group_ids: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            g.strip() for g in _env("LINE_ALLOWED_GROUP_IDS", "").split(",") if g.strip()
        )
    )

    # --- Telegram ---
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN", required=True))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID", required=True))
    # @username, которого пингуем в срочных сообщениях (необязательно)
    telegram_owner_username: str = field(default_factory=lambda: _env("TELEGRAM_OWNER_USERNAME", ""))

    # --- Anthropic ---
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", required=True))
    model_fast: str = field(default_factory=lambda: _env("MODEL_FAST", "claude-haiku-4-5-20251001"))
    model_summary: str = field(default_factory=lambda: _env("MODEL_SUMMARY", "claude-sonnet-5"))

    # --- Поведение ---
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "Asia/Bangkok"))
    summary_time: str = field(default_factory=lambda: _env("SUMMARY_TIME", "20:00"))  # HH:MM
    target_language: str = field(default_factory=lambda: _env("TARGET_LANGUAGE", "русский"))
    # Пересылать вложения сразу (instant) или пачкой вместе со сводкой (daily)
    media_delivery: str = field(default_factory=lambda: _env("MEDIA_DELIVERY", "instant"))
    # Сколько секунд копим вложения, чтобы отправить одним альбомом
    media_batch_seconds: int = field(default_factory=lambda: _env_int("MEDIA_BATCH_SECONDS", 45))
    # Слать каждое переведённое сообщение сразу (полный лог в реальном времени)
    forward_every_message: bool = field(default_factory=lambda: _env_bool("FORWARD_EVERY_MESSAGE", False))
    # Прикладывать к сводке полный лог переписки за день
    include_full_log: bool = field(default_factory=lambda: _env_bool("INCLUDE_FULL_LOG", False))
    # Сколько дней хранить скачанные файлы (0 = вечно)
    media_retention_days: int = field(default_factory=lambda: _env_int("MEDIA_RETENTION_DAYS", 90))
    # Глубина истории для команд /allevent, /todo, /money, /ask
    history_days: int = field(default_factory=lambda: _env_int("HISTORY_DAYS", 30))
    # Принимать команды из Telegram-чата
    enable_commands: bool = field(default_factory=lambda: _env_bool("ENABLE_COMMANDS", True))

    # --- Служебное ---
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "./data"))
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN", ""))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def summary_hour(self) -> int:
        return int(self.summary_time.split(":")[0])

    @property
    def summary_minute(self) -> int:
        return int(self.summary_time.split(":")[1])

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "bot.db")

    @property
    def media_dir(self) -> str:
        return os.path.join(self.data_dir, "media")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
