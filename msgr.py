"""RADI Chat (приложение Telegram для рабочих аккаунтов) — серверная часть.

- Подключение телефона: руководитель в кабинете получает 6-значный код (10 минут), вводит его в RADI Chat →
  телефон получает свой токен (в базе хранится только SHA-256). Руководитель может отключить телефон.
- Настройки для приложений: разрешения (редактирование, звонки, контакты…), белый список контактов компании,
  постоянное подключение. Приложение забирает их при запуске и раз в несколько минут.
- 💎 Карточка клиента BILLZ по Telegram-ID клиента: привязка из RADI Chat / бот клиентов / номер телефона.
- Журнал нарушений (попытки удалить, отправить номер и т. п.).
Личные данные клиентов отдаются только подключённым телефонам и не уходят во внешние сервисы.
"""
import hashlib
import json
import secrets
import time
from datetime import timedelta

import core

CODE_MINUTES = 10
PERMS = {  # ключ → подпись в кабинете (порядок = порядок в кабинете)
    "edit_messages": "✏️ Редактировать сообщения",
    "share_contacts": "📇 Отправлять номера, @юзернеймы и ссылки",
    "calls": "📞 Звонить",
    "secret_chats": "🔒 Секретные чаты",
    "create_groups": "👥 Создавать группы и каналы",
}
ASSIGN_MODES = {
    "first": "Кто первым ответил — тот и ведёт",
    "round": "Раздаёт бот по очереди (кто на смене)",
    "manual": "Назначает руководитель",
}
TIMEOUT_ACTIONS = {"keep": "остаётся у менеджера", "queue": "уходит в общие «Новые»", "notify": "уведомление руководителю"}
DEFAULTS = {"perms": {k: False for k in PERMS}, "whitelist": [], "keepalive": True,
            "assign_mode": "first", "unassigned_visible": True, "timeout_min": 0, "timeout_action": "keep"}


def init() -> None:
    with core.db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS msgr_devices(
                token_hash TEXT PRIMARY KEY, name TEXT, created_at TEXT, created_by INTEGER,
                last_seen TEXT, app_version TEXT, disabled INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS msgr_codes(code TEXT PRIMARY KEY, name TEXT, created_by INTEGER, expires REAL);
            CREATE TABLE IF NOT EXISTS msgr_links(tg_id INTEGER PRIMARY KEY, billz_id TEXT, by_device TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS msgr_kv(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS msgr_assign(peer INTEGER PRIMARY KEY, staff_uid INTEGER, at TEXT, how TEXT);
            CREATE TABLE IF NOT EXISTS msgr_groups(peer INTEGER PRIMARY KEY, title TEXT, kind TEXT, seen TEXT);
            CREATE TABLE IF NOT EXISTS msgr_staff(staff_uid INTEGER PRIMARY KEY, perms TEXT, see_others INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS msgr_violations(
                id INTEGER PRIMARY KEY, device TEXT, kind TEXT, peer INTEGER, detail TEXT, at TEXT);
        """)
        for table, col in (("msgr_devices", "staff_uid"), ("msgr_codes", "staff_uid")):
            if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} INTEGER")


def staff() -> list:
    """Сотрудники и руководители — за кем можно закрепить телефон."""
    import config
    seen, out = set(), []
    for uid in list(getattr(config, "EMPLOYEES", [])) + list(getattr(config, "ADMINS", [])):
        if uid not in seen:
            seen.add(uid)
            out.append({"uid": uid, "name": core.name(uid)})
    return out


def _now() -> str:
    return core.now().strftime(core.FMT)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- настройки ----------

def settings() -> dict:
    with core.db() as c:
        r = c.execute("SELECT value FROM msgr_kv WHERE key='settings'").fetchone()
    s = json.loads(r["value"]) if r else {}
    out = json.loads(json.dumps(DEFAULTS))
    out["perms"].update({k: bool(v) for k, v in (s.get("perms") or {}).items() if k in PERMS})
    out["whitelist"] = [str(x).strip() for x in (s.get("whitelist") or []) if str(x).strip()][:200]
    out["keepalive"] = bool(s.get("keepalive", True))
    out["assign_mode"] = s.get("assign_mode") if s.get("assign_mode") in ASSIGN_MODES else "first"
    out["unassigned_visible"] = bool(s.get("unassigned_visible", True))
    out["timeout_min"] = max(0, min(1440, int(s.get("timeout_min") or 0)))
    out["timeout_action"] = s.get("timeout_action") if s.get("timeout_action") in TIMEOUT_ACTIONS else "keep"
    return out


def save_settings(perms: dict = None, whitelist=None, keepalive=None, assign: dict = None) -> dict:
    s = settings()
    if perms is not None:
        s["perms"].update({k: bool(v) for k, v in perms.items() if k in PERMS})
    if whitelist is not None:
        if isinstance(whitelist, str):
            whitelist = whitelist.replace(",", "\n").splitlines()
        s["whitelist"] = [str(x).strip() for x in whitelist if str(x).strip()][:200]
    if keepalive is not None:
        s["keepalive"] = bool(keepalive)
    for k, v in (assign or {}).items():
        if k in ("assign_mode", "unassigned_visible", "timeout_min", "timeout_action"):
            s[k] = v
    with core.db() as c:
        c.execute("INSERT OR REPLACE INTO msgr_kv(key, value) VALUES('settings', ?)", (json.dumps(s, ensure_ascii=False),))
    return s


# ---------- подключение телефонов ----------

KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих 0/O, 1/I


def _norm_code(s) -> str:
    return "".join(ch for ch in str(s or "").upper() if ch.isalnum())


def fmt_key(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


def staff_number(staff_uid: int):
    """На каком рабочем номере сейчас работает сотрудник (по включённым телефонам)."""
    with core.db() as c:
        r = c.execute("SELECT number_id FROM msgr_devices WHERE staff_uid=? AND disabled=0 AND number_id IS NOT NULL "
                      "LIMIT 1", (staff_uid,)).fetchone()
    return r["number_id"] if r else None


def new_code(admin_uid: int, staff_uid: int = 0, name: str = "", number_id: int = 0) -> dict:
    """Ключ доступа для сотрудника staff_uid на рабочем номере number_id (8 символов, 10 минут, один раз).
    Один сотрудник — один номер: пока у него включён телефон на другом номере, ключ не выдаётся."""
    staff_uid, number_id = int(staff_uid or 0), int(number_id or 0)
    if staff_uid and staff_uid not in {x["uid"] for x in staff()}:
        staff_uid = 0
    if number_id:
        busy = staff_number(staff_uid) if staff_uid else None
        if busy and busy != number_id:
            return {"ok": False, "error": f"{core.name(staff_uid)} уже работает на другом рабочем номере. "
                                          "Сначала отключите его телефон там."}
    name = core.name(staff_uid) if staff_uid else (name or "").strip()[:60]
    code = "".join(secrets.choice(KEY_ALPHABET) for _ in range(8))
    with core.db() as c:
        c.execute("DELETE FROM msgr_codes WHERE expires < ?", (time.time(),))
        c.execute("INSERT OR REPLACE INTO msgr_codes(code, name, created_by, expires, staff_uid, number_id) "
                  "VALUES(?, ?, ?, ?, ?, ?)",
                  (code, name, admin_uid, time.time() + CODE_MINUTES * 60, staff_uid or None, number_id or None))
    return {"ok": True, "code": fmt_key(code), "minutes": CODE_MINUTES, "name": name}


_pair_fails = {}
_all_fails = []


def _code_row(code: str, ip: str):
    """Строка ключа или (None, ошибка). Не больше 10 ошибок в час с адреса и 60 в час всего."""
    now = time.time()
    fails = [t for t in _pair_fails.get(ip, []) if now - t < 3600]
    _all_fails[:] = [t for t in _all_fails if now - t < 3600]
    if len(fails) >= 10 or len(_all_fails) >= 60:
        return None, "Слишком много попыток. Попробуйте через час."
    with core.db() as c:
        r = c.execute("SELECT * FROM msgr_codes WHERE code=? AND expires >= ?", (_norm_code(code), now)).fetchone()
    if not r:
        _pair_fails[ip] = fails + [now]
        _all_fails.append(now)
        return None, "Ключ неверный или устарел. Получите новый у руководителя."
    return dict(r), ""


def _new_device(r: dict, device_name: str, auth_hash: str = None) -> dict:
    token = secrets.token_urlsafe(32)
    name = r["name"] or (device_name or "").strip()[:60] or "Телефон"
    with core.db() as c:
        c.execute("DELETE FROM msgr_codes WHERE code=?", (r["code"],))
        c.execute("INSERT INTO msgr_devices(token_hash, name, created_at, created_by, last_seen, staff_uid, number_id, auth_hash) "
                  "VALUES(?, ?, ?, ?, ?, ?, ?, ?)", (_hash(token), name, _now(), r["created_by"], _now(), r["staff_uid"],
                                                     r.get("number_id"), auth_hash))
    return {"ok": True, "token": token, "name": name}


def pair(code: str, device_name: str = "", ip: str = "") -> dict:
    """Ключ → токен телефона (телефон уже вошёл в рабочий аккаунт сам)."""
    r, err = _code_row(code, ip)
    return _new_device(r, device_name) if r else {"ok": False, "error": err}


def keylogin(code: str, login_token: str, device_name: str = "", ip: str = "") -> dict:
    """Вход телефона в рабочий аккаунт только по ключу: сервер подтверждает токен входа от имени рабочего номера."""
    r, err = _code_row(code, ip)
    if not r:
        return {"ok": False, "error": err}
    if not r.get("number_id"):
        return {"ok": False, "error": "Ключ выдан без рабочего номера. Получите новый ключ в кабинете."}
    import tgnumbers
    acc = tgnumbers.accept_login_token(r["number_id"], login_token)
    if not acc.get("ok"):
        return acc
    out = _new_device(r, device_name, acc["auth_hash"])
    out["number_id"] = r["number_id"]
    return out


def device(token: str, version: str = ""):
    """Подключённый и не отключённый телефон по токену (и отметка «был онлайн»)."""
    if not token:
        return None
    h = _hash(token)
    with core.db() as c:
        r = c.execute("SELECT * FROM msgr_devices WHERE token_hash=?", (h,)).fetchone()
        if not r or r["disabled"]:
            return None
        c.execute("UPDATE msgr_devices SET last_seen=?, app_version=COALESCE(NULLIF(?, ''), app_version) WHERE token_hash=?",
                  (_now(), version[:40], h))
    return dict(r)


def devices() -> list:
    with core.db() as c:
        rows = c.execute("SELECT * FROM msgr_devices ORDER BY disabled, created_at DESC").fetchall()
    return [{"id": r["token_hash"][:12], "name": core.name(r["staff_uid"]) if r["staff_uid"] else r["name"],
             "staff_uid": r["staff_uid"] or 0, "number_id": r["number_id"] if "number_id" in r.keys() else None,
             "since": (r["created_at"] or "")[:16],
             "seen": (r["last_seen"] or "")[:16], "version": r["app_version"] or "", "disabled": bool(r["disabled"])}
            for r in rows]


def set_disabled(dev_id: str, disabled: bool) -> bool:
    """Отключить телефон: RADI перестаёт работать, а если вход был по ключу — завершается и сеанс Telegram."""
    dev_id = "".join(ch for ch in str(dev_id) if ch in "0123456789abcdef")[:12]
    if len(dev_id) < 12:
        return False
    with core.db() as c:
        r = c.execute("SELECT number_id, auth_hash FROM msgr_devices WHERE substr(token_hash, 1, 12)=?", (dev_id,)).fetchone()
        ok = c.execute("UPDATE msgr_devices SET disabled=? WHERE substr(token_hash, 1, 12)=?",
                       (1 if disabled else 0, dev_id)).rowcount > 0
    if ok and disabled and r and r["number_id"] and r["auth_hash"]:
        import tgnumbers
        if tgnumbers.reset_authorization(r["number_id"], r["auth_hash"]):
            with core.db() as c:  # сеанс Telegram завершён — включить обратно можно только новым ключом
                c.execute("UPDATE msgr_devices SET auth_hash=NULL WHERE substr(token_hash, 1, 12)=?", (dev_id,))
    return ok


# ---------- 💎 карточка клиента ----------

def _tail(phone) -> str:
    d = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return d[-9:] if len(d) >= 9 else ""


def _find_billz_id(tg_id: int, phone: str = ""):
    """(billz_id, источник) — привязка RADI Chat → бот клиентов → номер телефона."""
    with core.db() as c:
        r = c.execute("SELECT billz_id FROM msgr_links WHERE tg_id=?", (tg_id,)).fetchone()
        if r and r["billz_id"]:
            return r["billz_id"], "link"
        bot = c.execute("SELECT billz_id, phone FROM clients WHERE user_id=?", (tg_id,)).fetchone()
        if bot and bot["billz_id"]:
            return bot["billz_id"], "bot"
        for p in (phone, bot["phone"] if bot else ""):
            t = _tail(p)
            if t:
                r = c.execute("SELECT id FROM billz_clients WHERE phone_tail=? OR phones LIKE ? LIMIT 1",
                              (t, f"%{t}%")).fetchone()
                if r:
                    return r["id"], "phone"
    return None, None


def card(tg_id: int, phone: str = "") -> dict:
    bid, src = _find_billz_id(tg_id, phone)
    notes = []
    with core.db() as c:
        for r in c.execute("SELECT uid, text, at FROM crm_notes WHERE chat_id=? ORDER BY id DESC LIMIT 20", (tg_id,)):
            notes.append({"text": r["text"], "who": core.name(r["uid"]), "at": (r["at"] or "")[:16]})
        bot = c.execute("SELECT * FROM clients WHERE user_id=?", (tg_id,)).fetchone()
        local = c.execute("SELECT * FROM billz_clients WHERE id=?", (bid,)).fetchone() if bid else None
        stier = c.execute("SELECT tier FROM store_tiers WHERE billz_id=?", (bid,)).fetchone() if bid else None
    if not bid:
        return {"found": False, "notes": notes, "in_bot": bool(bot)}
    tier = core.tier_of(dict(bot)) if bot else (stier["tier"] if stier and stier["tier"] in core.CLIENT_TIERS else "bronza")
    out = {"found": True, "source": src, "billz_id": bid, "tier": tier, "tier_title": core.tier_title(tier),
           "notes": notes, "in_bot": bool(bot)}
    if local:
        out.update({"name": " ".join(v for v in (local["first_name"], local["last_name"]) if v) or "Без имени",
                    "phone": local["phone"] or "", "balance": local["balance"] or 0, "last_tx": (local["last_tx"] or "")[:10]})
    try:  # свежие цифры из BILLZ (если недоступен — остаётся локальная копия)
        import erp
        if erp.enabled():
            b = erp.customer_card(bid)
            name = " ".join(v for v in (b.get("first_name"), b.get("last_name")) if v)
            out.update({k: v for k, v in {
                "name": name or out.get("name"),
                "phone": (b.get("phone_numbers") or [out.get("phone")])[0],
                "balance": b.get("balance"), "purchases": b.get("purchase_amount"), "debt": b.get("debt_amount"),
                "visits": b.get("visits_count"), "avg": b.get("average_purchase_amount"),
                "first_tx": str(b.get("first_purchase_date") or "")[:10], "last_tx": str(b.get("last_purchase_date") or "")[:10],
                "shop": b.get("registered_shop_name"), "billz_notes": (b.get("notes") or [])[:10],
            }.items() if v not in (None, "")})
    except Exception as e:  # noqa: BLE001
        out["billz_error"] = str(e)[:200]
    return out


def search(q: str) -> list:
    res = core.billz_base(q, 0, 20)
    items = []
    for x in res.get("items", []):
        bid = x.get("store_id")
        if not bid and x.get("bot_id"):
            with core.db() as c:
                r = c.execute("SELECT billz_id FROM clients WHERE user_id=?", (x["bot_id"],)).fetchone()
            bid = r["billz_id"] if r else None
        if bid:
            items.append({"billz_id": bid, "name": x.get("name") or "Без имени", "phone": x.get("phone") or "",
                          "last_tx": (x.get("last_tx") or "")[:10]})
    return items[:20]


def link(tg_id: int, billz_id: str, dev: dict) -> None:
    with core.db() as c:
        if billz_id:
            c.execute("INSERT OR REPLACE INTO msgr_links(tg_id, billz_id, by_device, at) VALUES(?, ?, ?, ?)",
                      (tg_id, billz_id, dev_name(dev), _now()))
        else:
            c.execute("DELETE FROM msgr_links WHERE tg_id=?", (tg_id,))


# ---------- журнал нарушений ----------

def dev_name(dev: dict) -> str:
    dev = dev or {}
    return core.name(dev["staff_uid"]) if dev.get("staff_uid") else dev.get("name", "")


def violation(dev: dict, kind: str, peer: int = 0, detail: str = "") -> None:
    with core.db() as c:
        c.execute("INSERT INTO msgr_violations(device, kind, peer, detail, at) VALUES(?, ?, ?, ?, ?)",
                  (dev_name(dev), str(kind)[:40], int(peer or 0), str(detail)[:300], _now()))
        c.execute("DELETE FROM msgr_violations WHERE at < ?",
                  ((core.now() - timedelta(days=90)).strftime(core.FMT),))


def violations(limit: int = 100) -> list:
    with core.db() as c:
        rows = c.execute("SELECT * FROM msgr_violations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        names = {r["chat_id"]: r["chat_name"] for r in c.execute(
            "SELECT chat_id, chat_name FROM biz_messages WHERE chat_id IN (%s) GROUP BY chat_id" %
            ",".join("?" * len(rows)), [r["peer"] for r in rows])} if rows else {}
    return [{"device": r["device"], "kind": r["kind"], "peer": names.get(r["peer"]) or (str(r["peer"]) if r["peer"] else ""),
             "detail": r["detail"], "at": (r["at"] or "")[:16]} for r in rows]


# ---------- удалённое, пока телефон был офлайн (копия от бизнес-бота) ----------

def deleted(chat_id: int, limit: int = 100) -> list:
    with core.db() as c:
        rows = c.execute("SELECT msg_id, outgoing, kind, text, sent_at, deleted_at FROM biz_messages "
                         "WHERE chat_id=? AND deleted_at IS NOT NULL ORDER BY msg_id DESC LIMIT ?", (chat_id, limit)).fetchall()
    return [{"id": r["msg_id"], "out": bool(r["outgoing"]), "kind": r["kind"] or "text", "text": r["text"] or "",
             "sent": (r["sent_at"] or "")[:16], "deleted": (r["deleted_at"] or "")[:16]} for r in rows]


# ---------- назначение чатов сотрудникам ----------
# peer: id чата в Telegram (личный чат — id человека > 0; группа/канал — отрицательный, как в Telegram Bot API).
# Личные чаты без назначения видны всем (если unassigned_visible) — это очередь «Новые».
# Группы и каналы — только назначенным.

def staff_perms(staff_uid: int) -> dict:
    """Доступы сотрудника: свои (если заданы) поверх общих «по умолчанию»; see_others: 0 — не видит чужие, 1 — только чтение."""
    base = settings()["perms"]
    with core.db() as c:
        r = c.execute("SELECT * FROM msgr_staff WHERE staff_uid=?", (staff_uid or 0,)).fetchone()
    own = json.loads(r["perms"]) if r and r["perms"] else None
    out = dict(base)
    if own is not None:
        out.update({k: bool(v) for k, v in own.items() if k in PERMS})
    return {"perms": out, "custom": own is not None, "see_others": int(r["see_others"] or 0) if r else 0}


def save_staff(staff_uid: int, perms=None, see_others=None, reset: bool = False) -> None:
    cur = staff_perms(staff_uid)
    own = None if reset else ({k: bool(v) for k, v in (perms or {}).items() if k in PERMS} if perms is not None
                              else (cur["perms"] if cur["custom"] else None))
    see = int(see_others) if see_others is not None else cur["see_others"]
    with core.db() as c:
        c.execute("INSERT OR REPLACE INTO msgr_staff(staff_uid, perms, see_others) VALUES(?, ?, ?)",
                  (staff_uid, json.dumps(own) if own is not None else None, see))


def assign(peer: int, staff_uid: int, how: str = "admin") -> None:
    with core.db() as c:
        if staff_uid:
            c.execute("INSERT OR REPLACE INTO msgr_assign(peer, staff_uid, at, how) VALUES(?, ?, ?, ?)",
                      (int(peer), int(staff_uid), _now(), how))
        else:
            c.execute("DELETE FROM msgr_assign WHERE peer=?", (int(peer),))


def owner_of(peer: int):
    with core.db() as c:
        r = c.execute("SELECT staff_uid FROM msgr_assign WHERE peer=?", (int(peer),)).fetchone()
    return r["staff_uid"] if r else None


def claim(dev: dict, peer: int) -> dict:
    """Менеджер пишет клиенту: в режиме «кто первый» свободный чат закрепляется за ним."""
    me = (dev or {}).get("staff_uid") or 0
    who = owner_of(peer)
    if who == me and me:
        return {"ok": True, "mine": True}
    if who:
        return {"ok": False, "taken": True, "by": core.name(who)}
    if settings()["assign_mode"] == "first" and me and peer > 0:
        assign(peer, me, "first")
        return {"ok": True, "mine": True, "claimed": True}
    return {"ok": False, "error": "Этот чат вам не назначен."}


def assignments_for(dev: dict) -> dict:
    """Для приложения: какие чаты мои, какие чужие и как вести себя со свободными."""
    me = (dev or {}).get("staff_uid") or 0
    s = settings()
    with core.db() as c:
        rows = c.execute("SELECT peer, staff_uid FROM msgr_assign").fetchall()
    sp = staff_perms(me)
    return {"enabled": bool(me), "mode": s["assign_mode"], "unassigned_visible": s["unassigned_visible"],
            "see_others": sp["see_others"],
            "mine": [r["peer"] for r in rows if r["staff_uid"] == me],
            "others": [r["peer"] for r in rows if r["staff_uid"] != me]}


def _on_shift(uid: int) -> bool:
    try:
        with core.db() as c:
            return c.execute("SELECT 1 FROM sessions WHERE user_id=? AND check_out IS NULL", (uid,)).fetchone() is not None
    except Exception:  # noqa: BLE001
        return True


def on_incoming(peer: int) -> None:
    """Новое сообщение клиента (из бизнес-бота). Режим «по очереди»: свободный личный чат — следующему на смене."""
    s = settings()
    if s["assign_mode"] != "round" or peer <= 0 or owner_of(peer):
        return
    with core.db() as c:
        devs = c.execute("SELECT DISTINCT staff_uid FROM msgr_devices WHERE disabled=0 AND staff_uid IS NOT NULL").fetchall()
        cands = sorted(r["staff_uid"] for r in devs)
        if not cands:
            return
        on = [u for u in cands if _on_shift(u)] or cands
        r = c.execute("SELECT value FROM msgr_kv WHERE key='round_last'").fetchone()
        last = int(r["value"]) if r else 0
        nxt = next((u for u in on if u > last), on[0])
        c.execute("INSERT OR REPLACE INTO msgr_kv(key, value) VALUES('round_last', ?)", (str(nxt),))
    assign(peer, nxt, "round")


def report_groups(items: list) -> None:
    """Приложение сообщает группы и каналы рабочего аккаунта (бизнес-бот видит только личные чаты)."""
    with core.db() as c:
        for it in (items or [])[:500]:
            try:
                peer = int(it.get("id"))
            except (TypeError, ValueError):
                continue
            if peer >= 0:
                continue
            c.execute("INSERT OR REPLACE INTO msgr_groups(peer, title, kind, seen) VALUES(?, ?, ?, ?)",
                      (peer, str(it.get("title") or "")[:120], str(it.get("kind") or "group")[:10], _now()))


def chats_admin(q: str = "", limit: int = 100) -> list:
    """Для кабинета: последние чаты рабочих аккаунтов и кому назначены."""
    q = (q or "").strip().lower()
    with core.db() as c:
        rows = c.execute("SELECT chat_id, MAX(chat_name) AS name, MAX(chat_username) AS un, MAX(sent_at) AS last "
                         "FROM biz_messages GROUP BY chat_id ORDER BY last DESC LIMIT 500").fetchall()
        assigned = {r["peer"]: r["staff_uid"] for r in c.execute("SELECT peer, staff_uid FROM msgr_assign")}
        groups = c.execute("SELECT peer, title, kind, seen FROM msgr_groups ORDER BY title").fetchall()
    out = []
    for g in groups:  # группы и каналы — сверху: по умолчанию их не видит никто
        if q and q not in (g["title"] or "").lower():
            continue
        who = assigned.get(g["peer"])
        out.append({"peer": g["peer"], "name": ("📢 " if g["kind"] == "channel" else "👥 ") + (g["title"] or str(g["peer"])),
                    "username": "", "last": "", "staff_uid": who or 0, "staff": core.name(who) if who else "", "group": True})
    for r in rows:
        name = r["name"] or str(r["chat_id"])
        if q and q not in f"{name} {r['un'] or ''}".lower():
            continue
        who = assigned.get(r["chat_id"])
        out.append({"peer": r["chat_id"], "name": name, "username": r["un"] or "", "last": (r["last"] or "")[:16],
                    "staff_uid": who or 0, "staff": core.name(who) if who else ""})
        if len(out) >= limit:
            break
    return out


_last_timeout_check = 0.0


def check_timeouts() -> None:
    """Раз в минуту: клиент ждёт ответа дольше N минут → действие из настроек (в «Новые» или уведомление руководителю)."""
    global _last_timeout_check
    if time.time() - _last_timeout_check < 60:
        return
    _last_timeout_check = time.time()
    s = settings()
    if not s["timeout_min"] or s["timeout_action"] == "keep":
        return
    limit = (core.now() - timedelta(minutes=s["timeout_min"])).strftime(core.FMT)
    with core.db() as c:
        rows = c.execute("SELECT a.peer, a.staff_uid, m.chat_name, m.sent_at FROM msgr_assign a "
                         "JOIN biz_messages m ON m.chat_id = a.peer "
                         "WHERE a.peer > 0 AND m.sent_at = (SELECT MAX(sent_at) FROM biz_messages WHERE chat_id = a.peer) "
                         "AND m.outgoing = 0 AND m.sent_at <= ? GROUP BY a.peer", (limit,)).fetchall()
        for r in rows:
            key = f"timeout:{r['peer']}"
            done = c.execute("SELECT value FROM msgr_kv WHERE key=?", (key,)).fetchone()
            if done and done["value"] == r["sent_at"]:
                continue  # по этому сообщению клиента уже сработало
            c.execute("INSERT OR REPLACE INTO msgr_kv(key, value) VALUES(?, ?)", (key, r["sent_at"]))
            who = core.name(r["staff_uid"])
            if s["timeout_action"] == "queue":
                c.execute("DELETE FROM msgr_assign WHERE peer=?", (r["peer"],))
                text = f"⏱ RADI Chat: {who} не ответил(а) клиенту {r['chat_name']} {s['timeout_min']} мин — чат возвращён в «Новые»."
            else:
                text = f"⏱ RADI Chat: клиент {r['chat_name']} ждёт ответа больше {s['timeout_min']} мин. Менеджер: {who}."
            core.notify_admins(c, text)
