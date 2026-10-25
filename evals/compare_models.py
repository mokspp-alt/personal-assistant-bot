"""Сравнение моделей Claude на задачах бота: понимает ли модель фразу и правильно ли меняет данные.

Каждый случай — реальная фраза, исходные дела и заметки, время «сейчас». Эталон — вызовы
инструментов, которые сделал бы идеальный помощник; правильность проверяется по итоговому
состоянию базы (сроки, напоминания, повторы, списки, выполненные дела, заметки, настройки),
а не по тексту вызовов. Названия дел у моделей разные, поэтому они сверяются регулярками,
как и ответы на вопросы. Тексты ответов всех моделей пишутся в файл для просмотра.

Запуск на сервере (ключ и прокси берутся из .env):
    .venv/bin/python evals/compare_models.py
    .venv/bin/python evals/compare_models.py claude-haiku-5-5:medium claude-sonnet-5-5:low
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lifebot.assistant import Assistant
from lifebot.storage import Storage
from lifebot.tools import ChatTools

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 24, 14, 5, tzinfo=TZ)  # суббота
DEFAULT_CONFIGS = ["claude-haiku-5-5:low", "claude-haiku-5-5:medium", "claude-sonnet-5-5:low"]

# Цены за 1 млн токенов, $: ввод, вывод, чтение кэша, запись кэша 5 мин, запись кэша 1 ч.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50, 4.00),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00, 8.00),
}


def task(title: str, list: str = "дела", due_date: str = "none", due_time: str = "none",
         remind: str = "none", repeat: str = "none") -> tuple[str, dict]:
    return ("add_task", dict(title=title, list=list, due_date=due_date, due_time=due_time,
                             remind_minutes_before=remind, repeat=repeat))


@dataclass
class Case:
    id: str
    message: str
    gold: list[tuple[str, dict]] = field(default_factory=list)  # эталонные вызовы инструментов
    titles: list[str] = field(default_factory=list)  # регулярки: должны быть среди открытых дел
    reply: list[str] = field(default_factory=list)  # регулярки, которые должны быть в ответе
    setup: list[tuple[str, dict]] = field(default_factory=list)  # действия до сообщения (дела #1, #2…)
    notes: list[str] = field(default_factory=list)  # регулярки: должны быть среди заметок
    now: datetime = NOW


HAIRCUT = task("Стрижка", due_date="30.10.2026", due_time="18:00", remind="60")
SHOPPING = [task("Молоко", "покупки"), task("Хлеб", "покупки")]
DOOR_CODE = ("add_note", dict(text="Код домофона: 45К1234"))

CASES = [
    # --- новые дела ---
    Case("напомни завтра", "Напомни завтра в 9:30 позвонить маме",
         [task("Позвонить маме", due_date="tomorrow", due_time="09:30", remind="0")], [r"мам"]),
    Case("покупки", "купить молоко и хлеб", SHOPPING, [r"молок", r"хлеб"]),
    Case("день недели", "в пятницу в 18:00 стрижка", [HAIRCUT], [r"стриж"]),
    Case("через минуты", "через 20 минут вытащить бельё из стиралки",
         [task("Вытащить бельё", due_date="today", due_time="14:25", remind="0")], [r"бель"]),
    Case("через час", "напомни через час позвонить в банк",
         [task("Позвонить в банк", due_date="today", due_time="15:05", remind="0")], [r"банк"]),
    Case("без срока", "надо записаться к стоматологу", [task("Записаться к стоматологу")],
         [r"стоматолог"]),
    Case("каждый день", "каждый день в 21:00 пить таблетку",
         [task("Выпить таблетку", due_date="today", due_time="21:00", remind="0", repeat="daily")],
         [r"таблет"]),
    Case("день рождения", "12 марта день рождения у Оли",
         [task("День рождения Оли", due_date="12.03.2027", repeat="yearly")], [r"Ол"]),
    Case("распознавание речи", "напомни в понедельник в восемь утра вынести мусор",
         [task("Вынести мусор", due_date="26.10.2026", due_time="08:00", remind="0")], [r"мусор"]),
    # --- изменения ---
    Case("купил", "молоко купил", [("complete_task", dict(task_id=1))], [r"хлеб"], setup=SHOPPING),
    Case("перенеси", "перенеси стрижку на субботу",
         [("update_task", dict(task_id=1, title="keep", list="keep", due_date="31.10.2026",
                               due_time="keep", remind_minutes_before="keep", repeat="keep"))],
         [r"стриж"], setup=[HAIRCUT]),
    Case("отменили", "стрижку отменили", [("delete_task", dict(task_id=1))], setup=[HAIRCUT]),
    Case("напомни раньше", "по стрижке напомни за два часа",
         [("update_task", dict(task_id=1, title="keep", list="keep", due_date="keep",
                               due_time="keep", remind_minutes_before="120", repeat="keep"))],
         [r"стриж"], setup=[HAIRCUT]),
    # --- заметки и вопросы ---
    Case("запомни", "запомни: код домофона 45К1234", [DOOR_CODE], notes=[r"45К1234"]),
    Case("вспомни", "какой у нас код домофона?", [], reply=[r"45К1234"], setup=[DOOR_CODE],
         notes=[r"45К1234"]),
    Case("на сегодня", "что у меня на сегодня?", [], [r"бель", r"стриж"], reply=[r"бель"],
         setup=[task("Вытащить бельё", due_date="today", due_time="16:00", remind="0"), HAIRCUT]),
    Case("сводка", "утреннюю сводку присылай в 7:45",
         [("update_settings", dict(setting="digest_time", value="07:45"))]),
    Case("болтовня", "привет! как дела?"),
]


def fresh(case: Case) -> tuple[Storage, ChatTools]:
    s = Storage(":memory:")
    tools = ChatTools(s.chat(1), s, case.now)
    for name, args in case.setup:
        getattr(tools, name)(**args)
    return s, ChatTools(s.chat(1), s, case.now)


def snapshot(s: Storage) -> tuple:
    tasks = sorted((t.list, str(t.due_date), t.due_time, t.remind_before, t.repeat)
                   for t in s.open_tasks(1))
    return tuple(tasks), len(s.notes(1)), s.chat(1).tz, s.chat(1).digest_time


def expected(case: Case) -> tuple:
    s, tools = fresh(case)
    for name, args in case.gold:
        getattr(tools, name)(**args)
    return snapshot(s)


def cost(model: str, usage) -> float:
    p_in, p_out, p_read, p_w5, p_w1h = PRICES.get(model, PRICES["claude-opus-5-5"])
    creation = getattr(usage, "cache_creation", None)
    w1h = getattr(creation, "ephemeral_1h_input_tokens", 0) or 0 if creation else 0
    w5 = (getattr(usage, "cache_creation_input_tokens", 0) or 0) - w1h
    return ((usage.input_tokens or 0) * p_in + (usage.output_tokens or 0) * p_out
            + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p_read
            + w5 * p_w5 + w1h * p_w1h) / 1_000_000


@dataclass
class Result:
    case: Case
    ok: bool
    why: str
    reply: str
    cost: float
    seconds: float


async def run_case(assistant: Assistant, case: Case) -> Result:
    """assistant — свой на каждый случай: подменяем его create, чтобы посчитать стоимость."""
    s, tools = fresh(case)
    spent = []
    create = assistant.client.beta.messages.create

    async def counting_create(**kw):
        response = await create(**kw)
        spent.append(cost(kw["model"], response.usage))
        return response

    assistant.client.beta.messages.create = counting_create
    started = time.monotonic()
    try:
        reply = await assistant.handle(1, case.message, tools)
    except Exception as e:
        return Result(case, False, f"ошибка: {e!r}", "", sum(spent), time.monotonic() - started)
    seconds = time.monotonic() - started

    got, want = snapshot(s), expected(case)
    problems = []
    if got[0] != want[0]:
        problems.append(f"дела {list(got[0])} вместо {list(want[0])}")
    if got[1] != want[1]:
        problems.append(f"заметок {got[1]} вместо {want[1]}")
    if got[2:] != want[2:]:
        problems.append(f"настройки {got[2:]} вместо {want[2:]}")
    titles = [t.title for t in s.open_tasks(1)]
    for pattern in case.titles:
        if not any(re.search(pattern, t, re.IGNORECASE) for t in titles):
            problems.append(f"нет дела /{pattern}/ среди {titles}")
    notes = [n.text for n in s.notes(1)]
    for pattern in case.notes:
        if not any(re.search(pattern, n, re.IGNORECASE) for n in notes):
            problems.append(f"нет заметки /{pattern}/")
    for pattern in case.reply:
        if not re.search(pattern, reply, re.IGNORECASE):
            problems.append(f"в ответе нет /{pattern}/")
    return Result(case, not problems, "; ".join(problems), reply, sum(spent), seconds)


async def run_config(config: str, key: str, proxy: str | None) -> list[Result]:
    model, _, effort = config.partition(":")
    semaphore = asyncio.Semaphore(4)

    async def one(case: Case) -> Result:
        async with semaphore:
            return await run_case(Assistant(key, proxy, model=model, effort=effort or "medium"), case)

    # Первый случай отдельно: он записывает кэш инструкций, остальные его читают.
    first = await one(CASES[0])
    rest = await asyncio.gather(*(one(c) for c in CASES[1:]))
    return [first, *rest]


def load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


async def main() -> None:
    load_env(ROOT / ".env")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("Нет ANTHROPIC_API_KEY в .env")
    proxy = os.environ.get("ANTHROPIC_PROXY") or os.environ.get("TELEGRAM_PROXY") or None
    configs = sys.argv[1:] or DEFAULT_CONFIGS
    print(f"Случаев: {len(CASES)}, конфигураций: {len(configs)}. Это займёт пару минут.\n")

    all_results = {}
    for config in configs:
        all_results[config] = await run_config(config, key, proxy)
        results = all_results[config]
        passed = sum(r.ok for r in results)
        print(f"{config:26} верно {passed}/{len(results)}  "
              f"${sum(r.cost for r in results) / len(results):.4f} за сообщение  "
              f"{sum(r.seconds for r in results) / len(results):.1f} с")

    print("\nОшибки:")
    for config, results in all_results.items():
        for r in results:
            if not r.ok:
                print(f"- {config} / {r.case.id}: {r.why}")

    out = ROOT / "evals" / f"results-{datetime.now():%Y%m%d-%H%M}.txt"
    with out.open("w", encoding="utf-8") as f:
        for case_index, case in enumerate(CASES):
            f.write(f"\n=== {case.id}: «{case.message}»\n")
            for config, results in all_results.items():
                r = results[case_index]
                f.write(f"[{config}] {'OK' if r.ok else 'ОШИБКА: ' + r.why}\n{r.reply}\n")
    print(f"\nВсе ответы моделей: {out}")


if __name__ == "__main__":
    asyncio.run(main())
