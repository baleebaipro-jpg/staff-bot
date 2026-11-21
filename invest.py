"""Инвестиции сотрудников в товар компании (по принципам шариата: без процентов
и без вкладов — сотрудник инвестирует капитал в товар и получает долю прибыли).

Термины в текстах: «инвестиция», «капитал», «доля прибыли», «ожидаемая прибыль».
НЕ используем: «вклад», «проценты», «ставка», «годовых», «начисления».

Инвестиция на 12 месяцев, доля прибыли зависит от того, как прошёл каждый месяц.

Каждый месяц (30-го числа, в феврале — в последний день) руководитель
выбирает уровень месяца: слабый / средний / хороший. Уровень задаёт годовую
ориентир прибыли за год (18% / 20,5% / 23% — только для расчёта, сотрудникам не показывается);
за месяц распределяется 1/12 от него.
Все 12 месяцев слабые — инвестор получает 18%, все хорошие — 23%,
вперемешку — среднее между ними.

Вклад работает с дня подтверждения: первый и последний календарные месяцы
засчитываются частично, по дням (подтвердили 21 ноября — за ноябрь
распределяется 10/30 месячной доли, за ноябрь следующего года — 20/30).

Жизнь инвестиции:
    pending   — заявка, ждёт руководителя (сотрудник может отменить)
    active    — капитал получен, работает в товаре
      ├ rating  — срок закончился, но последний месяц ещё не оценён
      └ matured — срок закончился, все месяцы оценены — можно выплатить
    paid      — выплачено (сумма выплаты запоминается)
    withdrawn — досрочный выход: возвращён только капитал
    rejected  — заявку отклонил руководитель
    cancelled — сотрудник отменил заявку
"""
import calendar
import json
import os
import re
from datetime import date, datetime, timedelta

import config
import core

ENABLED = getattr(config, "INVEST_ENABLED", True)
SOON_TEXT = "💰 Раздел «Инвестиции» скоро появится ⏳"
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


# ---------- куда слать: группа с темами или руководителям в личку ----------
# Группа — INVEST_CHAT_ID в config.py (или команда /investchat в группе).
# Тема для каждого вида сообщений — INVEST_TOPICS в config.py.
TOPICS = getattr(config, "INVEST_TOPICS", {})


def group(c=None):
    """ID группы для инвестиций или None (тогда — личка руководителей)."""
    try:
        if c is not None:
            r = c.execute("SELECT value FROM flags WHERE name='invest_chat'").fetchone()
        else:
            with core.db() as c2:
                r = c2.execute("SELECT value FROM flags WHERE name='invest_chat'").fetchone()
    except Exception:
        r = None
    if r is not None:  # задано командой /investchat («» — отключено)
        return int(r["value"].partition(":")[0]) if r["value"] else None
    return getattr(config, "INVEST_CHAT_ID", None)


def set_group(chat_id) -> None:
    with core.db() as c:
        c.execute("INSERT OR REPLACE INTO flags(name, value) VALUES('invest_chat', ?)", (str(chat_id or ""),))


def topic(kind: str):
    """Номер темы для вида сообщения. Тема 1 — «General», туда шлём без номера."""
    t = TOPICS.get(kind)
    return None if t in (None, 1) else t


def notify_team(c, text: str, buttons=None, kind: str = "new", photo: str = None) -> None:
    """Сообщение по инвестициям: в нужную тему группы, иначе руководителям в личку.
    kind: new — заявки, payout — выплаты по сроку, early — досрочный выход, access — заявки на участие, rating — оценка месяца."""
    markup = json.dumps(buttons, ensure_ascii=False) if buttons else None
    chat = group(c)
    if not chat:
        for a in config.ADMINS:
            c.execute("INSERT INTO outbox(chat_id, text, markup, photo) VALUES(?, ?, ?, ?)", (a, text, markup, photo))
        return
    c.execute("INSERT INTO outbox(chat_id, text, markup, thread_id, photo) VALUES(?, ?, ?, ?, ?)",
              (chat, text, markup, topic(kind), photo))


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
    календарного месяца приходится на инвестицию [start, end)."""
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
    d["early"] = bool(d.get("early_at")) and d["status"] == "active"
    d["early_info"] = json.loads(d["early_info"]) if d.get("early_info") else None
    d["pay_method_title"] = PAY_METHODS.get(d.get("pay_method") or "", "")
    d.pop("pay_report", None)
    if d["status"] in ("paid", "withdrawn") and d.get("paid_amount") is not None:
        d["payout"] = d["paid_amount"]
        d["accrued"] = d["paid_amount"] - amount
    else:
        d["payout"] = amount + d["accrued"]
    d["percent"] = round(d["accrued"] / amount * 100, 2) if amount else 0
    return d


# ---------- сотрудник ----------

# Доступ к инвестициям — по заявке на участие (тема «access» в группе).
# Инвестировать могут сотрудники и руководители.

def can_invest(uid: int) -> bool:
    return bool(core.employee(uid)) or core.is_admin(uid)


def access_status(uid: int) -> str:
    """approved / pending / rejected / none. У кого уже были инвестиции — доступ есть."""
    with core.db() as c:
        r = c.execute("SELECT status FROM invest_access WHERE user_id=?", (uid,)).fetchone()
        if r and r["status"] == "approved":
            return "approved"
        had = c.execute("SELECT COUNT(*) FROM investments WHERE user_id=? AND status IN ('active', 'paid', 'withdrawn')",
                        (uid,)).fetchone()[0]
    if had:
        return "approved"
    return r["status"] if r else "none"


def access_request(uid: int, plan_amount: str = "", note: str = ""):
    if not can_invest(uid):
        return False, "Инвестиции доступны только сотрудникам компании."
    st = access_status(uid)
    if st == "approved":
        return False, "Доступ к инвестициям у вас уже открыт."
    if st == "pending":
        return False, "Ваша заявка на участие уже на рассмотрении."
    plan_amount, note = str(plan_amount or "").strip()[:40], str(note or "").strip()[:500]
    with core.db() as c:
        c.execute("INSERT OR REPLACE INTO invest_access(user_id, status, plan_amount, note, created_at) "
                  "VALUES(?, 'pending', ?, ?, ?)", (uid, plan_amount, note, core.now().strftime(core.FMT)))
        e = core.employee(uid) or {}
        role = e.get("role") or ("Руководитель" if core.is_admin(uid) else "")
        text = f"🙋 Заявка на участие в инвестициях\n👤 {core.name(uid)}" + (f" · {role}" if role else "")
        if plan_amount:
            text += f"\nПланирует инвестировать: {plan_amount}"
        if note:
            text += f"\nКомментарий: {note}"
        notify_team(c, text, [[("✅ Открыть доступ", f"inv_acc:ok:{uid}"), ("✕ Отклонить", f"inv_acc:no:{uid}")]],
                    kind="access")
    return True, "📨 Заявка на участие отправлена руководителю. Когда доступ откроют, бот пришлёт сообщение."


def access_decide(uid: int, approve: bool):
    with core.db() as c:
        r = c.execute("SELECT status FROM invest_access WHERE user_id=?", (uid,)).fetchone()
        if not r or r["status"] != "pending":
            return False, "Заявки на участие уже нет."
        c.execute("UPDATE invest_access SET status=?, decided_at=? WHERE user_id=?",
                  ("approved" if approve else "rejected", core.now().strftime(core.FMT), uid))
        if approve:
            core.notify(c, uid, "✅ Доступ к инвестициям открыт!\nОткройте «🏠 Личный кабинет» → «💰 Инвестиции», "
                                "чтобы инвестировать в товар компании.")
            return True, f"✅ Доступ открыт: {core.name(uid)}"
        core.notify(c, uid, "✕ Заявка на участие в инвестициях пока отклонена. Подробности — у руководителя.")
    return True, f"✕ Отклонено: {core.name(uid)}"


def access_pending() -> list:
    with core.db() as c:
        return [{"user_id": r["user_id"], "name": core.name(r["user_id"]), "plan_amount": r["plan_amount"],
                 "note": r["note"],
                 "created": datetime.strptime(r["created_at"][:10], DAY).strftime("%d.%m.%Y")}
                for r in c.execute("SELECT * FROM invest_access WHERE status='pending' ORDER BY created_at")]


def request(uid: int, amount):
    if not can_invest(uid):
        return False, "Инвестиции доступны только сотрудникам компании."
    if access_status(uid) != "approved":
        return False, "Сначала подайте заявку на участие — руководитель откроет доступ."
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
        notify_team(
            c,
            f"💰 Заявка на инвестицию №{inv_id}\n"
            f"👤 {core.name(uid)}\n"
            f"Капитал: {money(amount)} в товар на {TERM} мес.\n"
            f"Ожидаемая доля прибыли: {money(lo)} – {money(hi)} (не гарантируется)\n\n"
            f"Подтвердите, когда получите деньги.",
            [[("✅ Деньги получил", f"inv:ok:{inv_id}"), ("✕ Отклонить", f"inv:no:{inv_id}")]],
        )
    return True, "📨 Заявка отправлена руководителю. Передайте ему деньги — после подтверждения ваш капитал начнёт работать в товаре."


def cancel(uid: int, inv_id: int):
    with core.db() as c:
        r = c.execute("SELECT * FROM investments WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if not r:
            return False, "Заявка не найдена."
        if r["status"] != "pending":
            return False, "Вклад уже работает — заявку отменить нельзя. Досрочно можно забрать только вложенную сумму (кнопка «Забрать досрочно»)."
        c.execute("UPDATE investments SET status='cancelled', decided_at=? WHERE id=?",
                  (core.now().strftime(core.FMT), inv_id))
        notify_team(c, f"↩️ {core.name(uid)} отменил(а) заявку на инвестицию №{inv_id} ({money(r['amount'])}).")
    return True, "Заявка отменена."


def mine(uid: int) -> dict:
    today, rated = _today(), ratings()
    with core.db() as c:
        rows = [_row(r, today, rated) for r in c.execute(
            "SELECT * FROM investments WHERE user_id=? AND status!='cancelled' ORDER BY id DESC", (uid,))]
    live = [r for r in rows if r["status"] in ("active", "rating", "matured")]
    return {
        "access": access_status(uid),
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
            core.notify(c, r["user_id"], f"✕ Заявка на инвестицию {money(r['amount'])} отклонена руководителем.")
            return True, f"✕ Заявка отклонена: {who}, {money(r['amount'])}"
        start = _today()
        end = add_months(start, r["months"])
        c.execute("UPDATE investments SET status='active', decided_at=?, start_date=?, end_date=? WHERE id=?",
                  (now, start.strftime(DAY), end.strftime(DAY), inv_id))
        a = r["amount"]
        core.notify(c, r["user_id"],
                    f"✅ Инвестиция в товар принята!\n"
                    f"Капитал: {money(a)}\n"
                    f"Срок: {start:%d.%m.%Y} — {end:%d.%m.%Y}\n"
                    f"Ваша доля прибыли зависит от итогов продаж каждого месяца. "
                    f"Ожидаемо: {money(a * ANNUAL_MIN / 100)} – {money(a * ANNUAL_MAX / 100)} (не гарантируется).\n"
                    f"Доля прибыли за каждый месяц будет видна в кабинете.")
    return True, f"✅ Инвестиция принята: {who}, {money(r['amount'])} до {end:%d.%m.%Y}"


def months_to_rate(today: date = None) -> list:
    """Месяцы, которые уже пора оценить, но оценки нет (только если в них работали инвестиции)."""
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
    (на уже выплаченные инвестиции это не влияет)."""
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
            core.notify(c, uid, f"📊 Итог месяца {verb}: {month_title(ym).lower()} — {lv['emoji']} {lv['title'].lower()} месяц\n"
                                f"Ваша доля прибыли за месяц: +{money(earned)}")
    return True, f"{lv['emoji']} {month_title(ym)}: {lv['title'].lower()} месяц"


def mark_paid(inv_id: int):
    today, rated = _today(), ratings()
    with core.db() as c:
        r = c.execute("SELECT * FROM investments WHERE id=?", (inv_id,)).fetchone()
        if not r or r["status"] != "active":
            return False, "Вклад не найден или уже выплачен."
        row = _row(r, today, rated)
        if row["status"] == "active":
            return False, "Срок инвестиции ещё не закончился."
        if row["status"] == "rating":
            return False, "Сначала оцените все месяцы инвестиции."
        c.execute("UPDATE investments SET status='paid', paid_at=?, paid_amount=? WHERE id=?",
                  (core.now().strftime(core.FMT), row["payout"], inv_id))
        notify_team(c, f"💸 Выплачено: инвестиция №{inv_id}, {core.name(r['user_id'])} — {money(row['payout'])}", kind="payout")
        core.notify(c, r["user_id"], f"💸 Инвестиция №{inv_id} выплачена: {money(row['payout'])} "
                                     f"(капитал {money(r['amount'])} + доля прибыли {money(row['accrued'])}). "
                                     f"Спасибо, что инвестируете в команду!")
    return True, f"💸 Выплачено: {core.name(r['user_id'])}, {money(row['payout'])}"


# ---------- досрочный вывод: возвращается только тело вклада ----------

PAY_METHODS = {"online": "💳 онлайн", "offline": "💵 офлайн"}
REPORT_HINT = {"online": "чек / скриншот перевода", "offline": "фото передачи денег"}
REPORT_DIR = os.path.join(core._DATA_DIR, "invest_reports")


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def early_request(uid: int, inv_id: int, full_name: str = "", phone: str = "", requisites: str = ""):
    """Сотрудник просит досрочный выход из инвестиции. Нужны ФИО, телефон и реквизиты."""
    full_name, requisites = str(full_name or "").strip()[:100], str(requisites or "").strip()[:300]
    phone = str(phone or "").strip()[:30]
    if len(full_name) < 3:
        return False, "Укажите ФИО полностью."
    if not 9 <= len(_digits(phone)) <= 15:
        return False, "Укажите номер телефона, например +998 90 123 45 67."
    if len(requisites) < 4:
        return False, "Укажите реквизиты: номер карты или банк и счёт."
    today, rated = _today(), ratings()
    with core.db() as c:
        r = c.execute("SELECT * FROM investments WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if not r or r["status"] != "active":
            return False, "Вклад не найден."
        row = _row(r, today, rated)
        if row["status"] != "active":
            return False, "Срок инвестиции уже закончился — вам выплатят капитал и долю прибыли."
        if r["early_at"]:
            return False, "Заявка на досрочный выход уже отправлена."
        info = json.dumps({"name": full_name, "phone": phone, "requisites": requisites}, ensure_ascii=False)
        c.execute("UPDATE investments SET early_at=?, early_info=? WHERE id=?",
                  (core.now().strftime(core.FMT), info, inv_id))
        notify_team(
            c,
            f"↩️ Досрочный выход — инвестиция №{inv_id}\n"
            f"👤 {core.name(uid)}\n"
            f"Капитал: {money(r['amount'])} с {row['start']} (до {row['end']})\n"
            f"Доля прибыли: +{money(row['accrued'])} — не выплачивается\n"
            f"Вернуть капитал: {money(r['amount'])}\n\n"
            f"ФИО: {full_name}\n"
            f"Телефон: {phone}\n"
            f"Реквизиты: {requisites}",
            [[("✅ Выплатить", f"inv_early:ok:{inv_id}"), ("✕ Отклонить", f"inv_early:no:{inv_id}")]],
            kind="early")
    return True, f"📨 Заявка на досрочный выход отправлена. Вам вернут капитал {money(r['amount'])}."


def early_cancel(uid: int, inv_id: int):
    with core.db() as c:
        r = c.execute("SELECT * FROM investments WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if not r or r["status"] != "active" or not r["early_at"]:
            return False, "Заявки на досрочный выход нет."
        c.execute("UPDATE investments SET early_at=NULL WHERE id=?", (inv_id,))
        notify_team(c, f"↩️ {core.name(uid)} передумал(а) выходить из инвестиции №{inv_id} — она продолжает работать.",
                    kind="early")
    return True, "Заявка отменена — инвестиция продолжает работать."


def early_reject(inv_id: int):
    with core.db() as c:
        r = c.execute("SELECT * FROM investments WHERE id=?", (inv_id,)).fetchone()
        if not r or r["status"] != "active" or not r["early_at"]:
            return False, "Заявки на досрочный выход уже нет."
        c.execute("UPDATE investments SET early_at=NULL WHERE id=?", (inv_id,))
        core.notify(c, r["user_id"], f"✕ Досрочный выход из инвестиции №{inv_id} отклонён — она продолжает работать.")
    return True, f"✕ Отклонено: {core.name(r['user_id'])}, инвестиция продолжает работать"


def early_pay(inv_id: int, method: str, report: str, post_to_group: bool = True):
    """Досрочная выплата. method — online/offline, report — фото-отчёт:
    'tg:<file_id>' (прислали в Telegram) или путь к файлу (загрузили в кабинете)."""
    if method not in PAY_METHODS:
        return False, "Выберите: онлайн или офлайн оплата."
    if not report:
        return False, "Нужен фото-отчёт о выплате."
    with core.db() as c:
        r = c.execute("SELECT * FROM investments WHERE id=?", (inv_id,)).fetchone()
        if not r or r["status"] != "active" or not r["early_at"]:
            return False, "Заявки на досрочный выход уже нет."
        c.execute("UPDATE investments SET status='withdrawn', paid_at=?, paid_amount=?, pay_method=?, pay_report=? "
                  "WHERE id=?", (core.now().strftime(core.FMT), r["amount"], method, report, inv_id))
        who = core.name(r["user_id"])
        caption = (f"↩️ Досрочный выход: капитал возвращён, инвестиция №{inv_id}\n👤 {who} — {money(r['amount'])}\n"
                   f"Способ: {PAY_METHODS[method]}")
        if post_to_group:
            notify_team(c, caption, kind="early", photo=report)
        core.notify(c, r["user_id"], f"↩️ Досрочный выход из инвестиции №{inv_id}: вам возвращён капитал {money(r['amount'])} "
                                     f"({PAY_METHODS[method]}). Фото-отчёт ниже.")
        c.execute("INSERT INTO outbox(chat_id, text, photo) VALUES(?, ?, ?)",
                  (r["user_id"], f"📸 Отчёт о выплате по инвестиции №{inv_id}", report))
    return True, f"↩️ Капитал возвращён досрочно: {who}, {money(r['amount'])} ({PAY_METHODS[method]})"


def save_report(inv_id: int, jpeg: bytes) -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"{inv_id}_{int(core.now().timestamp())}.jpg")
    with open(path, "wb") as f:
        f.write(jpeg)
    return path


# Ожидание фото-отчёта в Telegram: руководитель выбрал способ — ждём его фото 30 минут.

def wait_report(admin_id: int, inv_id: int, method: str) -> None:
    until = (core.now() + timedelta(minutes=30)).strftime(core.FMT)
    with core.db() as c:
        c.execute("INSERT OR REPLACE INTO flags(name, value) VALUES(?, ?)",
                  (f"inv_pay:{admin_id}", f"{inv_id}|{method}|{until}"))


def waiting_report(admin_id: int):
    """(inv_id, method) если ждём от руководителя фото-отчёт, иначе None."""
    with core.db() as c:
        r = c.execute("SELECT value FROM flags WHERE name=?", (f"inv_pay:{admin_id}",)).fetchone()
    if not r or not r["value"]:
        return None
    inv_id, method, until = r["value"].split("|")
    if core.now().strftime(core.FMT) > until:
        return None
    return int(inv_id), method


def stop_waiting(admin_id: int) -> None:
    with core.db() as c:
        c.execute("DELETE FROM flags WHERE name=?", (f"inv_pay:{admin_id}",))


def overview() -> dict:
    """Для руководителя: все инвестиции, итоги и какие месяцы надо оценить."""
    today, rated = _today(), ratings()
    with core.db() as c:
        rows = [_row(r, today, rated) for r in c.execute(
            "SELECT * FROM investments WHERE status IN ('pending', 'active', 'paid', 'withdrawn') "
            "ORDER BY id DESC LIMIT 300")]
    for r in rows:
        r["name"] = core.name(r["user_id"])
    live = [r for r in rows if r["status"] in ("active", "rating", "matured")]
    cur = today.strftime("%Y-%m")
    last = sorted(rated)[-1] if rated else None
    return {
        "access": access_pending(),
        "pending": [r for r in rows if r["status"] == "pending"],
        "matured": [r for r in rows if r["status"] == "matured"],
        "active": [r for r in rows if r["status"] in ("active", "rating")],
        "early": [r for r in rows if r["early"]],
        "paid": [r for r in rows if r["status"] in ("paid", "withdrawn")][:20],
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
        notify_team(
            c, f"📊 Пора оценить месяц: {month_title(ym).lower()}.\nКак прошли продажи? От этого зависит доля прибыли инвесторов.",
            [[(f"{v['emoji']} {v['title']}", f"inv_lvl:{ym}:{k}") for k, v in LEVELS.items()]], kind="rating")


def check_matured() -> None:
    """Срок инвестиции закончился и все месяцы оценены — сообщить один раз."""
    today, rated = _today(), ratings()
    with core.db() as c:
        due = c.execute("SELECT * FROM investments WHERE status='active' AND end_date<=?",
                        (today.strftime(DAY),)).fetchall()
    for r in due:
        row = _row(r, today, rated)
        if row["status"] != "matured" or not core.flag_once(f"inv_matured:{r['id']}"):
            continue
        with core.db() as c:
            core.notify(c, r["user_id"], f"🎉 Срок вашей инвестиции №{r['id']} закончился!\n"
                                         f"К выплате: {money(row['payout'])} (капитал {money(r['amount'])} + "
                                         f"доля прибыли {money(row['accrued'])}). Руководитель получил уведомление.")
            notify_team(c, f"⏰ Пора выплатить инвестицию №{r['id']}: {core.name(r['user_id'])} — "
                                  f"{money(row['payout'])}.\nПосле выплаты отметьте в кабинете → Инвестиции.",
                        kind="payout")
