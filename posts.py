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
import erp
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
            if r["user_id"] in staff and not two_bots():
                continue
            total += 1
            langs[i18n.lang_of(r["lang"])] += 1
    segs = [{"key": "seg:" + k, "title": t, "count": len(core.segment_uids(k))} for k, t in core.SEGMENTS.items()]
    return {"clients": total, "client_langs": langs, "staff": len(config.EMPLOYEES), "chats": chats(), "segments": segs,
            "bonus_max": erp.BONUS_MAX, "bonus_on": erp.enabled()}


def two_bots() -> bool:
    return bool(getattr(config, "CLIENT_BOT_TOKEN", ""))


def _recipients(audience: list) -> list:
    """[(chat_id, thread_id, lang, via), ...] без повторов. via — через какого бота: client / staff / None.
    При двух ботах в «бот клиентов» попадают все, кто зарегистрировался в клиентском боте,
    включая сотрудников и руководителя (они получают пост именно там)."""
    out, seen = [], set()
    staff = set(config.EMPLOYEES) | set(config.ADMINS)

    def add(chat, thread, lang, via=None):
        if (chat, via) not in seen:
            seen.add((chat, via))
            out.append((chat, thread, lang, via))
    if "clients" in audience:
        with core.db() as c:
            for r in c.execute("SELECT user_id, lang FROM clients ORDER BY created_at"):
                if two_bots():
                    add(r["user_id"], None, i18n.lang_of(r["lang"]), "client")
                elif r["user_id"] not in staff:
                    add(r["user_id"], None, i18n.lang_of(r["lang"]))
    if "staff" in audience:
        for uid in config.EMPLOYEES:
            add(uid, None, core.staff_lang(uid), "staff" if two_bots() else None)
    langs = None
    for a in audience:
        if a.startswith("seg:") and a[4:] in core.SEGMENTS:
            if langs is None:
                with core.db() as c:
                    langs = {r["user_id"]: r["lang"] for r in c.execute("SELECT user_id, lang FROM clients")}
            for uid in core.segment_uids(a[4:]):
                add(uid, None, i18n.lang_of(langs.get(uid)), "client" if two_bots() else None)
    known = {ch["id"]: ch for ch in chats()}
    for a in audience:
        if a.startswith("chat:"):
            ch = known.get(int(a[5:]))
            if ch:
                add(ch["id"], ch["thread_id"], ch["lang"], "staff" if two_bots() else None)
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


def test(owner: int, texts, photo: str, btn_text: str, btn_url: str, text: str = "", audience=None):
    """Предпросмотр: прислать себе пост на каждом заполненном языке.
    Выбран «бот клиентов» и вы зарегистрированы в нём — пример придёт туда, иначе — в бот сотрудников."""
    btn_text, btn_url = str(btn_text or "").strip()[:60], str(btn_url or "").strip()[:500]
    texts = _texts(texts, text)
    err = _check(texts, bool(photo), btn_text, btn_url)
    if err:
        return False, err
    via = "client" if two_bots() and "clients" in (audience or []) and core.get_client(owner) else None
    if two_bots() and not via:
        via = "staff"
    with core.db() as c:
        for lang, t in (texts.items() or [("", " ")]):
            c.execute("INSERT INTO outbox(chat_id, text, markup, photo, via) VALUES(?, ?, ?, ?, ?)",
                      (owner, t, _markup(btn_text, btn_url), photo, via))
    langs = ", ".join(k.upper() for k in texts) or "фото"
    where = "в бот клиентов" if via == "client" else "в бот"
    return True, f"👁 Пост отправлен вам {where} ({langs}) — посмотрите, как он выглядит."


def create(owner: int, texts, photo: str, btn_text: str, btn_url: str, audience: list,
           to_news: bool = False, send_at: str = "", text: str = "", bonus=0):
    btn_text, btn_url = str(btn_text or "").strip()[:60], str(btn_url or "").strip()[:500]
    texts = _texts(texts, text)
    err = _check(texts, bool(photo), btn_text, btn_url)
    if err:
        return False, err
    valid = {"clients", "staff"} | {f"chat:{ch['id']}" for ch in chats()} | {"seg:" + k for k in core.SEGMENTS}
    audience = [a for a in dict.fromkeys(str(a) for a in (audience or [])) if a in valid]
    if not audience:
        return False, "Выберите, кому отправить пост."
    if not _recipients(audience):
        return False, "В выбранной аудитории пока никого нет."
    try:
        bonus = round(float(str(bonus or 0).replace(" ", "").replace(",", ".")), 2)
    except ValueError:
        return False, "Неверная сумма бонуса."
    if bonus < 0 or bonus > erp.BONUS_MAX:
        return False, f"Бонус — от 0 до {erp.BONUS_MAX:,} сум.".replace(",", " ")
    if bonus and not erp.enabled():
        return False, "Бонусы начисляются через магазин — он не подключён."
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
            "send_at, created_at, bonus) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)",
            (owner, i18n.pick(texts, "ru"), json.dumps(texts, ensure_ascii=False), photo, btn_text, btn_url,
             json.dumps(audience), int(bool(to_news)), (when or core.now()).strftime(core.FMT),
             core.now().strftime(core.FMT), bonus))
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
        bonus = float(p["bonus"] or 0)
        names = {r["user_id"]: core.client_first_name(dict(r)) for r in c.execute("SELECT * FROM clients")}
        for chat, thread, lang, via in rec:
            personal = chat > 0
            name = (names.get(chat) or (core.name(chat) if core.employee(chat) else "")) if personal else ""
            body = (i18n.pick(texts, lang) or " ").replace("{name}", name or {"uz": "do'stim", "ru": "друг", "en": "friend"}[lang])
            body = body.replace("{bonus}", f"{int(bonus):,}".replace(",", " ") + " " + i18n.t(lang, "currency"))
            c.execute("INSERT INTO outbox(chat_id, text, markup, photo, thread_id, post_id, via) VALUES(?, ?, ?, ?, ?, ?, ?)",
                      (chat, body, markup, p["photo"], thread, pid, via))
            if bonus and personal and chat in names and via != "staff":
                c.execute("INSERT OR IGNORE INTO bonus_grants(post_id, user_id, amount, status) VALUES(?, ?, ?, 'pending')",
                          (pid, chat, bonus))
        c.execute("UPDATE posts SET status='sent', sent_at=?, total=? WHERE id=?",
                  (core.now().strftime(core.FMT), len(rec), pid))
    if p["to_news"] and texts:
        core.add_news(texts=texts)
    if float(p["bonus"] or 0):
        erp.start_grants(pid)
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
    names = {"clients": "все в боте", "staff": "бот сотрудников", **{"seg:" + k: t for k, t in core.SEGMENTS.items()}}
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
            g = c.execute("SELECT COUNT(*), SUM(status='ok'), SUM(status='pending'), SUM(status IN ('fail', 'no_store')) "
                          "FROM bonus_grants WHERE post_id=?", (p["id"],)).fetchone()
            d["bonus_total"], d["bonus_ok"], d["bonus_wait"], d["bonus_fail"] = g[0] or 0, g[1] or 0, g[2] or 0, g[3] or 0
            d["when"] = datetime.strptime(p["send_at"], core.FMT).strftime("%d.%m %H:%M")
            for k in ("photo", "texts"):
                d.pop(k, None)
            out.append(d)
    return out
