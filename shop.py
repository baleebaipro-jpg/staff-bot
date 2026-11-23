"""ERP XUPING — свой учёт продаж, баллов и бонусов (этап 1).

Точки продаж, роли и права сотрудников, типы оплаты, настройки лояльности,
ввод продажи кассиром, баллы лояльности (1 балл = 1 сум) и бонусы
(для Uzum Nasiya / рассрочки — заморожены на 6 месяцев, размораживает владелец вручную).

Клиент ERP — по номеру телефона (shop_clients), связан с клиентом бота (clients.user_id),
если тот зарегистрирован с этим номером. Баланс баллов — сумма проводок shop_loyalty.
"""
import json
import re
from datetime import datetime, timedelta

import config
import core
import i18n

ROLES = {"owner": "Владелец", "senior": "Старший", "cashier": "Кассир", "seller": "Продавец"}
PERMS = {
    "sell": "Вводить продажи",
    "return": "Возвраты",
    "unfreeze": "Размораживать бонусы",
    "settings": "Настройки ERP",
    "delete": "Удалять продажи",
}
ROLE_PERMS = {"owner": set(PERMS), "cashier": {"sell"}, "senior": set(), "seller": set()}
PAY_KINDS = {"normal": "Обычная", "debt": "В долг", "nasiya": "Рассрочка / Uzum Nasiya (бонус замораживается)"}
FREEZE_MONTHS = 6
DEFAULT_SETTINGS = {
    "loyalty_pct": 2.0,          # % баллов лояльности по умолчанию
    "loyalty_expire_days": 0,    # сгорание баллов после N дней без покупок (0 — не сгорают)
    "spend_limit_pct": 100.0,    # сколько % покупки можно оплатить баллами
    "client_live": False,        # ERP — основной для клиентов: уведомления и баллы в кабинете (выкл. на тестовый месяц)
}
DEFAULT_PAY_TYPES = [("Наличные", "💵", "normal"), ("Uzcard", "💳", "normal"), ("Humo", "💳", "normal"),
                     ("Click", "📱", "normal"), ("Payme", "📱", "normal"), ("Перевод", "🔁", "normal"),
                     ("Uzum Nasiya", "🟣", "nasiya"), ("В долг", "📝", "debt")]


def init() -> None:
    with core.db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS shop_points(id INTEGER PRIMARY KEY, title TEXT, address TEXT, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS shop_staff(user_id INTEGER PRIMARY KEY, role TEXT, point_id INTEGER, kpi_pct REAL DEFAULT 0,
                senior_id INTEGER, senior_pct REAL DEFAULT 0, perms TEXT DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS shop_pay_types(id INTEGER PRIMARY KEY, title TEXT, icon TEXT, kind TEXT DEFAULT 'normal',
                active INTEGER DEFAULT 1, sort INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS shop_clients(id INTEGER PRIMARY KEY, phone TEXT UNIQUE, name TEXT, bot_uid INTEGER,
                note TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS shop_sales(id INTEGER PRIMARY KEY, point_id INTEGER, cashier_id INTEGER, seller_id INTEGER,
                client_id INTEGER, amount REAL, points_spent REAL DEFAULT 0, revenue REAL, loyalty_on INTEGER, loyalty_pct REAL,
                loyalty_amount REAL DEFAULT 0, bonus_amount REAL DEFAULT 0, bonus_frozen INTEGER DEFAULT 0,
                exchange INTEGER DEFAULT 0, debt_amount REAL DEFAULT 0, kpi_mode TEXT DEFAULT 'now', status TEXT DEFAULT 'ok',
                note TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS shop_sale_pay(sale_id INTEGER, pay_type_id INTEGER, amount REAL);
            CREATE TABLE IF NOT EXISTS shop_loyalty(id INTEGER PRIMARY KEY, client_id INTEGER, amount REAL, kind TEXT,
                sale_id INTEGER, note TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS shop_bonus(id INTEGER PRIMARY KEY, client_id INTEGER, sale_id INTEGER, amount REAL,
                unfreeze_at TEXT, status TEXT DEFAULT 'frozen', released_at TEXT, released_by INTEGER, reminded INTEGER DEFAULT 0);
        """)
        for tbl, col, ddl in (("shop_sales", "discount", "REAL DEFAULT 0"), ("shop_points", "billz_id", "TEXT"),
                              ("shop_pay_types", "billz_id", "TEXT")):
            if col not in {r[1] for r in c.execute(f"PRAGMA table_info({tbl})")}:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}")
        c.execute("CREATE TABLE IF NOT EXISTS shop_drafts(id INTEGER PRIMARY KEY, cashier_id INTEGER, data TEXT, title TEXT, "
                  "created_at TEXT)")
        if not c.execute("SELECT 1 FROM shop_pay_types").fetchone():
            for i, (t, ic, k) in enumerate(DEFAULT_PAY_TYPES):
                c.execute("INSERT INTO shop_pay_types(title, icon, kind, sort) VALUES(?, ?, ?, ?)", (t, ic, k, i))
        if not c.execute("SELECT 1 FROM shop_points").fetchone():
            c.execute("INSERT INTO shop_points(title, address) VALUES('Основной магазин', '')")


# ---------- настройки ----------

def settings() -> dict:
    return dict(DEFAULT_SETTINGS, **(core.flag_json("shop_settings") or {}))


def save_settings(data: dict):
    st = settings()
    try:
        for k in ("loyalty_pct", "spend_limit_pct"):
            if k in data:
                v = float(data[k])
                if not 0 <= v <= 100:
                    return False, "Процент — от 0 до 100."
                st[k] = v
        if "loyalty_expire_days" in data:
            v = int(data["loyalty_expire_days"] or 0)
            if not 0 <= v <= 3650:
                return False, "Сгорание — от 0 до 3650 дней."
            st["loyalty_expire_days"] = v
    except (TypeError, ValueError):
        return False, "Проверьте числа."
    if "client_live" in data:
        st["client_live"] = bool(data["client_live"])
    core.flag_json("shop_settings", st)
    return True, "Настройки сохранены"


# ---------- сотрудники, роли, права ----------

def staff_row(uid: int) -> dict:
    """Роль сотрудника в ERP. Руководитель бота — всегда владелец."""
    if core.is_admin(uid):
        return {"user_id": uid, "role": "owner", "point_id": None, "kpi_pct": 0, "senior_id": None, "senior_pct": 0,
                "perms": sorted(PERMS)}
    with core.db() as c:
        r = c.execute("SELECT * FROM shop_staff WHERE user_id=?", (uid,)).fetchone()
    if not r:
        return {}
    d = dict(r)
    d["perms"] = sorted(set(json.loads(d["perms"] or "[]")) | ROLE_PERMS.get(d["role"], set()))
    return d


def can(uid: int, perm: str) -> bool:
    return perm in staff_row(uid).get("perms", [])


def has_access(uid: int) -> bool:
    return bool(staff_row(uid))


def set_staff(owner: int, uid: int, role: str, point_id, kpi_pct, senior_id, senior_pct, perms):
    if not core.employee(uid):
        return False, "Сотрудник не найден."
    if role == "none":
        with core.db() as c:
            c.execute("DELETE FROM shop_staff WHERE user_id=?", (uid,))
            c.execute("UPDATE shop_staff SET senior_id=NULL WHERE senior_id=?", (uid,))
            import staff_text as st
            c.execute("INSERT INTO outbox(chat_id, text, markup, via) VALUES(?, ?, 'staff_kb', 'staff')",
                      (uid, st.s(core.staff_lang(uid), "shop_closed")))
        return True, f"{core.name(uid)}: доступ к ERP закрыт"
    if role not in ROLES or role == "owner":
        return False, "Неверная роль."
    try:
        kpi_pct, senior_pct = float(kpi_pct or 0), float(senior_pct or 0)
        point_id = int(point_id) if point_id else None
        senior_id = int(senior_id) if senior_id else None
    except (TypeError, ValueError):
        return False, "Проверьте числа."
    if not (0 <= kpi_pct <= 100 and 0 <= senior_pct <= 100):
        return False, "KPI — от 0 до 100%."
    if senior_id == uid:
        return False, "Сотрудник не может быть старшим сам себе."
    # нельзя сделать «петлю»: старший не может быть помощником своего помощника
    s = senior_id
    for _ in range(50):
        if not s:
            break
        if s == uid:
            return False, "Так получится круг: этот старший сам в команде сотрудника."
        s = (staff_row(s) or {}).get("senior_id")
    perms = sorted(p for p in (perms or []) if p in PERMS)
    with core.db() as c:
        had = c.execute("SELECT role FROM shop_staff WHERE user_id=?", (uid,)).fetchone()
        c.execute("INSERT OR REPLACE INTO shop_staff(user_id, role, point_id, kpi_pct, senior_id, senior_pct, perms) "
                  "VALUES(?, ?, ?, ?, ?, ?, ?)", (uid, role, point_id, kpi_pct, senior_id, senior_pct, json.dumps(perms)))
        if not had or had["role"] != role:  # сотруднику — сообщение и новая клавиатура с кнопкой «🧾 Продажи»
            import staff_text as st
            L = core.staff_lang(uid)
            c.execute("INSERT INTO outbox(chat_id, text, markup, via) VALUES(?, ?, 'staff_kb', 'staff')",
                      (uid, st.s(L, "shop_access", role=ROLES[role])))
    return True, f"{core.name(uid)}: {ROLES[role]}"


def staff_list() -> list:
    with core.db() as c:
        rows = {r["user_id"]: dict(r) for r in c.execute("SELECT * FROM shop_staff")}
    out = []
    for uid in config.EMPLOYEES:
        r = rows.get(uid) or {}
        out.append({"id": uid, "name": core.name(uid), "role": r.get("role") or "none", "point_id": r.get("point_id"),
                    "kpi_pct": r.get("kpi_pct") or 0, "senior_id": r.get("senior_id"), "senior_pct": r.get("senior_pct") or 0,
                    "perms": json.loads(r.get("perms") or "[]")})
    return out


def team_of(uid: int) -> list:
    """Прямые помощники старшего."""
    with core.db() as c:
        return [r["user_id"] for r in c.execute("SELECT user_id FROM shop_staff WHERE senior_id=?", (uid,))]


# ---------- точки и типы оплаты ----------

def points(active_only=False) -> list:
    with core.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM shop_points" + (" WHERE active=1" if active_only else "") + " ORDER BY id")]


def save_point(pid, title, address, active=True):
    title = str(title or "").strip()[:80]
    if not title:
        return False, "Введите название точки."
    with core.db() as c:
        if pid:
            c.execute("UPDATE shop_points SET title=?, address=?, active=? WHERE id=?",
                      (title, str(address or "")[:200], int(bool(active)), int(pid)))
        else:
            c.execute("INSERT INTO shop_points(title, address) VALUES(?, ?)", (title, str(address or "")[:200]))
    return True, f"Точка «{title}» сохранена"


def pay_types(active_only=False) -> list:
    with core.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM shop_pay_types" + (" WHERE active=1" if active_only else "")
                                           + " ORDER BY sort, id")]


def save_pay_type(tid, title, icon, kind, active=True):
    title, icon = str(title or "").strip()[:40], str(icon or "💳").strip()[:8] or "💳"
    if not title:
        return False, "Введите название типа оплаты."
    if kind not in PAY_KINDS:
        kind = "normal"
    with core.db() as c:
        if tid:
            c.execute("UPDATE shop_pay_types SET title=?, icon=?, kind=?, active=? WHERE id=?",
                      (title, icon, kind, int(bool(active)), int(tid)))
        else:
            n = c.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM shop_pay_types").fetchone()[0]
            c.execute("INSERT INTO shop_pay_types(title, icon, kind, sort) VALUES(?, ?, ?, ?)", (title, icon, kind, n))
    return True, f"Тип оплаты «{title}» сохранён"


# ---------- клиенты и балансы ----------

def _phone(p) -> str:
    d = re.sub(r"\D", "", str(p or ""))
    if len(d) == 9:
        d = "998" + d
    return "+" + d if len(d) >= 9 else ""


def _tail(p) -> str:
    return re.sub(r"\D", "", str(p or ""))[-9:]


def loyalty_balance(client_id: int) -> float:
    with core.db() as c:
        return round(c.execute("SELECT COALESCE(SUM(amount), 0) FROM shop_loyalty WHERE client_id=?",
                               (client_id,)).fetchone()[0], 2)


def frozen_bonuses(client_id: int) -> list:
    with core.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM shop_bonus WHERE client_id=? AND status='frozen' ORDER BY unfreeze_at",
                                           (client_id,))]


def find_client(phone: str) -> dict:
    """Клиент по номеру: из ERP, иначе подсказка имени из бота / базы магазина. {"found", "id", "name", ...}"""
    tail = _tail(phone)
    if len(tail) < 9:
        return {"found": False, "error": "Введите номер полностью"}
    with core.db() as c:
        r = c.execute("SELECT * FROM shop_clients WHERE phone LIKE ?", ("%" + tail,)).fetchone()
        bot = c.execute("SELECT * FROM clients WHERE phone LIKE ?", ("%" + tail,)).fetchone()
        store = c.execute("SELECT * FROM billz_clients WHERE phone_tail=?", (tail,)).fetchone()
    name = (r and r["name"]) or (bot and core.client_full_name(dict(bot))) or \
        (store and " ".join(x for x in (store["first_name"], store["last_name"]) if x)) or ""
    res = {"found": bool(r), "id": r["id"] if r else None, "phone": _phone(phone), "name": name,
           "in_bot": bool(bot), "tier": core.tier_title(core.tier_of(dict(bot))) if bot else None,
           "note": (r and r["note"]) or ""}
    if r:
        res["points"] = loyalty_balance(r["id"])
        fb = frozen_bonuses(r["id"])
        res["frozen"] = sum(b["amount"] for b in fb)
        res["frozen_until"] = fb[0]["unfreeze_at"][:10] if fb else None
    else:
        res["points"], res["frozen"], res["frozen_until"] = 0, 0, None
    return res


def search_clients(q: str, limit: int = 20) -> list:
    """Гибкий поиск клиента: часть номера (от 3 цифр, в любом формате) или имя / фамилия.
    Ищет в ERP, в боте и в копии базы магазина; один номер — одна строка."""
    q = str(q or "").strip()
    digits = re.sub(r"\D", "", q)
    words = [w for w in re.split(r"\s+", re.sub(r"[\d+()\-]", " ", q).lower()) if len(w) >= 2]
    if len(digits) < 3 and not words:
        return []
    if len(digits) >= 3 and not words:
        part = digits[-9:] if len(digits) > 9 else digits
        cond = lambda col: (f"REPLACE(REPLACE(REPLACE({col}, ' ', ''), '-', ''), '+', '') LIKE ?", ["%" + part + "%"])
    else:
        cond = None
    out, seen = [], set()

    def add(phone, name, src, extra=None):
        tail = _tail(phone)
        if not tail or len(tail) < 9:
            return
        if tail in seen:
            for o in out:
                if o["tail"] == tail:
                    o["src"] = sorted(set(o["src"]) | {src})
                    if not o["name"] and name:
                        o["name"] = name
            return
        seen.add(tail)
        out.append(dict({"phone": _phone(phone), "tail": tail, "name": name or "", "src": [src]}, **(extra or {})))

    def name_ok(text):
        t = (text or "").lower()
        return all(w in t for w in words)
    with core.db() as c:
        if cond:
            w, a = cond("phone")
            rows = c.execute(f"SELECT * FROM shop_clients WHERE {w} LIMIT 50", a).fetchall()
        else:
            rows = [r for r in c.execute("SELECT * FROM shop_clients") if name_ok(r["name"])][:50]
        for r in rows:
            add(r["phone"], r["name"], "erp")
        if cond:
            w, a = cond("phone")
            rows = c.execute(f"SELECT * FROM clients WHERE phone IS NOT NULL AND {w} LIMIT 50", a).fetchall()
        else:
            rows = [r for r in c.execute("SELECT * FROM clients WHERE phone IS NOT NULL")
                    if name_ok(core.client_full_name(dict(r)) + " " + (r["username"] or ""))][:50]
        for r in rows:
            add(r["phone"], core.client_full_name(dict(r)), "bot")
        if cond:
            rows = c.execute("SELECT * FROM billz_clients WHERE phones LIKE ? LIMIT 50", ["%" + (digits[-9:] if len(digits) > 9 else digits) + "%"]).fetchall()
        else:
            like = "%" + words[0] + "%"
            rows = [r for r in c.execute("SELECT * FROM billz_clients WHERE LOWER(COALESCE(first_name,'') || ' ' || COALESCE(last_name,'')) "
                                         "LIKE ? LIMIT 300", (like,)) if name_ok(f"{r['first_name'] or ''} {r['last_name'] or ''}")][:50]
            if not rows and words:  # LOWER() в SQLite не понимает кириллицу — проверим в Python
                rows = [r for r in c.execute("SELECT * FROM billz_clients") if name_ok(f"{r['first_name'] or ''} {r['last_name'] or ''}")][:50]
        for r in rows:
            add(r["phone"], " ".join(x for x in (r["first_name"], r["last_name"]) if x), "store")
        pts = {r["client_id"]: r["bal"] for r in c.execute("SELECT client_id, SUM(amount) AS bal FROM shop_loyalty GROUP BY client_id")}
        ids = {_tail(r["phone"]): r["id"] for r in c.execute("SELECT id, phone FROM shop_clients")}
    for o in out:
        o["points"] = round(pts.get(ids.get(o["tail"]), 0) or 0, 2)
    # сначала точные совпадения номера, потом те, у кого есть имя
    out.sort(key=lambda o: (not (digits and o["tail"].endswith(digits[-9:])), not o["name"], o["name"].lower()))
    return out[:limit]


def _client_id(c, phone: str, name: str) -> int:
    tail = _tail(phone)
    r = c.execute("SELECT id, name FROM shop_clients WHERE phone LIKE ?", ("%" + tail,)).fetchone()
    bot = c.execute("SELECT user_id FROM clients WHERE phone LIKE ?", ("%" + tail,)).fetchone()
    if r:
        if name and not r["name"]:
            c.execute("UPDATE shop_clients SET name=? WHERE id=?", (name, r["id"]))
        if bot:
            c.execute("UPDATE shop_clients SET bot_uid=? WHERE id=?", (bot["user_id"], r["id"]))
        return r["id"]
    return c.execute("INSERT INTO shop_clients(phone, name, bot_uid, created_at) VALUES(?, ?, ?, ?)",
                     (_phone(phone), name or None, bot["user_id"] if bot else None,
                      core.now().strftime(core.FMT))).lastrowid


# ---------- продажа ----------

def _num(v) -> float:
    return round(float(str(v or 0).replace(" ", "").replace(",", ".")), 2)


def create_sale(uid: int, d: dict):
    """Кассир вводит продажу. Возвращает (ok, msg, sale_id)."""
    me = staff_row(uid)
    if "sell" not in me.get("perms", []):
        return False, "Вводить продажи может только кассир.", None
    st = settings()
    try:
        amount = _num(d.get("amount"))
        spent = _num(d.get("points_spent"))
        pays = [(int(p["type_id"]), _num(p["amount"])) for p in (d.get("payments") or []) if _num(p.get("amount"))]
        loyalty_val = _num(d.get("loyalty_value"))
        bonus_val = _num(d.get("bonus_value"))
    except (TypeError, ValueError, KeyError):
        return False, "Проверьте суммы.", None
    try:
        disc_val = _num(d.get("discount_value"))
    except (TypeError, ValueError):
        return False, "Проверьте скидку.", None
    discount = round(amount * disc_val / 100, 2) if d.get("discount_mode") == "pct" else disc_val
    if discount < 0 or discount > amount:
        return False, "Скидка не может быть больше суммы.", None
    total = round(amount - discount, 2)  # к оплате
    phone, seller = _phone(d.get("phone")), d.get("seller_id")
    if len(_tail(phone)) < 9:
        return False, "Введите номер клиента.", None
    if amount <= 0:
        return False, "Введите сумму покупки.", None
    point_id = int(d.get("point_id") or me.get("point_id") or 0) or None
    try:
        seller = int(seller)
    except (TypeError, ValueError):
        return False, "Выберите продавца.", None
    if not core.employee(seller) and not core.is_admin(seller):
        return False, "Выберите продавца.", None
    types = {t["id"]: t for t in pay_types()}
    if any(t not in types for t, _ in pays):
        return False, "Неизвестный тип оплаты.", None
    if spent < 0 or any(a < 0 for _, a in pays) or loyalty_val < 0 or bonus_val < 0:
        return False, "Суммы не могут быть отрицательными.", None
    if spent > total * st["spend_limit_pct"] / 100 + 0.5:
        return False, f"Баллами можно оплатить не больше {st['spend_limit_pct']:g}% покупки.", None
    if abs(sum(a for _, a in pays) + spent - total) > 0.5:
        fmt = lambda v: f"{v:,.0f}".replace(",", " ")
        return False, f"Оплата не сходится: к оплате {fmt(total)}, оплачено {fmt(sum(a for _, a in pays) + spent)} (вместе с баллами).", None
    loyalty_on = bool(d.get("loyalty_on"))
    loyalty_mode = d.get("loyalty_mode") or "pct"  # pct / sum
    revenue = total - spent  # выручка для KPI — без скидки и без оплаченного баллами
    if loyalty_on:
        pct = loyalty_val if loyalty_mode == "pct" else None
        loyalty_amount = round(revenue * (pct if pct is not None else 0) / 100, 2) if loyalty_mode == "pct" else loyalty_val
        if pct is not None and pct > 100:
            return False, "Процент баллов — до 100.", None
    else:
        pct, loyalty_amount = None, 0.0
    bonus_mode = d.get("bonus_mode") or "sum"
    bonus_amount = round(total * bonus_val / 100, 2) if bonus_mode == "pct" else bonus_val
    freeze = any(types[t]["kind"] == "nasiya" for t, _ in pays) or bool(d.get("bonus_freeze"))
    debt = sum(a for t, a in pays if types[t]["kind"] == "debt")
    now = core.now()
    with core.db() as c:
        cid = _client_id(c, phone, str(d.get("name") or "").strip()[:80])
        bal = c.execute("SELECT COALESCE(SUM(amount), 0) FROM shop_loyalty WHERE client_id=?", (cid,)).fetchone()[0]
        if spent > bal + 0.5:
            raise_msg = f"У клиента только {bal:,.0f} баллов.".replace(",", " ")
            return False, raise_msg, None
        sid = c.execute(
            "INSERT INTO shop_sales(point_id, cashier_id, seller_id, client_id, amount, points_spent, revenue, loyalty_on, "
            "loyalty_pct, loyalty_amount, bonus_amount, bonus_frozen, exchange, debt_amount, kpi_mode, note, created_at, discount) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (point_id, uid, seller, cid, amount, spent, revenue, int(loyalty_on), pct, loyalty_amount, bonus_amount,
             int(freeze and bonus_amount > 0), int(bool(d.get("exchange"))), debt, "pending" if debt else "now",
             str(d.get("note") or "")[:300], now.strftime(core.FMT), discount)).lastrowid
        for t, a in pays:
            c.execute("INSERT INTO shop_sale_pay(sale_id, pay_type_id, amount) VALUES(?, ?, ?)", (sid, t, a))
        if spent:
            c.execute("INSERT INTO shop_loyalty(client_id, amount, kind, sale_id, created_at) VALUES(?, ?, 'spend', ?, ?)",
                      (cid, -spent, sid, now.strftime(core.FMT)))
        if loyalty_amount:
            c.execute("INSERT INTO shop_loyalty(client_id, amount, kind, sale_id, created_at) VALUES(?, ?, 'earn', ?, ?)",
                      (cid, loyalty_amount, sid, now.strftime(core.FMT)))
        if bonus_amount:
            if freeze:  # Uzum Nasiya / рассрочка — заморожен на 6 месяцев, разморозка вручную
                until = _add_months(now, FREEZE_MONTHS)
                c.execute("INSERT INTO shop_bonus(client_id, sale_id, amount, unfreeze_at) VALUES(?, ?, ?, ?)",
                          (cid, sid, bonus_amount, until.strftime(core.FMT)))
            else:  # обычный бонус сразу становится баллами
                c.execute("INSERT INTO shop_loyalty(client_id, amount, kind, sale_id, created_at) VALUES(?, ?, 'bonus', ?, ?)",
                          (cid, bonus_amount, sid, now.strftime(core.FMT)))
        bot_uid = c.execute("SELECT bot_uid FROM shop_clients WHERE id=?", (cid,)).fetchone()["bot_uid"]
        if d.get("draft_id"):  # провели отложенную — черновик больше не нужен
            c.execute("DELETE FROM shop_drafts WHERE id=?", (int(d["draft_id"]),))
    if settings()["client_live"] and bot_uid:
        _notify_sale(bot_uid, sid)
    return True, f"🧾 Продажа №{sid} сохранена", sid


def _add_months(t: datetime, n: int) -> datetime:
    m = t.month - 1 + n
    y, m = t.year + m // 12, m % 12 + 1
    for day in (t.day, 30, 29, 28):
        try:
            return t.replace(year=y, month=m, day=day)
        except ValueError:
            continue
    return t + timedelta(days=30 * n)


def _money(v, lang) -> str:
    return f"{int(round(v or 0)):,}".replace(",", " ") + " " + i18n.t(lang, "currency")


def _notify_sale(bot_uid: int, sid: int) -> None:
    """«Спасибо за покупку» из ERP: сумма, начислено, баланс баллов, замороженный бонус."""
    with core.db() as c:
        s = c.execute("SELECT * FROM shop_sales WHERE id=?", (sid,)).fetchone()
        cl = c.execute("SELECT * FROM clients WHERE user_id=?", (bot_uid,)).fetchone()
    if not s or not cl:
        return
    lang = i18n.lang_of(cl["lang"])
    name = core.client_first_name(dict(cl)) or {"uz": "Hurmatli mijoz", "ru": "Дорогой клиент", "en": "Dear customer"}[lang]
    text = i18n.t(lang, "thanks_purchase", name=name, sum=_money(s["amount"], lang), bonus=_money(loyalty_balance(s["client_id"]), lang))
    extra = []
    if s["loyalty_amount"]:
        extra.append(i18n.t(lang, "erp_earned", sum=_money(s["loyalty_amount"], lang)))
    if s["points_spent"]:
        extra.append(i18n.t(lang, "erp_spent", sum=_money(s["points_spent"], lang)))
    if s["bonus_amount"] and s["bonus_frozen"]:
        with core.db() as c:
            b = c.execute("SELECT unfreeze_at FROM shop_bonus WHERE sale_id=?", (sid,)).fetchone()
        extra.append(i18n.t(lang, "erp_frozen", sum=_money(s["bonus_amount"], lang), date=b["unfreeze_at"][8:10] + "." +
                            b["unfreeze_at"][5:7] + "." + b["unfreeze_at"][:4]))
    if extra:
        text += "\n" + "\n".join(extra)
    with core.db() as c:
        c.execute("INSERT INTO outbox(chat_id, text, via) VALUES(?, ?, 'client')", (bot_uid, text))


def visible_sellers(uid: int):
    """Чьи продажи видит сотрудник: владелец — все (None), старший — свои + прямых помощников, остальные — свои
    (кассир — ещё и введённые им)."""
    me = staff_row(uid)
    if me.get("role") == "owner":
        return None
    ids = {uid}
    if me.get("role") == "senior":
        ids |= set(team_of(uid))
    return ids


def sales_list(uid: int, day_from: str = None, day_to: str = None, limit: int = 300, q: str = "", point_id=None) -> list:
    t = core.now()
    day_from = day_from or t.strftime("%Y-%m-%d")
    day_to = day_to or day_from
    who = visible_sellers(uid)
    with core.db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT s.*, sc.name AS client_name, sc.phone AS client_phone, p.title AS point FROM shop_sales s "
            "LEFT JOIN shop_clients sc ON sc.id=s.client_id LEFT JOIN shop_points p ON p.id=s.point_id "
            "WHERE substr(s.created_at, 1, 10) BETWEEN ? AND ? ORDER BY s.id DESC LIMIT ?", (day_from, day_to, limit))]
        pays = {}
        for r in c.execute("SELECT sp.*, t.title, t.icon FROM shop_sale_pay sp LEFT JOIN shop_pay_types t ON t.id=sp.pay_type_id "
                           "WHERE sale_id IN (%s)" % ",".join(str(r["id"]) for r in rows) if rows else "SELECT 1 WHERE 0"):
            pays.setdefault(r["sale_id"], []).append({"title": r["title"], "icon": r["icon"], "amount": r["amount"]})
    out = []
    q = str(q or "").strip().lower()
    for r in rows:
        if who is not None and r["seller_id"] not in who and r["cashier_id"] != uid:
            continue
        if point_id and r["point_id"] != int(point_id):
            continue
        r["seller"] = core.name(r["seller_id"]) if r["seller_id"] else "—"
        r["cashier"] = core.name(r["cashier_id"]) if r["cashier_id"] else "—"
        r["pays"] = pays.get(r["id"], [])
        r["time"] = r["created_at"][11:16]
        r["date"] = r["created_at"][8:10] + "." + r["created_at"][5:7] + "." + r["created_at"][:4]
        if q and q not in f"{r['id']} {r['client_name'] or ''} {r['client_phone'] or ''} {r['seller']} {r['cashier']}".lower():
            continue
        out.append(r)
    return out


def billz_sales_list(day_from: str = None, day_to: str = None, q: str = "", point_id=None) -> list:
    """Продажи из BILLZ (копия) в том же виде, что и наши, — для транзакций, главной и сверки."""
    t = core.now()
    day_from = day_from or t.strftime("%Y-%m-%d")
    day_to = day_to or day_from
    with core.db() as c:
        rows = c.execute("SELECT * FROM billz_sales WHERE substr(date, 1, 10) BETWEEN ? AND ? ORDER BY date DESC",
                         (day_from, day_to)).fetchall()
        pts = {r["billz_id"]: dict(r) for r in c.execute("SELECT * FROM shop_points WHERE billz_id IS NOT NULL")}
        icons = {(r["title"] or "").lower(): r["icon"] for r in c.execute("SELECT title, icon FROM shop_pay_types")}
    q = str(q or "").strip().lower()
    out = []
    for r in rows:
        pt = pts.get(r["shop_id"]) or {}
        if point_id and pt.get("id") != int(point_id):
            continue
        typ = r["order_type"] or "SALE"
        total = r["total"] or 0
        pays = [{"title": p["name"] or "—", "icon": icons.get((p["name"] or "").lower(), "💳"), "amount": p["sum"]}
                for p in json.loads(r["payments"] or "[]")]
        d = {"id": r["external_id"] or r["order_id"][:8], "source": "billz", "point_id": pt.get("id"), "point": pt.get("title") or r["shop_name"],
             "client_name": r["customer_name"] or "", "client_phone": "", "seller": r["sellers"] or "—", "cashier": r["cashier"] or "—",
             "amount": total, "discount": 0, "revenue": total, "points_spent": 0, "loyalty_amount": 0, "bonus_amount": 0,
             "bonus_frozen": 0, "exchange": 0, "debt_amount": 0, "note": r["comment"] or "", "pays": pays, "units": r["units"],
             "status": "ok" if typ == "SALE" else "return" if typ == "RETURN" else "exchange", "order_type": typ,
             "time": (r["date"] or "")[11:16] or "00:00", "date": (r["date"] or "")[8:10] + "." + (r["date"] or "")[5:7] + "." + (r["date"] or "")[:4]}
        if q and q not in f"{d['id']} {d['client_name']} {d['seller']} {d['cashier']}".lower():
            continue
        out.append(d)
    return out


def summary(rows: list) -> dict:
    ok = [r for r in rows if r["status"] == "ok"]
    by_pay = {}
    for r in ok:
        for p in r["pays"]:
            by_pay[p["icon"] + " " + p["title"]] = by_pay.get(p["icon"] + " " + p["title"], 0) + p["amount"]
    by_point, by_seller, by_hour = {}, {}, [0] * 24
    for r in ok:
        by_point[r["point"] or "—"] = by_point.get(r["point"] or "—", 0) + r["amount"] - (r["discount"] or 0)
        by_seller[r["seller"]] = by_seller.get(r["seller"], 0) + r["amount"] - (r["discount"] or 0)
        by_hour[int(r["time"][:2])] += r["amount"] - (r["discount"] or 0)
    tot = sum(r["amount"] - (r["discount"] or 0) for r in ok)
    return {"count": len(ok), "total": tot, "avg": tot / len(ok) if ok else 0, "discount": sum(r["discount"] or 0 for r in ok),
            "by_point": sorted(by_point.items(), key=lambda x: -x[1]), "by_seller": sorted(by_seller.items(), key=lambda x: -x[1]),
            "by_hour": by_hour, "amount": sum(r["amount"] for r in ok), "revenue": sum(r["revenue"] for r in ok),
            "points_spent": sum(r["points_spent"] for r in ok), "loyalty": sum(r["loyalty_amount"] for r in ok),
            "bonus": sum(r["bonus_amount"] for r in ok), "by_pay": by_pay}


# ---------- отложенные продажи ----------

def save_draft(uid: int, data: dict, draft_id=None):
    if not can(uid, "sell"):
        return False, "Нет права вводить продажи.", None
    data = {k: v for k, v in (data or {}).items() if k != "draft_id"}
    title = (str(data.get("name") or data.get("phone") or "Без клиента")[:40] + " · " +
             f"{_num(data.get('amount')):,.0f}".replace(",", " "))
    with core.db() as c:
        if draft_id:
            c.execute("UPDATE shop_drafts SET data=?, title=? WHERE id=? AND cashier_id=?",
                      (json.dumps(data, ensure_ascii=False), title, int(draft_id), uid))
            did = int(draft_id)
        else:
            did = c.execute("INSERT INTO shop_drafts(cashier_id, data, title, created_at) VALUES(?, ?, ?, ?)",
                            (uid, json.dumps(data, ensure_ascii=False), title, core.now().strftime(core.FMT))).lastrowid
    return True, "⏸ Продажа отложена", did


def drafts(uid: int) -> list:
    me = staff_row(uid)
    with core.db() as c:
        rows = c.execute("SELECT * FROM shop_drafts" + ("" if me.get("role") == "owner" else " WHERE cashier_id=?") +
                         " ORDER BY id DESC", () if me.get("role") == "owner" else (uid,)).fetchall()
    return [{"id": r["id"], "title": r["title"], "data": json.loads(r["data"]), "cashier": core.name(r["cashier_id"]),
             "when": r["created_at"][8:10] + "." + r["created_at"][5:7] + " " + r["created_at"][11:16]} for r in rows]


def delete_draft(uid: int, did: int):
    me = staff_row(uid)
    with core.db() as c:
        cur = c.execute("DELETE FROM shop_drafts WHERE id=?" + ("" if me.get("role") == "owner" else " AND cashier_id=?"),
                        (did,) if me.get("role") == "owner" else (did, uid))
    return (True, "Отложенная продажа удалена") if cur.rowcount else (False, "Не найдено")


# ---------- точки и типы оплаты из BILLZ ----------

def import_from_billz():
    """Загрузить магазины и типы оплаты из BILLZ (новые добавляются, существующие — по id/названию — обновляются)."""
    import erp
    if not erp.enabled():
        return False, "BILLZ не подключён."
    try:
        shops = (erp._api("GET", "/v1/shop", params={"page": 1, "limit": 100}) or {}).get("shops") or []
        ptypes = (erp._api("GET", "/v1/company/payment_type", params={"page": 1, "limit": 1000}) or {}).get("company_payment_types") or []
    except erp.BillzError as e:
        return False, f"Не удалось загрузить из BILLZ: {e}"
    np = nt = 0
    with core.db() as c:
        for s in shops:
            if s.get("deleted_at") or not s.get("name"):
                continue
            r = c.execute("SELECT id FROM shop_points WHERE billz_id=?", (s["id"],)).fetchone() or next(
                (x for x in c.execute("SELECT id, title FROM shop_points WHERE billz_id IS NULL").fetchall()
                 if (x["title"] or "").strip().lower() == s["name"].strip().lower()), None)
            if r:
                c.execute("UPDATE shop_points SET title=?, billz_id=?, address=COALESCE(NULLIF(?, ''), address) WHERE id=?",
                          (s["name"], s["id"], s.get("address") or "", r["id"]))
            else:
                c.execute("INSERT INTO shop_points(title, address, billz_id) VALUES(?, ?, ?)", (s["name"], s.get("address") or "", s["id"]))
                np += 1
        # «Основной магазин» по умолчанию больше не нужен, если пришли настоящие и по нему нет продаж
        if shops:
            c.execute("UPDATE shop_points SET active=0 WHERE billz_id IS NULL AND title='Основной магазин' AND "
                      "id NOT IN (SELECT DISTINCT point_id FROM shop_sales WHERE point_id IS NOT NULL)")
        for t in sorted(ptypes, key=lambda x: x.get("sequence_number") or 0):
            if t.get("dont_show_in_make_payment") or not t.get("name"):
                continue
            name, low = t["name"], t["name"].lower()
            kind = "nasiya" if re.search(r"nasiya|насия|рассроч|imanum|muddatli|installment", low) else \
                "debt" if re.search(r"долг|qarz|debt", low) else "normal"
            icon = "💵" if t.get("is_cash_payment_type") or "налич" in low or "naqd" in low else "🟣" if kind == "nasiya" else \
                "📝" if kind == "debt" else "💳"
            # сравнение названий — в Python: LOWER() в SQLite не понимает кириллицу
            r = c.execute("SELECT id FROM shop_pay_types WHERE billz_id=?", (t["id"],)).fetchone() or next(
                (x for x in c.execute("SELECT id, title FROM shop_pay_types WHERE billz_id IS NULL").fetchall()
                 if (x["title"] or "").strip().lower() == low.strip()), None)
            seq = int(t.get("sequence_number") or 0)
            if r:
                c.execute("UPDATE shop_pay_types SET title=?, billz_id=?, sort=?, active=1 WHERE id=?", (name, t["id"], seq, r["id"]))
            else:
                c.execute("INSERT INTO shop_pay_types(title, icon, kind, sort, billz_id) VALUES(?, ?, ?, ?, ?)",
                          (name, icon, kind, seq, t["id"]))
                nt += 1
        if ptypes:  # свои типы «по умолчанию», которых нет в BILLZ и по которым не было оплат, — выключаем и ставим в конец
            c.execute("UPDATE shop_pay_types SET active=0 WHERE billz_id IS NULL AND "
                      "id NOT IN (SELECT DISTINCT pay_type_id FROM shop_sale_pay)")
            c.execute("UPDATE shop_pay_types SET sort=1000+id WHERE billz_id IS NULL")
    return True, f"Из BILLZ: точек {len(shops)} (новых {np}), типов оплаты {len(ptypes)} (новых {nt})"


# ---------- бонусы: разморозка ----------

def bonuses_due(limit: int = 100) -> list:
    """Замороженные бонусы, у которых наступил срок (владелец подтверждает разморозку)."""
    with core.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT b.*, sc.name, sc.phone FROM shop_bonus b LEFT JOIN shop_clients sc ON sc.id=b.client_id "
            "WHERE b.status='frozen' ORDER BY b.unfreeze_at LIMIT ?", (limit,))]


def unfreeze(uid: int, bonus_id: int):
    if not can(uid, "unfreeze"):
        return False, "Нет права размораживать бонусы."
    with core.db() as c:
        b = c.execute("SELECT * FROM shop_bonus WHERE id=? AND status='frozen'", (bonus_id,)).fetchone()
        if not b:
            return False, "Бонус уже разморожен или не найден."
        now = core.now().strftime(core.FMT)
        c.execute("UPDATE shop_bonus SET status='released', released_at=?, released_by=? WHERE id=?", (now, uid, bonus_id))
        c.execute("INSERT INTO shop_loyalty(client_id, amount, kind, sale_id, note, created_at) VALUES(?, ?, 'release', ?, ?, ?)",
                  (b["client_id"], b["amount"], b["sale_id"], f"бонус #{bonus_id}", now))
        bot_uid = c.execute("SELECT bot_uid FROM shop_clients WHERE id=?", (b["client_id"],)).fetchone()["bot_uid"]
    if settings()["client_live"] and bot_uid:
        cl = core.get_client(bot_uid) or {}
        lang = i18n.lang_of(cl.get("lang"))
        with core.db() as c:
            c.execute("INSERT INTO outbox(chat_id, text, via) VALUES(?, ?, 'client')", (bot_uid, i18n.t(
                lang, "erp_released", sum=_money(b["amount"], lang), bal=_money(loyalty_balance(b["client_id"]), lang))))
    return True, f"🔓 Бонус {b['amount']:,.0f} разморожен — переведён в баллы".replace(",", " ")


# ---------- ежедневно: напоминания о разморозке и сгорание баллов ----------

def daily() -> None:
    t = core.now()
    if not core.flag_once(f"shop_daily:{t:%Y-%m-%d}"):
        return
    with core.db() as c:
        due = c.execute("SELECT b.*, sc.name, sc.phone FROM shop_bonus b LEFT JOIN shop_clients sc ON sc.id=b.client_id "
                        "WHERE b.status='frozen' AND b.reminded=0 AND b.unfreeze_at<=?", (t.strftime(core.FMT),)).fetchall()
        if due:
            lines = [f"• {r['name'] or r['phone']}: {r['amount']:,.0f} (продажа №{r['sale_id']})".replace(",", " ") for r in due[:30]]
            core.notify_admins(c, "🔓 Пора проверить рассрочку и разморозить бонусы (ERP → Бонусы):\n" + "\n".join(lines))
            c.execute("UPDATE shop_bonus SET reminded=1 WHERE status='frozen' AND reminded=0 AND unfreeze_at<=?",
                      (t.strftime(core.FMT),))
    days = settings()["loyalty_expire_days"]
    if days:
        edge = (t - timedelta(days=days)).strftime(core.FMT)
        with core.db() as c:
            for r in c.execute("SELECT client_id, SUM(amount) AS bal FROM shop_loyalty GROUP BY client_id HAVING bal>0").fetchall():
                last = c.execute("SELECT MAX(created_at) FROM shop_sales WHERE client_id=? AND status='ok'",
                                 (r["client_id"],)).fetchone()[0]
                if last and last < edge:
                    c.execute("INSERT INTO shop_loyalty(client_id, amount, kind, note, created_at) VALUES(?, ?, 'expire', ?, ?)",
                              (r["client_id"], -r["bal"], f"нет покупок {days} дн.", t.strftime(core.FMT)))


def client_wallet_for_bot(uid: int):
    """Баллы и замороженный бонус клиента бота (для кабинета), если ERP — основной."""
    if not settings()["client_live"]:
        return None
    with core.db() as c:
        cl = c.execute("SELECT phone FROM clients WHERE user_id=?", (uid,)).fetchone()
        r = c.execute("SELECT id FROM shop_clients WHERE bot_uid=? OR (phone LIKE ? AND ?<>'')",
                      (uid, "%" + _tail(cl["phone"] if cl else ""), _tail(cl["phone"] if cl else ""))).fetchone()
    if not r:
        return {"points": 0, "frozen": 0, "frozen_until": None}
    fb = frozen_bonuses(r["id"])
    return {"points": loyalty_balance(r["id"]), "frozen": sum(b["amount"] for b in fb),
            "frozen_until": fb[0]["unfreeze_at"][:10] if fb else None}
