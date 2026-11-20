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
import core

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


def destinations(target: dict) -> list:
    """Куда слать: список чатов из "chats" (или старый формат с одним chat_id)."""
    chats = target.get("chats") or [target]
    return [c for c in chats if c.get("chat_id")]


def enabled() -> bool:
    return bool(SOURCE_CHAT_ID) and any(destinations(t) for t in TARGETS.values())


CURRENCY = getattr(config, "RELAY_CURRENCY", "so'm")
_PRICE_RE = {
    target: re.compile(
        rf"(?:{'|'.join(re.escape(w) for w in words)})\w*[\s:=\-–—]*(\d[\d\s.,']*\d|\d)"
        r"(?:\s*(?:so'?m|сум|sum|uzs))?",
        re.IGNORECASE,
    )
    for target, words in KEYWORDS.items()
}


def parse_caption(text: str):
    """'Probalik yangi Toplam\nsilver 45.000 bronza 55.000'
    -> ({'silver': 45000, 'bronza': 55000}, 'Probalik yangi Toplam')"""
    prices = {}
    rest = text
    for target, rx in _PRICE_RE.items():
        m = rx.search(text)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if digits:
                prices[target] = int(digits)
            rest = rest.replace(m.group(0), " ")
    # всё, что осталось кроме цен, — заголовок
    lines = [re.sub(r"\s+", " ", ln).strip(" ,;:-–—|/") for ln in rest.splitlines()]
    title = "\n".join(ln for ln in lines if ln)
    return prices, title


def parse_prices(text: str) -> dict:
    return parse_caption(text)[0]


def money(n: int) -> str:
    return f"{n:,}".replace(",", ".") + f" {CURRENCY}"


def build_caption(target: str, prices: dict, title: str) -> str:
    s, b = prices.get("silver"), prices.get("bronza")
    if target == "silver":
        if b and b > s:
            price_line = f"{money(b)}❌ {money(s)}✅ ({money(b - s)} foyda)"
        else:
            price_line = f"{money(s)}✅"
    else:
        price_line = f"{money(b)}✅"
    parts = [title, price_line, (TARGETS.get(target) or {}).get("footer", "")]
    return "\n\n".join(p.strip() for p in parts if p and p.strip())[:1024]


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
    prices, title = parse_caption("\n".join(album["captions"]))
    if not prices:
        return  # подписи с ценами нет — это не товар, пропускаем
    items = [it for _, it in sorted(album["items"])]
    sent = []
    for name in prices:
        target = TARGETS.get(name) or {}
        caption = build_caption(name, prices, title)
        for dest in destinations(target):
            label = dest.get("title") or name
            try:
                for start in range(0, len(items), 10):  # в альбоме Telegram максимум 10
                    chunk = items[start:start + 10]
                    media = _media(chunk, caption if start == 0 else None)
                    await _send_with_retry(lambda m=media, d=dest: context.bot.send_media_group(
                        d["chat_id"], m, message_thread_id=d.get("thread_id")))
                sent.append(label)
            except Exception as e:
                logging.error("Не удалось переслать в %s: %s", label, e)
    if sent:
        logging.info("Переслано %s фото/видео в: %s", len(items), ", ".join(sent))
        try:
            core.log_relay_post(len(items))
        except Exception:
            logging.exception("Не удалось записать альбом в счётчик")
        _last_source["thread_id"] = album["first"].message_thread_id
        await _praise(album["first"])
        try:
            await album["first"].set_reaction("👍")
        except Exception:
            pass


# ================= Простая переотправка из отдельной темы =================
# Всё из этой темы копируется в группы без изменений.
# Копия не показывает «Переслано от …» — источник скрыт.

COPY_CHAT_ID = getattr(config, "RELAY_COPY_CHAT_ID", None)
COPY_THREAD_ID = getattr(config, "RELAY_COPY_THREAD_ID", None)
# Чаты, куда идёт ТОЛЬКО простая переотправка (без фото с ценами)
COPY_ONLY_CHATS = getattr(config, "RELAY_COPY_ONLY_CHATS", [])

_copy_albums: dict = {}


def _is_copy_source(msg) -> bool:
    return bool(COPY_CHAT_ID) and msg.chat_id == COPY_CHAT_ID and (
        not COPY_THREAD_ID or msg.message_thread_id == COPY_THREAD_ID
    )


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Все сообщения из групп: либо простая копия, либо фото с ценами."""
    msg = update.effective_message
    if not msg:
        return
    if _is_copy_source(msg):
        await _collect_copy(msg, context)
    elif msg.photo or msg.video:
        await collect(update, context)


async def _collect_copy(msg, context):
    if not msg.media_group_id:
        await _copy([msg.message_id], msg, context)
        return
    album = _copy_albums.setdefault(msg.media_group_id, {"ids": [], "first": msg, "task": None})
    album["ids"].append(msg.message_id)
    if album["task"]:
        album["task"].cancel()
    album["task"] = asyncio.create_task(_copy_later(msg.media_group_id, context))


async def _copy_later(key, context):
    try:
        await asyncio.sleep(WAIT_SECONDS)
    except asyncio.CancelledError:
        return
    album = _copy_albums.pop(key, None)
    if album:
        await _copy(sorted(album["ids"]), album["first"], context)


async def _copy(ids, first, context):
    sent = []
    all_dests = [(name, d) for name, t in TARGETS.items() for d in destinations(t)]
    all_dests += [("copy", d) for d in COPY_ONLY_CHATS if d.get("chat_id")]
    for name, dest in all_dests:
        if True:
            label = dest.get("title") or name
            try:
                await _send_with_retry(lambda d=dest: context.bot.copy_messages(
                    d["chat_id"], first.chat_id, ids, message_thread_id=d.get("thread_id")))
                sent.append(label)
            except Exception as e:
                logging.error("Не удалось скопировать в %s: %s", label, e)
    if sent:
        logging.info("Скопировано %s сообщ. в: %s", len(ids), ", ".join(sent))
        try:
            await first.set_reaction("👍")
        except Exception:
            pass


# ================= Подколка фотографа, если альбомов мало =================

TEASE = getattr(config, "RELAY_TEASE", None)
_last_source = {"thread_id": None}


async def daily_check(bot) -> None:
    """Вызывается ботом каждые ~10 секунд. Один раз в день в TEASE['time']
    проверяет, сколько альбомов с ценами разослано, и подкалывает фотографа."""
    if not TEASE or not SOURCE_CHAT_ID:
        return
    t = core.now()
    if t.weekday() not in TEASE.get("days", range(7)):
        return
    if t < core.at(t, TEASE.get("time", "18:00")):
        return
    count = core.relay_posts_today()
    if count >= TEASE.get("min_albums", 3):
        return
    if not core.flag_once(f"tease:{t:%Y-%m-%d}"):
        return  # сегодня уже подкалывали
    thread = TEASE.get("thread_id") or SOURCE_THREAD_ID or _last_source["thread_id"]
    text = TEASE["text"].format(count=count, min=TEASE.get("min_albums", 3))
    try:
        await bot.send_message(TEASE.get("chat_id") or SOURCE_CHAT_ID, text, message_thread_id=thread)
        logging.info("Подколка отправлена (альбомов сегодня: %s)", count)
    except Exception as e:
        logging.error("Не удалось отправить подколку: %s", e)


# ================= Похвала после каждого альбома =================

PRAISE = getattr(config, "RELAY_PRAISE", None)


async def _praise(first) -> None:
    """Ответить на альбом, отметив отправителя."""
    if not PRAISE:
        return
    user = first.from_user
    mention = user.mention_html() if user and not user.is_bot else ""
    text = PRAISE.format(mention=mention).strip()
    try:
        await _send_with_retry(lambda: first.reply_html(text))
    except Exception as e:
        logging.warning("Не удалось отправить похвалу: %s", e)
