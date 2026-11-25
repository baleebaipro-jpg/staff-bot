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
    who = "🧑‍💼 менеджер" if r["outgoing"] else "👤 клиент"
    body = (r["text"] or "").strip()
    if r["kind"] not in ("text", None):
        body = KIND_LABEL.get(r["kind"], r["kind"]) + (f": {body}" if body else "")
    return f"[{r['sent_at'][8:10]}.{r['sent_at'][5:7]} {r['sent_at'][11:16]}] писал(а) {who}: {body or '—'}"


def _targets(owner=None) -> list:
    """Своя группа/тема рабочего аккаунта (BIZ_NOTIFY_ROUTES), иначе группа «Bot control» (BIZ_NOTIFY_CHAT),
    иначе — личка руководителей."""
    route = getattr(config, "BIZ_NOTIFY_ROUTES", {}).get(owner) if owner else None
    if route:
        return [route]
    chat = getattr(config, "BIZ_NOTIFY_CHAT", None)
    return [(chat, getattr(config, "BIZ_NOTIFY_THREAD", None))] if chat else [(a, None) for a in config.ADMINS]


async def _to_admins(bot, text: str, document=None, owner=None) -> None:
    for a, th in _targets(owner):
        try:
            if document:
                document.seek(0)
                await bot.send_document(a, document, caption=text[:1024], filename=document.name, message_thread_id=th)
            else:
                await bot.send_message(a, text[:4096], message_thread_id=th)
        except TelegramError as e:
            logging.warning("Контроль бизнес-чатов: не отправлено %s: %s", a, e)


async def _send_media(bot, r, caption: str, owner=None) -> None:
    send = {"photo": bot.send_photo, "video": bot.send_video, "voice": bot.send_voice, "audio": bot.send_audio,
            "document": bot.send_document, "animation": bot.send_animation, "video_note": bot.send_video_note,
            "sticker": bot.send_sticker}.get(r["kind"])
    if not send or not r["file_id"]:
        return
    for a, th in _targets(owner):
        try:
            if r["kind"] in ("video_note", "sticker"):
                await send(a, r["file_id"], message_thread_id=th)
                await bot.send_message(a, caption[:4096], message_thread_id=th)
            else:
                await send(a, r["file_id"], caption=caption[:1024], message_thread_id=th)
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
    if update.business_connection or update.business_message or update.edited_business_message or update.deleted_business_messages:
        logging.info("Бизнес-обновление: %s", "connection" if update.business_connection else "message" if update.business_message
                     else "edited" if update.edited_business_message else "deleted")
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
            await _to_admins(bot, owner=bc.user.id, text=f"🔗 Рабочий аккаунт подключён к контролю: {name}{_at(bc.user)}\n\n"
                                  "Теперь я сообщу, если в его личных переписках удалят или изменят сообщение.")
    else:
        _event(bc.id, None, "", "disconnected", "")
        await _to_admins(bot, owner=bc.user.id, text=f"⚠️ Бот ОТКЛЮЧЁН от рабочего аккаунта: {name}{_at(bc.user)}\n"
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
    dur = next((getattr(getattr(m, k, None), "duration", None) for k in ("voice", "audio", "video", "video_note")
                if getattr(m, k, None)), None)
    if dur:
        with core.db() as c:
            c.execute("UPDATE biz_messages SET dur=? WHERE conn_id=? AND chat_id=? AND msg_id=?",
                      (int(dur), m.business_connection_id, chat.id, m.message_id))
    if edited and old and (old["text"] or "") != text:
        who = "🧑‍💼 МЕНЕДЖЕР" if outgoing else "👤 КЛИЕНТ"  # изменить может только автор — здесь точно
        info = f"было: {old['text'] or '—'}\nстало: {text or '—'}"
        with core.db() as c:  # история изменений — для CRM
            c.execute("INSERT INTO biz_edits(conn_id, chat_id, msg_id, old, new, at) VALUES(?, ?, ?, ?, ?, ?)",
                      (m.business_connection_id, chat.id, m.message_id, old["text"] or "", text, _now()))
        _event(m.business_connection_id, chat.id, chat_name, "edited", info)
        await _to_admins(bot, owner=owner_id, text=f"✏️ Изменено сообщение\n🧑‍💼 Менеджер (рабочий аккаунт): {_owner_name(row)}\n👤 Клиент: {chat_name}"
                              f"{' (@' + chat.username + ')' if chat.username else ''}\n✍️ Изменил: {who}\n\n"
                              f"Было:\n{old['text'] or '—'}\n\nСтало:\n{text or '—'}")


async def _on_deleted(d, bot) -> None:
    row = await _ensure_conn(bot, d.business_connection_id)
    own = row["user_id"] if row else None
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
            f"🧑‍💼 Менеджер (рабочий аккаунт): {_owner_name(row)}\n"
            f"👤 Клиент: {chat_name}{' (@' + chat.username + ')' if chat.username else ''}"
            f"\n🕐 {core.now():%d.%m.%Y %H:%M}" + (f"\n📨 Сообщений: {len(ids)}" if whole else ""))
    lines = [_line(r) for r in rows]
    tail = f"\n\n❔ Ещё {unknown} сообщ. — были до подключения контроля, текст неизвестен." if unknown else ""
    # кто удалил — Telegram не сообщает; оценка по тому, чьи сообщения удалены
    if rows and all(r["outgoing"] for r in rows) and not unknown:
        tail += "\n\n🔎 Кто удалил: вероятно, МЕНЕДЖЕР — удалены его собственные сообщения."
    elif rows and not any(r["outgoing"] for r in rows) and not unknown:
        tail += "\n\n🔎 Кто удалил: клиент или менеджер — удалены сообщения клиента (Telegram не сообщает, кто именно)."
    else:
        tail += "\n\n🔎 Кто удалил: Telegram не сообщает (удалены сообщения обеих сторон или неизвестные)."
    _event(d.business_connection_id, chat.id, chat_name, "deleted", "\n".join(lines) or f"{len(ids)} сообщ.")
    if len(lines) > LIST_MAX:
        f = io.BytesIO("\n".join(lines).encode("utf-8"))
        f.name = f"udalennaya_perepiska_{core.now():%Y%m%d_%H%M}.txt"
        await _to_admins(bot, head + "\n\n" + "\n".join(lines[:5]) + "\n…\n📄 Вся удалённая переписка — в файле." + tail, owner=own)
        await _to_admins(bot, f"📄 {chat_name} — удалённая переписка", document=f, owner=own)
    else:
        await _to_admins(bot, head + ("\n\n" + "\n".join(lines) if lines else "") + tail, owner=own)
    for r in [r for r in rows if r["file_id"]][:MEDIA_MAX]:
        await _send_media(bot, r, "🗑 Удалено: " + _line(r), owner=own)


def connections() -> list:
    with core.db() as c:
        rows = c.execute("SELECT * FROM biz_connections ORDER BY updated_at DESC").fetchall()
        out = []
        for r in rows:
            n = c.execute("SELECT COUNT(*) FROM biz_messages WHERE conn_id=?", (r["id"],)).fetchone()[0]
            dl = c.execute("SELECT COUNT(*) FROM biz_messages WHERE conn_id=? AND deleted_at IS NOT NULL", (r["id"],)).fetchone()[0]
            last = c.execute("SELECT MAX(sent_at) FROM biz_messages WHERE conn_id=?", (r["id"],)).fetchone()[0]
            chats = c.execute("SELECT COUNT(DISTINCT chat_id) FROM biz_messages WHERE conn_id=?", (r["id"],)).fetchone()[0]
            out.append({"name": _owner_name(r), "username": r["username"], "enabled": bool(r["enabled"]),
                        "messages": n, "deleted": dl, "since": r["created_at"], "last": last or "", "chats": chats})
    return out


def recent_events(limit: int = 8) -> list:
    with core.db() as c:
        return [dict(r) for r in c.execute("SELECT kind, chat_name, at, substr(info, 1, 60) AS info FROM biz_events "
                                           "ORDER BY id DESC LIMIT ?", (limit,))]
