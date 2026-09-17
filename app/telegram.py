"""Отправка в Telegram."""
from __future__ import annotations

import asyncio
import html
import logging
import os

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 4096
MAX_CAPTION = 1024
# Telegram отказывается принимать файлы больше 50 МБ через Bot API
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Режет длинный текст по абзацам/строкам, не ломая разметку посреди слова."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for block in text.split("\n"):
        candidate = f"{current}\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > limit:
            parts.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        parts.append(current)
    return parts


class TelegramClient:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, **kwargs) -> dict | None:
        url = API.format(token=self.token, method=method)
        files = kwargs.pop("files", None)
        for attempt in range(3):
            try:
                if files:
                    r = await self._client.post(url, data=kwargs, files=files)
                else:
                    r = await self._client.post(url, json=kwargs)
                data = r.json()
                if data.get("ok"):
                    return data.get("result")
                # 429 — ждём столько, сколько просит Telegram
                retry_after = (data.get("parameters") or {}).get("retry_after")
                if retry_after:
                    await asyncio.sleep(retry_after + 1)
                    continue
                log.error("Telegram %s вернул ошибку: %s", method, data)
                return None
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram %s, попытка %s: %s", method, attempt + 1, exc)
                await asyncio.sleep(2 * (attempt + 1))
        return None

    # ---------- приём команд ----------

    async def delete_webhook(self) -> None:
        """Снимаем вебхук, иначе getUpdates отвечает 409."""
        await self._call("deleteWebhook", drop_pending_updates=False)

    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict]:
        url = API.format(token=self.token, method="getUpdates")
        try:
            r = await self._client.post(
                url,
                json={"offset": offset, "timeout": timeout,
                      "allowed_updates": ["message"]},
                timeout=httpx.Timeout(timeout + 20, connect=10.0),
            )
            data = r.json()
            if data.get("ok"):
                return data.get("result") or []
            log.warning("getUpdates вернул ошибку: %s", data)
        except httpx.ReadTimeout:
            pass  # штатное завершение long polling
        except Exception as exc:  # noqa: BLE001
            log.warning("getUpdates: %s", exc)
        return []

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        """Подсказки команд в меню Telegram."""
        await self._call(
            "setMyCommands",
            commands=[{"command": c, "description": d[:256]} for c, d in commands],
        )

    async def send_chat_action(self, action: str = "typing") -> None:
        await self._call("sendChatAction", chat_id=self.chat_id, action=action)

    async def send_text(self, text: str, silent: bool = False) -> None:
        for chunk in split_text(text):
            await self._call(
                "sendMessage",
                chat_id=self.chat_id,
                text=chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
                disable_notification=silent,
            )

    async def send_file(self, path: str, caption: str = "", silent: bool = False) -> bool:
        """Отправляет один файл. Фото и видео — как медиа, остальное — документом."""
        if not os.path.exists(path):
            log.warning("Файл не найден: %s", path)
            return False
        size = os.path.getsize(path)
        if size > MAX_UPLOAD_BYTES:
            await self.send_text(
                f"{caption}\n\n⚠️ Файл {esc(os.path.basename(path))} "
                f"({size // 1024 // 1024} МБ) слишком большой для Telegram — лежит на сервере.",
                silent=silent,
            )
            return False

        ext = os.path.splitext(path)[1].lower()
        if ext in {".jpg", ".jpeg", ".png", ".webp"}:
            method, field = "sendPhoto", "photo"
        elif ext in {".mp4", ".mov"}:
            method, field = "sendVideo", "video"
        elif ext in {".m4a", ".mp3", ".ogg", ".wav"}:
            method, field = "sendAudio", "audio"
        else:
            method, field = "sendDocument", "document"

        with open(path, "rb") as fh:
            result = await self._call(
                method,
                chat_id=self.chat_id,
                caption=caption[:MAX_CAPTION],
                parse_mode="HTML",
                disable_notification=silent,
                files={field: (os.path.basename(path), fh)},
            )
        if result is None and method != "sendDocument":
            # например, LINE отдал .jpg, который Telegram не принял как фото
            with open(path, "rb") as fh:
                result = await self._call(
                    "sendDocument",
                    chat_id=self.chat_id,
                    caption=caption[:MAX_CAPTION],
                    parse_mode="HTML",
                    disable_notification=silent,
                    files={"document": (os.path.basename(path), fh)},
                )
        return result is not None

    async def send_album(self, items: list[tuple[str, str]], silent: bool = False) -> None:
        """items — список (путь, подпись). Фото/видео уходят альбомами по 10 штук,
        всё остальное — отдельными документами."""
        media_exts = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}
        albumable = [(p, c) for p, c in items
                     if os.path.splitext(p)[1].lower() in media_exts
                     and os.path.exists(p) and os.path.getsize(p) <= MAX_UPLOAD_BYTES]
        others = [(p, c) for p, c in items if (p, c) not in albumable]

        for i in range(0, len(albumable), 10):
            batch = albumable[i:i + 10]
            if len(batch) == 1:
                await self.send_file(batch[0][0], batch[0][1], silent=silent)
                continue
            media, files = [], {}
            handles = []
            try:
                for idx, (path, caption) in enumerate(batch):
                    key = f"file{idx}"
                    ext = os.path.splitext(path)[1].lower()
                    kind = "video" if ext in {".mp4", ".mov"} else "photo"
                    entry = {"type": kind, "media": f"attach://{key}"}
                    if caption:
                        entry["caption"] = caption[:MAX_CAPTION]
                        entry["parse_mode"] = "HTML"
                    media.append(entry)
                    fh = open(path, "rb")  # noqa: SIM115
                    handles.append(fh)
                    files[key] = (os.path.basename(path), fh)
                import json as _json
                await self._call(
                    "sendMediaGroup",
                    chat_id=self.chat_id,
                    media=_json.dumps(media, ensure_ascii=False),
                    disable_notification=silent,
                    files=files,
                )
            finally:
                for fh in handles:
                    fh.close()

        for path, caption in others:
            await self.send_file(path, caption, silent=silent)
