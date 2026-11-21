"""Инвестиции сотрудников: вклад на срок под фиксированный % в месяц.

Жизнь вклада:
    pending   — сотрудник подал заявку, ждёт руководителя (может отменить)
    active    — руководитель получил деньги и подтвердил; идут проценты
                (когда срок прошёл — считается «готов к выплате»)
    paid      — руководитель выплатил тело + доход
    rejected  — руководитель отклонил заявку
    cancelled — сотрудник сам отменил заявку
"""
import calendar
from datetime import date, datetime

import config
import core

PLANS = getattr(config, "INVEST_PLANS", {})
MIN_AMOUNT = getattr(config, "INVEST_MIN", 0)
MAX_AMOUNT = getattr(config, "INVEST_MAX", 10 ** 12)
CURRENCY = getattr(config, "INVEST_CURRENCY", "so'm")
DAY = "%Y-%m-%d"


def money(n) -> str:
    return f"{int(round(n)):,}".replace(",", " ") + f" {CURRENCY}"


def pct(rate) -> str:
    return f"{rate:g}%"


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def profit_total(amount: int, rate: float, months: int) -> int:
    return int(round(amount * rate / 100 * months))


def plans() -> list:
    return [{"key": k, "months": p["months"], "rate": p["rate"]} for k, p in PLANS.items()]


def _row(r, today: date) -> dict:
    """Вклад для показа: сколько начислено, сколько дней осталось и т.п."""
    d = dict(r)
    total_profit = profit_total(d["amount"], d["rate"], d["months"])
    d["profit_total"] = total_profit
    d["payout"] = d["amount"] + total_profit
    d["created"] = datetime.strptime(d["created_at"][:10], DAY).strftime("%d.%m.%Y")
    d["accrued"], d["progress"], d["days_left"] = 0, 0, None
    if d["status"] in ("active", "paid") and d["start_date"]:
        start = datetime.strptime(d["start_date"], DAY).date()
        end = datetime.strptime(d["end_date"], DAY).date()
        span = max(1, (end - start).days)
        passed = span if d["status"] == "paid" else min(span, max(0, (today - start).days))
        d["progress"] = round(passed / span * 100, 1)
        d["accrued"] = int(round(total_profit * passed / span))
        d["days_left"] = max(0, (end - today).days)
        if d["status"] == "active" and today >= end:
            d["status"] = "matured"
    for k in ("start_date", "end_date"):
        if d[k]:
            d[k.split("_")[0]] = datetime.strptime(d[k], DAY).strftime("%d.%m.%Y")
    return d


def _today() -> date:
    return core.now().date()


# ---------- сотрудник ----------

def request(uid: int, amount, plan_key: str):
    if not core.employee(uid):
        return False, "Инвестиции доступны только сотрудникам."
    plan = PLANS.get(str(plan_key))
    if not plan:
        return False, "Выберите срок вклада."
    try:
        amount = int(str(amount).replace(" ", "").replace(".", "").replace(",", ""))
    except ValueError:
        return False, "Введите сумму числом."
    if amount < MIN_AMOUNT:
        return False, f"Минимальная сумма — {money(MIN_AMOUNT)}."
    if amount > MAX_AMOUNT:
        return False, f"Максимальная сумма — {money(MAX_AMOUNT)}."
    with core.db() as c:
        if c.execute("SELECT COUNT(*) FROM investments WHERE user_id=? AND status='pending'", (uid,)).fetchone()[0]:
            return False, "У вас уже есть заявка на рассмотрении. Дождитесь ответа или отмените её."
        cur = c.execute(
            "INSERT INTO investments(user_id, amount, months, rate, status, created_at) VALUES(?,?,?,?, 'pending', ?)",
            (uid, amount, plan["months"], plan["rate"], core.now().strftime(core.FMT)))
        inv_id = cur.lastrowid
        profit = profit_total(amount, plan["rate"], plan["months"])
        core.notify_admins(
            c,
            f"💰 Заявка на вклад №{inv_id}\n"
            f"👤 {core.name(uid)}\n"
            f"Сумма: {money(amount)}\n"
            f"Срок: {plan['months']} мес. под {pct(plan['rate'])} в месяц\n"
            f"Доход: {money(profit)} · к выплате: {money(amount + profit)}\n\n"
            f"Подтвердите, когда получите деньги.",
            [[("✅ Деньги получил", f"inv:ok:{inv_id}"), ("✕ Отклонить", f"inv:no:{inv_id}")]],
        )
    return True, "📨 Заявка отправлена руководителю. Передайте ему деньги — после подтверждения начнут идти проценты."


def cancel(uid: int, inv_id: int):
    with core.db() as c:
        r = c.execute("SELECT * FROM investments WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if not r:
            return False, "Заявка не найдена."
        if r["status"] != "pending":
            return False, "Подтверждённый вклад отменить нельзя — он выплачивается после окончания срока."
        c.execute("UPDATE investments SET status='cancelled', decided_at=? WHERE id=?",
                  (core.now().strftime(core.FMT), inv_id))
        core.notify_admins(c, f"↩️ {core.name(uid)} отменил(а) заявку на вклад №{inv_id} ({money(r['amount'])}).")
    return True, "Заявка отменена."


def mine(uid: int) -> dict:
    today = _today()
    with core.db() as c:
        rows = [_row(r, today) for r in c.execute(
            "SELECT * FROM investments WHERE user_id=? AND status NOT IN ('cancelled') ORDER BY id DESC", (uid,))]
    live = [r for r in rows if r["status"] in ("active", "matured")]
    return {
        "items": rows,
        "invested": sum(r["amount"] for r in live),
        "accrued": sum(r["accrued"] for r in live),
        "payout": sum(r["payout"] for r in live),
        "earned": sum(r["profit_total"] for r in rows if r["status"] == "paid"),
    }


# ---------- руководитель ----------

def decide(inv_id: int, approve: bool):
    with core.db() as c:
        r = c.execute("SELECT * FROM investments WHERE id=?", (inv_id,)).fetchone()
        if not r:
            return False, "Заявка не найдена."
        if r["status"] != "pending":
            return False, {"cancelled": "Сотрудник уже отменил эту заявку.",
                           "rejected": "Заявка уже отклонена."}.get(r["status"], "Заявка уже подтверждена.")
        now = core.now().strftime(core.FMT)
        who = core.name(r["user_id"])
        if not approve:
            c.execute("UPDATE investments SET status='rejected', decided_at=? WHERE id=?", (now, inv_id))
            core.notify(c, r["user_id"], f"✕ Заявка на вклад {money(r['amount'])} отклонена руководителем.")
            return True, f"✕ Заявка отклонена: {who}, {money(r['amount'])}"
        start = _today()
        end = add_months(start, r["months"])
        c.execute("UPDATE investments SET status='active', decided_at=?, start_date=?, end_date=? WHERE id=?",
                  (now, start.strftime(DAY), end.strftime(DAY), inv_id))
        payout = r["amount"] + profit_total(r["amount"], r["rate"], r["months"])
        core.notify(c, r["user_id"],
                    f"✅ Вклад открыт!\n"
                    f"Сумма: {money(r['amount'])} под {pct(r['rate'])} в месяц\n"
                    f"Срок: {start:%d.%m.%Y} — {end:%d.%m.%Y}\n"
                    f"К выплате в конце срока: {money(payout)}")
    return True, f"✅ Вклад открыт: {who}, {money(r['amount'])} до {end:%d.%m.%Y}"


def mark_paid(inv_id: int):
    today = _today()
    with core.db() as c:
        r = c.execute("SELECT * FROM investments WHERE id=?", (inv_id,)).fetchone()
        if not r or r["status"] != "active":
            return False, "Вклад не найден или уже выплачен."
        if today < datetime.strptime(r["end_date"], DAY).date():
            return False, "Срок вклада ещё не закончился."
        c.execute("UPDATE investments SET status='paid', paid_at=? WHERE id=?",
                  (core.now().strftime(core.FMT), inv_id))
        payout = r["amount"] + profit_total(r["amount"], r["rate"], r["months"])
        core.notify(c, r["user_id"], f"💸 Вклад №{inv_id} выплачен: {money(payout)}. Спасибо, что инвестируете в команду!")
    return True, f"💸 Отмечено как выплачено: {core.name(r['user_id'])}, {money(payout)}"


def overview() -> dict:
    """Для руководителя: все вклады и итоги."""
    today = _today()
    with core.db() as c:
        rows = [_row(r, today) for r in c.execute(
            "SELECT * FROM investments WHERE status IN ('pending', 'active', 'paid') ORDER BY id DESC LIMIT 300")]
    for r in rows:
        r["name"] = core.name(r["user_id"])
    live = [r for r in rows if r["status"] in ("active", "matured")]
    return {
        "pending": [r for r in rows if r["status"] == "pending"],
        "matured": [r for r in rows if r["status"] == "matured"],
        "active": [r for r in rows if r["status"] == "active"],
        "paid": [r for r in rows if r["status"] == "paid"][:20],
        "invested": sum(r["amount"] for r in live),
        "accrued": sum(r["accrued"] for r in live),
        "obligations": sum(r["payout"] for r in live),
        "to_pay_now": sum(r["payout"] for r in rows if r["status"] == "matured"),
    }


def check_matured() -> None:
    """Раз в цикл бота: сообщить, что срок вклада закончился (один раз на вклад)."""
    today = _today().strftime(DAY)
    with core.db() as c:
        due = c.execute("SELECT * FROM investments WHERE status='active' AND end_date<=?", (today,)).fetchall()
    for r in due:
        if not core.flag_once(f"inv_matured:{r['id']}"):
            continue
        payout = money(r["amount"] + profit_total(r["amount"], r["rate"], r["months"]))
        with core.db() as c:
            core.notify(c, r["user_id"], f"🎉 Срок вашего вклада №{r['id']} закончился! К выплате: {payout}.\n"
                                         f"Руководитель получил уведомление.")
            core.notify_admins(c, f"⏰ Пора выплатить вклад №{r['id']}: {core.name(r['user_id'])} — {payout}.\n"
                                  f"После выплаты отметьте в кабинете → Команда → Инвестиции.")
