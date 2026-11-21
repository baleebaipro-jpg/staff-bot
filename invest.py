"""Инвестиции сотрудников: вклад на 12 месяцев, доход зависит от того,
как прошёл каждый месяц у компании.

Каждый месяц (30-го числа, в феврале — в последний день) руководитель
выбирает уровень месяца: слабый / средний / хороший. Уровень задаёт годовую
ставку (по умолчанию 18% / 20,5% / 23%), за месяц начисляется 1/12 от неё.
Все 12 месяцев слабые — инвестор получает 18%, все хорошие — 23%,
вперемешку — среднее между ними.

Вклад работает с дня подтверждения: первый и последний календарные месяцы
засчитываются частично, по дням (подтвердили 21 ноября — за ноябрь
начисляется 10/30 месячной доли, за ноябрь следующего года — 20/30).

Жизнь вклада:
    pending   — заявка, ждёт руководителя (сотрудник может отменить)
    active    — деньги получены, идут начисления
      ├ rating  — срок закончился, но последний месяц ещё не оценён
      └ matured — срок закончился, все месяцы оценены — можно выплатить
    paid      — выплачено (сумма выплаты запоминается)
    rejected  — заявку отклонил руководитель
    cancelled — сотрудник отменил заявку
"""
import calendar
from datetime import date, datetime, timedelta

import config
import core

TERM = getattr(config, "INVEST_TERM_MONTHS", 12)
LEVELS = getattr(config, "INVEST_LEVELS", {
    "weak": {"title": "Слабый", "emoji": "🟠", "annual": 18.0},
    "mid": {"title": "Средний", "emoji": "🟡", "annual": 20.5},
    "good": {"title": "Хороший", "emoji": "🟢", "annual": 23.0},
})
RATE_DAY = getattr(config, "INVEST_RATE_DAY", 30)
RATE_TIME = getattr(config, "INVEST_RATE_TIME", "10:00")
MIN_AMOUNT = getattr(config, "INVEST_MIN", 0)
MAX_AMOUNT = getattr(config, "INVEST_MAX", 10 ** 12)
CURRENCY = getattr(config, "INVEST_CURRENCY", "сум")
DAY = "%Y-%m-%d"
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь",
          "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]

_annual = [v["annual"] for v in LEVELS.values()]
ANNUAL_MIN, ANNUAL_MAX = min(_annual), max(_annual)


# ---------- вспомогательное ----------

def money(n) -> str:
    return f"{int(round(n)):,}".replace(",", " ") + f" {CURRENCY}"


def pct(x) -> str:
    return f"{round(x, 2):g}%".replace(".", ",")


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def month_title(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{MONTHS[m - 1].capitalize()} {y}"


def rate_day(ym: str) -> date:
    """Когда можно оценить месяц: 30-е число (в феврале — последний день)."""
    y, m = map(int, ym.split("-"))
    return date(y, m, min(RATE_DAY, calendar.monthrange(y, m)[1]))


def month_weights(start: date, end: date) -> list:
    """[('2026-11', 0.333), ('2026-12', 1.0), ...] — какая доля каждого
    календарного месяца приходится на вклад [start, end)."""
    out = []
    cur = date(start.year, start.month, 1)
    while cur < end:
        days_in = calendar.monthrange(cur.year, cur.month)[1]
        nxt = cur + timedelta(days=days_in)
        days = (min(end, nxt) - max(start, cur)).days
        if days > 0:
            out.append((cur.strftime("%Y-%m"), days / days_in))
        cur = nxt
    return out


def _today() -> date:
    return core.now().date()


def _d(s: str) -> date:
    return datetime.strptime(s, DAY).date()


def ratings() -> dict:
    """{'2026-11': 'good', ...}"""
    with core.db() as c:
        return {r["month"]: r["level"] for r in c.execute("SELECT month, level FROM invest_months")}


def monthly_rate(level: str) -> float:
    """% за полный месяц для уровня."""
    return LEVELS[level]["annual"] / 12


# ---------- расчёт по одному вкладу ----------

def _row(r, today: date, rated: dict) -> dict:
    d = dict(r)
    amount = d["amount"]
    d["created"] = datetime.strptime(d["created_at"][:10], DAY).strftime("%d.%m.%Y")
    d["min_total"] = int(round(amount * ANNUAL_MIN / 100 * d["months"] / 12))
    d["max_total"] = int(round(amount * ANNUAL_MAX / 100 * d["months"] / 12))
    d["accrued"], d["progress"], d["days_left"], d["months_list"] = 0, 0, None, []
    if d["status"] in ("active", "paid") and d["start_date"]:
        start, end = _d(d["start_date"]), _d(d["end_date"])
        accrued, rest_w = 0.0, 0.0
        for ym, w in month_weights(start, end):
            lvl = rated.get(ym)
            earned = amount * w * monthly_rate(lvl) / 100 if lvl else 0
            accrued += earned
            if not lvl:
                rest_w += w
            if lvl or rate_day(ym) <= today or ym <= today.strftime("%Y-%m"):
                d["months_list"].append({"month": ym, "title": month_title(ym), "share": round(w * 100),
                                         "level": lvl, "earned": int(round(earned))})
        d["accrued"] = int(round(accrued))
        d["min_total"] = int(round(accrued + amount * rest_w * ANNUAL_MIN / 12 / 100))
        d["max_total"] = int(round(accrued + amount * rest_w * ANNUAL_MAX / 12 / 100))
        span = max(1, (end - start).days)
        passed = span if d["status"] == "paid" else min(span, max(0, (today - start).days))
        d["progress"] = round(passed / span * 100, 1)
        d["days_left"] = max(0, (end - today).days)
        d["start"], d["end"] = start.strftime("%d.%m.%Y"), end.strftime("%d.%m.%Y")
        if d["status"] == "active" and today >= end:
            d["status"] = "matured" if rest_w == 0 else "rating"
    if d["status"] == "paid" and d.get("paid_amount"):
        d["payout"] = d["paid_amount"]
        d["accrued"] = d["paid_amount"] - amount
    else:
        d["payout"] = amount + d["accrued"]
    d["percent"] = round(d["accrued"] / amount * 100, 2) if amount else 0
    return d


# ---------- сотрудник ----------

def request(uid: int, amount):
    if not core.employee(uid):
        return False, "Инвестиции доступны только сотрудникам."
    try:
        amount = int(str(amount).replace(" ", "").replace(" ", "").replace(".", "").replace(",", ""))
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
            "INSERT INTO investments(user_id, amount, months, status, created_at) VALUES(?, ?, ?, 'pending', ?)",
            (uid, amount, TERM, core.now().strftime(core.FMT)))
        inv_id = cur.lastrowid
        lo, hi = amount * ANNUAL_MIN / 100, amount * ANNUAL_MAX / 100
        core.notify_admins(
            c,
            f"💰 Заявка на вклад №{inv_id}\n"
            f"👤 {core.name(uid)}\n"
            f"Сумма: {money(amount)} на {TERM} мес.\n"
            f"Доход инвестора: {pct(ANNUAL_MIN)}–{pct(ANNUAL_MAX)} → {money(lo)} – {money(hi)}\n\n"
            f"Подтвердите, когда получите деньги.",
            [[("✅ Деньги получил", f"inv:ok:{inv_id}"), ("✕ Отклонить", f"inv:no:{inv_id}")]],
        )
    return True, "📨 Заявка отправлена руководителю. Передайте ему деньги — после подтверждения начнутся начисления."


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
    today, rated = _today(), ratings()
    with core.db() as c:
        rows = [_row(r, today, rated) for r in c.execute(
            "SELECT * FROM investments WHERE user_id=? AND status!='cancelled' ORDER BY id DESC", (uid,))]
    live = [r for r in rows if r["status"] in ("active", "rating", "matured")]
    return {
        "items": rows,
        "invested": sum(r["amount"] for r in live),
        "accrued": sum(r["accrued"] for r in live),
        "payout": sum(r["payout"] for r in live),
        "earned": sum(r["accrued"] for r in rows if r["status"] == "paid"),
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
        a = r["amount"]
        core.notify(c, r["user_id"],
                    f"✅ Вклад открыт!\n"
                    f"Сумма: {money(a)}\n"
                    f"Срок: {start:%d.%m.%Y} — {end:%d.%m.%Y}\n"
                    f"Доход зависит от того, как пройдёт каждый месяц: от {pct(ANNUAL_MIN)} до {pct(ANNUAL_MAX)} "
                    f"({money(a * ANNUAL_MIN / 100)} – {money(a * ANNUAL_MAX / 100)}).\n"
                    f"Начисления будут видны в кабинете каждый месяц.")
    return True, f"✅ Вклад открыт: {who}, {money(r['amount'])} до {end:%d.%m.%Y}"


def months_to_rate(today: date = None) -> list:
    """Месяцы, которые уже пора оценить, но оценки нет (только если в них работали вклады)."""
    today = today or _today()
    rated = ratings()
    need = set()
    with core.db() as c:
        for r in c.execute("SELECT start_date, end_date FROM investments WHERE status='active'"):
            for ym, _ in month_weights(_d(r["start_date"]), _d(r["end_date"])):
                if ym not in rated and rate_day(ym) <= today:
                    need.add(ym)
    return sorted(need)


def set_level(ym: str, level: str):
    """Руководитель оценивает месяц. Повторный выбор меняет оценку
    (на уже выплаченные вклады это не влияет)."""
    if level not in LEVELS:
        return False, "Выберите уровень месяца."
    try:
        rd = rate_day(ym)
    except (ValueError, IndexError):
        return False, "Неверный месяц."
    today = _today()
    if rd > today:
        return False, f"{month_title(ym)} можно оценить с {rd:%d.%m.%Y}."
    lv = LEVELS[level]
    with core.db() as c:
        old = c.execute("SELECT level FROM invest_months WHERE month=?", (ym,)).fetchone()
        c.execute("INSERT OR REPLACE INTO invest_months(month, level, set_at) VALUES(?, ?, ?)",
                  (ym, level, core.now().strftime(core.FMT)))
        # сообщить инвесторам, сколько им начислено за этот месяц
        per_user = {}
        for r in c.execute("SELECT * FROM investments WHERE status='active'"):
            for m, w in month_weights(_d(r["start_date"]), _d(r["end_date"])):
                if m == ym:
                    per_user[r["user_id"]] = per_user.get(r["user_id"], 0) + r["amount"] * w * monthly_rate(level) / 100
        verb = "изменена" if old else "подведён"
        for uid, earned in per_user.items():
            core.notify(c, uid, f"📊 Итог месяца {verb}: {month_title(ym).lower()} — {lv['emoji']} {lv['title'].lower()} "
                                f"({pct(monthly_rate(level))} за месяц)\n"
                                f"Вам начислено: +{money(earned)}")
    return True, f"{lv['emoji']} {month_title(ym)}: {lv['title'].lower()} месяц"


def mark_paid(inv_id: int):
    today, rated = _today(), ratings()
    with core.db() as c:
        r = c.execute("SELECT * FROM investments WHERE id=?", (inv_id,)).fetchone()
        if not r or r["status"] != "active":
            return False, "Вклад не найден или уже выплачен."
        row = _row(r, today, rated)
        if row["status"] == "active":
            return False, "Срок вклада ещё не закончился."
        if row["status"] == "rating":
            return False, "Сначала оцените все месяцы вклада."
        c.execute("UPDATE investments SET status='paid', paid_at=?, paid_amount=? WHERE id=?",
                  (core.now().strftime(core.FMT), row["payout"], inv_id))
        core.notify(c, r["user_id"], f"💸 Вклад №{inv_id} выплачен: {money(row['payout'])} "
                                     f"(доход +{money(row['accrued'])}, {pct(row['percent'])}). "
                                     f"Спасибо, что инвестируете в команду!")
    return True, f"💸 Выплачено: {core.name(r['user_id'])}, {money(row['payout'])}"


def overview() -> dict:
    """Для руководителя: все вклады, итоги и какие месяцы надо оценить."""
    today, rated = _today(), ratings()
    with core.db() as c:
        rows = [_row(r, today, rated) for r in c.execute(
            "SELECT * FROM investments WHERE status IN ('pending', 'active', 'paid') ORDER BY id DESC LIMIT 300")]
    for r in rows:
        r["name"] = core.name(r["user_id"])
    live = [r for r in rows if r["status"] in ("active", "rating", "matured")]
    cur = today.strftime("%Y-%m")
    last = sorted(rated)[-1] if rated else None
    return {
        "pending": [r for r in rows if r["status"] == "pending"],
        "matured": [r for r in rows if r["status"] == "matured"],
        "active": [r for r in rows if r["status"] in ("active", "rating")],
        "paid": [r for r in rows if r["status"] == "paid"][:20],
        "invested": sum(r["amount"] for r in live),
        "accrued": sum(r["accrued"] for r in live),
        "obligations_min": sum(r["amount"] + r["min_total"] for r in live),
        "obligations_max": sum(r["amount"] + r["max_total"] for r in live),
        "to_pay_now": sum(r["payout"] for r in rows if r["status"] == "matured"),
        "to_rate": [{"month": m, "title": month_title(m)} for m in months_to_rate(today)],
        "last_rated": {"month": last, "title": month_title(last), "level": rated[last]} if last else None,
        "current": {"month": cur, "title": month_title(cur), "opens": rate_day(cur).strftime("%d.%m"),
                    "level": rated.get(cur)},
        "history": [{"month": m, "title": month_title(m), "level": rated[m]} for m in sorted(rated, reverse=True)[:12]],
    }


def levels_info() -> list:
    return [{"key": k, "title": v["title"], "emoji": v["emoji"], "annual": v["annual"],
             "monthly": round(v["annual"] / 12, 3)} for k, v in LEVELS.items()]


def history_public(limit: int = 12) -> list:
    rated = ratings()
    return [{"month": m, "title": month_title(m), "level": rated[m]} for m in sorted(rated, reverse=True)[:limit]]


# ---------- фоновые проверки (из цикла бота) ----------

def check_rating_day() -> None:
    """В день оценки (30-е) напомнить руководителю и прислать кнопки."""
    now = core.now()
    ym = now.strftime("%Y-%m")
    if now.date() != rate_day(ym) or now.strftime("%H:%M") < RATE_TIME:
        return
    if ym not in months_to_rate(now.date()):
        return
    if not core.flag_once(f"inv_rate:{ym}"):
        return
    with core.db() as c:
        core.notify_admins(
            c, f"📊 Пора оценить месяц: {month_title(ym).lower()}.\nКак он прошёл? От этого зависит доход инвесторов.",
            [[(f"{v['emoji']} {v['title']}", f"inv_lvl:{ym}:{k}") for k, v in LEVELS.items()]])


def check_matured() -> None:
    """Срок вклада закончился и все месяцы оценены — сообщить один раз."""
    today, rated = _today(), ratings()
    with core.db() as c:
        due = c.execute("SELECT * FROM investments WHERE status='active' AND end_date<=?",
                        (today.strftime(DAY),)).fetchall()
    for r in due:
        row = _row(r, today, rated)
        if row["status"] != "matured" or not core.flag_once(f"inv_matured:{r['id']}"):
            continue
        with core.db() as c:
            core.notify(c, r["user_id"], f"🎉 Срок вашего вклада №{r['id']} закончился!\n"
                                         f"К выплате: {money(row['payout'])} (доход +{money(row['accrued'])}, "
                                         f"{pct(row['percent'])}). Руководитель получил уведомление.")
            core.notify_admins(c, f"⏰ Пора выплатить вклад №{r['id']}: {core.name(r['user_id'])} — "
                                  f"{money(row['payout'])}.\nПосле выплаты отметьте в кабинете → Инвестиции.")
