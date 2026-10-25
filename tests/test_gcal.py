"""Google Календарь: тело события, запросы к API (через подставной HTTP), синхронизация дел,
подключение через бота — ключ файлом и /calendar."""

import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
import rsa
from conftest import Sent, fake_assistant, fake_context, fake_update, run, text, tool_use

from lifebot import bot, gcal
from lifebot.storage import Storage
from lifebot.tools import ChatTools

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 24, 14, 5, tzinfo=TZ)
_, PRIVATE = rsa.newkeys(1024)
KEY = {"type": "service_account", "client_email": "bot@assistant.iam.gserviceaccount.com",
       "private_key": PRIVATE.save_pkcs1().decode(), "private_key_id": "k1",
       "token_uri": "https://oauth2.googleapis.com/token"}


def tools_with_calendar():
    s = Storage(":memory:")
    s.update_chat(1, calendar_id="me@gmail.com")
    return ChatTools(s.chat(1), s, NOW, calendar_ready=True)


def test_event_body_for_train():
    t = tools_with_calendar()
    result = t.add_task("Встретить тёщу", "дела", "30.10.2026", "07:52", "90", "none", True, 60,
                        "Ленинградский вокзал", "Поезд 016А, вагон 5, место 12")
    assert "будет в Google Календаре" in result
    body = gcal.event_body(t.s.task(1, 1), "Europe/Moscow")
    assert body["summary"] == "Встретить тёщу" and body["location"] == "Ленинградский вокзал"
    assert body["description"].startswith("Поезд 016А, вагон 5, место 12")
    assert body["start"] == {"dateTime": "2026-10-30T07:52:00", "timeZone": "Europe/Moscow"}
    assert body["end"] == {"dateTime": "2026-10-30T08:52:00", "timeZone": "Europe/Moscow"}
    assert body["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 90}]}
    assert "recurrence" not in body


def test_event_body_all_day_birthday():
    t = tools_with_calendar()
    t.add_task("День рождения Оли", "дела", "12.03.2027", "none", "none", "yearly", True, 0, "none", "none")
    body = gcal.event_body(t.s.task(1, 1), "Europe/Moscow")
    assert body["start"] == {"date": "2027-03-12"} and body["end"] == {"date": "2027-03-13"}
    assert body["recurrence"] == ["RRULE:FREQ=YEARLY"] and body["reminders"]["overrides"] == []
    assert "location" not in body


def test_calendar_rules_in_tools():
    t = tools_with_calendar()
    with pytest.raises(ValueError, match="нужна дата"):
        t.add_task("Концерт", "дела", "none", "none", "none", "none", True, 120, "none", "none")
    t.add_task("Стрижка", "дела", "30.10.2026", "18:00", "60", "none", True, 60, "none", "none")
    t.s.update_task(1, 1, event_id="ev1")
    t.update_task(1, "keep", "keep", "keep", "keep", "keep", "keep", "no", -1, "keep", "keep")
    assert t.deleted_events == ["ev1"] and not t.s.task(1, 1).calendar and t.s.task(1, 1).event_id is None
    t.update_task(1, "keep", "keep", "keep", "keep", "keep", "keep", "yes", 45, "Салон на Ленина", "keep")
    task = t.s.task(1, 1)
    assert task.calendar and task.duration == 45 and task.location == "Салон на Ленина"
    assert "в календаре (45 мин, Салон на Ленина)" in t.state()
    t.s.update_task(1, 1, event_id="ev2")
    t.delete_task(1)
    assert t.deleted_events == ["ev1", "ev2"]

    plain = ChatTools(t.s.chat(2), t.s, NOW)  # календарь не подключён
    assert "не подключён — записано только в боте" in plain.add_task(
        "Врач", "дела", "tomorrow", "10:00", "60", "none", True, 60, "none", "none")
    assert "Google Календарь: не подключён" in plain.state()


class FakeGoogle:
    """Подставной Google: выдаёт токен и запоминает запросы к календарю."""

    def __init__(self, status=200):
        self.requests, self.status = [], status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            assert form["grant_type"] == "urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer"
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer tok"
        self.requests.append((request.method, request.url.raw_path.decode(),
                              json.loads(request.content) if request.content else None))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "x"})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"id": "ev42", "summary": "Дмитрий"})


def test_api_requests_and_sync():
    google = FakeGoogle()
    cal = gcal.GoogleCalendar(KEY, transport=httpx.MockTransport(google))
    assert run(cal.check("me@gmail.com")) == "Дмитрий"

    t = tools_with_calendar()
    t.add_task("Встретить тёщу", "дела", "30.10.2026", "07:52", "90", "none", True, 60, "Вокзал", "none")
    t.add_task("Молоко", "покупки", "none", "none", "none", "none", False, 0, "none", "none")
    assert run(gcal.sync(cal, t.s, t.s.chat(1), [1, 2], [])) == []
    assert t.s.task(1, 1).event_id == "ev42"
    t.update_task(1, "keep", "keep", "keep", "08:10", "keep", "keep", "keep", -1, "keep", "keep")
    t.delete_task(2)
    run(gcal.sync(cal, t.s, t.s.chat(1), [1], ["old"]))
    calls = [(m, p) for m, p, _ in google.requests]
    assert calls == [
        ("GET", "/calendar/v3/calendars/me%40gmail.com"),
        ("POST", "/calendar/v3/calendars/me%40gmail.com/events"),
        ("DELETE", "/calendar/v3/calendars/me%40gmail.com/events/old"),
        ("PUT", "/calendar/v3/calendars/me%40gmail.com/events/ev42"),
    ]
    assert google.requests[3][2]["start"]["dateTime"] == "2026-10-30T08:10:00"


def test_api_errors():
    cal = gcal.GoogleCalendar(KEY, transport=httpx.MockTransport(FakeGoogle(403)))
    with pytest.raises(gcal.CalendarError, match="откройте его для bot@assistant"):
        run(cal.check("me@gmail.com"))
    gone = gcal.GoogleCalendar(KEY, transport=httpx.MockTransport(FakeGoogle(410)))
    run(gone.delete("me@gmail.com", "gone"))  # уже удалено руками — не ошибка

    t = tools_with_calendar()
    t.add_task("Врач", "дела", "tomorrow", "10:00", "60", "none", True, 60, "none", "none")
    errors = run(gcal.sync(cal, t.s, t.s.chat(1), [1], []))
    assert len(errors) == 1 and errors[0].startswith("«Врач» не попало в календарь: нет доступа")


class FakeCalendar:
    email = "bot@assistant.iam.gserviceaccount.com"

    def __init__(self):
        self.calls = []

    async def check(self, calendar_id):
        self.calls.append(("check", calendar_id))
        return "Дмитрий"

    async def insert(self, calendar_id, body):
        self.calls.append(("insert", body["summary"]))
        return "ev1"

    async def update(self, calendar_id, event_id, body):
        self.calls.append(("update", event_id))

    async def delete(self, calendar_id, event_id):
        self.calls.append(("delete", event_id))


def test_screenshot_of_ticket_goes_to_calendar():
    s = Storage(":memory:")
    s.update_chat(1, calendar_id="me@gmail.com")
    cal = FakeCalendar()
    a = fake_assistant([
        ("tool_use", [tool_use("t1", "add_task", title="Встретить тёщу", list="дела", due_date="30.10.2026",
                               due_time="07:52", remind_minutes_before="90", repeat="none", calendar=True,
                               duration_minutes=60, location="Ленинградский вокзал",
                               details="Поезд 016А, вагон 5")]),
        ("end_turn", [text("Записал: встретить тёщу 30.10 в 7:52, напомню за полтора часа.")]),
    ])
    context = fake_context(s, a)
    context.application.bot_data["calendar"] = cal
    sent = Sent()
    run(bot.handle_text(fake_update(sent), context, "надо встретить маму, вот билет"))
    assert "Google Календарь: подключён" in a.client.beta.messages.calls[0]["messages"][-1]["content"][-1]["text"]
    assert cal.calls == [("insert", "Встретить тёщу")] and s.task(1, 1).event_id == "ev1"
    assert "⚠️" not in sent.texts

    # «Удалить» под сообщением удаляет и событие.
    async def answer(*a):
        pass

    async def edit_message_text(t):
        sent.append((t, None))

    query = SimpleNamespace(data="del:1", answer=answer, edit_message_text=edit_message_text,
                            message=SimpleNamespace(text="Встретить тёщу"))
    run(bot.on_button(SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=1)), context))
    assert cal.calls[-1] == ("delete", "ev1") and s.open_tasks(1) == []


def test_key_upload_and_calendar_command(tmp_path, monkeypatch):
    path = tmp_path / "google-sa.json"
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(path))
    s = Storage(":memory:")
    context = fake_context(s)
    sent, deleted = Sent(), []

    run(bot.calendar_cmd(fake_update(sent), context))
    assert "console.cloud.google.com" in sent.texts

    async def get_file():
        async def download_as_bytearray():
            return bytearray(json.dumps(KEY).encode())
        return SimpleNamespace(download_as_bytearray=download_as_bytearray)

    async def delete():
        deleted.append(True)

    async def send_message(t, reply_markup=None):
        sent.append((t, reply_markup))

    document = SimpleNamespace(file_name="assistant-4f1a.json", file_size=2300, get_file=get_file)
    update = fake_update(sent, message_extra=dict(document=document, photo=None, caption=None, delete=delete))
    update.effective_chat.send_message = send_message
    run(bot.on_file(update, context))
    assert deleted and json.loads(path.read_text())["client_email"] == KEY["client_email"]
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert "Адрес бота: bot@assistant.iam.gserviceaccount.com" in sent[-1][0]
    assert isinstance(context.application.bot_data["calendar"], gcal.GoogleCalendar)

    cal = FakeCalendar()
    context.application.bot_data["calendar"] = cal
    context.args = ["me@gmail.com"]
    run(bot.calendar_cmd(fake_update(sent), context))
    assert "✅ Google Календарь подключён: «Дмитрий»" in sent[-1][0] and s.chat(1).calendar_id == "me@gmail.com"
    context.args = ["off"]
    run(bot.calendar_cmd(fake_update(sent), context))
    assert s.chat(1).calendar_id is None


def test_old_database_migrates(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, tz TEXT NOT NULL DEFAULT 'Europe/Moscow',
                            digest_time TEXT DEFAULT '08:30');
        CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, title TEXT NOT NULL,
            list TEXT NOT NULL DEFAULT 'дела', due_date TEXT, due_time TEXT, remind_before INTEGER,
            remind_at TEXT, repeat TEXT NOT NULL DEFAULT 'none', done_at TEXT, created_at TEXT NOT NULL);
        INSERT INTO chats (chat_id) VALUES (1);
        INSERT INTO tasks (chat_id, title, created_at) VALUES (1, 'Старое дело', '2026-10-24T10:00:00+00:00');
    """)
    db.commit()
    db.close()
    s = Storage(str(path))
    assert s.chat(1).calendar_id is None
    task = s.task(1, 1)
    assert task.title == "Старое дело" and task.calendar is False and task.event_id is None
