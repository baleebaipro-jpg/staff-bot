"""Пересылка фото/видео с ценами из темы фотографа в группы Silver и Bronza.

Фотограф скидывает в тему альбом и подпись, например:
    silver 45.000 bronza 55.000
Бот ждёт, пока придёт весь альбом, находит цены и отправляет альбом
в группу Silver с ценой silver и в группу Bronza с ценой bronza.
"""
import asyncio
import logging
import re

from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import ContextTypes

import config

SOURCE_CHAT_ID = getattr(config, "RELAY_SOURCE_CHAT_ID", None)
SOURCE_THREAD_ID = getattr(config, "RELAY_SOURCE_THREAD_ID", None)
TARGETS = getattr(config, "RELAY_TARGETS", {})

# Как может быть написано название в подписи
KEYWORDS = {
    "silver": ["silver", "силвер", "сильвер", "серебро", "kumush"],
    "bronza": ["bronza", "bronze", "bronz", "бронза", "бронз"],
}
WAIT_SECONDS = 3  # сколько ждать остальные фото альбома

_albums: dict = {}


def enabled() -> bool:
    return bool(SOURCE_CHAT_ID) and any(t.get("chat_id") for t in TARGETS.values())


def parse_prices(text: str) -> dict:
    """'silver 45.000 bronza 55.000' -> {'silver': '45.000', 'bronza': '55.000'}"""
    prices = {}
    for target, words in KEYWORDS.items():
        alt = "|".join(re.escape(w) for w in words)
        m = re.search(rf"(?:{alt})\w*[\s:=\-–—]*(\d[\d\s.,']*\d|\d)", text, re.IGNORECASE)
        if m:
            prices[target] = re.sub(r"\s+", " ", m.group(1)).strip()
    return prices


def _is_source(update: Update) -> bool:
    msg = update.effective_message
    if not msg or msg.chat_id != SOURCE_CHAT_ID:
        return False
    if SOURCE_THREAD_ID and msg.message_thread_id != SOURCE_THREAD_ID:
        return False
    return True


async def collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Принимает каждое фото/видео из темы фотографа."""
    if not enabled() or not _is_source(update):
        return
    msg = update.effective_message
    if msg.photo:
        item = ("photo", msg.photo[-1].file_id)
    elif msg.video:
        item = ("video", msg.video.file_id)
    else:
        return
    key = msg.media_group_id or f"single-{msg.message_id}"
    album = _albums.setdefault(key, {"items": [], "captions": [], "first": msg, "task": None})
    album["items"].append((msg.message_id, item))
    if msg.caption:
        album["captions"].append(msg.caption)
    if album["task"]:
        album["task"].cancel()
    album["task"] = asyncio.create_task(_flush_later(key, context))


async def _flush_later(key, context):
    try:
        await asyncio.sleep(WAIT_SECONDS)
    except asyncio.CancelledError:
        return
    album = _albums.pop(key, None)
    if album:
        await _send(album, context)


def _media(items, caption):
    out = []
    for i, (kind, file_id) in enumerate(items):
        cap = caption if i == 0 else None
        out.append(InputMediaPhoto(file_id, caption=cap) if kind == "photo" else InputMediaVideo(file_id, caption=cap))
    return out


async def _send_with_retry(coro_factory):
    for attempt in range(5):
        try:
            return await coro_factory()
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except BadRequest:
            raise
        except NetworkError as e:
            logging.warning("Сбой сети при пересылке (%s), пробую ещё раз...", e)
            await asyncio.sleep(2 + attempt * 2)
    raise RuntimeError("не удалось отправить после 5 попыток")


async def _send(album, context):
    prices = parse_prices("\n".join(album["captions"]))
    if not prices:
        return  # подписи с ценами нет — это не товар, пропускаем
    items = [it for _, it in sorted(album["items"])]
    sent = []
    for name, price in prices.items():
        target = TARGETS.get(name) or {}
        if not target.get("chat_id"):
            continue
        caption = target.get("template", "{price}").format(price=price)
        try:
            for start in range(0, len(items), 10):  # в альбоме Telegram максимум 10
                chunk = items[start:start + 10]
                media = _media(chunk, caption if start == 0 else None)
                await _send_with_retry(lambda m=media: context.bot.send_media_group(
                    target["chat_id"], m, message_thread_id=target.get("thread_id")))
            sent.append(name)
        except Exception as e:
            logging.error("Не удалось переслать в %s: %s", name, e)
    if sent:
        logging.info("Переслано %s фото/видео в: %s", len(items), ", ".join(sent))
        try:
            await album["first"].set_reaction("👍")
        except Exception:
            pass
