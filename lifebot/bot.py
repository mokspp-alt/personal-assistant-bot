"""Telegram-бот: голос, текст, фото и файлы → дела, напоминания и заметки; утренняя сводка."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, TypeHandler, filters,
)

from . import attachments
from .assistant import Assistant
from .dates import parse_hhmm
from .storage import Chat, Storage, Task
from .stt import Transcriber
from .tools import (
    ChatTools, complete, digest_text, reminder_text, roll_repeats, snooze, task_line, tasks_text,
)

log = logging.getLogger(__name__)

BTN_ALL = "📋 Все дела"
BTN_TODAY = "📅 Сегодня"
BTN_NOTES = "🗒 Заметки"
KEYBOARD = ReplyKeyboardMarkup([[BTN_ALL, BTN_TODAY], [BTN_NOTES]], resize_keyboard=True)
MAX_MESSAGE = 4000  # Telegram режет сообщения длиннее 4096 символов
DIGEST_WINDOW = timedelta(hours=3)  # сводку, пропущенную дольше, уже не шлём

HELP = (
    "Пишите или говорите голосом как обычно:\n"
    "• «напомни завтра в 9:30 позвонить маме»\n"
    "• «купить молоко, хлеб и батарейки»\n"
    "• «в пятницу в 18:00 стрижка», «каждый месяц 10-го платить за квартиру»\n"
    "• «запомни: размер обуви Маши 31», «какой у нас код домофона?»\n"
    "• «что у меня на сегодня?», «перенеси стрижку на субботу», «молоко купил»\n"
    "Можно прислать фото или файл — квитанцию, письмо, билет: разберу и запишу срок.\n\n"
    "Каждое утро пришлю сводку дел на день. Команды:\n"
    "/list — все дела\n"
    "/today — дела на сегодня\n"
    "/notes — заметки\n"
    "/done 12 — отметить дело #12 выполненным, /del 12 — удалить\n"
    "/digest 08:30 — время утренней сводки (off — не присылать)\n"
    "/tz Europe/Moscow — часовой пояс"
)


def store(context: ContextTypes.DEFAULT_TYPE) -> Storage:
    return context.application.bot_data["storage"]


def tz_of(chat: Chat):
    try:
        return chat.zone
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Europe/Moscow")


def now_for(chat: Chat) -> datetime:
    return datetime.now(tz_of(chat))


def split_text(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Режет длинный текст по строкам на куски, которые Telegram примет."""
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        if current and len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    return parts + [current] if current or not parts else parts


async def reply(update: Update, text: str, markup=None) -> None:
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await update.effective_message.reply_text(chunk, reply_markup=(markup or KEYBOARD) if last else None)


def task_keyboard(task: Task) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Готово", callback_data=f"done:{task.id}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{task.id}"),
    ]])


def reminder_keyboard(task: Task) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Готово", callback_data=f"done:{task.id}")],
        [InlineKeyboardButton("⏰ 15 мин", callback_data=f"snz:{task.id}:15"),
         InlineKeyboardButton("⏰ 1 час", callback_data=f"snz:{task.id}:60"),
         InlineKeyboardButton("🌅 Завтра", callback_data=f"snz:{task.id}:tmr")],
    ])


# --- доступ -------------------------------------------------------------------------------


def allowed_users() -> set[int]:
    raw = os.environ.get("ALLOWED_USERS", "")
    return {int(x) for x in raw.replace(",", " ").split() if x.strip().lstrip("-").isdigit()}


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Личный бот: отвечает только владельцу. Без ALLOWED_USERS владелец — первый, кто написал."""
    user = update.effective_user
    if user is None:
        raise ApplicationHandlerStop
    allowed = allowed_users()
    if not allowed:
        s = store(context)
        owner = s.meta("owner")
        if owner is None:
            s.set_meta("owner", str(user.id))
            log.info("Владелец бота: %s (%s)", user.id, user.full_name)
            return
        allowed = {int(owner)}
    if user.id in allowed:
        return
    log.info("Чужой пользователь %s (%s) — не отвечаю", user.id, user.full_name)
    if update.effective_chat and update.effective_chat.type == "private" and update.effective_message:
        await update.effective_message.reply_text(
            f"Это личный бот. Если он ваш, добавьте свой id {user.id} в ALLOWED_USERS в .env на сервере.")
    raise ApplicationHandlerStop


# --- команды ------------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store(context).chat(update.effective_chat.id)
    note = "" if context.application.bot_data.get("assistant") else (
        "\n\n⚠️ Claude не подключён (нет ANTHROPIC_API_KEY): записываю дела без сроков и напоминаний.")
    await reply(update, "Привет! Я ваш помощник по личным делам.\n\n" + HELP + note)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, HELP)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    await reply(update, tasks_text(s.open_tasks(chat.chat_id), now_for(chat)))


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    now = now_for(chat)
    today = now.date()
    tasks = [t for t in s.open_tasks(chat.chat_id) if t.due_date and t.due_date <= today]
    if not tasks:
        await reply(update, "На сегодня дел со сроком нет. Все дела — /list")
        return
    await reply(update, tasks_text(tasks, now))


async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    notes = s.notes(chat.chat_id)
    if not notes:
        await reply(update, "Заметок нет. Скажите «запомни, что …» — сохраню.")
        return
    await reply(update, "🗒 Заметки:\n\n" + "\n".join(f"#{n.id} {n.text}" for n in notes[-50:]))


async def _task_arg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Task | None:
    s = store(context)
    arg = (context.args or [""])[0].lstrip("#")
    task = s.task(update.effective_chat.id, int(arg)) if arg.isdigit() else None
    if task is None:
        await reply(update, "Укажите номер дела из /list, например: /done 12")
    return task


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if task := await _task_arg(update, context):
        chat = store(context).chat(update.effective_chat.id)
        await reply(update, "✅ " + complete(store(context), task, now_for(chat)))


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if task := await _task_arg(update, context):
        store(context).delete_task(task.chat_id, task.id)
        await reply(update, f"🗑 Удалено: {task.title}")


async def tz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    if not context.args:
        await reply(update, f"Часовой пояс: {chat.tz}. Сменить: /tz Europe/Moscow")
        return
    tools = ChatTools(chat, s, now_for(chat))
    try:
        await reply(update, tools.update_settings("timezone", context.args[0]))
    except ValueError as e:
        await reply(update, f"Не получилось: {e}. Пример: /tz Asia/Yekaterinburg")


async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    if not context.args:
        await reply(update, f"Утренняя сводка: {chat.digest_time or 'выключена'}. "
                            "Изменить: /digest 08:30 или /digest off")
        return
    tools = ChatTools(chat, s, now_for(chat))
    try:
        await reply(update, tools.update_settings("digest_time", context.args[0]))
    except ValueError as e:
        await reply(update, f"Не получилось: {e}")


# --- сообщения ----------------------------------------------------------------------------


async def handle_with_assistant(update: Update, context: ContextTypes.DEFAULT_TYPE, chat: Chat,
                                text: str, heard: str, files: list[dict] | None = None,
                                label: str = "") -> bool:
    """True, если сообщение обработал Claude; False — Claude недоступен и ничего не изменено."""
    assistant: Assistant | None = context.application.bot_data.get("assistant")
    if assistant is None:
        return False
    s = store(context)
    tools = ChatTools(chat, s, now_for(chat))
    try:
        reply_text = await assistant.handle(chat.chat_id, text, tools, files, label)
    except Exception:
        if not tools.changed:
            log.exception("Claude недоступен")
            return False
        log.exception("Claude оборвался после изменений")
        reply_text = "Изменения внесены, но ответ не получился. Проверьте /list."
    tasks = [t for t in (s.task(chat.chat_id, i) for i in tools.changed) if t and t.done_at is None]
    markup = task_keyboard(tasks[0]) if len(tasks) == 1 else None
    await reply(update, heard + reply_text, markup)
    return True


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                      voice: bool = False) -> None:
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    heard = f"🎤 «{text}»\n\n" if voice else ""
    if text == BTN_ALL:
        await list_cmd(update, context)
        return
    if text == BTN_TODAY:
        await today_cmd(update, context)
        return
    if text == BTN_NOTES:
        await notes_cmd(update, context)
        return
    if await handle_with_assistant(update, context, chat, text, heard):
        return
    if context.application.bot_data.get("assistant") is not None:
        await reply(update, heard + "Claude сейчас недоступен 😕 Попробуйте через минуту — "
                                    "или посмотрите дела: /list")
        return
    # Без Claude: каждое сообщение — дело без срока.
    task_id = s.add_task(chat.chat_id, text.strip())
    task = s.task(chat.chat_id, task_id)
    await reply(update, heard + f"Записал: {task_line(task, now_for(chat).date())}", task_keyboard(task))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_text(update, context, update.effective_message.text or "")


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    media = message.voice or message.audio
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "voice.ogg")
        try:
            file = await media.get_file()
            await file.download_to_drive(path)
            text = await context.application.bot_data["stt"].transcribe(path)
        except Exception:
            log.exception("Не удалось распознать голосовое")
            await message.reply_text("Не получилось распознать голосовое 😕 Напишите текстом.")
            return
    if not text:
        await message.reply_text("Не расслышал. Повторите, пожалуйста.")
        return
    await handle_text(update, context, text, voice=True)


MAX_FILE_BYTES = 20 * 1024 * 1024  # больше Telegram боту не отдаёт


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фото и документы: Claude извлекает данные, пересказывает и записывает сроки."""
    message = update.effective_message
    chat = store(context).chat(update.effective_chat.id)
    if context.application.bot_data.get("assistant") is None:
        await message.reply_text("Чтобы разбирать фото и файлы, боту нужен Claude (ANTHROPIC_API_KEY).")
        return
    if message.photo:
        media, name, mime = message.photo[-1], "фото", "image/jpeg"
    else:
        media = message.document
        name, mime = media.file_name or "файл", media.mime_type
    if (media.file_size or 0) > MAX_FILE_BYTES:
        await message.reply_text("Файл больше 20 МБ — Telegram не отдаёт боту такие. Пришлите поменьше.")
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        data = bytes(await (await media.get_file()).download_as_bytearray())
        block = attachments.to_block(name, mime, data)
    except attachments.UnsupportedFile as e:
        await message.reply_text(str(e))
        return
    except Exception:
        log.exception("Не удалось скачать файл")
        await message.reply_text("Не получилось скачать файл. Попробуйте ещё раз.")
        return
    text = message.caption or "Посмотри, что здесь, и помоги разобраться."
    if not await handle_with_assistant(update, context, chat, text, "", [block], name):
        await message.reply_text("Не получилось разобрать файл: Claude сейчас недоступен. "
                                 "Попробуйте чуть позже.")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    action, task_id, *arg = query.data.split(":")
    task = s.task(chat.chat_id, int(task_id))
    if task is None:
        await query.answer("Этого дела уже нет")
        await query.edit_message_reply_markup(None)
        return
    now = now_for(chat)
    if action == "done":
        result = "✅ " + complete(s, task, now)
    elif action == "del":
        s.delete_task(chat.chat_id, task.id)
        result = f"🗑 Удалено: {task.title}"
    elif action == "snz":
        result = "⏰ " + snooze(s, task, now, None if arg[0] == "tmr" else int(arg[0]))
    else:
        await query.answer()
        return
    await query.answer()
    await query.edit_message_text(f"{query.message.text}\n\n{result}")


# --- напоминания и сводка -----------------------------------------------------------------


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store(context)
    now_utc = datetime.now(timezone.utc)
    for task in s.due_reminders(now_utc):
        chat = s.chat(task.chat_id)
        try:
            await context.bot.send_message(task.chat_id, reminder_text(task, now_utc.astimezone(tz_of(chat))),
                                           reply_markup=reminder_keyboard(task))
        except NetworkError:
            log.warning("Нет связи с Telegram, напоминание #%s отправлю позже", task.id)
            return
        except Exception:
            log.exception("Напоминание #%s не отправлено", task.id)
        s.update_task(task.chat_id, task.id, remind_at=None)

    for chat in s.chats():
        try:
            now = now_utc.astimezone(tz_of(chat))
            roll_repeats(s, chat, now)
            if not chat.digest_time:
                continue
            h, m = parse_hhmm(chat.digest_time)
            at = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if not at <= now < at + DIGEST_WINDOW or not s.mark_sent(chat.chat_id, f"digest:{now.date()}"):
                continue
            if text := digest_text(s.open_tasks(chat.chat_id), now):
                await context.bot.send_message(chat.chat_id, text, reply_markup=KEYBOARD)
        except Exception:
            log.exception("Сводка для чата %s не отправлена", chat.chat_id)


async def preload_model(app: Application) -> None:
    """Загружаем Whisper сразу при старте, чтобы первое голосовое не ждало."""
    app.create_task(asyncio.to_thread(app.bot_data["stt"].load))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ["BOT_TOKEN"]
    builder = Application.builder().token(token).concurrent_updates(True).post_init(preload_model)
    # Если api.telegram.org с сервера недоступен — через прокси: http://… или socks5://user:pass@host:port
    if proxy := os.environ.get("TELEGRAM_PROXY"):
        builder = builder.proxy(proxy).get_updates_proxy(proxy)
    app = builder.build()
    app.bot_data["storage"] = Storage(os.environ.get("DB_PATH", "data/assistant.db"))
    app.bot_data["stt"] = Transcriber()
    app.bot_data["assistant"] = Assistant.from_env()
    log.info("Claude: %s", "включён" if app.bot_data["assistant"] else "выключен (нет ANTHROPIC_API_KEY)")

    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("notes", notes_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("tz", tz_cmd))
    app.add_handler(CommandHandler("digest", digest_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(tick, interval=60, first=5)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
