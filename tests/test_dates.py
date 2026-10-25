from datetime import date

import pytest

from lifebot.dates import advance, human_day, next_date, parse_day, parse_hhmm

TODAY = date(2026, 10, 24)  # суббота


@pytest.mark.parametrize("value, expected", [
    ("today", TODAY),
    ("tomorrow", date(2026, 10, 25)),
    ("none", None),
    ("31.10.2026", date(2026, 10, 31)),
    ("31.10", date(2026, 10, 31)),
    ("01.01", date(2027, 1, 1)),  # ближайшее такое число
    ("2026-12-05", date(2026, 12, 5)),
])
def test_parse_day(value, expected):
    assert parse_day(value, TODAY) == expected


def test_parse_day_errors():
    with pytest.raises(ValueError, match="нет такой даты"):
        parse_day("31.02.2027", TODAY)
    with pytest.raises(ValueError, match="не понимаю дату"):
        parse_day("в пятницу", TODAY)


def test_parse_hhmm():
    assert parse_hhmm("9:05") == (9, 5)
    assert parse_hhmm("18.30") == (18, 30)
    assert parse_hhmm("25:00") is None


def test_repeats():
    assert next_date(TODAY, "daily") == date(2026, 10, 25)
    assert next_date(date(2026, 10, 23), "weekdays") == date(2026, 10, 26)  # пятница → понедельник
    assert next_date(TODAY, "weekly") == date(2026, 10, 31)
    assert next_date(date(2026, 1, 31), "monthly") == date(2026, 2, 28)
    assert next_date(date(2026, 12, 15), "monthly") == date(2027, 1, 15)
    assert next_date(date(2028, 2, 29), "yearly") == date(2029, 2, 28)
    with pytest.raises(ValueError):
        next_date(TODAY, "none")


def test_advance_skips_missed():
    assert advance(date(2026, 10, 20), "daily", TODAY) == TODAY
    assert advance(TODAY, "daily", TODAY) == date(2026, 10, 25)
    assert advance(date(2026, 10, 1), "weekly", TODAY) == date(2026, 10, 29)


def test_human_day():
    assert human_day(TODAY, TODAY) == "сегодня"
    assert human_day(date(2026, 10, 25), TODAY) == "завтра"
    assert human_day(date(2026, 10, 28), TODAY) == "ср 28.10"
    assert human_day(date(2026, 11, 28), TODAY) == "28.11"
    assert human_day(date(2027, 1, 2), TODAY) == "02.01.2027"
