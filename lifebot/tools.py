"""Операции над делами и заметками (их вызывает Claude и кнопки) и тексты списков, сводки, напоминаний."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .dates import REPEATS, advance, human_day, human_now, parse_day, parse_hhmm
from .storage import DEFAULT_LIST, Chat, Storage, Task

MAX_STATE_TASKS = 150


# --- тексты ------------------------------------------------------------------------------


def when_text(task: Task, today: date) -> str:
    if not task.due_date:
        return ""
    s = human_day(task.due_date, today)
    return f"{s} {task.due_time}" if task.due_time else s


def task_line(task: Task, today: date, show_list: bool = False) -> str:
    parts = [task.title]
    if when := when_text(task, today):
        parts.append(f"— {when}")
    if task.remind_at:
        parts.append("🔔")
    if task.repeat != "none":
        parts.append(f"🔁 {REPEATS[task.repeat]}")
    if show_list and task.list != DEFAULT_LIST:
        parts.append(f"[{task.list}]")
    return f"#{task.id} " + " ".join(parts)


def tasks_text(tasks: list[Task], now: datetime) -> str:
    """Все открытые дела: по срокам, а без срока — по спискам."""
    if not tasks:
        return "Дел нет 🎉 Напишите или скажите, что нужно сделать."
    today = now.date()
    groups: dict[str, list[str]] = {}

    def add(title: str, line: str) -> None:
        groups.setdefault(title, []).append(line)

    for t in tasks:
        due = t.due_at(now.tzinfo)
        if t.due_date is None:
            add(f"📝 {t.list.capitalize()}", task_line(t, today))
        elif t.due_date < today or (due is not None and due < now):
            add("⚠️ Просрочено", task_line(t, today, show_list=True))
        elif t.due_date == today:
            add("📅 Сегодня", task_line(t, today, show_list=True))
        elif t.due_date == today + timedelta(days=1):
            add("Завтра", task_line(t, today, show_list=True))
        else:
            add("Позже", task_line(t, today, show_list=True))
    order = ["⚠️ Просрочено", "📅 Сегодня", "Завтра", "Позже"]
    titles = [t for t in order if t in groups] + sorted(t for t in groups if t not in order)
    return "\n\n".join(f"{title}\n" + "\n".join(groups[title]) for title in titles)


def digest_text(tasks: list[Task], now: datetime) -> str | None:
    """Утренняя сводка: просроченное и дела на сегодня; None — сводка не нужна."""
    today = now.date()
    overdue = [t for t in tasks if t.due_date and t.due_date < today]
    todays = [t for t in tasks if t.due_date == today]
    if not (overdue or todays):
        return None
    lines = [f"☀️ Доброе утро! {human_now(now).split(',')[0].capitalize()}, {now:%d.%m}."]
    if todays:
        lines += ["", "📅 Сегодня:"] + [task_line(t, today, show_list=True) for t in todays]
    if overdue:
        lines += ["", "⚠️ Не сделано раньше:"] + [task_line(t, today, show_list=True) for t in overdue]
    return "\n".join(lines)


def reminder_text(task: Task, now: datetime) -> str:
    text = f"⏰ {task.title}"
    if task.due_time and task.due_date:
        due = task.due_at(now.tzinfo)
        text += f"\nСрок: {when_text(task, now.date())}"
        if due > now:
            minutes = math.ceil((due - now).total_seconds() / 60)
            text += f" (через {minutes // 60} ч {minutes % 60} мин)" if minutes >= 60 else f" (через {minutes} мин)"
    if task.repeat != "none":
        text += f"\n🔁 {REPEATS[task.repeat]}"
    return text


# --- операции ----------------------------------------------------------------------------


def schedule_reminder(task: Task, now: datetime) -> datetime | None:
    """Время ближайшего напоминания: по сроку; если оно уже прошло, а срок ещё нет, — сразу."""
    planned = task.planned_reminder(now.tzinfo)
    if planned is None:
        return None
    if planned >= now:
        return planned
    return now if task.due_at(now.tzinfo) >= now else None


def roll_repeats(s: Storage, chat: Chat, now: datetime) -> None:
    """Пропущенные повторы (вчерашняя таблетка не отмечена) переносит на ближайший раз."""
    today = now.date()
    for t in s.open_tasks(chat.chat_id):
        if t.repeat != "none" and t.due_date and t.due_date < today:
            reschedule(s, t, advance(t.due_date, t.repeat, today), now)


def reschedule(s: Storage, task: Task, day: date, now: datetime) -> Task:
    s.update_task(task.chat_id, task.id, due_date=day)
    task = s.task(task.chat_id, task.id)
    s.update_task(task.chat_id, task.id, remind_at=schedule_reminder(task, now))
    return s.task(task.chat_id, task.id)


def complete(s: Storage, task: Task, now: datetime) -> str:
    """Отметить выполненным; повторяющееся переходит на следующий раз."""
    if task.repeat == "none":
        s.update_task(task.chat_id, task.id, done_at=now, remind_at=None)
        return f"Выполнено: #{task.id} {task.title}"
    start = task.due_date or now.date()
    task = reschedule(s, task, advance(start, task.repeat, now.date()), now)
    return f"Отмечено: #{task.id} {task.title}. Следующий раз — {when_text(task, now.date())}"


def snooze(s: Storage, task: Task, now: datetime, minutes: int | None) -> str:
    """Напомнить позже: через minutes минут; None — перенести на завтра."""
    if minutes is not None:
        at = now + timedelta(minutes=minutes)
        s.update_task(task.chat_id, task.id, remind_at=at)
        return f"Напомню в {at:%H:%M}"
    if task.remind_before is None and task.due_time:
        s.update_task(task.chat_id, task.id, remind_before=0)
    task = reschedule(s, s.task(task.chat_id, task.id), now.date() + timedelta(days=1), now)
    return f"Перенёс на {when_text(task, now.date())}"


class ChatTools:
    """Инструменты Claude: каждый метод соответствует инструменту в assistant.TOOLS."""

    def __init__(self, chat: Chat, s: Storage, now: datetime) -> None:
        self.chat, self.s, self.now = chat, s, now
        self.changed: list[int] = []  # дела, созданные или изменённые в этом ответе

    @property
    def tz(self):
        return self.now.tzinfo

    def _task(self, task_id: int) -> Task:
        task = self.s.task(self.chat.chat_id, task_id)
        if task is None:
            raise ValueError(f"дела #{task_id} нет")
        return task

    def _touch(self, task_id: int) -> None:
        if task_id in self.changed:
            self.changed.remove(task_id)
        self.changed.append(task_id)

    def _when(self, due_date: str, due_time: str) -> tuple[date | None, str | None]:
        day = parse_day(due_date, self.now.date())
        if due_time.strip().lower() in ("", "none"):
            return day, None
        hm = parse_hhmm(due_time)
        if hm is None:
            raise ValueError(f"не понимаю время «{due_time}», нужно ЧЧ:ММ")
        time = f"{hm[0]:02d}:{hm[1]:02d}"
        if day is None:  # «в 18:00» без дня — сегодня, а если уже прошло — завтра
            day = self.now.date()
            if (hm[0], hm[1]) <= (self.now.hour, self.now.minute):
                day += timedelta(days=1)
        return day, time

    @staticmethod
    def _remind(value: str) -> int | None:
        v = value.strip().lower()
        if v in ("", "none"):
            return None
        try:
            minutes = int(v)
        except ValueError:
            raise ValueError(f"напоминание — число минут до срока или none, а не «{value}»") from None
        if not 0 <= minutes <= 60 * 24 * 30:
            raise ValueError("напоминать можно от 0 минут до 30 дней до срока")
        return minutes

    def _apply_schedule(self, task_id: int) -> Task:
        task = self._task(task_id)
        if task.remind_before is not None and not task.due_time:
            raise ValueError("для напоминания нужно время срока (due_time)")
        self.s.update_task(self.chat.chat_id, task_id, remind_at=schedule_reminder(task, self.now))
        return self._task(task_id)

    def _describe(self, task: Task) -> str:
        line = task_line(task, self.now.date(), show_list=True)
        if task.remind_at:
            line += f"; напомню {human_day(task.remind_at.astimezone(self.tz).date(), self.now.date())} " \
                    f"в {task.remind_at.astimezone(self.tz):%H:%M}"
        elif task.remind_before is not None:
            line += "; срок уже прошёл, напоминания не будет"
        return line

    def add_task(self, title: str, list: str, due_date: str, due_time: str,
                 remind_minutes_before: str, repeat: str) -> str:
        if not title.strip():
            raise ValueError("пустое название дела")
        day, time = self._when(due_date, due_time)
        remind = self._remind(remind_minutes_before)
        if remind is not None and time is None:
            raise ValueError("для напоминания нужно время срока (due_time)")
        if repeat != "none" and day is None:
            day = self.now.date()
        task_id = self.s.add_task(self.chat.chat_id, title.strip(), list.strip().lower() or DEFAULT_LIST,
                                  day, time, remind, repeat=repeat)
        self._touch(task_id)
        return "Записано: " + self._describe(self._apply_schedule(task_id))

    def update_task(self, task_id: int, title: str, list: str, due_date: str, due_time: str,
                    remind_minutes_before: str, repeat: str) -> str:
        task = self._task(task_id)
        fields: dict = {}
        if title != "keep":
            fields["title"] = title.strip()
        if list != "keep":
            fields["list"] = list.strip().lower() or DEFAULT_LIST
        if due_date != "keep" or due_time != "keep":
            day, time = self._when(
                due_date if due_date != "keep" else (task.due_date.strftime("%d.%m.%Y") if task.due_date else "none"),
                due_time if due_time != "keep" else (task.due_time or "none"))
            fields["due_date"], fields["due_time"] = day, time
        if remind_minutes_before != "keep":
            fields["remind_before"] = self._remind(remind_minutes_before)
        if repeat != "keep":
            fields["repeat"] = repeat
            if repeat != "none" and not fields.get("due_date", task.due_date):
                fields["due_date"] = self.now.date()
        self.s.update_task(self.chat.chat_id, task_id, **fields)
        self._touch(task_id)
        return "Изменено: " + self._describe(self._apply_schedule(task_id))

    def complete_task(self, task_id: int) -> str:
        return complete(self.s, self._task(task_id), self.now)

    def delete_task(self, task_id: int) -> str:
        task = self._task(task_id)
        self.s.delete_task(self.chat.chat_id, task_id)
        if task_id in self.changed:
            self.changed.remove(task_id)
        return f"Удалено: #{task_id} {task.title}"

    def done_tasks(self, days: int) -> str:
        done = self.s.done_tasks(self.chat.chat_id, self.now - timedelta(days=max(1, min(days, 365))))
        if not done:
            return "За этот срок выполненных дел нет."
        return "\n".join(f"{t.done_at.astimezone(self.tz):%d.%m %H:%M} — #{t.id} {t.title}" for t in done)

    def add_note(self, text: str) -> str:
        if not text.strip():
            raise ValueError("пустая заметка")
        note_id = self.s.add_note(self.chat.chat_id, text.strip())
        return f"Заметка #{note_id} сохранена."

    def update_note(self, note_id: int, text: str) -> str:
        if self.s.note(self.chat.chat_id, note_id) is None:
            raise ValueError(f"заметки #{note_id} нет")
        self.s.update_note(self.chat.chat_id, note_id, text.strip())
        return f"Заметка #{note_id} изменена."

    def search_notes(self, query: str) -> str:
        """Заметки, где есть все слова запроса (по началу слова); пустой запрос — последние 30."""
        notes = self.s.notes(self.chat.chat_id)
        words = [w[:5] for w in query.lower().split() if len(w) > 1]
        if words:
            found = [n for n in notes if all(w in n.text.lower() for w in words)]
            if not found:  # хотя бы одно слово
                found = [n for n in notes if any(w in n.text.lower() for w in words)]
        else:
            found = notes[-30:]
        if not found:
            return f"Ничего не нашлось. Всего заметок: {len(notes)}."
        return "\n".join(f"#{n.id} ({n.created_at.astimezone(self.tz):%d.%m.%Y}) {n.text}" for n in found[-30:])

    def delete_note(self, note_id: int) -> str:
        if self.s.note(self.chat.chat_id, note_id) is None:
            raise ValueError(f"заметки #{note_id} нет")
        self.s.delete_note(self.chat.chat_id, note_id)
        return f"Заметка #{note_id} удалена."

    def update_settings(self, setting: str, value: str) -> str:
        v = value.strip()
        if setting == "timezone":
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"неизвестный часовой пояс «{v}»") from None
            self.s.update_chat(self.chat.chat_id, tz=v)
            self.chat = self.s.chat(self.chat.chat_id)
            self.now = self.now.astimezone(ZoneInfo(v))
            for t in self.s.open_tasks(self.chat.chat_id):  # сроки — местное время, напоминания пересчитать
                if t.remind_at:
                    self.s.update_task(t.chat_id, t.id, remind_at=schedule_reminder(t, self.now))
            return f"Часовой пояс: {v}, сейчас {self.now:%H:%M}."
        if setting == "digest_time":
            if v.lower() == "off":
                self.s.update_chat(self.chat.chat_id, digest_time=None)
                return "Утренняя сводка выключена."
            hm = parse_hhmm(v)
            if hm is None:
                raise ValueError("время сводки — ЧЧ:ММ или off")
            self.s.update_chat(self.chat.chat_id, digest_time=f"{hm[0]:02d}:{hm[1]:02d}")
            return f"Утренняя сводка в {hm[0]:02d}:{hm[1]:02d}."
        raise ValueError(f"неизвестная настройка {setting}")

    def state(self) -> str:
        """Описание для модели: текущее время, настройки, открытые дела."""
        today = self.now.date()
        tasks = self.s.open_tasks(self.chat.chat_id)
        lines = [
            f"Сейчас: {human_now(self.now)} ({self.chat.tz}).",
            f"Утренняя сводка: {self.chat.digest_time or 'выключена'}.",
            f"Заметок: {len(self.s.notes(self.chat.chat_id))} (содержимое — через search_notes).",
        ]
        if not tasks:
            lines.append("Открытых дел нет.")
            return "\n".join(lines)
        lists = sorted({t.list for t in tasks})
        lines += [f"Списки: {', '.join(lists)}.", f"Открытые дела ({len(tasks)}):"]
        for t in tasks[:MAX_STATE_TASKS]:
            parts = [f"#{t.id} [{t.list}] {t.title}"]
            if t.due_date:
                parts.append(f"срок {t.due_date:%d.%m.%Y} ({human_day(t.due_date, today)})"
                             + (f" {t.due_time}" if t.due_time else ""))
            if t.remind_before is not None:
                parts.append(f"напомнить за {t.remind_before} мин")
            if t.remind_at:
                parts.append(f"напоминание {t.remind_at.astimezone(self.tz):%d.%m %H:%M}")
            if t.repeat != "none":
                parts.append(f"повтор {t.repeat}")
            lines.append("; ".join(parts))
        if len(tasks) > MAX_STATE_TASKS:
            lines.append(f"…и ещё {len(tasks) - MAX_STATE_TASKS}")
        return "\n".join(lines)
