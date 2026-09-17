"""Клиент LINE Messaging API: подпись вебхука, профили, скачивание вложений."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import mimetypes
import os
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.line.me/v2/bot"
DATA_BASE = "https://api-data.line.me/v2/bot"

# LINE не отдаёт расширение файла — определяем по content-type
EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "application/pdf": ".pdf",
}


def verify_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    """Проверка X-Line-Signature (HMAC-SHA256 + base64)."""
    if not signature:
        return False
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


class LineClient:
    def __init__(self, access_token: str, media_dir: str) -> None:
        self.access_token = access_token
        self.media_dir = media_dir
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- метаданные ----------

    async def group_summary(self, group_id: str) -> dict:
        """Название и иконка группы. Может вернуть {} — не критично."""
        try:
            r = await self._client.get(f"{API_BASE}/group/{group_id}/summary")
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось получить данные группы %s: %s", group_id, exc)
            return {}

    async def member_profile(self, group_id: str, user_id: str) -> dict:
        """Имя участника группы. Работает, только если участник не скрыл профиль."""
        for url in (
            f"{API_BASE}/group/{group_id}/member/{user_id}",
            f"{API_BASE}/room/{group_id}/member/{user_id}",
            f"{API_BASE}/profile/{user_id}",
        ):
            try:
                r = await self._client.get(url)
                if r.status_code == 200:
                    return r.json()
            except Exception as exc:  # noqa: BLE001
                log.debug("Профиль %s недоступен по %s: %s", user_id, url, exc)
        return {}

    # ---------- вложения ----------

    async def download_content(self, message_id: str, day: str, hint_name: str | None = None) -> tuple[str, str] | None:
        """Скачивает вложение. LINE удаляет контент через некоторое время, поэтому
        качаем сразу при получении вебхука.

        Возвращает (путь_к_файлу, имя_файла) или None.
        """
        url = f"{DATA_BASE}/message/{message_id}/content"
        try:
            async with self._client.stream("GET", url) as resp:
                if resp.status_code == 404:
                    log.warning("Контент %s уже удалён на стороне LINE", message_id)
                    return None
                resp.raise_for_status()

                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
                ext = EXT_BY_MIME.get(content_type) or mimetypes.guess_extension(content_type) or ".bin"

                if hint_name:
                    base, hint_ext = os.path.splitext(hint_name)
                    safe_base = "".join(c for c in base if c.isalnum() or c in " ._-")[:80].strip() or message_id
                    file_name = f"{safe_base}{hint_ext or ext}"
                else:
                    stamp = datetime.now().strftime("%H%M%S")
                    file_name = f"{stamp}_{message_id[:10]}{ext}"

                day_dir = os.path.join(self.media_dir, day)
                os.makedirs(day_dir, exist_ok=True)
                path = os.path.join(day_dir, file_name)

                # не перезатираем одноимённые файлы
                counter = 1
                base, ext2 = os.path.splitext(path)
                while os.path.exists(path):
                    path = f"{base}_{counter}{ext2}"
                    counter += 1

                with open(path, "wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        fh.write(chunk)

            log.info("Скачал вложение %s → %s", message_id, path)
            return path, os.path.basename(path)
        except Exception as exc:  # noqa: BLE001
            log.error("Ошибка скачивания %s: %s", message_id, exc)
            return None
