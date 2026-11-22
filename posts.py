"""Постинг: руководитель пишет пост в кабинете и рассылает его через бота.

Пост: текст на трёх языках (uz / ru / en — заполнять можно не все),
необязательное фото, необязательная кнопка-ссылка.
Каждый получатель получает пост на своём языке:
  клиенты — на языке, который выбрали при входе в бота;
  сотрудники — на языке, который выбрали в боте (uz / ru);
  группы и каналы — на языке из POST_CHATS (по умолчанию узбекский).
Если перевода на нужный язык нет — на заполненном (uz → ru → en).

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
import i18n

DIR = os.path.join(core._DATA_DIR, "posts")
TEXT_MAX, CAPTION_MAX = 4000, 1024


def chats() -> list:
    """Группы и каналы для постов: [{'id': -100..., 'title': '...', 'lang': 'uz'}]"""
    out = []
    for ch in getattr(config, "POST_CHATS", []):
        out.append({"id": int(ch["chat_id"]), "title": ch.get("title") or str(ch["chat_id"]),
                    "thread_id": ch.get("thread_id"), "lang": i18n.lang_of(ch.get("lang"))})
    return out


def audience_info() -> dict:
    with core.db() as c:
        langs = {k: 0 for k in i18n.LANGS}
        staff = set(config.EMPLOYEES) | set(config.ADMINS)
        total = 0
        for r in c.execute("SELECT user_id, lang FROM clients"):
            if r["user_id"] in staff:
                continue
            total += 1
            langs[i18n.lang_of(r["lang"])] += 1
    return {"clients": total, "client_langs": langs, "staff": len(config.EMPLOYEES), "chats": chats()}


def _recipients(audience: list) -> list:
    """[(chat_id, thread_id, lang), ...] без повторов."""
    out, seen = [], set()
    staff = set(config.EMPLOYEES) | set(config.ADMINS)

    def add(chat, thread, lang):
        if chat not in seen:
            seen.add(chat)
            out.append((chat, thread, lang))
    if "clients" in audience:
        with core.db() as c:
            for r in c.execute("SELECT user_id, lang FROM clients ORDER BY created_at"):
                if r["user_id"] not in staff:
                    add(r["user_id"], None, i18n.lang_of(r["lang"]))
    if "staff" in audience:
        for uid in config.EMPLOYEES:
            add(uid, None, core.staff_lang(uid))
    known = {ch["id"]: ch for ch in chats()}
    for a in audience:
        if a.startswith("chat:"):
            ch = known.get(int(a[5:]))
            if ch:
                add(ch["id"], ch["thread_id"], ch["lang"])
    return out


def _texts(texts, text="") -> dict:
    """Тексты по языкам; для совместимости принимает и один текст (как узбекский)."""
    t = i18n.clean_texts(texts)
    if not t and str(text or "").strip():
        t = {i18n.DEFAULT: str(text).strip()}
    return t


def _check(texts: dict, photo: bool, btn_text: str, btn_url: str):
    if not texts and not photo:
        return "Напишите текст поста хотя бы на одном языке или добавьте фото."
    limit = CAPTION_MAX if photo else TEXT_MAX
    for lang, t in texts.items():
        if len(t) > limit:
            return f"Текст ({lang.upper()}) слишком длинный: максимум {limit} символов" + (" с фото." if photo else ".")
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


def test(owner: int, texts, photo: str, btn_text: str, btn_url: str, text: str = ""):
    """Предпросмотр: прислать себе пост на каждом заполненном языке."""
    btn_text, btn_url = str(btn_text or "").strip()[:60], str(btn_url or "").strip()[:500]
    texts = _texts(texts, text)
    err = _check(texts, bool(photo), btn_text, btn_url)
    if err:
        return False, err
    with core.db() as c:
        for lang, t in (texts.items() or [("", " ")]):
            c.execute("INSERT INTO outbox(chat_id, text, markup, photo) VALUES(?, ?, ?, ?)",
                      (owner, t, _markup(btn_text, btn_url), photo))
    langs = ", ".join(k.upper() for k in texts) or "фото"
    return True, f"👁 Пост отправлен вам в бот ({langs}) — посмотрите, как он выглядит."


def create(owner: int, texts, photo: str, btn_text: str, btn_url: str, audience: list,
           to_news: bool = False, send_at: str = "", text: str = ""):
    btn_text, btn_url = str(btn_text or "").strip()[:60], str(btn_url or "").strip()[:500]
    texts = _texts(texts, text)
    err = _check(texts, bool(photo), btn_text, btn_url)
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
            "INSERT INTO posts(owner_id, text, texts, photo, button_text, button_url, audience, to_news, status, "
            "send_at, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)",
            (owner, i18n.pick(texts, "ru"), json.dumps(texts, ensure_ascii=False), photo, btn_text, btn_url,
             json.dumps(audience), int(bool(to_news)), (when or core.now()).strftime(core.FMT),
             core.now().strftime(core.FMT)))
        pid = cur.lastrowid
    if when:
        return True, f"🕒 Пост запланирован на {when:%d.%m %H:%M}"
    n = dispatch(pid)
    return True, f"📣 Пост отправляется: получателей {n}"


def _post_texts(p) -> dict:
    return json.loads(p["texts"]) if p["texts"] else _texts(None, p["text"])


def dispatch(pid: int) -> int:
    """Поставить пост в очередь отправки — каждому на его языке. Возвращает число получателей."""
    with core.db() as c:
        p = c.execute("SELECT * FROM posts WHERE id=? AND status='scheduled'", (pid,)).fetchone()
        if not p:
            return 0
        texts = _post_texts(p)
        rec = _recipients(json.loads(p["audience"]))
        markup = _markup(p["button_text"], p["button_url"])
        for chat, thread, lang in rec:
            c.execute("INSERT INTO outbox(chat_id, text, markup, photo, thread_id, post_id) VALUES(?, ?, ?, ?, ?, ?)",
                      (chat, i18n.pick(texts, lang) or " ", markup, p["photo"], thread, pid))
        c.execute("UPDATE posts SET status='sent', sent_at=?, total=? WHERE id=?",
                  (core.now().strftime(core.FMT), len(rec), pid))
    if p["to_news"] and texts:
        core.add_news(texts=texts)
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
    names = {"clients": "бот клиентов", "staff": "бот сотрудников"}
    titles = {f"chat:{ch['id']}": ch["title"] for ch in chats()}
    with core.db() as c:
        for p in c.execute("SELECT * FROM posts WHERE status!='cancelled' ORDER BY id DESC LIMIT ?", (limit,)):
            d = dict(p)
            texts = _post_texts(p)
            st = c.execute("SELECT SUM(sent=1 AND COALESCE(failed,0)=0), SUM(COALESCE(failed,0)), SUM(sent=0) "
                           "FROM outbox WHERE post_id=?", (p["id"],)).fetchone()
            d["delivered"], d["failed"], d["queued"] = st[0] or 0, st[1] or 0, st[2] or 0
            d["audience_titles"] = [names.get(a) or titles.get(a, a) for a in json.loads(p["audience"])]
            d["text"] = i18n.pick(texts, "ru")
            d["langs"] = [k for k in i18n.LANGS if texts.get(k)]
            d["has_photo"] = bool(p["photo"])
            d["when"] = datetime.strptime(p["send_at"], core.FMT).strftime("%d.%m %H:%M")
            for k in ("photo", "texts"):
                d.pop(k, None)
            out.append(d)
    return out
