"""Фото и файлы: преобразование в блоки для Claude и путь от фото квитанции до дела."""

import base64
import io
import os
from types import SimpleNamespace

import pytest
from conftest import Sent, fake_assistant, fake_context, fake_update, run, text, tool_use
from openpyxl import Workbook
from PIL import Image

from lifebot import attachments, bot
from lifebot.storage import Storage


def png(width, height):
    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, "PNG")
    return out.getvalue()


def test_small_image_kept_as_is():
    data = png(800, 600)
    block = attachments.to_block("фото", "image/png", data)
    assert block["type"] == "image" and block["source"]["media_type"] == "image/png"
    assert base64.b64decode(block["source"]["data"]) == data


def test_big_image_downscaled():
    block = attachments.to_block("скрин.png", "image/png", png(5000, 3000))
    assert block["source"]["media_type"] == "image/jpeg"
    with Image.open(io.BytesIO(base64.b64decode(block["source"]["data"]))) as img:
        assert max(img.size) == attachments.MAX_SIDE


def test_image_type_from_content():
    # Скриншот PNG, присланный с именем .jpg и типом image/jpeg: API отклонил бы несовпадение.
    block = attachments.to_block("Screenshot.jpg", "image/jpeg", png(1080, 2400))
    assert block["source"]["media_type"] == "image/png"
    # BMP API не понимает — пережимаем в JPEG.
    out = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(out, "BMP")
    block = attachments.to_block("скан.bmp", "image/bmp", out.getvalue())
    assert block["type"] == "image" and block["source"]["media_type"] == "image/jpeg"


def test_heavy_image_recompressed():
    noisy = Image.frombytes("RGB", (2000, 2000), os.urandom(2000 * 2000 * 3))
    out = io.BytesIO()
    noisy.save(out, "PNG")
    assert len(out.getvalue()) > attachments.MAX_IMAGE_BYTES
    block = attachments.to_block("тяжёлый.png", "image/png", out.getvalue())
    assert block["source"]["media_type"] == "image/jpeg"
    assert len(base64.b64decode(block["source"]["data"])) <= attachments.MAX_IMAGE_BYTES


def test_pdf_text_and_excel():
    pdf = attachments.to_block("счёт.pdf", "application/pdf", b"%PDF-1.4 ...")
    assert pdf["source"]["media_type"] == "application/pdf" and pdf["title"] == "счёт.pdf"

    txt = attachments.to_block("список.txt", None, "молоко\nхлеб".encode())
    assert txt["source"]["type"] == "text" and "хлеб" in txt["source"]["data"]

    book = Workbook()
    book.active.title = "Платежи"
    book.active.append(["Что", "До"])
    book.active.append(["Квартира", "10.11"])
    out = io.BytesIO()
    book.save(out)
    xlsx = attachments.to_block("платежи.xlsx", None, out.getvalue())
    assert "Платежи" in xlsx["source"]["data"] and "Квартира,10.11" in xlsx["source"]["data"]


def test_unsupported_and_broken_files():
    with pytest.raises(attachments.UnsupportedFile, match="Пока понимаю"):
        attachments.to_block("отчёт.docx", "application/msword", b"...")
    with pytest.raises(attachments.UnsupportedFile, match="картинку"):
        attachments.to_block("фото.jpg", "image/jpeg", b"not an image")


def test_photo_of_bill_to_task():
    s = Storage(":memory:")
    a = fake_assistant([
        ("tool_use", [tool_use("t1", "add_task", title="Оплатить электричество 2 340 ₽", list="дела",
                               due_date="10.11.2026", due_time="10:00", remind_minutes_before="0",
                               repeat="none")]),
        ("end_turn", [text("Квитанция за электричество: 2 340 ₽ до 10 ноября. Напомню 10.11 в 10:00.")]),
    ])

    async def get_file():
        async def download_as_bytearray():
            return bytearray(png(1000, 800))
        return SimpleNamespace(download_as_bytearray=download_as_bytearray)

    async def send_chat_action(*args):
        pass

    photo = SimpleNamespace(file_size=50_000, get_file=get_file)
    sent = Sent()
    update = fake_update(sent, message_extra=dict(photo=[photo], document=None, caption="напомни оплатить"))
    context = fake_context(s, a, bot=SimpleNamespace(send_chat_action=send_chat_action))
    run(bot.on_file(update, context))

    content = a.client.beta.messages.calls[0]["messages"][-1]["content"]
    assert content[0]["type"] == "image" and "напомни оплатить" in content[-1]["text"]
    assert "2 340 ₽ до 10 ноября" in sent.texts
    assert s.task(1, 1).title == "Оплатить электричество 2 340 ₽"
    # В истории — пометка о фото, без самой картинки.
    assert a.history[1][0] == {"role": "user", "content": "[фото] напомни оплатить"}
