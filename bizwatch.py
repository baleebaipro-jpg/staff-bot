"""Контроль рабочих Telegram-аккаунтов через Telegram Business.

Рабочий аккаунт (с Telegram Premium) подключает бота сотрудников: Настройки → Telegram для бизнеса → Чат-боты.
Бот получает все личные переписки этого аккаунта, запоминает сообщения и сообщает руководителю:
- что сообщение (или вся переписка) удалено — с текстом и медиа удалённого;
- что сообщение изменено — было / стало;
- что бота отключили от аккаунта.
Сам бот в этих чатах НИЧЕГО не пишет: такие обновления перехватываются раньше всех остальных обработчиков.
"""
import io
import logging
from datetime import timedelta

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

import config
import core

KEEP_DAYS = 120          # сколько дней храним переписку
LIST_MAX = 15            # сколько удалённых сообщений показывать в тексте (остальные — файлом)
MEDIA_MAX = 10           # сколько удалённых фото/видео/голосовых пересылать


def init() -> None:
    with core.db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS biz_connections(
                id TEXT PRIMARY KEY, user_id INTEGER, name TEXT, username TEXT, enabled INTEGER,
                created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS biz_messages(
                conn_id TEXT, chat_id INTEGER, msg_id INTEGER, chat_name TEXT, chat_username TEXT,
                from_id INTEGER, from_name TEXT, outgoing INTEGER, kind TEXT, text TEXT, file_id TEXT,
                sent_at TEXT, edited INTEGER DEFAULT 0, deleted_at TEXT,
                PRIMARY KEY(conn_id, chat_id, msg_id));
            CREATE TABLE IF NOT EXISTS biz_events(
                id INTEGER PRIMARY KEY, conn_id TEXT, chat_id INTEGER, chat_name TEXT, kind TEXT,
                info TEXT, at TEXT);
        """)


def _local(dt) -> str:
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo(config.TIMEZONE)).strftime(core.FMT)


def _now() -> str:
    return core.now().strftime(core.FMT)


def _who(user) -> str:
    if not user:
        return "—"
    n = " ".join(x for x in [user.first_name, user.last_name] if x) or str(user.id)
    return n + (f" (@{user.username})" if user.username else "")


def _at(user) -> str:
    return f" (@{user.username})" if user and user.username else ""


def _owner(conn_id: str):
    with core.db() as c:
        return c.execute("SELECT * FROM biz_connections WHERE id=?", (conn_id,)).fetchone()


def _owner_name(row) -> str:
    if not row:
        return "рабочий аккаунт"
    emp = core.employee(row["user_id"])
    return emp["name"] if emp else (row["name"] or str(row["user_id"]))


async def _ensure_conn(bot, conn_id: str):
    row = _owner(conn_id)
    if row:
        return row
    try:
        bc = await bot.get_business_connection(conn_id)
    except TelegramError:
        return None
    _save_conn(bc)
    return _owner(conn_id)


def _save_conn(bc) -> None:
    t = _now()
    with core.db() as c:
        c.execute("INSERT INTO biz_connections(id, user_id, name, username, enabled, created_at, updated_at) "
                  "VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id, name=excluded.name, "
                  "username=excluded.username, enabled=excluded.enabled, updated_at=excluded.updated_at",
                  (bc.id, bc.user.id, _who(bc.user).split(" (@")[0], bc.user.username or "", int(bool(bc.is_enabled)), t, t))


def _media(m):
    """(вид, file_id) для медиа сообщения."""
    if m.photo:
        return "photo", m.photo[-1].file_id
    for kind in ("video", "video_note", "voice", "audio", "document", "animation", "sticker"):
        obj = getattr(m, kind, None)
        if obj:
            return kind, obj.file_id
    if m.contact:
        return "contact", None
    if m.location:
        return "location", None
    return ("text" if m.text else "other"), None


KIND_LABEL = {"photo": "📷 фото", "video": "🎬 видео", "video_note": "⭕ кружок", "voice": "🎤 голосовое",
              "audio": "🎵 аудио", "document": "📎 файл", "animation": "🎞 GIF", "sticker": "🙂 стикер",
              "contact": "👤 контакт", "location": "📍 геолокация", "other": "сообщение"}


def _line(r) -> str:
    who = "🧑‍💼" if r["outgoing"] else "👤"
    body = (r["text"] or "").strip()
    if r["kind"] not in ("text", None):
        body = KIND_LABEL.get(r["kind"], r["kind"]) + (f": {body}" if body else "")
    return f"[{r['sent_at'][8:10]}.{r['sent_at'][5:7]} {r['sent_at'][11:16]}] {who} {r['from_name']}: {body or '—'}"


async def _to_admins(bot, text: str, document=None) -> None:
    for a in config.ADMINS:
        try:
            if document:
                document.seek(0)
                await bot.send_document(a, document, caption=text[:1024], filename=document.name)
            else:
                await bot.send_message(a, text[:4096])
        except TelegramError as e:
            logging.warning("Контроль бизнес-чатов: не отправлено %s: %s", a, e)


async def _send_media(bot, r, caption: str) -> None:
    send = {"photo": bot.send_photo, "video": bot.send_video, "voice": bot.send_voice, "audio": bot.send_audio,
            "document": bot.send_document, "animation": bot.send_animation, "video_note": bot.send_video_note,
            "sticker": bot.send_sticker}.get(r["kind"])
    if not send or not r["file_id"]:
        return
    for a in config.ADMINS:
        try:
            if r["kind"] in ("video_note", "sticker"):
                await send(a, r["file_id"])
                await bot.send_message(a, caption[:4096])
            else:
                await send(a, r["file_id"], caption=caption[:1024])
        except TelegramError as e:
            logging.warning("Контроль бизнес-чатов: медиа не отправлено %s: %s", a, e)


def _event(conn_id, chat_id, chat_name, kind, info) -> None:
    with core.db() as c:
        c.execute("INSERT INTO biz_events(conn_id, chat_id, chat_name, kind, info, at) VALUES(?, ?, ?, ?, ?, ?)",
                  (conn_id, chat_id, chat_name, kind, info[:4000], _now()))


def _cleanup() -> None:
    if not core.flag_once("biz_cleanup_" + core.now().strftime("%Y-%m-%d")):
        return
    old = (core.now() - timedelta(days=KEEP_DAYS)).strftime(core.FMT)
    with core.db() as c:
        c.execute("DELETE FROM biz_messages WHERE sent_at < ?", (old,))
        c.execute("DELETE FROM biz_events WHERE at < ?", (old,))


async def on_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перехват всех бизнес-обновлений (раньше остальных обработчиков) — бот в этих чатах молчит."""
    try:
        if update.business_connection:
            await _on_connection(update.business_connection, context.bot)
        elif update.business_message or update.edited_business_message:
            await _on_message(update.business_message or update.edited_business_message,
                              bool(update.edited_business_message), context.bot)
        elif update.deleted_business_messages:
            await _on_deleted(update.deleted_business_messages, context.bot)
        else:
            return  # обычное обновление — дальше, как всегда
    except Exception:
        logging.exception("Ошибка контроля бизнес-чатов")
    raise ApplicationHandlerStop


async def _on_connection(bc, bot) -> None:
    was = _owner(bc.id)
    _save_conn(bc)
    row = _owner(bc.id)
    name = _owner_name(row)
    if bc.is_enabled:
        if not was or not was["enabled"]:
            _event(bc.id, None, "", "connected", "")
            await _to_admins(bot, f"🔗 Рабочий аккаунт подключён к контролю: {name}{_at(bc.user)}\n\n"
                                  "Теперь я сообщу, если в его личных переписках удалят или изменят сообщение.")
    else:
        _event(bc.id, None, "", "disconnected", "")
        await _to_admins(bot, f"⚠️ Бот ОТКЛЮЧЁН от рабочего аккаунта: {name}{_at(bc.user)}\n"
                              "Контроль переписок по этому аккаунту остановлен.")


async def _on_message(m, edited: bool, bot) -> None:
    _cleanup()
    row = await _ensure_conn(bot, m.business_connection_id)
    owner_id = row["user_id"] if row else None
    kind, file_id = _media(m)
    text = m.text or m.caption or ""
    chat = m.chat
    chat_name = " ".join(x for x in [chat.first_name, chat.last_name] if x) or chat.title or str(chat.id)
    frm = m.from_user
    outgoing = int(bool(frm and owner_id and frm.id == owner_id))
    from_name = _owner_name(row) if outgoing else _who(frm).split(" (@")[0]
    with core.db() as c:
        old = c.execute("SELECT * FROM biz_messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
                        (m.business_connection_id, chat.id, m.message_id)).fetchone()
        c.execute("INSERT INTO biz_messages(conn_id, chat_id, msg_id, chat_name, chat_username, from_id, from_name, outgoing, "
                  "kind, text, file_id, sent_at, edited) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                  "ON CONFLICT(conn_id, chat_id, msg_id) DO UPDATE SET text=excluded.text, kind=excluded.kind, "
                  "file_id=excluded.file_id, edited=excluded.edited, chat_name=excluded.chat_name",
                  (m.business_connection_id, chat.id, m.message_id, chat_name, chat.username or "",
                   frm.id if frm else None, from_name, outgoing, kind, text,
                   file_id, (old["sent_at"] if old else _local(m.date)), int(edited)))
    if edited and old and (old["text"] or "") != text:
        who = f"🧑‍💼 {_owner_name(row)}" if outgoing else f"👤 собеседник ({from_name})"
        info = f"было: {old['text'] or '—'}\nстало: {text or '—'}"
        _event(m.business_connection_id, chat.id, chat_name, "edited", info)
        await _to_admins(bot, f"✏️ Изменено сообщение\n📱 Аккаунт: {_owner_name(row)}\n💬 Чат с: {chat_name}"
                              f"{' (@' + chat.username + ')' if chat.username else ''}\n✍️ Изменил: {who}\n\n"
                              f"Было:\n{old['text'] or '—'}\n\nСтало:\n{text or '—'}")


async def _on_deleted(d, bot) -> None:
    row = await _ensure_conn(bot, d.business_connection_id)
    ids = list(d.message_ids)
    chat = d.chat
    chat_name = " ".join(x for x in [chat.first_name, chat.last_name] if x) or chat.title or str(chat.id)
    t = _now()
    with core.db() as c:
        q = ",".join("?" * len(ids))
        rows = c.execute(f"SELECT * FROM biz_messages WHERE conn_id=? AND chat_id=? AND msg_id IN ({q}) ORDER BY msg_id",
                         (d.business_connection_id, chat.id, *ids)).fetchall()
        c.execute(f"UPDATE biz_messages SET deleted_at=? WHERE conn_id=? AND chat_id=? AND msg_id IN ({q})",
                  (t, d.business_connection_id, chat.id, *ids))
    unknown = len(ids) - len(rows)
    whole = len(ids) > LIST_MAX
    head = (f"🗑 {'Удалена переписка' if whole else 'Удалено сообщений: ' + str(len(ids))}\n"
            f"📱 Аккаунт: {_owner_name(row)}\n💬 Чат с: {chat_name}{' (@' + chat.username + ')' if chat.username else ''}"
            f"\n🕐 {core.now():%d.%m.%Y %H:%M}" + (f"\n📨 Сообщений: {len(ids)}" if whole else ""))
    lines = [_line(r) for r in rows]
    tail = f"\n\n❔ Ещё {unknown} сообщ. — были до подключения контроля, текст неизвестен." if unknown else ""
    _event(d.business_connection_id, chat.id, chat_name, "deleted", "\n".join(lines) or f"{len(ids)} сообщ.")
    if len(lines) > LIST_MAX:
        f = io.BytesIO("\n".join(lines).encode("utf-8"))
        f.name = f"udalennaya_perepiska_{core.now():%Y%m%d_%H%M}.txt"
        await _to_admins(bot, head + "\n\n" + "\n".join(lines[:5]) + "\n…\n📄 Вся удалённая переписка — в файле." + tail)
        await _to_admins(bot, f"📄 {chat_name} — удалённая переписка", document=f)
    else:
        await _to_admins(bot, head + ("\n\n" + "\n".join(lines) if lines else "") + tail)
    for r in [r for r in rows if r["file_id"]][:MEDIA_MAX]:
        await _send_media(bot, r, "🗑 Удалено: " + _line(r))


def connections() -> list:
    with core.db() as c:
        rows = c.execute("SELECT * FROM biz_connections ORDER BY updated_at DESC").fetchall()
        out = []
        for r in rows:
            n = c.execute("SELECT COUNT(*) FROM biz_messages WHERE conn_id=?", (r["id"],)).fetchone()[0]
            dl = c.execute("SELECT COUNT(*) FROM biz_messages WHERE conn_id=? AND deleted_at IS NOT NULL", (r["id"],)).fetchone()[0]
            out.append({"name": _owner_name(r), "username": r["username"], "enabled": bool(r["enabled"]),
                        "messages": n, "deleted": dl, "since": r["created_at"]})
    return out
