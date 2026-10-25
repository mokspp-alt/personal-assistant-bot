"""Google Календарь: дела с пометкой «в календарь» становятся событиями с оповещениями.

Доступ — через служебный аккаунт Google (JSON-ключ лежит в data/google-sa.json): человек
открывает свой календарь для адреса аккаунта с правом «Внесение изменений в мероприятия»,
и бот создаёт, меняет и удаляет события прямо в нём. На iPhone они приходят как обычные
события Google Календаря.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from google.auth import crypt, jwt

from .storage import Chat, Storage, Task

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar"
DEFAULT_DURATION = 60
RRULES = {
    "daily": "RRULE:FREQ=DAILY",
    "weekdays": "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "weekly": "RRULE:FREQ=WEEKLY",
    "monthly": "RRULE:FREQ=MONTHLY",
    "yearly": "RRULE:FREQ=YEARLY",
}


class CalendarError(Exception):
    pass


def is_service_account(data: dict) -> bool:
    return data.get("type") == "service_account" and bool(data.get("private_key")) \
        and bool(data.get("client_email"))


def key_path() -> Path:
    return Path(os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "data/google-sa.json"))


def save_key(data: dict, path: Path | None = None) -> Path:
    """Сохраняет JSON-ключ служебного аккаунта так, чтобы читал его только владелец файла."""
    path = path or key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)
    return path


def event_body(task: Task, tz: str) -> dict:
    body = {
        "summary": task.title,
        "description": "\n".join(x for x in [task.details or "", "Записано ассистентом в Telegram"] if x),
    }
    if task.location:
        body["location"] = task.location
    if task.due_time:
        start = task.due_at(None).replace(tzinfo=None)
        end = start + timedelta(minutes=task.duration or DEFAULT_DURATION)
        body["start"] = {"dateTime": start.isoformat(timespec="seconds"), "timeZone": tz}
        body["end"] = {"dateTime": end.isoformat(timespec="seconds"), "timeZone": tz}
    else:  # без времени — событие на весь день
        body["start"] = {"date": task.due_date.isoformat()}
        body["end"] = {"date": (task.due_date + timedelta(days=1)).isoformat()}
    minutes = task.remind_before if task.remind_before is not None else (
        DEFAULT_DURATION if task.due_time else None)
    body["reminders"] = {"useDefault": False, "overrides": (
        [{"method": "popup", "minutes": minutes}] if minutes is not None else [])}
    if task.repeat in RRULES:
        body["recurrence"] = [RRULES[task.repeat]]
    return body


class GoogleCalendar:
    def __init__(self, info: dict, proxy: str | None = None, transport=None) -> None:
        self.info = info
        self.email: str = info["client_email"]
        self.client = httpx.AsyncClient(proxy=proxy, transport=transport, timeout=30.0)
        self._token: str | None = None
        self._expires = 0.0

    @classmethod
    def from_env(cls, path: Path | None = None) -> GoogleCalendar | None:
        path = path or key_path()
        if not path.exists():
            return None
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("Не читается ключ Google %s", path)
            return None
        if not is_service_account(info):
            log.error("%s — не ключ служебного аккаунта Google", path)
            return None
        proxy = (os.environ.get("GOOGLE_PROXY") or os.environ.get("ANTHROPIC_PROXY")
                 or os.environ.get("TELEGRAM_PROXY") or None)
        return cls(info, proxy)

    async def token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        now = int(time.time())
        token_uri = self.info.get("token_uri", "https://oauth2.googleapis.com/token")
        signer = crypt.RSASigner.from_service_account_info(self.info)
        assertion = jwt.encode(signer, {"iss": self.email, "scope": SCOPE, "aud": token_uri,
                                        "iat": now, "exp": now + 3600})
        r = await self.client.post(token_uri, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion.decode() if isinstance(assertion, bytes) else assertion})
        if r.status_code != 200:
            raise CalendarError(f"Google не выдал доступ ({r.status_code}): {r.text[:200]}")
        data = r.json()
        self._token, self._expires = data["access_token"], now + int(data.get("expires_in", 3600))
        return self._token

    async def _request(self, method: str, calendar_id: str, path: str = "", body: dict | None = None):
        url = f"{API}/calendars/{quote(calendar_id, safe='')}{path}"
        r = await self.client.request(method, url, json=body,
                                      headers={"Authorization": f"Bearer {await self.token()}"})
        if method == "DELETE" and r.status_code in (404, 410):
            return None  # уже удалено руками
        if r.status_code in (403, 404):
            raise CalendarError(f"нет доступа к календарю {calendar_id}: откройте его для {self.email} "
                                "с правом «Внесение изменений в мероприятия»")
        if r.status_code >= 400:
            raise CalendarError(f"ошибка Google Календаря ({r.status_code}): {r.text[:200]}")
        return r.json() if r.content else None

    async def check(self, calendar_id: str) -> str:
        """Проверяет, что в календарь можно писать; возвращает его название."""
        data = await self._request("GET", calendar_id)
        return data.get("summary", calendar_id)

    async def insert(self, calendar_id: str, body: dict) -> str:
        return (await self._request("POST", calendar_id, "/events", body))["id"]

    async def update(self, calendar_id: str, event_id: str, body: dict) -> None:
        await self._request("PUT", calendar_id, f"/events/{quote(event_id, safe='')}", body)

    async def delete(self, calendar_id: str, event_id: str) -> None:
        await self._request("DELETE", calendar_id, f"/events/{quote(event_id, safe='')}")


async def sync(cal: GoogleCalendar | None, s: Storage, chat: Chat, task_ids: list[int],
               deleted_events: list[str]) -> list[str]:
    """Переносит изменения дел в календарь. Возвращает ошибки — их показывают человеку."""
    if cal is None or not chat.calendar_id:
        return []
    errors = []
    for event_id in deleted_events:
        try:
            await cal.delete(chat.calendar_id, event_id)
        except Exception as e:
            log.exception("Событие %s не удалено", event_id)
            errors.append(f"событие не удалилось: {e}")
    for task_id in task_ids:
        task = s.task(chat.chat_id, task_id)
        if task is None or not task.calendar or task.due_date is None:
            continue
        try:
            body = event_body(task, chat.tz)
            if task.event_id:
                await cal.update(chat.calendar_id, task.event_id, body)
            else:
                s.update_task(chat.chat_id, task.id, event_id=await cal.insert(chat.calendar_id, body))
        except Exception as e:
            log.exception("Дело #%s не попало в календарь", task_id)
            errors.append(f"«{task.title}» не попало в календарь: {e}")
    return errors
