"""Даты и время дел: разбор того, что присылает модель, повторы и человеческий вид сроков."""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
REPEATS = {
    "none": "",
    "daily": "каждый день",
    "weekdays": "по будням",
    "weekly": "каждую неделю",
    "monthly": "каждый месяц",
    "yearly": "каждый год",
}


def parse_hhmm(value: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*", value)
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_day(value: str, today: date) -> date | None:
    """today / tomorrow / ДД.ММ.ГГГГ / ДД.ММ (ближайшее такое число) / ГГГГ-ММ-ДД; none → None."""
    v = value.strip().lower()
    if v in ("", "none"):
        return None
    if v == "today":
        return today
    if v == "tomorrow":
        return today + timedelta(days=1)
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", v):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if m := re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", v):
            d, mo = int(m.group(1)), int(m.group(2))
            if m.group(3):
                return date(int(m.group(3)), mo, d)
            result = date(today.year, mo, d)
            return result if result >= today else date(today.year + 1, mo, d)
    except ValueError:
        raise ValueError(f"нет такой даты «{value}»") from None
    raise ValueError(f"не понимаю дату «{value}», нужно today, tomorrow или ДД.ММ.ГГГГ")


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year, month = d.year + month // 12, month % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def next_date(d: date, repeat: str) -> date:
    """Следующий раз повторяющегося дела после дня d."""
    if repeat == "daily":
        return d + timedelta(days=1)
    if repeat == "weekdays":
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d
    if repeat == "weekly":
        return d + timedelta(days=7)
    if repeat == "monthly":
        return _add_months(d, 1)
    if repeat == "yearly":
        return _add_months(d, 12)
    raise ValueError(f"дело не повторяется: {repeat}")


def advance(d: date, repeat: str, not_before: date) -> date:
    """Первый раз повторяющегося дела после d, но не раньше not_before."""
    d = next_date(d, repeat)
    while d < not_before:
        d = next_date(d, repeat)
    return d


def human_day(d: date, today: date) -> str:
    delta = (d - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if delta == -1:
        return "вчера"
    if 1 < delta < 7:
        return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"
    return f"{d:%d.%m}" if d.year == today.year else f"{d:%d.%m.%Y}"


def human_now(now: datetime) -> str:
    return f"{WEEKDAYS_FULL[now.weekday()]}, {now:%d.%m.%Y}, {now:%H:%M}"
