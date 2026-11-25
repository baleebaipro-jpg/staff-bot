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
DEFAULTS = {"perms": {k: False for k in PERMS}, "whitelist": [], "keepalive": True}


def init() -> None:
    with core.db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS msgr_devices(
                token_hash TEXT PRIMARY KEY, name TEXT, created_at TEXT, created_by INTEGER,
                last_seen TEXT, app_version TEXT, disabled INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS msgr_codes(code TEXT PRIMARY KEY, name TEXT, created_by INTEGER, expires REAL);
            CREATE TABLE IF NOT EXISTS msgr_links(tg_id INTEGER PRIMARY KEY, billz_id TEXT, by_device TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS msgr_kv(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS msgr_violations(
                id INTEGER PRIMARY KEY, device TEXT, kind TEXT, peer INTEGER, detail TEXT, at TEXT);
        """)


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
    return out


def save_settings(perms: dict = None, whitelist=None, keepalive=None) -> dict:
    s = settings()
    if perms is not None:
        s["perms"].update({k: bool(v) for k, v in perms.items() if k in PERMS})
    if whitelist is not None:
        if isinstance(whitelist, str):
            whitelist = whitelist.replace(",", "\n").splitlines()
        s["whitelist"] = [str(x).strip() for x in whitelist if str(x).strip()][:200]
    if keepalive is not None:
        s["keepalive"] = bool(keepalive)
    with core.db() as c:
        c.execute("INSERT OR REPLACE INTO msgr_kv(key, value) VALUES('settings', ?)", (json.dumps(s, ensure_ascii=False),))
    return s


# ---------- подключение телефонов ----------

def new_code(admin_uid: int, name: str = "") -> dict:
    code = f"{secrets.randbelow(10 ** 6):06d}"
    with core.db() as c:
        c.execute("DELETE FROM msgr_codes WHERE expires < ?", (time.time(),))
        c.execute("INSERT OR REPLACE INTO msgr_codes(code, name, created_by, expires) VALUES(?, ?, ?, ?)",
                  (code, (name or "").strip()[:60], admin_uid, time.time() + CODE_MINUTES * 60))
    return {"code": code, "minutes": CODE_MINUTES}


_pair_fails = {}


def pair(code: str, device_name: str = "", ip: str = "") -> dict:
    """Код из кабинета → токен телефона. Не больше 10 неверных попыток в час с одного адреса."""
    now = time.time()
    fails = [t for t in _pair_fails.get(ip, []) if now - t < 3600]
    if len(fails) >= 10:
        return {"ok": False, "error": "Слишком много попыток. Попробуйте через час."}
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    with core.db() as c:
        r = c.execute("SELECT * FROM msgr_codes WHERE code=? AND expires >= ?", (code, now)).fetchone()
        if not r:
            _pair_fails[ip] = fails + [now]
            return {"ok": False, "error": "Код неверный или устарел. Получите новый в кабинете."}
        c.execute("DELETE FROM msgr_codes WHERE code=?", (code,))
        token = secrets.token_urlsafe(32)
        name = r["name"] or (device_name or "").strip()[:60] or "Телефон"
        c.execute("INSERT INTO msgr_devices(token_hash, name, created_at, created_by, last_seen) VALUES(?, ?, ?, ?, ?)",
                  (_hash(token), name, _now(), r["created_by"], _now()))
    return {"ok": True, "token": token, "name": name}


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
    return [{"id": r["token_hash"][:12], "name": r["name"], "since": (r["created_at"] or "")[:16],
             "seen": (r["last_seen"] or "")[:16], "version": r["app_version"] or "", "disabled": bool(r["disabled"])}
            for r in rows]


def set_disabled(dev_id: str, disabled: bool) -> bool:
    dev_id = "".join(ch for ch in str(dev_id) if ch in "0123456789abcdef")[:12]
    if len(dev_id) < 12:
        return False
    with core.db() as c:
        return c.execute("UPDATE msgr_devices SET disabled=? WHERE substr(token_hash, 1, 12)=?",
                         (1 if disabled else 0, dev_id)).rowcount > 0


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
                      (tg_id, billz_id, (dev or {}).get("name", ""), _now()))
        else:
            c.execute("DELETE FROM msgr_links WHERE tg_id=?", (tg_id,))


# ---------- журнал нарушений ----------

def violation(dev: dict, kind: str, peer: int = 0, detail: str = "") -> None:
    with core.db() as c:
        c.execute("INSERT INTO msgr_violations(device, kind, peer, detail, at) VALUES(?, ?, ?, ?, ?)",
                  ((dev or {}).get("name", ""), str(kind)[:40], int(peer or 0), str(detail)[:300], _now()))
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
