"""Операции с делами и заметками, как их вызывает Claude, и тексты списков."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lifebot.storage import Storage
from lifebot.tools import ChatTools, complete, digest_text, roll_repeats, snooze, tasks_text

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 24, 14, 5, tzinfo=TZ)  # суббота


def tools(now=NOW):
    s = Storage(":memory:")
    return ChatTools(s.chat(1), s, now)


def local(dt):
    return dt.astimezone(TZ).strftime("%d.%m %H:%M")


def test_add_task_with_reminder():
    t = tools()
    result = t.add_task("Позвонить маме", "дела", "tomorrow", "9:30", "0", "none")
    assert "Записано: #1 Позвонить маме — завтра 09:30 🔔" in result and "напомню завтра в 09:30" in result
    task = t.s.task(1, 1)
    assert task.due_date == date(2026, 10, 25) and task.due_time == "09:30"
    assert local(task.remind_at) == "25.10 09:30"
    assert t.changed == [1]


def test_time_without_day():
    t = tools()
    t.add_task("Забрать посылку", "дела", "none", "18:00", "30", "none")
    t.add_task("Выключить духовку", "дела", "none", "13:00", "0", "none")  # уже прошло — завтра
    assert local(t.s.task(1, 1).remind_at) == "24.10 17:30"
    assert t.s.task(1, 2).due_date == date(2026, 10, 25)


def test_reminder_needs_time_and_late_reminder_fires_now():
    t = tools()
    with pytest.raises(ValueError, match="нужно время"):
        t.add_task("Купить подарок", "дела", "tomorrow", "none", "60", "none")
    # «Напомни за час», а до срока 25 минут — напомнить сразу.
    t.add_task("Выйти к врачу", "дела", "today", "14:30", "60", "none")
    assert t.s.task(1, 1).remind_at == NOW


def test_shopping_list_and_lists_text():
    t = tools()
    t.add_task("Молоко", "Покупки", "none", "none", "none", "none")
    t.add_task("Хлеб", "покупки", "none", "none", "none", "none")
    t.add_task("Отчёт", "дела", "today", "12:00", "none", "none")
    t.add_task("Стрижка", "дела", "27.10.2026", "18:00", "60", "none")
    text = tasks_text(t.s.open_tasks(1), NOW)
    assert text.index("⚠️ Просрочено\n#3 Отчёт — сегодня 12:00") < text.index("Позже\n#4 Стрижка — вт 27.10 18:00 🔔")
    assert "📝 Покупки\n#1 Молоко\n#2 Хлеб" in text


def test_update_task_keep_fields():
    t = tools()
    t.add_task("Стрижка", "дела", "27.10.2026", "18:00", "60", "none")
    t.update_task(1, "keep", "keep", "keep", "19:00", "keep", "keep")
    task = t.s.task(1, 1)
    assert task.due_date == date(2026, 10, 27) and task.due_time == "19:00" and local(task.remind_at) == "27.10 18:00"
    t.update_task(1, "Стрижка у Оли", "keep", "28.10.2026", "keep", "none", "keep")
    task = t.s.task(1, 1)
    assert task.title == "Стрижка у Оли" and task.due_date == date(2026, 10, 28) and task.remind_at is None
    with pytest.raises(ValueError, match="дела #7 нет"):
        t.update_task(7, "keep", "keep", "keep", "keep", "keep", "keep")


def test_complete_and_repeat():
    t = tools()
    t.add_task("Купить хлеб", "покупки", "none", "none", "none", "none")
    t.add_task("Таблетка", "дела", "today", "21:00", "0", "daily")
    assert "Выполнено" in t.complete_task(1)
    assert t.s.task(1, 1).done_at is not None and [x.id for x in t.s.open_tasks(1)] == [2]

    assert "Следующий раз — завтра 21:00" in t.complete_task(2)
    task = t.s.task(1, 2)
    assert task.done_at is None and local(task.remind_at) == "25.10 21:00"
    assert "Купить хлеб" in t.done_tasks(7)


def test_missed_repeat_rolls_to_today():
    t = tools(NOW - timedelta(days=3))
    t.add_task("Таблетка", "дела", "today", "21:00", "0", "daily")
    t.s.update_task(1, 1, remind_at=None)  # напомнили три дня назад, «готово» не нажали
    roll_repeats(t.s, t.chat, NOW)
    task = t.s.task(1, 1)
    assert task.due_date == NOW.date() and local(task.remind_at) == "24.10 21:00"


def test_snooze():
    t = tools()
    t.add_task("Позвонить в банк", "дела", "today", "14:00", "0", "none")
    task = t.s.task(1, 1)
    assert snooze(t.s, task, NOW, 60) == "Напомню в 15:05"
    assert local(t.s.task(1, 1).remind_at) == "24.10 15:05"
    assert snooze(t.s, task, NOW, None) == "Перенёс на завтра 14:00"
    assert local(t.s.task(1, 1).remind_at) == "25.10 14:00"


def test_complete_one_off_clears_reminder():
    t = tools()
    t.add_task("Позвонить", "дела", "today", "18:00", "0", "none")
    complete(t.s, t.s.task(1, 1), NOW)
    assert t.s.due_reminders(NOW + timedelta(days=1)) == []


def test_notes_search():
    t = tools()
    t.add_note("Код домофона: 45К1234")
    t.add_note("Размер обуви Маши — 31")
    t.add_note("Wi-Fi на даче: пароль sunflower")
    assert "45К1234" in t.search_notes("код домофона")
    assert "31" in t.search_notes("размер обуви") and "домофон" not in t.search_notes("размер обуви")
    assert "sunflower" in t.search_notes("пароль от вайфая")  # хотя бы одно слово
    assert "Ничего не нашлось. Всего заметок: 3" in t.search_notes("паспорт")
    t.update_note(2, "Размер обуви Маши — 32")
    assert "32" in t.search_notes("обуви")
    t.delete_note(1)
    assert "домофона" not in t.search_notes("")


def test_timezone_change_moves_reminders():
    t = tools()
    t.add_task("Созвон", "дела", "tomorrow", "10:00", "0", "none")
    t.update_settings("timezone", "Asia/Yekaterinburg")
    task = t.s.task(1, 1)
    assert task.remind_at.astimezone(ZoneInfo("Asia/Yekaterinburg")).strftime("%H:%M") == "10:00"
    with pytest.raises(ValueError, match="неизвестный часовой пояс"):
        t.update_settings("timezone", "Марс/Олимп")
    assert "07:45" in t.update_settings("digest_time", "7:45")
    assert t.s.chat(1).digest_time == "07:45"
    t.update_settings("digest_time", "off")
    assert t.s.chat(1).digest_time is None


def test_state_for_model():
    t = tools()
    t.add_task("Стрижка", "дела", "27.10.2026", "18:00", "60", "weekly")
    t.add_note("что-то")
    state = t.state()
    assert "Сейчас: суббота, 24.10.2026, 14:05 (Europe/Moscow)" in state
    assert "#1 [дела] Стрижка; срок 27.10.2026 (вт 27.10) 18:00; напомнить за 60 мин; " \
           "напоминание 27.10 17:00; повтор weekly" in state
    assert "Заметок: 1" in state


def test_digest():
    t = tools()
    assert digest_text(t.s.open_tasks(1), NOW) is None
    t.add_task("Молоко", "покупки", "none", "none", "none", "none")
    assert digest_text(t.s.open_tasks(1), NOW) is None  # без срока — не повод писать утром
    t.add_task("Отчёт", "дела", "23.10.2026", "none", "none", "none")
    t.add_task("Стрижка", "дела", "today", "18:00", "60", "none")
    text = digest_text(t.s.open_tasks(1), NOW)
    assert text.startswith("☀️ Доброе утро! Суббота, 24.10.")
    assert "📅 Сегодня:\n#3 Стрижка — сегодня 18:00 🔔" in text
    assert "⚠️ Не сделано раньше:\n#2 Отчёт — вчера" in text
