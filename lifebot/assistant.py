"""Свободный диалог через Claude: человек пишет как угодно, модель отвечает и, когда нужно,
меняет дела и заметки через инструменты (tools.ChatTools).

Без ключа бот всё равно работает: текст записывается делом без срока, списки — командами.
"""

from __future__ import annotations

import json
import logging
import os

import anthropic

log = logging.getLogger(__name__)

MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
HISTORY_TURNS = 10  # сколько последних обменов репликами помнить


SYSTEM = """\
Ты — личный помощник в Telegram-боте: ведёшь дела, напоминания, списки и заметки одного человека.
Он пишет или говорит голосом (текст — расшифровка речи, в ней бывают ошибки и лишние слова).

Разговаривай свободно: отвечай на любые вопросы, помогай планировать, раскладывать большое дело
на шаги, вспоминать, что было записано. Данные бота меняй только инструментами и только когда
человек этого хочет или явно сообщает новое дело.

Дела (add_task):
- «Надо», «не забыть», «напомни», «купить …», «записать к врачу» — это дело. Название короткое,
  в повелительной форме или как пункт списка: «Позвонить маме», «Молоко».
- Сроки переводи сам по блоку «Состояние» (там текущие дата, день недели и время):
  «завтра» → tomorrow, «в пятницу» → ближайшая пятница ДД.ММ.ГГГГ, «через 20 минут» → сегодняшняя
  дата и время ЧЧ:ММ. Нет срока — due_date и due_time "none". Не выдумывай срок, которого не было.
- Напоминание (remind_minutes_before) ставь, если просят напомнить или у дела есть точное время:
  "0" — в момент срока, "30" — за полчаса, "1440" — за день. Без времени напомнить нельзя: дело
  без времени и так придёт в утренней сводке своего дня; если человеку важно время — спроси.
  Встреча или визит со временем — по умолчанию напомни за 60 минут, если не сказано иначе.
- Покупки — список "покупки", каждая вещь отдельным делом («купи молоко и хлеб» → два дела).
  Другие списки заводи, только если человек сам их называет («в список для отпуска …»);
  иначе список "дела".
- Повторы: «каждый день», «по будням», «каждую неделю/месяц/год» → repeat. День рождения —
  yearly с датой.
- Несколько дел в одном сообщении — несколько вызовов.

Google Календарь (calendar в add_task). Если в «Состоянии» он подключён, ставь calendar=true
для событий с конкретным днём или временем, которые человек проживает, а не выполняет: встречи,
визиты к врачу, поездки, поезда и самолёты, встретить или забрать кого-то, праздники, дни рождения,
мероприятия; и всегда, когда просят «в календарь». Для обычных дел и покупок — false. Длительность
(duration_minutes) — по смыслу: визит — 60, встреча с поезда — 60, поездка — по билету (от
отправления до прибытия); без времени — 0. В location — адрес, вокзал или аэропорт; в details —
всё полезное: номер поезда или рейса, вагон, место, терминал, кто едет, кто прислал. Если
календарь не подключён, всё равно записывай дело, а календарь не обещай.

Билеты и расписания (часто — скриншот, пересланный от близких):
- Встретить или забрать кого-то → дело «Встретить …» на время прибытия, место — станция или
  аэропорт прибытия, напоминание за 90 минут (на дорогу), если не сказано иначе.
- Едет или летит сам человек → дело на время отправления, напоминание за 3 часа.
- Год в билете часто не указан: бери ближайшую будущую дату.

Изменения: «сделал», «купил», «готово» → complete_task (по номеру #id из «Состояния»);
«перенеси», «не в 5, а в 6», «напомни за час» → update_task; «удали», «не надо» → delete_task.
Если непонятно, о каком деле речь, — переспроси, а не угадывай.

Заметки — сведения, которые надо запомнить, а не сделать: «запомни, что …», «запиши: размер
обуви 38», номера, адреса, идеи, пароли от wifi. Вопрос «какой у меня …», «что я записывал про …»
— сначала search_notes. Ничего не нашлось — так и скажи, не придумывай.

Что сделано за период — done_tasks. Часовой пояс и время утренней сводки — update_settings.

Фото и файлы (квитанции, письма, скриншоты, билеты, приглашения, документы): кратко перескажи
главное и ответь на вопрос из подписи. Если там есть срок или действие (оплатить до, прийти
к 10:00, вылет 12.11 в 7:40) — предложи записать дело с напоминанием; если в подписи об этом
просят — сразу запиши. Не выдумывай то, что не видно или не читается.

Если просят то, чего бот не умеет (календарь Google, почта, звонки), честно скажи об этом.

Стиль: по-русски, коротко и по делу, 1–3 предложения; на сложный вопрос можно подробнее.
Без markdown: Telegram покажет звёздочки как есть. Время пиши как 8:20. После изменений коротко
подтверди, что сделал и когда напомнишь, — номера #id называть не нужно.
"""

# Кэш запросов (prompt caching). Инструкции и инструменты не меняются — держим их час.
# Историю и хвост запроса — 5 минут: они пригодятся, если сообщения идут подряд, и внутри
# одного ответа, где бот вызывает инструменты и делает несколько запросов подряд.
CACHED_SYSTEM = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]


def _with_history_breakpoint(history: list[dict]) -> list[dict]:
    """Копия истории с меткой кэша на последней реплике; сама история не меняется."""
    if not history:
        return []
    last = history[-1]
    marked = {"role": last["role"], "content": [
        {"type": "text", "text": last["content"], "cache_control": {"type": "ephemeral"}}]}
    return history[:-1] + [marked]


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {"type": "object", "properties": properties,
                         "required": list(properties), "additionalProperties": False},
    }


_REPEAT = ["none", "daily", "weekdays", "weekly", "monthly", "yearly"]
_TASK_FIELDS = {
    "title": {"type": "string", "description": "Коротко, что сделать"},
    "list": {"type": "string", "description": "Список: дела, покупки или названный человеком"},
    "due_date": {"type": "string", "description": "Срок: today, tomorrow, ДД.ММ.ГГГГ или none"},
    "due_time": {"type": "string", "description": "Время срока ЧЧ:ММ или none"},
    "remind_minutes_before": {"type": "string",
                              "description": "За сколько минут до срока напомнить (\"0\" — в срок) или none"},
}
_EVENT_FIELDS = {
    "duration_minutes": {"type": "integer", "description": "Длительность события в календаре, минут; 0 — нет"},
    "location": {"type": "string", "description": "Место события: адрес, вокзал, аэропорт; none — нет"},
    "details": {"type": "string", "description": "Подробности для события: поезд, вагон, место; none — нет"},
}


def _keep(field: dict) -> dict:
    return {**field, "description": field["description"] + "; keep — не менять"}


TOOLS = [
    _tool("add_task", "Записать новое дело (или пункт списка покупок).", {
        **_TASK_FIELDS,
        "repeat": {"type": "string", "enum": _REPEAT, "description": "Повтор дела"},
        "calendar": {"type": "boolean", "description": "Добавить событием в Google Календарь"},
        **_EVENT_FIELDS,
    }),
    _tool("update_task", "Изменить дело по номеру #id: название, список, срок, напоминание, повтор.", {
        "task_id": {"type": "integer"},
        **{k: _keep(v) for k, v in _TASK_FIELDS.items()},
        "repeat": {"type": "string", "enum": ["keep", *_REPEAT], "description": "Повтор или keep"},
        "calendar": {"type": "string", "enum": ["keep", "yes", "no"],
                     "description": "Событие в Google Календаре: yes — добавить, no — убрать"},
        "duration_minutes": {"type": "integer", "description": "Длительность события, минут; -1 — не менять"},
        "location": _keep(_EVENT_FIELDS["location"]),
        "details": _keep(_EVENT_FIELDS["details"]),
    }),
    _tool("complete_task", "Отметить дело выполненным; повторяющееся перейдёт на следующий раз.", {
        "task_id": {"type": "integer"},
    }),
    _tool("delete_task", "Удалить дело, которое больше не нужно или записано по ошибке.", {
        "task_id": {"type": "integer"},
    }),
    _tool("done_tasks", "Выполненные дела за последние N дней.", {
        "days": {"type": "integer"},
    }),
    _tool("add_note", "Сохранить заметку — сведения, которые нужно помнить.", {
        "text": {"type": "string", "description": "Текст заметки целиком, понятный без контекста"},
    }),
    _tool("update_note", "Заменить текст заметки по номеру #id (например, дополнить).", {
        "note_id": {"type": "integer"},
        "text": {"type": "string"},
    }),
    _tool("search_notes", "Найти заметки по словам; пустая строка — последние заметки.", {
        "query": {"type": "string"},
    }),
    _tool("delete_note", "Удалить заметку по номеру #id.", {
        "note_id": {"type": "integer"},
    }),
    _tool("update_settings", "Изменить настройку бота.", {
        "setting": {"type": "string", "enum": ["timezone", "digest_time"]},
        "value": {"type": "string", "description":
                  "timezone — например Europe/Moscow; digest_time — ЧЧ:ММ утренней сводки или off"},
    }),
]
TOOL_NAMES = {t["name"] for t in TOOLS}


class Assistant:
    def __init__(self, api_key: str, proxy: str | None = None, model: str = MODEL,
                 effort: str = EFFORT) -> None:
        self.model, self.effort = model, effort
        http_client = anthropic.DefaultAsyncHttpxClient(proxy=proxy) if proxy else None
        self.client = anthropic.AsyncAnthropic(api_key=api_key, http_client=http_client,
                                               timeout=90.0)
        self.history: dict[int, list[dict]] = {}

    @classmethod
    def from_env(cls) -> Assistant | None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        proxy = os.environ.get("ANTHROPIC_PROXY") or os.environ.get("TELEGRAM_PROXY") or None
        return cls(key, proxy)

    async def handle(self, chat_id: int, text: str, tools, attachments: list[dict] | None = None,
                     label: str = "") -> str:
        """tools — tools.ChatTools: методы-инструменты и state().
        attachments — блоки фото и файлов (attachments.to_block), label — их название для истории."""
        history = self.history.setdefault(chat_id, [])
        prompt = f"<Состояние>\n{tools.state()}\n</Состояние>\n\nСообщение: {text}"
        content = [*(attachments or []), {"type": "text", "text": prompt}]
        messages = _with_history_breakpoint(history) + [{"role": "user", "content": content}]
        extra = {}
        if not self.model.startswith("claude-haiku"):
            # Если модель откажется отвечать, API сам повторит запрос на другой. У Haiku такого нет.
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        for _ in range(12):
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                system=CACHED_SYSTEM,
                tools=TOOLS,
                messages=messages,
                cache_control={"type": "ephemeral"},  # хвост: переиспользуется в цикле инструментов
                output_config={"effort": self.effort},
                **extra,
            )
            if response.stop_reason == "refusal":
                raise RuntimeError("Claude отказался отвечать")
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                break
            messages.append({"role": "user", "content": [
                self._run_tool(block, tools) for block in response.content if block.type == "tool_use"
            ]})

        reply = "\n\n".join(b.text.strip() for b in response.content if b.type == "text").strip()
        reply = reply or "Готово."
        # В памяти держим только текст реплик, без служебного состояния и инструментов.
        # Сами файлы в историю не попадают (дорого), только пометка — найденное есть в ответе.
        said = f"[{label}] {text}".strip() if label else text
        history += [{"role": "user", "content": said}, {"role": "assistant", "content": reply}]
        del history[:-HISTORY_TURNS * 2]
        return reply

    @staticmethod
    def _run_tool(block, tools) -> dict:
        try:
            args = block.input if isinstance(block.input, dict) else json.loads(block.input)
            if block.name not in TOOL_NAMES:
                raise ValueError(f"неизвестный инструмент {block.name}")
            result = getattr(tools, block.name)(**args)
            return {"type": "tool_result", "tool_use_id": block.id, "content": str(result)}
        except Exception as e:  # ошибку отдаём модели — она объяснит или переспросит
            return {"type": "tool_result", "tool_use_id": block.id,
                    "content": f"Ошибка: {e}", "is_error": True}
