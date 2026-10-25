"""Сквозной диалог без Telegram: Claude, кнопки, доступ, напоминания и утренняя сводка."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import Sent, fake_assistant, fake_context, fake_update, run, text, tool_use
from telegram.ext import ApplicationHandlerStop

from lifebot import bot
from lifebot.storage import Storage

TZ = ZoneInfo("Europe/Moscow")


def say(s, assistant, msg, voice=False):
    sent = Sent()
    run(bot.handle_text(fake_update(sent), fake_context(s, assistant), msg, voice=voice))
    return sent


def test_free_text_adds_task_via_claude():
    s = Storage(":memory:")
    a = fake_assistant([
        ("tool_use", [tool_use("t1", "add_task", title="Позвонить маме", list="дела", due_date="tomorrow",
                               due_time="09:30", remind_minutes_before="0", repeat="none")]),
        ("end_turn", [text("Записал, напомню завтра в 9:30.")]),
    ])
    sent = say(s, a, "напомни завтра в полдесятого позвонить маме", voice=True)
    [(reply, markup)] = sent
    assert reply == "🎤 «напомни завтра в полдесятого позвонить маме»\n\nЗаписал, напомню завтра в 9:30."
    assert [b.callback_data for b in markup.inline_keyboard[0]] == ["done:1", "del:1"]
    assert s.task(1, 1).title == "Позвонить маме"

    first, second = a.client.beta.messages.calls
    assert first["model"] == "claude-haiku-5-5" and first["output_config"] == {"effort": "medium"}
    assert "fallbacks" not in first  # у Haiku нет запасной модели
    assert "Открытых дел нет" in first["messages"][-1]["content"][-1]["text"]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert all(t["strict"] for t in first["tools"])
    result = second["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "t1" and "Записано: #1 Позвонить маме — завтра 09:30" in result["content"]
    assert a.history[1] == [
        {"role": "user", "content": "напомни завтра в полдесятого позвонить маме"},
        {"role": "assistant", "content": "Записал, напомню завтра в 9:30."},
    ]

    # Следующее сообщение: история с меткой кэша, в состоянии — записанное дело.
    a.client.beta.messages.responses = [("end_turn", [text("Завтра в 9:30 — позвонить маме.")])]
    say(s, a, "что у меня завтра?")
    third = a.client.beta.messages.calls[2]
    assert third["messages"][1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "#1 [дела] Позвонить маме" in third["messages"][-1]["content"][-1]["text"]


def test_tool_error_goes_back_to_model():
    s = Storage(":memory:")
    a = fake_assistant([
        ("tool_use", [tool_use("t1", "complete_task", task_id=42)]),
        ("end_turn", [text("Не нашёл такого дела.")]),
    ])
    sent = say(s, a, "сделал")
    result = a.client.beta.messages.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "дела #42 нет" in result["content"]
    assert sent.texts == "Не нашёл такого дела."


def test_several_tasks_no_inline_keyboard():
    s = Storage(":memory:")
    a = fake_assistant([
        ("tool_use", [tool_use(f"t{i}", "add_task", title=x, list="покупки", due_date="none",
                               due_time="none", remind_minutes_before="none", repeat="none")
                      for i, x in enumerate(["Молоко", "Хлеб"])]),
        ("end_turn", [text("Добавил в покупки молоко и хлеб.")]),
    ])
    [(_, markup)] = say(s, a, "купить молоко и хлеб")
    assert markup is bot.KEYBOARD
    assert [t.title for t in s.open_tasks(1)] == ["Молоко", "Хлеб"]


def test_claude_down():
    s = Storage(":memory:")
    a = fake_assistant([RuntimeError("timeout")])
    assert "Claude сейчас недоступен" in say(s, a, "купить хлеб").texts
    assert s.open_tasks(1) == []


def test_without_claude_text_becomes_task():
    s = Storage(":memory:")
    sent = say(s, None, "Купить батарейки")
    assert sent.texts == "Записал: #1 Купить батарейки"
    assert say(s, None, bot.BTN_ALL).texts == "📝 Дела\n#1 Купить батарейки"


def test_buttons():
    s = Storage(":memory:")
    now = datetime.now(TZ)
    s.add_task(1, "Позвонить в банк", due_date=now.date(), due_time="23:59", remind_before=0)
    edited = []

    def press(data):
        async def answer(*a):
            pass

        async def edit_message_text(t):
            edited.append(t)

        query = SimpleNamespace(data=data, answer=answer, edit_message_text=edit_message_text,
                                message=SimpleNamespace(text="⏰ Позвонить в банк"))
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=1))
        run(bot.on_button(update, fake_context(s)))

    press("snz:1:15")
    assert edited[-1].startswith("⏰ Позвонить в банк\n\n⏰ Напомню в ")
    assert s.task(1, 1).remind_at > datetime.now(timezone.utc) + timedelta(minutes=14)
    press("done:1")
    assert edited[-1].endswith("✅ Выполнено: #1 Позвонить в банк")
    assert s.open_tasks(1) == []


def test_guard_owner_is_first_user(monkeypatch):
    monkeypatch.delenv("ALLOWED_USERS", raising=False)
    s = Storage(":memory:")
    run(bot.guard(fake_update(Sent(), user_id=100), fake_context(s)))
    assert s.meta("owner") == "100"
    run(bot.guard(fake_update(Sent(), user_id=100), fake_context(s)))
    stranger = Sent()
    with pytest.raises(ApplicationHandlerStop):
        run(bot.guard(fake_update(stranger, user_id=200), fake_context(s)))
    assert "Это личный бот" in stranger.texts and "200" in stranger.texts

    monkeypatch.setenv("ALLOWED_USERS", "200, 300")
    run(bot.guard(fake_update(Sent(), user_id=300), fake_context(s)))
    with pytest.raises(ApplicationHandlerStop):
        run(bot.guard(fake_update(Sent(), user_id=100), fake_context(s)))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))


def test_tick_reminders_and_digest():
    s = Storage(":memory:")
    now = datetime.now(TZ)
    # Сводка «только что» и одно дело на сегодня, напоминание по которому уже пора отправить.
    s.update_chat(1, digest_time=now.strftime("%H:%M"))
    due = now + timedelta(minutes=30)
    s.add_task(1, "Выйти к врачу", due_date=due.date(), due_time=due.strftime("%H:%M"), remind_before=60,
               remind_at=now - timedelta(minutes=1))
    fake = FakeBot()
    context = fake_context(s, bot=fake)
    run(bot.tick(context))
    reminder, digest = fake.sent
    assert reminder[1].startswith("⏰ Выйти к врачу\nСрок: ") and "(через 30 мин)" in reminder[1]
    assert [b.callback_data for b in reminder[2].inline_keyboard[1]] == ["snz:1:15", "snz:1:60", "snz:1:tmr"]
    assert "Выйти к врачу" in digest[1]
    assert s.task(1, 1).remind_at is None

    run(bot.tick(context))  # второй раз ни напоминания, ни сводки
    assert len(fake.sent) == 2


def test_split_long_text():
    parts = bot.split_text("\n".join(f"строка {i}" for i in range(2000)), limit=1000)
    assert all(len(p) <= 1000 for p in parts) and "\n".join(parts).count("строка") == 2000
    assert bot.split_text("") == [""]
