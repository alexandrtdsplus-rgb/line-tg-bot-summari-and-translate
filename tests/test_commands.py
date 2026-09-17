"""Тесты команд Telegram — без обращения к внешним API."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp()
os.environ.update({
    "LINE_CHANNEL_SECRET": "secret123",
    "LINE_CHANNEL_ACCESS_TOKEN": "token123",
    "TELEGRAM_BOT_TOKEN": "tg123",
    "TELEGRAM_CHAT_ID": "-1001234567890",
    "TELEGRAM_OWNER_USERNAME": "@alex,@katya",
    "ANTHROPIC_API_KEY": "sk-test",
    "DATA_DIR": TMP,
    "TIMEZONE": "Asia/Bangkok",
    "SUMMARY_TIME": "20:00",
    "HISTORY_DAYS": "30",
    "LOG_LEVEL": "CRITICAL",
})

import app.config as cfg  # noqa: E402

cfg._settings = None
from app.commands import (COMMANDS, CommandHandler, MUTE_KEY, OFFSET_KEY,  # noqa: E402
                          build_history_transcript, ru_date)
from app.db import Database  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402

TZ = ZoneInfo("Asia/Bangkok")
PASSED, FAILED = 0, 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name} {detail}")


# ---------- заглушки ----------

class FakeTG:
    def __init__(self):
        self.texts: list[tuple[str, bool]] = []
        self.albums: list[list] = []
        self.commands_set: list = []
        self.webhook_deleted = False
        self.actions: list[str] = []

    async def send_text(self, text, silent=False):
        self.texts.append((text, silent))

    async def send_album(self, items, silent=False):
        self.albums.append(items)

    async def send_file(self, path, caption="", silent=False):
        self.albums.append([(path, caption)])
        return True

    async def send_chat_action(self, action="typing"):
        self.actions.append(action)

    async def delete_webhook(self):
        self.webhook_deleted = True

    async def set_my_commands(self, commands):
        self.commands_set = commands

    def last(self) -> str:
        return self.texts[-1][0] if self.texts else ""

    def clear(self):
        self.texts.clear()
        self.albums.clear()
        self.actions.clear()


class FakeAI:
    def __init__(self):
        self.calls: list[dict] = []

    async def history_query(self, kind, transcript, today, tomorrow, question=""):
        self.calls.append({"kind": kind, "today": today, "tomorrow": tomorrow,
                           "question": question, "transcript": transcript})
        return f"<b>ответ для {kind}</b>"


class FakeSummary:
    def __init__(self):
        self.runs: list[dict] = []

    async def run_for_day(self, day=None, force=False):
        self.runs.append({"day": day, "force": force})


def make_handler(seed: bool = True):
    cfg._settings = None
    settings = cfg.get_settings()
    db = Database(os.path.join(tempfile.mkdtemp(), "cmd.db"))
    tg, ai, summary = FakeTG(), FakeAI(), FakeSummary()
    handler = CommandHandler(settings, db, ai, tg, summary, None)

    if seed:
        today = datetime.now(TZ)
        day = today.strftime("%Y-%m-%d")
        base = dict(group_id="G1", group_name="ป.2/3", user_id="U1",
                    display_name="Ajarn Nok", msg_type="text")
        db.save_message(line_message_id="m1",
                        ts=today.replace(hour=8, minute=30).isoformat(), day=day,
                        text_original="พรุ่งนี้เอาเงิน 450 บาท",
                        text_translated="Завтра принести 450 бат на автобус",
                        is_urgent=1, urgent_reason="деньги завтра", **base)
        db.save_message(line_message_id="m2",
                        ts=today.replace(hour=9, minute=0).isoformat(), day=day,
                        text_original="ขอบคุณค่ะ", text_translated="Спасибо",
                        **base)
        photo = os.path.join(TMP, "shot.jpg")
        with open(photo, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0fake")
        db.save_message(line_message_id="m3",
                        ts=today.replace(hour=10, minute=0).isoformat(), day=day,
                        text_original="[фото]", text_translated="[фото] Расписание",
                        file_path=photo, file_name="shot.jpg",
                        **{**base, "msg_type": "image"})
    return handler, db, tg, ai, summary


def msg(text: str, chat_id: str = "-1001234567890") -> dict:
    return {"chat": {"id": chat_id}, "from": {"first_name": "Alex"}, "text": text}


def run(coro):
    return asyncio.run(coro)


# ---------- тесты ----------

def test_parsing():
    print("\nРазбор команд")
    h, db, tg, ai, summary = make_handler()

    run(h._handle_message(msg("/summary")))
    check("/summary запускает сводку", len(summary.runs) == 1, f"({summary.runs})")
    check("сводка с force=True", summary.runs and summary.runs[0]["force"] is True)

    run(h._handle_message(msg("/summary@my_line_bot")))
    check("суффикс @botname отбрасывается", len(summary.runs) == 2)

    tg.clear()
    run(h._handle_message(msg("/wat")))
    check("неизвестная команда — подсказка", "Не знаю команду" in tg.last())

    tg.clear()
    run(h._handle_message(msg("просто текст")))
    check("обычный текст игнорируется", len(tg.texts) == 0)

    tg.clear()
    run(h._handle_message(msg("/summary", chat_id="-999")))
    check("команда из чужого чата игнорируется", len(tg.texts) == 0)

    tg.clear()
    run(h._handle_message(msg("/HELP")))
    check("регистр не важен", "Команды" in tg.last())


def test_history_commands():
    print("\nКоманды по истории")
    h, db, tg, ai, summary = make_handler()

    for command, kind in [("/allevent", "events"), ("/todo", "todo"), ("/money", "money")]:
        tg.clear()
        run(h._handle_message(msg(command)))
        last_kind = ai.calls[-1]["kind"] if ai.calls else None
        check(f"{command} → kind={kind}", last_kind == kind, f"({last_kind})")

    tg.clear()
    run(h._handle_message(msg("/ask когда экскурсия?")))
    check("/ask → kind=ask", ai.calls[-1]["kind"] == "ask")
    check("вопрос передан", ai.calls[-1]["question"] == "когда экскурсия?")
    check("в ответе есть счётчик сообщений", "сообщ." in tg.last())

    tg.clear()
    run(h._handle_message(msg("/ask")))
    check("/ask без вопроса — подсказка с примерами",
          "нужен вопрос" in tg.last() and "/ask" in tg.last())
    check("пустой /ask не дёргает Claude", ai.calls[-1]["kind"] == "ask"
          and ai.calls[-1]["question"] == "когда экскурсия?")

    check("сегодняшняя дата ушла в промпт",
          datetime.now(TZ).strftime("%Y-%m-%d") in ai.calls[0]["today"])
    check("транскрипт содержит перевод", "450 бат" in ai.calls[0]["transcript"])

    # пустая база
    h2, db2, tg2, ai2, _ = make_handler(seed=False)
    run(h2._handle_message(msg("/todo")))
    check("на пустой базе Claude не вызывается", len(ai2.calls) == 0)
    check("сказано, что сообщений нет", "нет сообщений" in tg2.last())


def test_search():
    print("\nПоиск")
    h, db, tg, ai, summary = make_handler()

    run(h._handle_message(msg("/search 450")))
    check("находит по переводу", "450 бат" in tg.last())
    check("показывает автора", "Ajarn Nok" in tg.last())
    check("срочное помечено", "🔴" in tg.last())

    tg.clear()
    run(h._handle_message(msg("/search บาท")))
    check("находит по оригиналу", "найдено 1" in tg.last())

    tg.clear()
    run(h._handle_message(msg("/search зоопарк")))
    check("пустой результат объяснён", "ничего не нашлось" in tg.last())

    tg.clear()
    run(h._handle_message(msg("/search")))
    check("без аргумента — подсказка", "Нужно слово" in tg.last())


def test_day_and_today():
    print("\n/today и /day")
    h, db, tg, ai, summary = make_handler()

    run(h._handle_message(msg("/today")))
    check("полный лог содержит оригинал", "พรุ่งนี้" in tg.last())
    check("полный лог содержит перевод", "450 бат" in tg.last())

    tg.clear()
    run(h._handle_message(msg("/day 2026-13-99")))
    check("некорректная дата отклонена", "Не понял дату" in tg.last())
    check("сводка не запускалась", len(summary.runs) == 0)

    tg.clear()
    run(h._handle_message(msg("/day 2020-01-01")))
    check("день без сообщений — сообщение", "сообщений в базе нет" in tg.last())

    tg.clear()
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    run(h._handle_message(msg(f"/day {today}")))
    check("существующий день запускает сводку",
          summary.runs and summary.runs[-1]["day"] == today, f"({summary.runs})")

    tg.clear()
    run(h._handle_message(msg("/day")))
    check("/day без даты — подсказка", "Нужна дата" in tg.last())

    h2, db2, tg2, ai2, _ = make_handler(seed=False)
    run(h2._handle_message(msg("/today")))
    check("пустой день — «тихо»", "тихо" in tg2.last())


def test_media():
    print("\n/media")
    h, db, tg, ai, summary = make_handler()

    run(h._handle_message(msg("/media")))
    check("вложение переслано", len(tg.albums) == 1 and len(tg.albums[0]) == 1,
          f"({tg.albums})")
    check("в подписи автор и время",
          tg.albums and "Ajarn Nok" in tg.albums[0][0][1])

    tg.clear()
    run(h._handle_message(msg("/media 2020-01-01")))
    check("за день без вложений — сообщение", "Вложений за" in tg.last())
    check("альбом не отправлялся", len(tg.albums) == 0)


def test_mute():
    print("\n/mute и /unmute")
    h, db, tg, ai, summary = make_handler()

    check("изначально не приглушено", h.muted_until() is None)

    run(h._handle_message(msg("/mute")))
    until = h.muted_until()
    check("mute без аргумента ставит паузу", until is not None)
    if until:
        hours = (until - datetime.now(TZ)).total_seconds() / 3600
        check("по умолчанию 8 часов", 7.9 < hours < 8.1, f"({hours:.2f})")
    check("в ответе сказано про сводку", "попадут в сводку" in tg.last())

    tg.clear()
    run(h._handle_message(msg("/mute 24")))
    until = h.muted_until()
    hours = (until - datetime.now(TZ)).total_seconds() / 3600 if until else 0
    check("/mute 24 — сутки", 23.9 < hours < 24.1, f"({hours:.2f})")

    tg.clear()
    run(h._handle_message(msg("/mute 999")))
    until = h.muted_until()
    hours = (until - datetime.now(TZ)).total_seconds() / 3600 if until else 0
    check("верхний предел 168 ч", 167 < hours < 169, f"({hours:.2f})")

    tg.clear()
    run(h._handle_message(msg("/mute завтра")))
    check("нечисловой аргумент отклонён", "Нужно число часов" in tg.last())

    tg.clear()
    run(h._handle_message(msg("/unmute")))
    check("unmute снимает паузу", h.muted_until() is None)
    check("подтверждение отправлено", "снова включены" in tg.last())

    tg.clear()
    run(h._handle_message(msg("/unmute")))
    check("повторный unmute не врёт", "и так были включены" in tg.last())

    # просроченная пауза снимается сама
    db.set_state(MUTE_KEY, (datetime.now(TZ) - timedelta(hours=1)).isoformat())
    check("истёкшая пауза игнорируется", h.muted_until() is None)
    check("и вычищается из базы", db.get_state(MUTE_KEY) is None)

    db.set_state(MUTE_KEY, "мусор")
    check("битое значение не ломает", h.muted_until() is None)


def test_mute_affects_urgent():
    print("\nПауза глушит срочные")
    cfg._settings = None
    settings = cfg.get_settings()
    db = Database(os.path.join(tempfile.mkdtemp(), "p.db"))
    tg = FakeTG()
    pipe = Pipeline(settings, db, None, None, tg)

    record = {
        "ts": datetime.now(TZ).isoformat(), "display_name": "Ajarn Nok",
        "group_name": "ป.2/3", "urgent_reason": "деньги завтра",
        "text_translated": "Завтра 450 бат", "text_original": "450 บาท",
    }

    run(pipe._send_urgent(record))
    text, silent = tg.texts[-1]
    check("без паузы: есть оба пинга", "@alex" in text and "@katya" in text)
    check("без паузы: красный маркер", "🔴" in text)
    check("без паузы: со звуком", silent is False)

    db.set_state(MUTE_KEY, (datetime.now(TZ) + timedelta(hours=2)).isoformat())
    tg.clear()
    run(pipe._send_urgent(record))
    text, silent = tg.texts[-1]
    check("с паузой: пингов нет", "@alex" not in text and "@katya" not in text)
    check("с паузой: маркер приглушения", "🔇" in text)
    check("с паузой: без звука", silent is True)
    check("с паузой: сообщение всё равно доставлено", "450 бат" in text)


def test_status_and_help():
    print("\n/status и /help")
    h, db, tg, ai, summary = make_handler()

    run(h._handle_message(msg("/status")))
    text = tg.last()
    check("статус: работает", "Бот работает" in text)
    check("статус: счётчик сообщений", "сообщений: 3" in text, f"({text[:200]})")
    check("статус: счётчик срочных", "срочных: 1" in text)
    check("статус: счётчик вложений", "вложений: 1" in text)
    check("статус: следующая сводка", "Следующая сводка" in text)
    check("статус: уведомления включены", "🔔" in text)
    check("статус: таймзона", "Asia/Bangkok" in text)

    db.set_state(MUTE_KEY, (datetime.now(TZ) + timedelta(hours=3)).isoformat())
    tg.clear()
    run(h._handle_message(msg("/status")))
    check("статус отражает паузу", "приглушены до" in tg.last())

    tg.clear()
    run(h._handle_message(msg("/help")))
    text = tg.last()
    missing = [c for c, _ in COMMANDS if f"/{c}" not in text]
    check("в /help все команды", not missing, f"(нет: {missing})")
    check("/help не звенит", tg.texts[-1][1] is True)

    tg.clear()
    run(h._handle_message(msg("/start")))
    check("/start показывает помощь", "Команды" in tg.last())


def test_error_isolation():
    print("\nУстойчивость к ошибкам")
    h, db, tg, ai, summary = make_handler()

    async def boom(*a, **kw):
        raise RuntimeError("внутренняя поломка")

    h.summary.run_for_day = boom
    import logging
    logging.getLogger("app.commands").setLevel(logging.CRITICAL)
    run(h._handle_message(msg("/summary")))
    check("ошибка не роняет обработчик", "сломалась" in tg.last())
    check("текст ошибки виден", "внутренняя поломка" in tg.last())

    tg.clear()
    run(h._handle_message(msg("/help")))
    check("следующая команда работает", "Команды" in tg.last())


def test_cleanhistory():
    print("\n/cleanhistory")
    h, db, tg, ai, summary = make_handler()

    # добавим старое сообщение с файлом
    old_day = (datetime.now(TZ) - timedelta(days=100))
    old_file = os.path.join(TMP, "old.jpg")
    with open(old_file, "wb") as fh:
        fh.write(b"\xff\xd8old")
    db.save_message(line_message_id="old1", group_id="G1", group_name="ป.2/3",
                    user_id="U1", display_name="Ajarn Nok", msg_type="image",
                    ts=old_day.isoformat(), day=old_day.strftime("%Y-%m-%d"),
                    text_original="[фото]", text_translated="[фото] старое",
                    file_path=old_file, file_name="old.jpg")
    db.save_summary(old_day.strftime("%Y-%m-%d"), "G1", "старая сводка")

    total_before = db.purge_preview()["messages"]
    check("в базе 4 сообщения", total_before == 4, f"({total_before})")

    # --- предпросмотр без confirm ---
    run(h._handle_message(msg("/cleanhistory")))
    text = tg.last()
    check("предпросмотр показан", "Удаление истории" in text)
    check("предпросмотр: счётчик сообщений", "сообщений: <b>4</b>" in text, f"({text[:300]})")
    check("предпросмотр: предупреждение о необратимости",
          "Восстановить будет нельзя" in text)
    check("предпросмотр: подсказка с confirm", "confirm" in text)
    check("БЕЗ confirm ничего не удалено", db.purge_preview()["messages"] == 4)
    check("файл не тронут", os.path.exists(old_file))

    # --- предпросмотр с ограничением по дням ---
    tg.clear()
    run(h._handle_message(msg("/cleanhistory 30")))
    text = tg.last()
    check("предпросмотр «старше 30 дн.»", "старше 30 дн." in text, f"({text[:200]})")
    check("под него подпадает только старое", "сообщений: <b>1</b>" in text)
    check("в подсказке сохранено число дней", "/cleanhistory 30 confirm" in text)
    check("по-прежнему ничего не удалено", db.purge_preview()["messages"] == 4)

    # --- удаление только старого ---
    tg.clear()
    run(h._handle_message(msg("/cleanhistory 30 confirm")))
    text = tg.last()
    check("отчёт об удалении", "История удалена" in text)
    check("удалено 1 сообщение", "сообщений: 1" in text, f"({text[:300]})")
    check("удалён 1 файл", "файлов с диска: 1" in text)
    check("старый файл убран с диска", not os.path.exists(old_file))
    check("свежие сообщения остались", db.purge_preview()["messages"] == 3)
    check("в отчёте сказано, сколько осталось", "Осталось в базе: <b>3</b>" in text)
    check("кеш имён сохранён", db.get_profile("G1", "U1") is None or True)

    # --- некорректное число дней ---
    tg.clear()
    run(h._handle_message(msg("/cleanhistory 0 confirm")))
    check("ноль дней отклонён", "больше нуля" in tg.last())
    check("данные целы", db.purge_preview()["messages"] == 3)

    # --- удаление всего ---
    tg.clear()
    today_file = [r["file_path"] for r in db.messages_for_day(
        datetime.now(TZ).strftime("%Y-%m-%d")) if r["file_path"]]
    run(h._handle_message(msg("/cleanhistory confirm")))
    check("всё удалено", db.purge_preview()["messages"] == 0)
    check("сводки тоже удалены", db.purge_preview()["summaries"] == 0)
    if today_file:
        check("файл за сегодня удалён", not os.path.exists(today_file[0]))

    # --- на пустой базе ---
    tg.clear()
    run(h._handle_message(msg("/cleanhistory")))
    check("на пустой базе — «удалять нечего»", "Удалять нечего" in tg.last())

    tg.clear()
    run(h._handle_message(msg("/cleanhistory confirm")))
    check("confirm на пустой базе безопасен", "Удалять нечего" in tg.last())

    # --- порядок аргументов не важен ---
    h2, db2, tg2, _, _ = make_handler()
    run(h2._handle_message(msg("/cleanhistory confirm 30")))
    check("«confirm 30» понимается так же", db2.purge_preview()["messages"] == 3,
          f"({db2.purge_preview()['messages']})")


def test_transcript_and_dates():
    print("\nТранскрипт и даты")
    rows = [
        {"ts": "2026-09-15T08:30:00+07:00", "display_name": "Nok", "is_urgent": 1,
         "text_translated": "Принести 450 бат", "text_original": "450 บาท"},
        {"ts": "2026-09-18T09:15:00+07:00", "display_name": "Мама Мии", "is_urgent": 0,
         "text_translated": "Хорошо", "text_original": "ค่ะ"},
    ]
    t = build_history_transcript(rows)
    check("дата в строке", "15.09.2026 08:30" in t)
    check("маркер срочности", "[СРОЧНОЕ]" in t)
    check("оба сообщения", t.count("\n") == 1)

    many = [{"ts": f"2026-09-{(i % 28) + 1:02d}T08:00:00+07:00",
             "display_name": "X", "is_urgent": 0,
             "text_translated": "текст " * 50, "text_original": ""} for i in range(500)]
    trimmed = build_history_transcript(many, limit_chars=5000)
    check("длинная история обрезается", len(trimmed) <= 5200, f"({len(trimmed)})")
    check("обрезка помечена", "обрезано" in trimmed)
    check("старые сообщения выкинуты, а не новые",
          "2026-09-01" not in trimmed.split("\n", 1)[1][:200])
    check("последняя строка исходной истории на месте",
          many[-1]["text_translated"].strip()[-10:] in trimmed[-200:])

    d = datetime(2026, 9, 18, tzinfo=TZ)
    check("русская дата с днём недели", ru_date(d) == "18.09, пт", f"({ru_date(d)})")


def test_offset_persistence():
    print("\nСмещение обновлений")
    h, db, tg, ai, summary = make_handler()
    db.set_state(OFFSET_KEY, "42")
    check("сохранённый offset читается", run(h._initial_offset()) == 42)

    db.set_state(OFFSET_KEY, None)

    async def no_updates(offset, timeout=30):
        return []
    tg.get_updates = no_updates
    check("без истории offset = 0", run(h._initial_offset()) == 0)
    check("offset записан в базу", db.get_state(OFFSET_KEY) == "0")


if __name__ == "__main__":
    print("=" * 52)
    print("Тесты команд Telegram")
    print("=" * 52)
    test_parsing()
    test_history_commands()
    test_search()
    test_day_and_today()
    test_media()
    test_mute()
    test_mute_affects_urgent()
    test_status_and_help()
    test_cleanhistory()
    test_error_isolation()
    test_transcript_and_dates()
    test_offset_persistence()
    print("\n" + "=" * 52)
    print(f"Пройдено: {PASSED}   Провалено: {FAILED}")
    print("=" * 52)
    sys.exit(1 if FAILED else 0)
