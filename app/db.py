"""Хранилище сообщений на SQLite."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    line_message_id   TEXT UNIQUE,
    group_id          TEXT NOT NULL,
    group_name        TEXT,
    user_id           TEXT,
    display_name      TEXT,
    ts                TEXT NOT NULL,          -- ISO8601 в локальной таймзоне
    day               TEXT NOT NULL,          -- YYYY-MM-DD в локальной таймзоне
    msg_type          TEXT NOT NULL,          -- text/image/video/audio/file/sticker
    text_original     TEXT,
    lang              TEXT,
    text_translated   TEXT,
    is_urgent         INTEGER DEFAULT 0,
    urgent_reason     TEXT,
    file_path         TEXT,
    file_name         TEXT,
    delivered_media   INTEGER DEFAULT 0,
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_day ON messages(day);
CREATE INDEX IF NOT EXISTS idx_messages_group_day ON messages(group_id, day);

CREATE TABLE IF NOT EXISTS profiles (
    group_id     TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    display_name TEXT,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS summaries (
    day        TEXT NOT NULL,
    group_id   TEXT NOT NULL,
    body       TEXT,
    sent_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (day, group_id)
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._init()

    def _init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- сообщения ----------

    def save_message(self, **kwargs: Any) -> int | None:
        """Сохраняет сообщение. Возвращает id или None, если такое уже есть (дубль вебхука)."""
        columns = [
            "line_message_id", "group_id", "group_name", "user_id", "display_name",
            "ts", "day", "msg_type", "text_original", "lang", "text_translated",
            "is_urgent", "urgent_reason", "file_path", "file_name",
        ]
        values = [kwargs.get(c) for c in columns]
        placeholders = ", ".join("?" for _ in columns)
        with self.connect() as conn:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO messages ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
            return cur.lastrowid if cur.rowcount else None

    def messages_for_day(self, day: str, group_id: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM messages WHERE day = ?"
        params: list[Any] = [day]
        if group_id:
            query += " AND group_id = ?"
            params.append(group_id)
        query += " ORDER BY ts ASC, id ASC"
        with self.connect() as conn:
            return list(conn.execute(query, params))

    def groups_active_on(self, day: str) -> list[tuple[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT group_id, COALESCE(MAX(group_name), '') AS group_name "
                "FROM messages WHERE day = ? GROUP BY group_id",
                (day,),
            )
            return [(r["group_id"], r["group_name"]) for r in rows]

    def undelivered_media(self, day: str, group_id: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(
                "SELECT * FROM messages WHERE day = ? AND group_id = ? "
                "AND file_path IS NOT NULL AND delivered_media = 0 ORDER BY ts ASC",
                (day, group_id),
            ))

    def mark_media_delivered(self, ids: list[int]) -> None:
        if not ids:
            return
        with self.connect() as conn:
            conn.executemany("UPDATE messages SET delivered_media = 1 WHERE id = ?", [(i,) for i in ids])

    # ---------- профили ----------

    def get_profile(self, group_id: str, user_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT display_name FROM profiles WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
            return row["display_name"] if row else None

    def save_profile(self, group_id: str, user_id: str, display_name: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO profiles (group_id, user_id, display_name, updated_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(group_id, user_id) DO UPDATE SET "
                "display_name = excluded.display_name, updated_at = CURRENT_TIMESTAMP",
                (group_id, user_id, display_name),
            )

    # ---------- сводки ----------

    def summary_exists(self, day: str, group_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM summaries WHERE day = ? AND group_id = ?", (day, group_id)
            ).fetchone()
            return row is not None

    def save_summary(self, day: str, group_id: str, body: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO summaries (day, group_id, body) VALUES (?, ?, ?) "
                "ON CONFLICT(day, group_id) DO UPDATE SET body = excluded.body, "
                "sent_at = CURRENT_TIMESTAMP",
                (day, group_id, body),
            )

    # ---------- состояние (mute и прочие флаги) ----------

    def get_state(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_state(self, key: str, value: str | None) -> None:
        with self.connect() as conn:
            if value is None:
                conn.execute("DELETE FROM state WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (key, value),
                )

    # ---------- выборки для команд ----------

    def messages_since(self, day_from: str, group_id: str | None = None) -> list[sqlite3.Row]:
        """Все сообщения начиная с указанного дня включительно."""
        query = "SELECT * FROM messages WHERE day >= ?"
        params: list[Any] = [day_from]
        if group_id:
            query += " AND group_id = ?"
            params.append(group_id)
        query += " ORDER BY ts ASC, id ASC"
        with self.connect() as conn:
            return list(conn.execute(query, params))

    def search_messages(self, needle: str, limit: int = 25) -> list[sqlite3.Row]:
        """Поиск по переводу и по оригиналу, свежие сверху."""
        pattern = f"%{needle}%"
        with self.connect() as conn:
            return list(conn.execute(
                "SELECT * FROM messages "
                "WHERE text_translated LIKE ? OR text_original LIKE ? "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (pattern, pattern, limit),
            ))

    def counts_for_day(self, day: str) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(is_urgent), 0) AS urgent, "
                "COALESCE(SUM(CASE WHEN file_path IS NOT NULL THEN 1 ELSE 0 END), 0) AS media "
                "FROM messages WHERE day = ?",
                (day,),
            ).fetchone()
            return {"total": row["total"], "urgent": row["urgent"], "media": row["media"]}

    def first_message_day(self) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT MIN(day) AS d FROM messages").fetchone()
            return row["d"] if row and row["d"] else None

    # ---------- удаление истории ----------

    def purge_preview(self, before_day: str | None = None) -> dict:
        """Что будет удалено. before_day=None — вся история."""
        where, params = ("", []) if before_day is None else ("WHERE day < ?", [before_day])
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS total, "
                f"COALESCE(SUM(CASE WHEN file_path IS NOT NULL THEN 1 ELSE 0 END), 0) AS files, "
                f"MIN(day) AS first_day, MAX(day) AS last_day "
                f"FROM messages {where}", params,
            ).fetchone()
            summaries = conn.execute(
                f"SELECT COUNT(*) AS n FROM summaries {where}", params,
            ).fetchone()["n"]
            return {
                "messages": row["total"],
                "files": row["files"],
                "summaries": summaries,
                "first_day": row["first_day"],
                "last_day": row["last_day"],
            }

    def purge_file_paths(self, before_day: str | None = None) -> list[str]:
        where, params = ("", []) if before_day is None else ("AND day < ?", [before_day])
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT file_path FROM messages WHERE file_path IS NOT NULL {where}",
                params,
            )
            return [r["file_path"] for r in rows]

    def purge_messages(self, before_day: str | None = None) -> dict:
        """Удаляет сообщения и сводки. Профили (кеш имён) сохраняются."""
        where, params = ("", []) if before_day is None else ("WHERE day < ?", [before_day])
        with self.connect() as conn:
            messages = conn.execute(f"DELETE FROM messages {where}", params).rowcount
            summaries = conn.execute(f"DELETE FROM summaries {where}", params).rowcount
        with self.connect() as conn:
            conn.execute("VACUUM")
        return {"messages": messages, "summaries": summaries}

    # ---------- обслуживание ----------

    def old_media_paths(self, keep_days: int, today: datetime) -> list[tuple[int, str]]:
        if keep_days <= 0:
            return []
        cutoff = (today - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, file_path FROM messages WHERE file_path IS NOT NULL AND day < ?",
                (cutoff,),
            )
            return [(r["id"], r["file_path"]) for r in rows]

    def clear_media_path(self, message_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE messages SET file_path = NULL WHERE id = ?", (message_id,))
