"""Сквозной прогон вебхука с подменёнными LINE / Claude / Telegram."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp()
os.environ.update({
    "LINE_CHANNEL_SECRET": "secret123",
    "LINE_CHANNEL_ACCESS_TOKEN": "token123",
    "TELEGRAM_BOT_TOKEN": "tg123",
    "TELEGRAM_CHAT_ID": "999",
    "TELEGRAM_OWNER_USERNAME": "@alext",
    "ANTHROPIC_API_KEY": "sk-test",
    "DATA_DIR": TMP,
    "TIMEZONE": "Asia/Bangkok",
    "SUMMARY_TIME": "20:00",
    "ADMIN_TOKEN": "admintok",
    "MEDIA_BATCH_SECONDS": "0",
    "LOG_LEVEL": "WARNING",
})

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

PASSED, FAILED = 0, 0
SENT_TEXTS: list[str] = []
SENT_MEDIA: list[tuple] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name} {detail}")


# ---------- заглушки внешних сервисов ----------

async def fake_send_text(text: str, silent: bool = False) -> None:
    SENT_TEXTS.append(text)


async def fake_send_album(items, silent: bool = False) -> None:
    SENT_MEDIA.extend(items)


async def fake_send_file(path, caption="", silent=False) -> bool:
    SENT_MEDIA.append((path, caption))
    return True


async def fake_member_profile(group_id, user_id):
    return {"displayName": "Ajarn Nok"}


async def fake_group_summary(group_id):
    return {"groupName": "ห้อง ป.2/3 ผู้ปกครอง"}


async def fake_download(message_id, day, hint_name=None):
    day_dir = os.path.join(TMP, "media", day)
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, hint_name or f"{message_id}.jpg")
    with open(path, "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
    return path, os.path.basename(path)


async def fake_translate(text, author, group_name):
    urgent = any(k in text for k in ("บาท", "พรุ่งนี้", "деньги", "завтра"))
    return {
        "lang": "th",
        "translation": f"[перевод] {text}",
        "urgent": urgent,
        "reason": "нужно сдать деньги завтра" if urgent else "",
    }


async def fake_describe(image_bytes, media_type, group_name):
    return "Объявление: экскурсия в зоопарк 20 сентября, сбор в 7:30."


async def fake_summary(transcript, group_name, day):
    return ("<b>📌 Главное</b>\nЭкскурсия и сбор денег.\n\n"
            "<b>✅ Что нужно сделать</b>\n• Сдать 450 бат до 18 сентября")


main.tg.send_text = fake_send_text
main.tg.send_album = fake_send_album
main.tg.send_file = fake_send_file
main.line.member_profile = fake_member_profile
main.line.group_summary = fake_group_summary
main.line.download_content = fake_download
main.ai.translate = fake_translate
main.ai.describe_image = fake_describe
main.ai.daily_summary = fake_summary


def sign(body: bytes) -> str:
    return base64.b64encode(
        hmac.new(b"secret123", body, hashlib.sha256).digest()
    ).decode()


def post_events(client: TestClient, events: list[dict], signature: str | None = None):
    body = json.dumps({"destination": "U0", "events": events}).encode()
    return client.post(
        "/webhook",
        content=body,
        headers={
            "X-Line-Signature": signature if signature is not None else sign(body),
            "Content-Type": "application/json",
        },
    )


from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

DAY = "2026-09-17"
TS = int(datetime(2026, 9, 17, 14, 6, tzinfo=ZoneInfo("Asia/Bangkok")).timestamp() * 1000)


def run() -> None:
    with TestClient(main.app) as client:
        print("\nСлужебные ручки")
        r = client.get("/health")
        check("health отвечает", r.status_code == 200 and r.json()["status"] == "ok")
        check("таймзона в health", r.json()["timezone"] == "Asia/Bangkok")

        r = client.get("/admin/stats")
        check("админка без токена закрыта", r.status_code == 403, f"({r.status_code})")

        print("\nЗащита вебхука")
        r = post_events(client, [], signature="wrong")
        check("неверная подпись → 403", r.status_code == 403, f"({r.status_code})")

        print("\nТекстовое сообщение (срочное)")
        SENT_TEXTS.clear()
        r = post_events(client, [{
            "type": "message", "timestamp": TS,
            "source": {"type": "group", "groupId": "Gtest", "userId": "U1"},
            "message": {"id": "msg1", "type": "text", "text": "พรุ่งนี้เอาเงิน 450 บาท"},
        }])
        check("вебхук принят", r.status_code == 200 and r.text == "OK")

        urgent = [t for t in SENT_TEXTS if "СРОЧНОЕ" in t]
        check("срочное улетело в Telegram сразу", len(urgent) == 1, f"({len(SENT_TEXTS)} сообщ.)")
        check("есть пинг владельца", "@alext" in urgent[0] if urgent else False)
        check("есть автор", "Ajarn Nok" in urgent[0] if urgent else False)
        check("есть название чата", "ป.2/3" in urgent[0] if urgent else False)
        check("есть перевод", "[перевод]" in urgent[0] if urgent else False)
        check("есть оригинал", "450 บาท" in urgent[0] if urgent else False)

        rows = main.db.messages_for_day(DAY, "Gtest")
        check("сообщение записано в базу", len(rows) == 1, f"({len(rows)})")
        check("помечено срочным", rows[0]["is_urgent"] == 1 if rows else False)

        print("\nОбычное сообщение (не срочное)")
        SENT_TEXTS.clear()
        post_events(client, [{
            "type": "message", "timestamp": TS + 60000,
            "source": {"type": "group", "groupId": "Gtest", "userId": "U2"},
            "message": {"id": "msg2", "type": "text", "text": "ขอบคุณค่ะ"},
        }])
        check("несрочное не шлётся сразу", len(SENT_TEXTS) == 0, f"({SENT_TEXTS})")
        check("но записано в базу", len(main.db.messages_for_day(DAY, "Gtest")) == 2)

        print("\nФото с объявлением")
        SENT_TEXTS.clear()
        SENT_MEDIA.clear()
        post_events(client, [{
            "type": "message", "timestamp": TS + 120000,
            "source": {"type": "group", "groupId": "Gtest", "userId": "U1"},
            "message": {"id": "msg3", "type": "image"},
        }])
        asyncio.run(asyncio.sleep(0))
        check("фото переслано в Telegram", len(SENT_MEDIA) >= 1, f"({SENT_MEDIA})")
        if SENT_MEDIA:
            check("в подписи — распознанный текст", "зоопарк" in SENT_MEDIA[0][1])
        rows = main.db.messages_for_day(DAY, "Gtest")
        photo = [r for r in rows if r["msg_type"] == "image"]
        check("файл сохранён на диск",
              bool(photo) and photo[0]["file_path"] and os.path.exists(photo[0]["file_path"]))

        print("\nPDF-файл")
        SENT_MEDIA.clear()
        post_events(client, [{
            "type": "message", "timestamp": TS + 180000,
            "source": {"type": "group", "groupId": "Gtest", "userId": "U1"},
            "message": {"id": "msg4", "type": "file", "fileName": "schedule.pdf"},
        }])
        check("файл переслан", len(SENT_MEDIA) >= 1)
        check("имя файла в подписи",
              any("schedule.pdf" in c for _, c in SENT_MEDIA) if SENT_MEDIA else False)

        print("\nДубль вебхука")
        before = len(main.db.messages_for_day(DAY, "Gtest"))
        post_events(client, [{
            "type": "message", "timestamp": TS,
            "source": {"type": "group", "groupId": "Gtest", "userId": "U1"},
            "message": {"id": "msg1", "type": "text", "text": "พรุ่งนี้เอาเงิน 450 บาท"},
        }])
        after = len(main.db.messages_for_day(DAY, "Gtest"))
        check("повтор не создал новую запись", before == after, f"({before} → {after})")

        print("\nСобытие join")
        SENT_TEXTS.clear()
        post_events(client, [{
            "type": "join", "timestamp": TS,
            "source": {"type": "group", "groupId": "Gnew"},
        }])
        check("бот сообщил groupId", any("Gnew" in t for t in SENT_TEXTS), f"({SENT_TEXTS})")

        print("\nДневная сводка")
        SENT_TEXTS.clear()
        asyncio.run(main.summary_service.run_for_day(day=DAY, force=True))
        summary = [t for t in SENT_TEXTS if "Сводка за" in t]
        check("сводка отправлена", len(summary) == 1, f"({len(SENT_TEXTS)} сообщ.)")
        if summary:
            check("в шапке дата", DAY in summary[0])
            check("в шапке счётчики", "срочных" in summary[0] and "вложений" in summary[0])
            check("тело сводки на месте", "Сдать 450 бат" in summary[0])
        check("сводка записана в базу", main.db.summary_exists(DAY, "Gtest"))

        print("\nПовторный запуск сводки")
        SENT_TEXTS.clear()
        asyncio.run(main.summary_service.run_for_day(day=DAY, force=False))
        check("дубликат сводки не шлётся", len([t for t in SENT_TEXTS if "Сводка за" in t]) == 0)

        print("\nПустой день")
        SENT_TEXTS.clear()
        asyncio.run(main.summary_service.run_for_day(day="2026-01-01", force=True))
        check("сообщение о тихом дне", any("тихо" in t for t in SENT_TEXTS), f"({SENT_TEXTS})")

        print("\nОчистка старых файлов")
        asyncio.run(main.summary_service.cleanup_media())
        check("свежие файлы не удалены",
              bool(photo) and os.path.exists(photo[0]["file_path"]))

        print("\nАдминка с токеном")
        r = client.get("/admin/stats", params={"token": "admintok", "day": DAY})
        check("stats отвечает", r.status_code == 200, f"({r.status_code})")
        if r.status_code == 200:
            data = r.json()
            check("счётчик сообщений", data["messages"] == 4, f"({data['messages']})")
            check("счётчик срочных", data["urgent"] == 1, f"({data['urgent']})")


if __name__ == "__main__":
    print("=" * 52)
    print("Сквозной прогон")
    print("=" * 52)
    try:
        run()
    finally:
        print("\n" + "=" * 52)
        print(f"Пройдено: {PASSED}   Провалено: {FAILED}")
        print("=" * 52)
    sys.exit(1 if FAILED else 0)
