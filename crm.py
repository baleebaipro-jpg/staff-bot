"""CRM-мессенджер RADI — этап 1: просмотр переписок рабочих аккаунтов (только чтение, для руководителя).

Данные — из bizwatch (Telegram Business): biz_connections, biz_messages, biz_edits.
Окно ответа 24 ч: бот может отвечать от имени аккаунта, только если клиент писал за последние 24 часа.
"""
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from datetime import timedelta

import config
import core

WINDOW_HOURS = 24
MEDIA_DIR = os.path.join(config.DATA_DIR, "crm_media")


def init() -> None:
    with core.db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS biz_edits(id INTEGER PRIMARY KEY, conn_id TEXT, chat_id INTEGER, msg_id INTEGER, "
                  "old TEXT, new TEXT, at TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS biz_msg_chat ON biz_messages(conn_id, chat_id, msg_id)")
        c.execute("CREATE TABLE IF NOT EXISTS crm_templates(id INTEGER PRIMARY KEY, title TEXT, text TEXT, sort INTEGER DEFAULT 0, "
                  "created_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS crm_notes(id INTEGER PRIMARY KEY, conn_id TEXT, chat_id INTEGER, uid INTEGER, "
                  "text TEXT, at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS crm_tasks(id INTEGER PRIMARY KEY, conn_id TEXT, chat_id INTEGER, kind TEXT, "
                  "task_id INTEGER, assignee INTEGER, title TEXT, due TEXT, created_by INTEGER, at TEXT)")
        if "sent_by" not in {r[1] for r in c.execute("PRAGMA table_info(biz_messages)")}:
            c.execute("ALTER TABLE biz_messages ADD COLUMN sent_by INTEGER")  # кто из CRM отправил


def _window(last_in: str):
    """Сколько часов ещё можно отвечать из CRM (None — клиент не писал или окно закрыто)."""
    if not last_in:
        return None
    left = core.parse(last_in) + timedelta(hours=WINDOW_HOURS) - core.now()
    return round(left.total_seconds() / 3600, 1) if left.total_seconds() > 0 else None


def accounts() -> list:
    import bizwatch
    with core.db() as c:
        rows = c.execute("SELECT * FROM biz_connections ORDER BY created_at").fetchall()
    return [{"id": r["id"], "name": bizwatch._owner_name(r), "username": r["username"], "enabled": bool(r["enabled"])}
            for r in rows]


def chats(account: str = "", q: str = "", only: str = "") -> list:
    """Список чатов: последний вопрос/ответ, окно 24 ч, сколько удалено. q — поиск по имени, @username, тексту."""
    import bizwatch
    q = (q or "").strip().lower()
    args, where = [], ""
    if account:
        where, args = "WHERE conn_id=?", [account]
    with core.db() as c:
        owners = {r["id"]: bizwatch._owner_name(r) for r in c.execute("SELECT * FROM biz_connections")}
        rows = c.execute(f"""
            SELECT conn_id, chat_id, MAX(msg_id) AS last_id, COUNT(*) AS n,
                   SUM(deleted_at IS NOT NULL) AS deleted, SUM(edited) AS edited,
                   MAX(CASE WHEN outgoing=0 THEN sent_at END) AS last_in,
                   MAX(CASE WHEN outgoing=1 THEN sent_at END) AS last_out
            FROM biz_messages {where} GROUP BY conn_id, chat_id ORDER BY MAX(sent_at) DESC LIMIT 500""", args).fetchall()
        out = []
        for r in rows:
            m = c.execute("SELECT * FROM biz_messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
                          (r["conn_id"], r["chat_id"], r["last_id"])).fetchone()
            name = m["chat_name"] or str(r["chat_id"])
            if q and q not in name.lower() and q not in (m["chat_username"] or "").lower():
                hit = c.execute("SELECT 1 FROM biz_messages WHERE conn_id=? AND chat_id=? AND LOWER(text) LIKE ? LIMIT 1",
                                (r["conn_id"], r["chat_id"], f"%{q}%")).fetchone()
                if not hit:
                    continue
            win = _window(r["last_in"])
            waiting = bool(r["last_in"] and (not r["last_out"] or r["last_in"] > r["last_out"]))
            item = {"conn": r["conn_id"], "chat": r["chat_id"], "name": name, "username": m["chat_username"] or "",
                    "account": owners.get(r["conn_id"], ""), "last": _preview(m), "last_out": bool(m["outgoing"]),
                    "at": m["sent_at"], "n": r["n"], "deleted": r["deleted"] or 0, "edited": r["edited"] or 0,
                    "window": win, "waiting": waiting}
            if only == "waiting" and not waiting:
                continue
            if only == "deleted" and not item["deleted"]:
                continue
            if only == "closing" and not (win is not None and win <= 2):
                continue
            out.append(item)
    return out


KIND = {"photo": "📷 Фото", "video": "🎬 Видео", "video_note": "⭕ Кружок", "voice": "🎤 Голосовое", "audio": "🎵 Аудио",
        "document": "📎 Файл", "animation": "🎞 GIF", "sticker": "🙂 Стикер", "contact": "👤 Контакт",
        "location": "📍 Геолокация", "other": "Сообщение"}


def _preview(m) -> str:
    t = (m["text"] or "").strip().replace("\n", " ")
    if m["kind"] not in ("text", None):
        t = KIND.get(m["kind"], "Сообщение") + (f": {t}" if t else "")
    return t[:90] or "—"


def _sig(conn: str, chat: int, msg: int) -> str:
    return hmac.new(config.BOT_TOKEN.encode(), f"{conn}:{chat}:{msg}".encode(), hashlib.sha256).hexdigest()[:24]


def messages(conn: str, chat: int, before: int = 0, limit: int = 60) -> dict:
    with core.db() as c:
        q = "SELECT * FROM biz_messages WHERE conn_id=? AND chat_id=?"
        args = [conn, chat]
        if before:
            q += " AND msg_id<?"
            args.append(before)
        rows = c.execute(q + " ORDER BY msg_id DESC LIMIT ?", args + [limit]).fetchall()[::-1]
        edits = {}
        if rows:
            for e in c.execute(f"SELECT * FROM biz_edits WHERE conn_id=? AND chat_id=? AND msg_id IN "
                               f"({','.join('?' * len(rows))}) ORDER BY id", [conn, chat] + [r["msg_id"] for r in rows]):
                edits.setdefault(e["msg_id"], []).append({"old": e["old"], "at": e["at"][11:16]})
        head = c.execute("SELECT chat_name, chat_username, MAX(CASE WHEN outgoing=0 THEN sent_at END) AS last_in "
                         "FROM biz_messages WHERE conn_id=? AND chat_id=?", (conn, chat)).fetchone()
    items = [{"id": r["msg_id"], "out": bool(r["outgoing"]), "from": r["from_name"],
              "via": core.name(r["sent_by"]) if r["sent_by"] else "", "kind": r["kind"] or "text",
              "text": r["text"] or "", "at": r["sent_at"], "edited": bool(r["edited"]), "edits": edits.get(r["msg_id"], []),
              "deleted": r["deleted_at"][:16] if r["deleted_at"] else "",
              "media": (f"/crm/media/{conn}/{chat}/{r['msg_id']}?s={_sig(conn, chat, r['msg_id'])}"
                        if r["file_id"] and r["kind"] in ("photo", "video", "voice", "audio", "video_note", "animation",
                                                          "document", "sticker") else "")}
             for r in rows]
    return {"name": head["chat_name"] if head else "", "username": head["chat_username"] if head else "",
            "window": _window(head["last_in"] if head else None), "items": items, "more": len(rows) == limit}


def media_file(conn: str, chat: int, msg: int, sig: str):
    """Файл сообщения: скачиваем через Bot API (бот, к которому подключён аккаунт) и кэшируем на диске."""
    if not hmac.compare_digest(sig or "", _sig(conn, chat, msg)):
        return None, None
    with core.db() as c:
        r = c.execute("SELECT file_id, kind FROM biz_messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
                      (conn, chat, msg)).fetchone()
    if not r or not r["file_id"]:
        return None, None
    os.makedirs(MEDIA_DIR, exist_ok=True)
    path = os.path.join(MEDIA_DIR, hashlib.sha1(r["file_id"].encode()).hexdigest())
    if not os.path.exists(path):
        token = getattr(config, "BIZ_BOT_TOKEN", "") or config.BOT_TOKEN
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getFile?file_id={r['file_id']}", timeout=20) as f:
            info = json.loads(f.read().decode())
        fp = (info.get("result") or {}).get("file_path")
        if not fp:
            return None, None
        with urllib.request.urlopen(f"https://api.telegram.org/file/bot{token}/{fp}", timeout=60) as f, open(path, "wb") as out:
            out.write(f.read())
    mime = {"photo": "image/jpeg", "sticker": "image/webp", "video": "video/mp4", "video_note": "video/mp4",
            "animation": "video/mp4", "voice": "audio/ogg", "audio": "audio/mpeg"}.get(r["kind"], "application/octet-stream")
    return path, mime


# ---------- отправка из CRM (через бизнес-подключение: от имени рабочего аккаунта) ----------

ERRORS = [
    ("not enough rights", "У бота нет права отвечать: Telegram → Настройки → Telegram для бизнеса → Чат-боты → бот → "
                          "включите «Ответы на сообщения»."),
    ("business_peer", "Окно ответа закрыто: клиент не писал больше 24 часов. Написать первым можно из Telegram."),
    ("peer_usage_missing", "Окно ответа закрыто: клиент не писал больше 24 часов. Написать первым можно из Telegram."),
    ("voice_messages_forbidden", "Клиент запретил голосовые и кружки в настройках приватности Telegram."),
    ("bot_business_missing", "Бот отключён от рабочего аккаунта — подключите его снова в «Чат-боты»."),
    ("business_connection_invalid", "Бот отключён от рабочего аккаунта — подключите его снова в «Чат-боты»."),
]


def _explain(err: str) -> str:
    e = (err or "").lower()
    for key, text in ERRORS:
        if key in e:
            return text
    return f"Telegram не принял сообщение: {err}"


def _api(method: str, params: dict, files: dict = None):
    token = getattr(config, "BIZ_BOT_TOKEN", "") or config.BOT_TOKEN
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        boundary = "----radicrm" + hashlib.md5(os.urandom(8)).hexdigest()
        body = b""
        for k, v in params.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for k, (fname, data, mime) in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                     f"Content-Type: {mime}\r\n\r\n").encode() + data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            j = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            j = {"ok": False, "description": str(e)}
    if not j.get("ok"):
        raise RuntimeError(j.get("description") or "error")
    return j["result"]


def send(uid: int, conn: str, chat: int, text: str = "", photo: bytes = b"", photo_name: str = "photo.jpg"):
    """Отправить клиенту от имени рабочего аккаунта. Сообщение сразу появляется в переписке CRM."""
    import bizwatch
    text = (text or "").strip()
    if not text and not photo:
        return False, "Напишите сообщение."
    with core.db() as c:
        head = c.execute("SELECT chat_name, chat_username FROM biz_messages WHERE conn_id=? AND chat_id=? LIMIT 1",
                         (conn, chat)).fetchone()
        owner = c.execute("SELECT * FROM biz_connections WHERE id=?", (conn,)).fetchone()
    if not head or not owner:
        return False, "Чат не найден."
    first = (head["chat_name"] or "").split()[0] if head["chat_name"] else ""
    text = text.replace("{имя}", first).replace("{ism}", first).replace("{name}", first)
    try:
        if photo:
            res = _api("sendPhoto", {"chat_id": chat, "business_connection_id": conn, "caption": text[:1024]},
                       {"photo": (photo_name, photo, "image/jpeg")})
        else:
            res = _api("sendMessage", {"chat_id": chat, "business_connection_id": conn, "text": text[:4096]})
    except Exception as e:  # noqa: BLE001
        return False, _explain(str(e))
    kind, file_id = "text", None
    if res.get("photo"):
        kind, file_id = "photo", res["photo"][-1]["file_id"]
    with core.db() as c:
        c.execute("INSERT INTO biz_messages(conn_id, chat_id, msg_id, chat_name, chat_username, from_id, from_name, outgoing, "
                  "kind, text, file_id, sent_at, edited, sent_by) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 0, ?) "
                  "ON CONFLICT(conn_id, chat_id, msg_id) DO UPDATE SET sent_by=excluded.sent_by",
                  (conn, chat, res["message_id"], head["chat_name"], head["chat_username"], owner["user_id"],
                   bizwatch._owner_name(owner), kind, text, file_id, core.now().strftime(core.FMT), uid))
    return True, "Отправлено ✅"


def templates() -> list:
    with core.db() as c:
        return [dict(r) for r in c.execute("SELECT id, title, text FROM crm_templates ORDER BY sort, id")]


def save_template(tid: int, title: str, text: str):
    title, text = (title or "").strip()[:60], (text or "").strip()[:4000]
    if not text:
        return False, "Напишите текст быстрого ответа."
    title = title or text.split("\n")[0][:40]
    with core.db() as c:
        if tid:
            c.execute("UPDATE crm_templates SET title=?, text=? WHERE id=?", (title, text, tid))
        else:
            c.execute("INSERT INTO crm_templates(title, text, created_at) VALUES(?, ?, ?)",
                      (title, text, core.now().strftime(core.FMT)))
    return True, "Быстрый ответ сохранён."


def delete_template(tid: int):
    with core.db() as c:
        c.execute("DELETE FROM crm_templates WHERE id=?", (tid,))
    return True, "Быстрый ответ удалён."


# ---------- заметки и задачи по клиенту (из чата CRM) ----------

def _client_name(c, conn, chat) -> str:
    r = c.execute("SELECT chat_name FROM biz_messages WHERE conn_id=? AND chat_id=? LIMIT 1", (conn, chat)).fetchone()
    return (r["chat_name"] if r else "") or str(chat)


def info(conn: str, chat: int) -> dict:
    """Заметки и задачи по этому клиенту (для панели в чате)."""
    with core.db() as c:
        notes = [{"id": r["id"], "text": r["text"], "who": core.name(r["uid"]), "at": r["at"][:16]}
                 for r in c.execute("SELECT * FROM crm_notes WHERE conn_id=? AND chat_id=? ORDER BY id DESC", (conn, chat))]
        tasks = []
        for r in c.execute("SELECT * FROM crm_tasks WHERE conn_id=? AND chat_id=? ORDER BY id DESC LIMIT 50", (conn, chat)):
            if r["kind"] == "project":
                t = c.execute("SELECT done_at FROM ptasks WHERE id=?", (r["task_id"],)).fetchone()
                done = bool(t and t["done_at"])
            else:
                t = c.execute("SELECT status FROM tasks WHERE id=?", (r["task_id"],)).fetchone()
                done = bool(t and t["status"] == "done")
            if not t:
                continue  # задачу удалили
            tasks.append({"id": r["id"], "title": r["title"], "due": r["due"], "who": core.name(r["assignee"]), "done": done})
    return {"notes": notes, "tasks": tasks}


def add_note(uid: int, conn: str, chat: int, text: str):
    text = (text or "").strip()[:2000]
    if not text:
        return False, "Напишите заметку."
    with core.db() as c:
        c.execute("INSERT INTO crm_notes(conn_id, chat_id, uid, text, at) VALUES(?, ?, ?, ?, ?)",
                  (conn, chat, uid, text, core.now().strftime(core.FMT)))
    return True, "📝 Заметка сохранена"


def delete_note(nid: int):
    with core.db() as c:
        c.execute("DELETE FROM crm_notes WHERE id=?", (nid,))
    return True, "Заметка удалена"


def add_task(uid: int, conn: str, chat: int, title: str, day: str, assignee: int):
    """Задача по клиенту: себе (руководителю) — в проект «💬 Клиенты (CRM)», видна в «Мои задачи на сегодня»;
    сотруднику — в его список задач на этот день (как задача от руководителя)."""
    import projects
    from datetime import datetime as _dt
    title = (title or "").strip()[:250]
    if not title:
        return False, "Напишите задачу."
    if day in ("today", "tomorrow", "", None):
        due = (core.now() + timedelta(days=1 if day == "tomorrow" else 0)).strftime("%Y-%m-%d")
    else:
        try:
            due = _dt.strptime(str(day)[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return False, "Неверная дата."
    with core.db() as c:
        client = _client_name(c, conn, chat)
    full = f"{title} · 💬 {client}"
    assignee = assignee or uid
    if core.employee(assignee) and assignee != uid:
        with core.db() as c:
            cur = c.execute("INSERT INTO tasks(user_id, title, created_at, due_date) VALUES(?, ?, ?, ?)",
                            (assignee, full, core.now().strftime(core.FMT), due))
            tid, kind = cur.lastrowid, "staff"
            core.notify(c, assignee, core.st.s(core.staff_lang(assignee), "task_new", t=f"{full} ({due[8:10]}.{due[5:7]})"))
    else:
        with core.db() as c:
            r = c.execute("SELECT id FROM projects WHERE owner_id=? AND title=?", (uid, "💬 Клиенты (CRM)")).fetchone()
        if not r:
            projects.add_project(uid, "💬 Клиенты (CRM)")
            with core.db() as c:
                r = c.execute("SELECT id FROM projects WHERE owner_id=? AND title=?", (uid, "💬 Клиенты (CRM)")).fetchone()
        ok, msg = projects.add_task(uid, r["id"], full, due)
        if not ok:
            return False, msg
        with core.db() as c:
            tid = c.execute("SELECT MAX(id) FROM ptasks WHERE owner_id=?", (uid,)).fetchone()[0]
        kind = "project"
    with core.db() as c:
        c.execute("INSERT INTO crm_tasks(conn_id, chat_id, kind, task_id, assignee, title, due, created_by, at) "
                  "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)", (conn, chat, kind, tid, assignee, title, due, uid,
                                                        core.now().strftime(core.FMT)))
    return True, f"📌 Задача на {due[8:10]}.{due[5:7]} — {core.name(assignee)}"


# ---------- голосовые из CRM ----------

def _ffmpeg() -> str:
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # своя копия ffmpeg (ставится из requirements.txt на сервере)
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return ""


def to_voice(blob: bytes, ext: str = "webm") -> bytes:
    """Любая запись с телефона (webm / mp4 / ogg) → голосовое Telegram: OGG + Opus, моно."""
    import subprocess
    import tempfile
    exe = _ffmpeg()
    if not exe:
        raise RuntimeError("на сервере нет ffmpeg")
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in." + ext), os.path.join(d, "out.ogg")
        with open(src, "wb") as f:
            f.write(blob)
        for codec in (["-c:a", "libopus"], ["-c:a", "opus", "-strict", "-2", "-ar", "48000"]):
            r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", src, "-vn", "-ac", "1", "-b:a", "32k", *codec, dst],
                               capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 100:
                with open(dst, "rb") as f:
                    return f.read()
        raise RuntimeError((r.stderr or b"").decode(errors="ignore")[-300:] or "ошибка конвертации")


def to_m4a(blob: bytes, ext: str = "webm") -> bytes:
    """Запасной вариант, если клиент запретил голосовые: обычный аудиофайл M4A (AAC)."""
    import subprocess
    import tempfile
    exe = _ffmpeg()
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in." + ext), os.path.join(d, "out.m4a")
        with open(src, "wb") as f:
            f.write(blob)
        r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", src, "-vn", "-ac", "1", "-c:a", "aac", "-b:a", "64k", dst],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or b"").decode(errors="ignore")[-300:])
        with open(dst, "rb") as f:
            return f.read()


def _save_out(uid, conn, chat, head, owner, msg_id, kind, file_id, text=""):
    import bizwatch
    with core.db() as c:
        c.execute("INSERT INTO biz_messages(conn_id, chat_id, msg_id, chat_name, chat_username, from_id, from_name, outgoing, "
                  "kind, text, file_id, sent_at, edited, sent_by) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 0, ?) "
                  "ON CONFLICT(conn_id, chat_id, msg_id) DO UPDATE SET sent_by=excluded.sent_by",
                  (conn, chat, msg_id, head["chat_name"], head["chat_username"], owner["user_id"],
                   bizwatch._owner_name(owner), kind, text, file_id, core.now().strftime(core.FMT), uid))


def send_voice(uid: int, conn: str, chat: int, blob: bytes, mime: str = "", duration: float = 0):
    import bizwatch
    if len(blob) < 500:
        return False, "Запись слишком короткая."
    with core.db() as c:
        head = c.execute("SELECT chat_name, chat_username FROM biz_messages WHERE conn_id=? AND chat_id=? LIMIT 1",
                         (conn, chat)).fetchone()
        owner = c.execute("SELECT * FROM biz_connections WHERE id=?", (conn,)).fetchone()
    if not head or not owner:
        return False, "Чат не найден."
    ext = "mp4" if "mp4" in mime or "aac" in mime or "m4a" in mime else "ogg" if "ogg" in mime else "webm"
    try:
        ogg = to_voice(blob, ext)
    except Exception as e:  # noqa: BLE001
        return False, f"Не удалось подготовить голосовое: {e}"
    params = {"chat_id": chat, "business_connection_id": conn}
    if duration:
        params["duration"] = int(round(duration))
    try:
        res = _api("sendVoice", params, {"voice": ("voice.ogg", ogg, "audio/ogg")})
    except Exception as e:  # noqa: BLE001
        if "voice_messages_forbidden" not in str(e).lower():
            return False, _explain(str(e))
        # клиент запретил голосовые → отправляем то же самое обычным аудиофайлом (это разрешено всегда)
        try:
            res = _api("sendAudio", dict(params, title="🎤 Голосовое", performer=owner["name"] or ""),
                       {"audio": ("golosovoe.m4a", to_m4a(blob, ext), "audio/mp4")})
        except Exception as e2:  # noqa: BLE001
            return False, _explain(str(e2))
        _save_out(uid, conn, chat, head, owner, res["message_id"], "audio", (res.get("audio") or {}).get("file_id"))
        return True, "🎤 Клиент запретил голосовые — отправлено аудиофайлом"
    file_id = (res.get("voice") or {}).get("file_id")
    with core.db() as c:
        c.execute("INSERT INTO biz_messages(conn_id, chat_id, msg_id, chat_name, chat_username, from_id, from_name, outgoing, "
                  "kind, text, file_id, sent_at, edited, sent_by) VALUES(?, ?, ?, ?, ?, ?, ?, 1, 'voice', '', ?, ?, 0, ?) "
                  "ON CONFLICT(conn_id, chat_id, msg_id) DO UPDATE SET sent_by=excluded.sent_by",
                  (conn, chat, res["message_id"], head["chat_name"], head["chat_username"], owner["user_id"],
                   bizwatch._owner_name(owner), file_id, core.now().strftime(core.FMT), uid))
    return True, "🎤 Голосовое отправлено"


# ---------- кружки из CRM ----------

def to_video_note(blob: bytes, ext: str = "webm") -> bytes:
    """Видео с телефона → кружок Telegram: квадрат 384×384 по центру, H.264 + AAC, MP4, до 60 сек."""
    import subprocess
    import tempfile
    exe = _ffmpeg()
    if not exe:
        raise RuntimeError("на сервере нет ffmpeg")
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in." + ext), os.path.join(d, "out.mp4")
        with open(src, "wb") as f:
            f.write(blob)
        vf = "crop='min(iw,ih)':'min(iw,ih)',scale=384:384,setsar=1"
        r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", src, "-t", "60", "-vf", vf, "-c:v", "libx264",
                            "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-profile:v", "main",
                            "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", dst],
                           capture_output=True, timeout=180)
        if r.returncode != 0 or not os.path.exists(dst):
            raise RuntimeError((r.stderr or b"").decode(errors="ignore")[-300:] or "ошибка конвертации")
        with open(dst, "rb") as f:
            return f.read()


def send_circle(uid: int, conn: str, chat: int, blob: bytes, mime: str = "", duration: float = 0):
    import bizwatch
    if len(blob) < 2000:
        return False, "Запись слишком короткая."
    with core.db() as c:
        head = c.execute("SELECT chat_name, chat_username FROM biz_messages WHERE conn_id=? AND chat_id=? LIMIT 1",
                         (conn, chat)).fetchone()
        owner = c.execute("SELECT * FROM biz_connections WHERE id=?", (conn,)).fetchone()
    if not head or not owner:
        return False, "Чат не найден."
    ext = "mp4" if "mp4" in mime or "quicktime" in mime else "webm"
    try:
        mp4 = to_video_note(blob, ext)
    except Exception as e:  # noqa: BLE001
        return False, f"Не удалось подготовить кружок: {e}"
    params = {"chat_id": chat, "business_connection_id": conn, "length": 384}
    if duration:
        params["duration"] = min(60, int(round(duration)))
    try:
        res = _api("sendVideoNote", params, {"video_note": ("circle.mp4", mp4, "video/mp4")})
    except Exception as e:  # noqa: BLE001
        if "voice_messages_forbidden" not in str(e).lower():
            return False, _explain(str(e))
        # клиент запретил кружки → то же видео обычным видеосообщением
        vp = {"chat_id": chat, "business_connection_id": conn, "width": 384, "height": 384, "supports_streaming": "true"}
        if duration:
            vp["duration"] = min(60, int(round(duration)))
        try:
            res = _api("sendVideo", vp, {"video": ("video.mp4", mp4, "video/mp4")})
        except Exception as e2:  # noqa: BLE001
            return False, _explain(str(e2))
        _save_out(uid, conn, chat, head, owner, res["message_id"], "video", (res.get("video") or {}).get("file_id"))
        return True, "⭕ Клиент запретил кружки — отправлено обычным видео"
    file_id = (res.get("video_note") or {}).get("file_id")
    with core.db() as c:
        c.execute("INSERT INTO biz_messages(conn_id, chat_id, msg_id, chat_name, chat_username, from_id, from_name, outgoing, "
                  "kind, text, file_id, sent_at, edited, sent_by) VALUES(?, ?, ?, ?, ?, ?, ?, 1, 'video_note', '', ?, ?, 0, ?) "
                  "ON CONFLICT(conn_id, chat_id, msg_id) DO UPDATE SET sent_by=excluded.sent_by",
                  (conn, chat, res["message_id"], head["chat_name"], head["chat_username"], owner["user_id"],
                   bizwatch._owner_name(owner), file_id, core.now().strftime(core.FMT), uid))
    return True, "⭕ Кружок отправлен"
