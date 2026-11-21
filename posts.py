"""Постинг: руководитель пишет пост в кабинете и рассылает его через бота.

Пост: текст, необязательное фото, необязательная кнопка-ссылка.
Аудитории: clients — все клиенты бота, staff — сотрудники, chat:<id> — группы
и каналы из POST_CHATS (бот должен быть там админом).
Отправить сейчас или запланировать. Сообщения уходят через общую очередь
(outbox), поэтому видно, сколько уже доставлено.
"""
import json
import os
from datetime import datetime

import config
import core

DIR = os.path.join(core._DATA_DIR, "posts")
TEXT_MAX, CAPTION_MAX = 4000, 1024


def chats() -> list:
    """Группы и каналы для постов: [{'id': -100..., 'title': '...'}]"""
    out = []
    for ch in getattr(config, "POST_CHATS", []):
        out.append({"id": int(ch["chat_id"]), "title": ch.get("title") or str(ch["chat_id"]),
                    "thread_id": ch.get("thread_id")})
    return out


def audience_info() -> dict:
    with core.db() as c:
        clients = c.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    return {"clients": clients, "staff": len(config.EMPLOYEES), "chats": chats()}


def _recipients(audience: list) -> list:
    """[(chat_id, thread_id), ...] без повторов."""
    out, seen = [], set()
    staff = set(config.EMPLOYEES) | set(config.ADMINS)

    def add(chat, thread=None):
        if chat not in seen:
            seen.add(chat)
            out.append((chat, thread))
    if "clients" in audience:
        with core.db() as c:
            for (uid,) in c.execute("SELECT user_id FROM clients ORDER BY created_at"):
                if uid not in staff:
                    add(uid)
    if "staff" in audience:
        for uid in config.EMPLOYEES:
            add(uid)
    known = {ch["id"]: ch for ch in chats()}
    for a in audience:
        if a.startswith("chat:"):
            ch = known.get(int(a[5:]))
            if ch:
                add(ch["id"], ch["thread_id"])
    return out


def _check(text: str, photo: bool, btn_text: str, btn_url: str):
    text = str(text or "").strip()
    if not text and not photo:
        return "Напишите текст поста или добавьте фото."
    if len(text) > (CAPTION_MAX if photo else TEXT_MAX):
        return f"Текст слишком длинный: максимум {CAPTION_MAX if photo else TEXT_MAX} символов" + \
               (" с фото." if photo else ".")
    if bool(btn_text) != bool(btn_url):
        return "Для кнопки нужны и текст, и ссылка."
    if btn_url and not (btn_url.startswith("https://") or btn_url.startswith("http://") or btn_url.startswith("tg://")):
        return "Ссылка кнопки должна начинаться с https://"
    return None


def _markup(btn_text, btn_url):
    return json.dumps([[[btn_text, "url:" + btn_url]]], ensure_ascii=False) if btn_text and btn_url else None


def save_photo(jpeg: bytes) -> str:
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, f"{int(core.now().timestamp() * 1000)}.jpg")
    with open(path, "wb") as f:
        f.write(jpeg)
    return path


def test(owner: int, text: str, photo: str, btn_text: str, btn_url: str):
    """Предпросмотр: отправить пост только себе."""
    btn_text, btn_url = str(btn_text or "").strip()[:60], str(btn_url or "").strip()[:500]
    err = _check(text, bool(photo), btn_text, btn_url)
    if err:
        return False, err
    with core.db() as c:
        c.execute("INSERT INTO outbox(chat_id, text, markup, photo) VALUES(?, ?, ?, ?)",
                  (owner, str(text or "").strip() or " ", _markup(btn_text, btn_url), photo))
    return True, "👁 Пост отправлен вам в бот — посмотрите, как он выглядит."


def create(owner: int, text: str, photo: str, btn_text: str, btn_url: str, audience: list,
           to_news: bool = False, send_at: str = ""):
    btn_text, btn_url = str(btn_text or "").strip()[:60], str(btn_url or "").strip()[:500]
    text = str(text or "").strip()
    err = _check(text, bool(photo), btn_text, btn_url)
    if err:
        return False, err
    valid = {"clients", "staff"} | {f"chat:{ch['id']}" for ch in chats()}
    audience = [a for a in dict.fromkeys(str(a) for a in (audience or [])) if a in valid]
    if not audience:
        return False, "Выберите, кому отправить пост."
    if not _recipients(audience):
        return False, "В выбранной аудитории пока никого нет."
    when = None
    if send_at:
        try:
            when = datetime.strptime(str(send_at)[:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            return False, "Неверная дата и время."
        if when <= core.now():
            return False, "Время публикации уже прошло — выберите будущее."
    with core.db() as c:
        cur = c.execute(
            "INSERT INTO posts(owner_id, text, photo, button_text, button_url, audience, to_news, status, send_at, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)",
            (owner, text, photo, btn_text, btn_url, json.dumps(audience), int(bool(to_news)),
             (when or core.now()).strftime(core.FMT), core.now().strftime(core.FMT)))
        pid = cur.lastrowid
    if when:
        return True, f"🕒 Пост запланирован на {when:%d.%m %H:%M}"
    n = dispatch(pid)
    return True, f"📣 Пост отправляется: получателей {n}"


def dispatch(pid: int) -> int:
    """Поставить пост в очередь отправки. Возвращает число получателей."""
    with core.db() as c:
        p = c.execute("SELECT * FROM posts WHERE id=? AND status='scheduled'", (pid,)).fetchone()
        if not p:
            return 0
        rec = _recipients(json.loads(p["audience"]))
        markup = _markup(p["button_text"], p["button_url"])
        for chat, thread in rec:
            c.execute("INSERT INTO outbox(chat_id, text, markup, photo, thread_id, post_id) VALUES(?, ?, ?, ?, ?, ?)",
                      (chat, p["text"] or " ", markup, p["photo"], thread, pid))
        c.execute("UPDATE posts SET status='sent', sent_at=?, total=? WHERE id=?",
                  (core.now().strftime(core.FMT), len(rec), pid))
        if p["to_news"] and p["text"]:
            c.execute("INSERT INTO news(text, created_at) VALUES(?, ?)", (p["text"], core.now().strftime(core.FMT)))
    return len(rec)


def dispatch_due() -> None:
    """Из цикла бота: отправить запланированные посты, время которых пришло."""
    with core.db() as c:
        due = [r["id"] for r in c.execute("SELECT id FROM posts WHERE status='scheduled' AND send_at<=?",
                                          (core.now().strftime(core.FMT),))]
    for pid in due:
        dispatch(pid)


def cancel(owner: int, pid: int):
    with core.db() as c:
        p = c.execute("SELECT * FROM posts WHERE id=? AND status='scheduled'", (pid,)).fetchone()
        if not p:
            return False, "Пост уже отправлен или не найден."
        c.execute("UPDATE posts SET status='cancelled' WHERE id=?", (pid,))
    return True, "Запланированный пост отменён."


def history(limit: int = 20) -> list:
    out = []
    names = {"clients": "клиенты", "staff": "сотрудники"}
    titles = {f"chat:{ch['id']}": ch["title"] for ch in chats()}
    with core.db() as c:
        for p in c.execute("SELECT * FROM posts WHERE status!='cancelled' ORDER BY id DESC LIMIT ?", (limit,)):
            d = dict(p)
            st = c.execute("SELECT SUM(sent=1 AND COALESCE(failed,0)=0), SUM(COALESCE(failed,0)), SUM(sent=0) "
                           "FROM outbox WHERE post_id=?", (p["id"],)).fetchone()
            d["delivered"], d["failed"], d["queued"] = st[0] or 0, st[1] or 0, st[2] or 0
            d["audience_titles"] = [names.get(a) or titles.get(a, a) for a in json.loads(p["audience"])]
            d["has_photo"] = bool(p["photo"])
            d["when"] = datetime.strptime(p["send_at"], core.FMT).strftime("%d.%m %H:%M")
            d.pop("photo", None)
            out.append(d)
    return out
