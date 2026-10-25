"""Подставные Claude и Telegram для тестов: заранее заданные ответы модели и запись отправленного."""

import asyncio
from types import SimpleNamespace

from lifebot.assistant import Assistant


def tool_use(id, name, **inp):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=inp)


def text(t):
    return SimpleNamespace(type="text", text=t)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})  # снимок на момент вызова
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        stop, content = item
        return SimpleNamespace(stop_reason=stop, content=content)


def fake_assistant(responses, model="claude-haiku-5-5"):
    a = Assistant.__new__(Assistant)
    a.history = {}
    a.model, a.effort = model, "medium"
    a.client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(responses)))
    return a


class Sent(list):
    """Отправленные ботом сообщения: (текст, клавиатура)."""

    @property
    def texts(self):
        return "\n".join(t for t, _ in self)


def fake_update(sent, chat_id=1, user_id=100, message_extra=None):
    async def reply_text(t, reply_markup=None):
        sent.append((t, reply_markup))

    message = SimpleNamespace(reply_text=reply_text, **(message_extra or {}))
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id, type="private"),
                           effective_user=SimpleNamespace(id=user_id, full_name="Тест"),
                           effective_message=message)


def fake_context(storage, assistant=None, args=None, bot=None):
    return SimpleNamespace(args=args or [], bot=bot,
                           application=SimpleNamespace(bot_data={"storage": storage, "assistant": assistant}))


def run(coro):
    return asyncio.run(coro)
