"""Тесты без обращения к внешним API."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Database  # noqa: E402
from app.line_api import EXT_BY_MIME, verify_signature  # noqa: E402
from app.summary import build_full_log, build_transcript  # noqa: E402
from app.telegram import esc, split_text  # noqa: E402

PASSED, FAILED = 0, 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name} {detail}")


def test_signature() -> None:
    print("\nПодпись вебхука LINE")
    secret = "test_secret_123"
    body = json.dumps({"events": []}).encode()
    good = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()

    check("верная подпись принимается", verify_signature(secret, body, good))
    check("подделанная подпись отклоняется", not verify_signature(secret, body, "AAAA"))
    check("пустая подпись отклоняется", not verify_signature(secret, body, ""))
    check("чужой секрет отклоняется", not verify_signature("other", body, good))
    check("изменённое тело отклоняется", not verify_signature(secret, body + b"x", good))


def test_db() -> None:
    print("\nБаза данных")
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "test.db"))

        base = dict(
            group_id="G1", group_name="ห้อง ป.2/3", user_id="U1",
            display_name="Ajarn Nok", ts="2026-09-17T08:30:00+07:00",
            day="2026-09-17", msg_type="text",
        )
        rid = db.save_message(line_message_id="m1", text_original="พรุ่งนี้เอาเงิน 450 บาท",
                              lang="th", text_translated="Завтра принести 450 бат",
                              is_urgent=1, urgent_reason="нужны деньги завтра", **base)
        check("сообщение сохраняется", rid is not None)

        dup = db.save_message(line_message_id="m1", text_original="дубль", **base)
        check("дубль вебхука игнорируется", dup is None)

        db.save_message(line_message_id="m2", text_original="[фото]",
                        text_translated="[фото] Расписание на неделю",
                        file_path="/tmp/photo.jpg", file_name="photo.jpg",
                        **{**base, "msg_type": "image", "ts": "2026-09-17T09:00:00+07:00"})

        rows = db.messages_for_day("2026-09-17")
        check("оба сообщения за день найдены", len(rows) == 2, f"(получено {len(rows)})")
        check("порядок по времени", rows[0]["line_message_id"] == "m1")
        check("срочность сохранена", rows[0]["is_urgent"] == 1)

        groups = db.groups_active_on("2026-09-17")
        check("группа найдена с названием", groups == [("G1", "ห้อง ป.2/3")], f"({groups})")

        pending = db.undelivered_media("2026-09-17", "G1")
        check("вложение в очереди на отправку", len(pending) == 1)
        db.mark_media_delivered([pending[0]["id"]])
        check("вложение помечено отправленным",
              len(db.undelivered_media("2026-09-17", "G1")) == 0)

        db.save_profile("G1", "U2", "Мама Сомчая")
        check("профиль кешируется", db.get_profile("G1", "U2") == "Мама Сомчая")
        db.save_profile("G1", "U2", "Сомчай — мама")
        check("профиль обновляется", db.get_profile("G1", "U2") == "Сомчай — мама")

        check("сводки ещё нет", not db.summary_exists("2026-09-17", "G1"))
        db.save_summary("2026-09-17", "G1", "текст сводки")
        check("сводка помечена отправленной", db.summary_exists("2026-09-17", "G1"))

        old = db.old_media_paths(keep_days=1, today=datetime(2026, 9, 30))
        check("старые файлы находятся для очистки", len(old) == 1, f"({old})")
        recent = db.old_media_paths(keep_days=90, today=datetime(2026, 9, 18))
        check("свежие файлы не трогаются", len(recent) == 0)


def test_telegram_helpers() -> None:
    print("\nHTML и разбиение текста")
    check("HTML экранируется", esc("<b>Ajarn & Co</b>") == "&lt;b&gt;Ajarn &amp; Co&lt;/b&gt;")
    check("None не ломает", esc(None) == "")

    short = "короткий текст"
    check("короткий текст не режется", split_text(short) == [short])

    long_text = "\n".join(f"строка номер {i} с текстом" for i in range(600))
    parts = split_text(long_text)
    check("длинный текст разбит", len(parts) > 1, f"({len(parts)} частей)")
    check("каждая часть в лимите", all(len(p) <= 4096 for p in parts))
    check("ничего не потеряно",
          sum(len(p) for p in parts) >= len(long_text) - len(parts))

    no_breaks = "x" * 10000
    parts2 = split_text(no_breaks)
    check("текст без переносов тоже режется", all(len(p) <= 4096 for p in parts2))


def test_transcript() -> None:
    print("\nФормирование транскрипта")
    rows = [
        {"ts": "2026-09-17T08:30:00+07:00", "display_name": "Ajarn Nok", "is_urgent": 1,
         "text_translated": "Завтра принести 450 бат на экскурсию",
         "text_original": "พรุ่งนี้เอาเงิน 450 บาท"},
        {"ts": "2026-09-17T09:15:00+07:00", "display_name": "Мама Мии", "is_urgent": 0,
         "text_translated": "Хорошо, спасибо", "text_original": "ค่ะ ขอบคุณ"},
    ]
    transcript = build_transcript(rows)
    check("время в транскрипте", "08:30" in transcript)
    check("маркер срочности", "[СРОЧНОЕ]" in transcript)
    check("несрочное без маркера", transcript.count("[СРОЧНОЕ]") == 1)
    check("автор указан", "Мама Мии" in transcript)

    full = build_full_log(rows)
    check("в полном логе есть оригинал", "พรุ่งนี้เอาเงิน 450 บาท" in full)
    check("в полном логе есть перевод", "450 бат" in full)


def test_prompts() -> None:
    print("\nПравила в промптах")
    from app.ai import (ASK_SYSTEM, EVENTS_SYSTEM, MONEY_SYSTEM, SUMMARY_SYSTEM,
                        TODO_SYSTEM, TRANSLATE_SYSTEM)

    fmt = dict(target="русский", today="2026-09-18", tomorrow="19.09")
    for name, tpl in [("SUMMARY", SUMMARY_SYSTEM), ("TODO", TODO_SYSTEM),
                      ("EVENTS", EVENTS_SYSTEM), ("MONEY", MONEY_SYSTEM),
                      ("ASK", ASK_SYSTEM)]:
        try:
            tpl.format(**fmt)
            ok = True
        except (KeyError, IndexError):
            ok = False
        check(f"{name} форматируется без ошибок", ok)

    check("справочные данные не сжимаются: SUMMARY",
          "СПРАВОЧНЫЕ ДАННЫЕ" in SUMMARY_SYSTEM)
    check("справочные данные не сжимаются: TODO",
          "СПРАВОЧНЫЕ ДАННЫЕ" in TODO_SYSTEM)
    check("справочные данные не сжимаются: EVENTS",
          "СПРАВОЧНЫЕ ДАННЫЕ" in EVENTS_SYSTEM)
    check("справочные данные не сжимаются: ASK",
          "СПРАВОЧНЫЕ ДАННЫЕ" in ASK_SYSTEM)

    check("номера страниц требуется сохранять",
          "страниц" in TODO_SYSTEM and "ТОЧНО" in TODO_SYSTEM)
    check("в /todo есть раздел про экзамены",
          "К контрольным и экзаменам" in TODO_SYSTEM)
    check("многодневные события разворачиваются по дням",
          "разворачивай его" in EVENTS_SYSTEM)
    check("в дне экзамена перечисляются предметы",
          "перечисляй предметы этого дня" in EVENTS_SYSTEM)
    check("расписание и темы сводятся вместе",
          "Сведи их сам" in TODO_SYSTEM and "РАЗНЫМИ" in TODO_SYSTEM)
    check("предметы сопоставляются по смыслу, а не дословно",
          "Сопоставляй по смыслу" in TODO_SYSTEM)
    check("предмет без тем не пропускается молча",
          "тем не присылали" in TODO_SYSTEM)
    check("/ask тоже сводит разные сообщения",
          "Своди данные из разных сообщений" in ASK_SYSTEM)
    check("события с разными датами не склеиваются: TODO",
          "РАЗНЫМИ датами" in TODO_SYSTEM)
    check("события с разными датами не склеиваются: EVENTS",
          "совпадение ДАТЫ" in EVENTS_SYSTEM)
    check("разные испытания не путаются в /ask",
          "Не путай разные испытания" in ASK_SYSTEM)
    check("номера с решёткой не принимаются за даты",
          all("не даты" in t or "а не даты" in t
              for t in (TODO_SYSTEM, EVENTS_SYSTEM, ASK_SYSTEM)))
    check("запрет сваливать неделю экзаменов в строку",
          "ошибка" in EVENTS_SYSTEM)

    check("каждое срочное попадает в разделы с действиями",
          "КАЖДОЕ сообщение, помеченное [СРОЧНОЕ]" in SUMMARY_SYSTEM)
    check("запрет путать будущее с прошлым",
          "Различай будущее и прошлое" in SUMMARY_SYSTEM)
    check("markdown запрещён везде",
          all("markdown" in t.lower() for t in
              (SUMMARY_SYSTEM, TODO_SYSTEM, EVENTS_SYSTEM, MONEY_SYSTEM, ASK_SYSTEM)))
    check("в переводчике описаны критерии срочности",
          "Срочное =" in TRANSLATE_SYSTEM and "НЕ срочное" in TRANSLATE_SYSTEM)


def test_mime_map() -> None:
    print("\nОпределение расширений вложений")
    check("jpeg → .jpg", EXT_BY_MIME["image/jpeg"] == ".jpg")
    check("mp4 → .mp4", EXT_BY_MIME["video/mp4"] == ".mp4")
    check("pdf → .pdf", EXT_BY_MIME["application/pdf"] == ".pdf")
    check("голосовое m4a", EXT_BY_MIME["audio/x-m4a"] == ".m4a")


if __name__ == "__main__":
    print("=" * 52)
    print("Тесты LINE → Telegram бота")
    print("=" * 52)
    test_signature()
    test_db()
    test_telegram_helpers()
    test_transcript()
    test_prompts()
    test_mime_map()
    print("\n" + "=" * 52)
    print(f"Пройдено: {PASSED}   Провалено: {FAILED}")
    print("=" * 52)
    sys.exit(1 if FAILED else 0)
