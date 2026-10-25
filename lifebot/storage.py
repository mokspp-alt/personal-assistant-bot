"""SQLite: настройки чата, дела, заметки, отправленные сводки, владелец бота."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    tz TEXT NOT NULL DEFAULT 'Europe/Moscow',
    digest_time TEXT DEFAULT '08:30',
    calendar_id TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    list TEXT NOT NULL DEFAULT 'дела',
    due_date TEXT,
    due_time TEXT,
    remind_before INTEGER,
    remind_at TEXT,
    repeat TEXT NOT NULL DEFAULT 'none',
    done_at TEXT,
    created_at TEXT NOT NULL,
    calendar INTEGER NOT NULL DEFAULT 0,
    event_id TEXT,
    duration INTEGER,
    location TEXT,
    details TEXT
);
CREATE INDEX IF NOT EXISTS tasks_chat ON tasks (chat_id, done_at);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sent (
    chat_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (chat_id, key)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Колонки, добавленные после первой версии, — для баз, созданных раньше.
MIGRATIONS = {
    "chats": {"calendar_id": "TEXT"},
    "tasks": {"calendar": "INTEGER NOT NULL DEFAULT 0", "event_id": "TEXT", "duration": "INTEGER",
              "location": "TEXT", "details": "TEXT"},
}
DEFAULT_LIST = "дела"
TASK_FIELDS = {"title", "list", "due_date", "due_time", "remind_before", "remind_at", "repeat", "done_at",
               "calendar", "event_id", "duration", "location", "details"}


@dataclass
class Chat:
    chat_id: int
    tz: str
    digest_time: str | None  # "08:30" или None — сводку не присылать
    calendar_id: str | None = None  # Google Календарь, куда писать события

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


@dataclass
class Task:
    id: int
    chat_id: int
    title: str
    list: str
    due_date: date | None
    due_time: str | None  # "18:00"
    remind_before: int | None  # минут до срока; None — без напоминания
    remind_at: datetime | None  # когда напомнить (UTC); None — уже напомнили или не нужно
    repeat: str
    done_at: datetime | None
    created_at: datetime
    calendar: bool = False  # дублировать событием в Google Календаре
    event_id: str | None = None
    duration: int | None = None  # минут, для события в календаре
    location: str | None = None
    details: str | None = None

    def due_at(self, tz) -> datetime | None:
        if not (self.due_date and self.due_time):
            return None
        h, m = map(int, self.due_time.split(":"))
        return datetime(self.due_date.year, self.due_date.month, self.due_date.day, h, m, tzinfo=tz)

    def planned_reminder(self, tz) -> datetime | None:
        """Когда напоминать по сроку и настройке; None — не напоминать."""
        due = self.due_at(tz)
        if due is None or self.remind_before is None:
            return None
        return due - timedelta(minutes=self.remind_before)


@dataclass
class Note:
    id: int
    text: str
    created_at: datetime


def _utc(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _task(r: sqlite3.Row) -> Task:
    return Task(
        r["id"], r["chat_id"], r["title"], r["list"],
        date.fromisoformat(r["due_date"]) if r["due_date"] else None,
        r["due_time"], r["remind_before"], _dt(r["remind_at"]), r["repeat"],
        _dt(r["done_at"]), datetime.fromisoformat(r["created_at"]),
        bool(r["calendar"]), r["event_id"], r["duration"], r["location"], r["details"],
    )


class Storage:
    def __init__(self, path: str) -> None:
        if path != ":memory:" and os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, sql in columns.items():
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql}")
        self.db.commit()

    # --- чаты ---

    def chat(self, chat_id: int) -> Chat:
        self.db.execute("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))
        self.db.commit()
        r = self.db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return Chat(r["chat_id"], r["tz"], r["digest_time"], r["calendar_id"])

    def chats(self) -> list[Chat]:
        ids = [r[0] for r in self.db.execute("SELECT chat_id FROM chats")]
        return [self.chat(i) for i in ids]

    def update_chat(self, chat_id: int, **fields) -> None:
        self.chat(chat_id)
        for name, value in fields.items():
            assert name in {"tz", "digest_time", "calendar_id"}
            self.db.execute(f"UPDATE chats SET {name} = ? WHERE chat_id = ?", (value, chat_id))
        self.db.commit()

    # --- дела ---

    def add_task(self, chat_id: int, title: str, list: str = DEFAULT_LIST, due_date: date | None = None,
                 due_time: str | None = None, remind_before: int | None = None,
                 remind_at: datetime | None = None, repeat: str = "none", calendar: bool = False,
                 duration: int | None = None, location: str | None = None, details: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO tasks (chat_id, title, list, due_date, due_time, remind_before, remind_at, "
            "repeat, created_at, calendar, duration, location, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, title, list, due_date.isoformat() if due_date else None, due_time, remind_before,
             _utc(remind_at) if remind_at else None, repeat, _utc(datetime.now(timezone.utc)),
             int(calendar), duration, location, details),
        )
        self.db.commit()
        return cur.lastrowid

    def task(self, chat_id: int, task_id: int) -> Task | None:
        r = self.db.execute("SELECT * FROM tasks WHERE chat_id = ? AND id = ?", (chat_id, task_id)).fetchone()
        return _task(r) if r else None

    def update_task(self, chat_id: int, task_id: int, **fields) -> None:
        for name, value in fields.items():
            assert name in TASK_FIELDS
            if isinstance(value, datetime):
                value = _utc(value)
            elif isinstance(value, date):
                value = value.isoformat()
            self.db.execute(f"UPDATE tasks SET {name} = ? WHERE chat_id = ? AND id = ?",
                            (value, chat_id, task_id))
        self.db.commit()

    def delete_task(self, chat_id: int, task_id: int) -> None:
        self.db.execute("DELETE FROM tasks WHERE chat_id = ? AND id = ?", (chat_id, task_id))
        self.db.commit()

    def open_tasks(self, chat_id: int) -> list[Task]:
        """Невыполненные дела: сначала со сроком (по сроку), потом без срока (по порядку записи)."""
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND done_at IS NULL "
            "ORDER BY due_date IS NULL, due_date, due_time IS NULL, due_time, id", (chat_id,))
        return [_task(r) for r in rows]

    def done_tasks(self, chat_id: int, since: datetime) -> list[Task]:
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND done_at >= ? ORDER BY done_at",
            (chat_id, _utc(since)))
        return [_task(r) for r in rows]

    def due_reminders(self, now: datetime) -> list[Task]:
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE done_at IS NULL AND remind_at IS NOT NULL AND remind_at <= ? "
            "ORDER BY remind_at", (_utc(now),))
        return [_task(r) for r in rows]

    # --- заметки ---

    def add_note(self, chat_id: int, text: str) -> int:
        cur = self.db.execute("INSERT INTO notes (chat_id, text, created_at) VALUES (?, ?, ?)",
                              (chat_id, text, _utc(datetime.now(timezone.utc))))
        self.db.commit()
        return cur.lastrowid

    def notes(self, chat_id: int) -> list[Note]:
        rows = self.db.execute("SELECT * FROM notes WHERE chat_id = ? ORDER BY id", (chat_id,))
        return [Note(r["id"], r["text"], datetime.fromisoformat(r["created_at"])) for r in rows]

    def note(self, chat_id: int, note_id: int) -> Note | None:
        return next((n for n in self.notes(chat_id) if n.id == note_id), None)

    def update_note(self, chat_id: int, note_id: int, text: str) -> None:
        self.db.execute("UPDATE notes SET text = ? WHERE chat_id = ? AND id = ?", (text, chat_id, note_id))
        self.db.commit()

    def delete_note(self, chat_id: int, note_id: int) -> None:
        self.db.execute("DELETE FROM notes WHERE chat_id = ? AND id = ?", (chat_id, note_id))
        self.db.commit()

    # --- прочее ---

    def mark_sent(self, chat_id: int, key: str) -> bool:
        """True, если сообщение с этим ключом ещё не отправлялось."""
        cur = self.db.execute("INSERT OR IGNORE INTO sent (chat_id, key) VALUES (?, ?)", (chat_id, key))
        self.db.commit()
        return cur.rowcount == 1

    def meta(self, key: str) -> str | None:
        r = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()
