"""Касса сотрудника и KPI.

Руководитель каждый день вносит продажи сотрудника (сумма и тип продажи). У каждого типа свой процент
(3%, 5%, 18%…), у некоторых сотрудников два типа. KPI = сумма × процент — сначала «Ожидаемый доход».
Кнопка «Оплачено» переводит KPI в прибыль: запись появляется в «Foydam» (таблица earnings).
"""
from datetime import datetime

import core
import staff_text as st


def init() -> None:
    with core.db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS kpi_types(
                id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, pct REAL, active INTEGER DEFAULT 1, created_at TEXT);
            CREATE TABLE IF NOT EXISTS kpi_sales(
                id INTEGER PRIMARY KEY, user_id INTEGER, type_id INTEGER, type_name TEXT, day TEXT, amount INTEGER,
                pct REAL, kpi INTEGER, created_by INTEGER, created_at TEXT, paid_at TEXT, earning_id INTEGER);
        """)


def _int(v) -> int:
    s = str(v or "").replace(" ", "").replace(" ", "").replace(",", "").replace(".", "")
    return int(s) if s.isdigit() else 0


def _pct(v):
    try:
        p = float(str(v).replace(",", ".").replace("%", "").strip())
    except ValueError:
        return None
    return p if 0 < p <= 100 else None


def _money(n) -> str:
    return f"{int(round(n or 0)):,}".replace(",", " ")


def _pct_txt(p) -> str:
    return (f"{p:g}").replace(".", ",")


def types(uid: int) -> list:
    with core.db() as c:
        return [dict(r) for r in c.execute("SELECT id, name, pct FROM kpi_types WHERE user_id=? AND active=1 ORDER BY id",
                                           (uid,))]


def save_type(uid: int, name: str, pct, tid: int = 0):
    if not core.employee(uid):
        return False, "Сотрудник не найден."
    name = (name or "").strip()[:40] or "Продажи"
    p = _pct(pct)
    if p is None:
        return False, "Укажите процент, например 5 или 3,5."
    with core.db() as c:
        if tid:
            cur = c.execute("UPDATE kpi_types SET name=?, pct=? WHERE id=? AND user_id=?", (name, p, tid, uid))
            if not cur.rowcount:
                return False, "Тип не найден."
            return True, f"Сохранено: {name} — {_pct_txt(p)}%"
        c.execute("INSERT INTO kpi_types(user_id, name, pct, created_at) VALUES(?, ?, ?, ?)",
                  (uid, name, p, core.now().strftime(core.FMT)))
    return True, f"Добавлен тип: {name} — {_pct_txt(p)}%"


def delete_type(tid: int):
    with core.db() as c:
        cur = c.execute("UPDATE kpi_types SET active=0 WHERE id=?", (tid,))
    return (True, "Тип продаж убран (старые продажи остаются).") if cur.rowcount else (False, "Тип не найден.")


def add_sale(uid: int, tid: int, amount, day: str, by: int):
    amount = _int(amount)
    if amount <= 0:
        return False, "Введите сумму продаж числом."
    try:
        d = datetime.strptime(day or core.now().strftime("%Y-%m-%d"), "%Y-%m-%d")
    except ValueError:
        return False, "Неверная дата."
    if d.date() > core.now().date():
        return False, "Дата в будущем."
    with core.db() as c:
        t = c.execute("SELECT * FROM kpi_types WHERE id=? AND user_id=? AND active=1", (tid, uid)).fetchone()
        if not t:
            return False, "Выберите тип продаж (или добавьте его с процентом)."
        kpi = int(round(amount * t["pct"] / 100))
        c.execute("INSERT INTO kpi_sales(user_id, type_id, type_name, day, amount, pct, kpi, created_by, created_at) "
                  "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (uid, tid, t["name"], d.strftime("%Y-%m-%d"), amount, t["pct"], kpi, by, core.now().strftime(core.FMT)))
        L = core.staff_lang(uid)
        core.notify(c, uid, st.s(L, "kpi_sale_msg", d=f"{d:%d.%m}", t=t["name"], a=_money(amount),
                                 p=_pct_txt(t["pct"]), k=_money(kpi)))
    return True, f"Продажа записана: {_money(amount)} × {_pct_txt(t['pct'])}% = KPI {_money(kpi)} сум"


def delete_sale(sid: int):
    with core.db() as c:
        r = c.execute("SELECT * FROM kpi_sales WHERE id=?", (sid,)).fetchone()
        if not r:
            return False, "Запись не найдена."
        if r["paid_at"]:
            return False, "KPI уже выплачен — сначала удалите запись в «Foyda»."
        c.execute("DELETE FROM kpi_sales WHERE id=?", (sid,))
    return True, "Продажа удалена."


def pay(uid: int, sid: int = 0):
    """Оплачено: одна продажа (sid) или все ожидающие сотрудника. KPI → прибыль (Foydam)."""
    with core.db() as c:
        q = "SELECT * FROM kpi_sales WHERE user_id=? AND paid_at IS NULL"
        args = [uid]
        if sid:
            q += " AND id=?"
            args.append(sid)
        rows = c.execute(q + " ORDER BY day, id", args).fetchall()
    rows = [r for r in rows if r["kpi"] > 0]
    if not rows:
        return False, "Нечего выплачивать."
    total = sum(r["kpi"] for r in rows)
    d1, d2 = rows[0]["day"], rows[-1]["day"]
    note = (f"KPI: {rows[0]['type_name']} {_money(rows[0]['amount'])} × {_pct_txt(rows[0]['pct'])}%" if len(rows) == 1
            else f"KPI: продажи {len(rows)} дн.")
    ok, msg = core.add_earning(uid, total, d2[:7], note, d1, d2)
    if not ok:
        return False, msg
    t = core.now().strftime(core.FMT)
    with core.db() as c:
        eid = c.execute("SELECT MAX(id) FROM earnings WHERE user_id=?", (uid,)).fetchone()[0]
        c.executemany("UPDATE kpi_sales SET paid_at=?, earning_id=? WHERE id=?", [(t, eid, r["id"]) for r in rows])
        L = core.staff_lang(uid)
        core.notify(c, uid, st.s(L, "kpi_paid_msg", k=_money(total)))
    return True, f"KPI выплачен: {_money(total)} сум → в «Foyda»"


def summary(uid: int) -> dict:
    """Ожидаемый доход (не выплаченный KPI), выплачено и продажи за месяц, список: ожидающие + этот месяц."""
    month = core.now().strftime("%Y-%m")
    with core.db() as c:
        rows = c.execute("SELECT * FROM kpi_sales WHERE user_id=? AND (paid_at IS NULL OR substr(day, 1, 7)=?) "
                         "ORDER BY day DESC, id DESC LIMIT 200", (uid, month)).fetchall()
        exp = c.execute("SELECT COALESCE(SUM(kpi), 0) FROM kpi_sales WHERE user_id=? AND paid_at IS NULL", (uid,)).fetchone()[0]
        paid_m = c.execute("SELECT COALESCE(SUM(kpi), 0) FROM kpi_sales WHERE user_id=? AND paid_at IS NOT NULL "
                           "AND substr(day, 1, 7)=?", (uid, month)).fetchone()[0]
        sales_m = c.execute("SELECT COALESCE(SUM(amount), 0) FROM kpi_sales WHERE user_id=? AND substr(day, 1, 7)=?",
                            (uid, month)).fetchone()[0]
        by_type = [dict(r) for r in c.execute(
            "SELECT type_name AS name, SUM(amount) AS amount, SUM(kpi) AS kpi FROM kpi_sales WHERE user_id=? "
            "AND substr(day, 1, 7)=? GROUP BY type_name ORDER BY SUM(amount) DESC", (uid, month))]
    return {"types": types(uid), "expected": exp, "paid_month": paid_m, "sales_month": sales_m, "by_type": by_type,
            "items": [{"id": r["id"], "day": f"{r['day'][8:10]}.{r['day'][5:7]}", "type": r["type_name"],
                       "amount": r["amount"], "pct": r["pct"], "kpi": r["kpi"], "paid": bool(r["paid_at"]),
                       "paid_at": core.parse(r["paid_at"]).strftime("%d.%m") if r["paid_at"] else ""} for r in rows]}


def summary_for_employee(uid: int) -> dict:
    """Сотруднику — только его доход: без выручки (суммы продаж) и без процентов."""
    s = summary(uid)
    return {"types": [{"id": t["id"], "name": t["name"]} for t in s["types"]], "expected": s["expected"],
            "paid_month": s["paid_month"],
            "items": [{k: v for k, v in x.items() if k not in ("amount", "pct")} for x in s["items"]]}


def team_summary() -> dict:
    import config
    return {uid: summary(uid) for uid in config.EMPLOYEES}
