"""Общая логика: база данных, приход/уход, часы, задачи, уведомления.
Этот файл используют и бот, и мини-апп."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

_DATA_DIR = getattr(config, "DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, "staff.db")
FMT = "%Y-%m-%d %H:%M:%S"
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ---------- вспомогательное ----------

def now() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE)).replace(tzinfo=None, microsecond=0)


def parse(s: str) -> datetime:
    return datetime.strptime(s, FMT)


def at(day, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m)


def fmt_min(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} ч {m} мин"
    return f"{h} ч" if h else f"{m} мин"


SHIFTS = getattr(config, "SHIFTS", {})
_shift_cache = {"t": 0.0, "data": {}}


def _shift_overrides() -> dict:
    """Графики, выбранные руководителем в кабинете (хранятся в базе)."""
    import time
    if time.time() - _shift_cache["t"] > 30:
        try:
            with db() as c:
                _shift_cache["data"] = {r[0]: r[1] for r in c.execute("SELECT user_id, shift FROM emp_shift")}
        except sqlite3.Error:
            pass
        _shift_cache["t"] = time.time()
    return _shift_cache["data"]


def employee(uid: int):
    base = config.EMPLOYEES.get(uid)
    if not base:
        return None
    e = dict(base)
    shift = _shift_overrides().get(uid) or e.get("shift")
    if shift in SHIFTS:
        e["shift"] = shift
        e["start"] = SHIFTS[shift].get("start")
        e["end"] = SHIFTS[shift].get("end")
    return e


def set_shift(uid: int, shift: str):
    if not config.EMPLOYEES.get(uid):
        return False, "Сотрудник не найден."
    if shift not in SHIFTS:
        return False, "Такого графика нет."
    with db() as c:
        c.execute("INSERT INTO emp_shift(user_id, shift) VALUES(?, ?) "
                  "ON CONFLICT(user_id) DO UPDATE SET shift=excluded.shift", (uid, shift))
        notify(c, uid, f"🕘 Ваш график изменён: {SHIFTS[shift]['title']}")
    _shift_cache["data"][uid] = shift
    return True, f"{name(uid)}: {SHIFTS[shift]['title']}"


def is_admin(uid: int) -> bool:
    return uid in config.ADMINS


def name(uid: int) -> str:
    e = employee(uid)
    return e["name"] if e else str(uid)


def has_schedule(emp) -> bool:
    return bool(emp.get("start") and emp.get("end"))


def schedule_text(emp) -> str:
    if emp.get("shift") in SHIFTS:
        return SHIFTS[emp["shift"]]["title"]
    return f"{emp['start']}–{emp['end']}" if has_schedule(emp) else "Свободный график"


def workday(emp, day) -> bool:
    """Рабочий день по графику. Без графика — ограничений и штрафов нет."""
    return has_schedule(emp) and day.weekday() in emp.get("days", range(7))


WINDOW = getattr(config, "WINDOW_MINUTES", 15)


def window(emp, kind: str, day):
    """Окно, когда можно отметить приход (kind="in") или уход ("out")."""
    base = at(day, emp["start"] if kind == "in" else emp["end"])
    return base - timedelta(minutes=WINDOW), base + timedelta(minutes=WINDOW)


def window_text(emp, kind: str, day) -> str:
    if not has_schedule(emp):
        return "в любое время"
    a, b = window(emp, kind, day)
    return f"{a:%H:%M}–{b:%H:%M}"


def in_window(emp, kind: str, t: datetime) -> bool:
    if not workday(emp, t):
        return True  # в выходной — без ограничений
    a, b = window(emp, kind, t)
    return a <= t <= b


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions(
                id INTEGER PRIMARY KEY, user_id INTEGER, check_in TEXT, check_out TEXT,
                late_min INTEGER DEFAULT 0, early_min INTEGER DEFAULT 0, auto_closed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, created_at TEXT, done_at TEXT);
            CREATE TABLE IF NOT EXISTS penalties(
                id INTEGER PRIMARY KEY, user_id INTEGER, day TEXT, reason TEXT, amount TEXT,
                UNIQUE(user_id, day, reason));
            CREATE TABLE IF NOT EXISTS relay_posts(
                id INTEGER PRIMARY KEY, day TEXT, created_at TEXT, items INTEGER);
            CREATE TABLE IF NOT EXISTS flags(name TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS emp_shift(user_id INTEGER PRIMARY KEY, shift TEXT);
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY, chat_id INTEGER, text TEXT, sent INTEGER DEFAULT 0);
            """
        )
        cols = {r[1] for r in c.execute("PRAGMA table_info(penalties)")}
        if "note" not in cols:
            c.execute("ALTER TABLE penalties ADD COLUMN note TEXT")


def notify_admins(c, text: str):
    for a in config.ADMINS:
        c.execute("INSERT INTO outbox(chat_id, text) VALUES(?, ?)", (a, text))


def notify(c, uid: int, text: str):
    c.execute("INSERT INTO outbox(chat_id, text) VALUES(?, ?)", (uid, text))


# ---------- приход / уход ----------

def _open_session(c, uid):
    return c.execute(
        "SELECT * FROM sessions WHERE user_id=? AND check_out IS NULL ORDER BY id DESC LIMIT 1", (uid,)
    ).fetchone()


def _close_stale(c, uid):
    """Если человек забыл отметить уход в прошлый день — закрыть по графику."""
    s = _open_session(c, uid)
    if not s:
        return
    cin = parse(s["check_in"])
    emp = employee(uid)
    t = now()
    if cin.date() >= t.date():
        # сегодня: закрываем, только если окно ухода уже прошло
        if not emp or not workday(emp, t) or t <= window(emp, "out", t)[1]:
            return
    if emp and has_schedule(emp):
        end = at(cin.date(), emp["end"])
    else:  # свободный график: считаем максимум 8 часов, но не позже конца дня
        end = min(cin + timedelta(hours=8), at(cin.date(), "23:59"))
    if end <= cin:
        end = cin
    c.execute("UPDATE sessions SET check_out=?, auto_closed=1 WHERE id=?", (end.strftime(FMT), s["id"]))
    notify_admins(
        c,
        f"🤷 {name(uid)} не отметил(а) уход {cin:%d.%m}. Закрыто автоматически в {end:%H:%M}.",
    )


def check_in(uid: int):
    emp = employee(uid)
    if not emp:
        return False, "Вы не в списке сотрудников."
    t = now()
    with db() as c:
        _close_stale(c, uid)
        s = _open_session(c, uid)
        if s:
            return False, f"Вы уже отметили приход сегодня в {s['check_in'][11:16]}."
        if c.execute("SELECT 1 FROM sessions WHERE user_id=? AND substr(check_in, 1, 10) = ?",
                     (uid, t.strftime("%Y-%m-%d"))).fetchone() and workday(emp, t):
            return False, "Сегодня смена уже завершена."
        if not in_window(emp, "in", t):
            a, b = window(emp, "in", t)
            if t > b:
                notify_admins(c, f"⛔ {emp['name']} пытался(ась) отметить приход в {t:%H:%M} — окно закрыто ({a:%H:%M}–{b:%H:%M})")
            return False, f"⏰ Отметить приход можно только с {a:%H:%M} до {b:%H:%M}.\nСейчас {t:%H:%M}."
        late = 0
        if workday(emp, t):
            diff = int((t - at(t, emp["start"])).total_seconds() // 60)
            if diff > config.GRACE_MINUTES:
                late = diff
        c.execute("INSERT INTO sessions(user_id, check_in, late_min) VALUES(?, ?, ?)", (uid, t.strftime(FMT), late))
        if late:
            notify_admins(c, f"⚠️ {emp['name']} пришёл(ла) в {t:%H:%M} — опоздание {fmt_min(late)} (по графику {emp['start']})")
        else:
            notify_admins(c, f"✅ {emp['name']} пришёл(ла) в {t:%H:%M}")
    msg = f"✅ Приход отмечен в {t:%H:%M}."
    if late:
        msg += f"\n⚠️ Опоздание: {fmt_min(late)}"
    elif workday(emp, t):
        msg += "\nВы вовремя 👍"
    return True, msg


def check_out(uid: int):
    emp = employee(uid)
    if not emp:
        return False, "Вы не в списке сотрудников."
    t = now()
    with db() as c:
        _close_stale(c, uid)
        s = _open_session(c, uid)
        if not s:
            return False, "Сегодня вы ещё не отмечали приход."
        if not in_window(emp, "out", t):
            a, b = window(emp, "out", t)
            return False, f"⏰ Отметить уход можно только с {a:%H:%M} до {b:%H:%M}.\nСейчас {t:%H:%M}."
        cin = parse(s["check_in"])
        worked = int((t - cin).total_seconds() // 60)
        early = 0
        if workday(emp, t):
            diff = int((at(t, emp["end"]) - t).total_seconds() // 60)
            if diff > 0:
                early = diff
        c.execute("UPDATE sessions SET check_out=?, early_min=? WHERE id=?", (t.strftime(FMT), early, s["id"]))
        text = f"🚪 {emp['name']} ушёл(ла) в {t:%H:%M}, отработал(а) {fmt_min(worked)}"
        if early:
            text += f"\n⚠️ Ранний уход: на {fmt_min(early)} раньше графика ({emp['end']})"
        notify_admins(c, text)
    msg = f"🚪 Уход отмечен в {t:%H:%M}.\nСегодня отработано: {fmt_min(worked)}"
    if early:
        msg += f"\n⚠️ Раньше графика на {fmt_min(early)}"
    return True, msg


# ---------- часы и история ----------

def _hours(c, uid, since: str = None) -> float:
    q = "SELECT check_in, check_out FROM sessions WHERE user_id=?"
    args = [uid]
    if since:
        q += " AND check_in >= ?"
        args.append(since)
    total = 0.0
    t = now()
    for r in c.execute(q, args):
        end = parse(r["check_out"]) if r["check_out"] else t
        total += max(0, (end - parse(r["check_in"])).total_seconds()) / 3600
    return total


def _streak(c, uid, emp) -> int:
    """Сколько рабочих дней подряд человек приходил (сегодня не обнуляет серию)."""
    days = {r[0][:10] for r in c.execute("SELECT check_in FROM sessions WHERE user_id=?", (uid,))}
    if not days:
        return 0
    d = now().date()
    if d.isoformat() not in days:
        d -= timedelta(days=1)
    count = 0
    for _ in range(3650):
        came = d.isoformat() in days
        if came:
            count += 1
        elif workday(emp, d):
            break
        d -= timedelta(days=1)
    return count


def profile(uid: int) -> dict:
    emp = employee(uid)
    t = now()
    with db() as c:
        _close_stale(c, uid)
        s = _open_session(c, uid)
        today = c.execute(
            "SELECT * FROM sessions WHERE user_id=? AND substr(check_in, 1, 10) = ? ORDER BY id", (uid, t.strftime("%Y-%m-%d"))
        ).fetchall()
        month_start = t.strftime("%Y-%m-01")
        week_start = (t - timedelta(days=t.weekday())).strftime("%Y-%m-%d")
        total = _hours(c, uid) + emp.get("hours_before", 0)
        late_month = c.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id=? AND late_min>0 AND check_in >= ?", (uid, month_start)
        ).fetchone()[0]
        res = {
            "id": uid,
            "name": emp["name"],
            "role": emp.get("role", "Сотрудник"),
            "streak": _streak(c, uid, emp),
            "schedule": schedule_text(emp),
            "has_schedule": has_schedule(emp),
            "days": ", ".join(WEEKDAYS[d] for d in emp.get("days", range(7))),
            "goal": config.GOAL_HOURS,
            "total_hours": round(total, 1),
            "today_hours": round(_hours(c, uid, t.strftime("%Y-%m-%d")), 2),
            "week_hours": round(_hours(c, uid, week_start), 1),
            "month_hours": round(_hours(c, uid, month_start), 1),
            "late_month": late_month,
            "on_site": bool(s),
            "since": s["check_in"][11:16] if s else None,
            "came_today": bool(today),
            "penalties_month": penalties_month(c, uid),
            "penalties": penalties_list(c, uid),
            "penalties_total": penalties_total(c, uid),
            "workday": workday(emp, t),
            "in_window": window_text(emp, "in", t),
            "out_window": window_text(emp, "out", t),
            "can_in": in_window(emp, "in", t) and not s and not (today and workday(emp, t)),
            "can_out": bool(s) and in_window(emp, "out", t),
        }
    return res


def history(uid: int, limit: int = 30) -> list:
    with db() as c:
        rows = c.execute(
            "SELECT * FROM sessions WHERE user_id=? ORDER BY check_in DESC LIMIT ?", (uid, limit)
        ).fetchall()
    out = []
    t = now()
    for r in rows:
        cin = parse(r["check_in"])
        cout = parse(r["check_out"]) if r["check_out"] else None
        out.append({
            "date": f"{cin:%d.%m.%Y}",
            "wd": WEEKDAYS[cin.weekday()],
            "in": f"{cin:%H:%M}",
            "out": f"{cout:%H:%M}" if cout else None,
            "worked": fmt_min(((cout or t) - cin).total_seconds() // 60),
            "late": fmt_min(r["late_min"]) if r["late_min"] else None,
            "early": fmt_min(r["early_min"]) if r["early_min"] else None,
            "auto": bool(r["auto_closed"]),
        })
    return out


# ---------- штрафы ----------

PENALTY_CHAT_ID = getattr(config, "PENALTY_CHAT_ID", None)
PENALTY_AMOUNT = getattr(config, "PENALTY_AMOUNT", "")


def check_penalties():
    """Штраф тем, кто не отметил приход до конца окна (например до 09:15).
    Вызывается ботом раз в минуту."""
    t = now()
    today = t.strftime("%Y-%m-%d")
    with db() as c:
        for uid in config.EMPLOYEES:
            emp = employee(uid)
            if not workday(emp, t) or t <= window(emp, "in", t)[1]:
                continue
            came = c.execute(
                "SELECT 1 FROM sessions WHERE user_id=? AND substr(check_in, 1, 10) = ?", (uid, today)
            ).fetchone()
            if came:
                continue
            reason = f"не отметил(а) приход до {window(emp, 'in', t)[1]:%H:%M} (график {emp['start']})"
            cur = c.execute(
                "INSERT OR IGNORE INTO penalties(user_id, day, reason, amount, note) VALUES(?, ?, 'late', ?, ?)",
                (uid, today, PENALTY_AMOUNT, reason),
            )
            if not cur.rowcount:
                continue  # штраф за сегодня уже выписан
            _announce(c, uid, "🔴 Штраф", reason, PENALTY_AMOUNT)


def _announce(c, uid, title, reason, amount):
    """Сообщение о штрафе: в группу (или руководителям) и самому сотруднику."""
    text = f"{title}\n👤 {name(uid)}\n📅 {now():%d.%m.%Y}\nПричина: {reason}"
    if amount:
        text += f"\n💰 Сумма: {amount}"
    if PENALTY_CHAT_ID:
        c.execute("INSERT INTO outbox(chat_id, text) VALUES(?, ?)", (PENALTY_CHAT_ID, text))
    else:
        notify_admins(c, text)
    notify(c, uid, text)


def add_penalty(uid: int, reason: str, amount: str = "") -> None:
    """Ручной штраф от руководителя."""
    t = now()
    reason = reason.strip()[:300]
    amount = amount.strip()[:50]
    with db() as c:
        c.execute(
            "INSERT INTO penalties(user_id, day, reason, amount, note) VALUES(?, ?, ?, ?, ?)",
            (uid, t.strftime("%Y-%m-%d"), f"manual:{t.strftime(FMT)}:{os.urandom(3).hex()}", amount, reason),
        )
        _announce(c, uid, "🔴 Штраф", reason, amount)


def delete_penalty(pid: int):
    with db() as c:
        r = c.execute("SELECT * FROM penalties WHERE id=?", (pid,)).fetchone()
        if not r:
            return False, "Штраф не найден."
        c.execute("DELETE FROM penalties WHERE id=?", (pid,))
        _announce(c, r["user_id"], "✅ Штраф отменён", r["note"] or "—", r["amount"])
    return True, "Штраф отменён."


def penalties_total(c, uid) -> int:
    return c.execute("SELECT COUNT(*) FROM penalties WHERE user_id=?", (uid,)).fetchone()[0]


def penalties_list(c, uid) -> list:
    rows = c.execute(
        "SELECT * FROM penalties WHERE user_id=? AND day >= ? ORDER BY id DESC",
        (uid, now().strftime("%Y-%m-01")),
    ).fetchall()
    return [
        {"id": r["id"], "date": datetime.strptime(r["day"], "%Y-%m-%d").strftime("%d.%m"),
         "reason": r["note"] or "опоздание", "amount": r["amount"] or "",
         "auto": r["reason"] == "late"}
        for r in rows
    ]


def penalties_month(c, uid) -> int:
    return c.execute(
        "SELECT COUNT(*) FROM penalties WHERE user_id=? AND day >= ?", (uid, now().strftime("%Y-%m-01"))
    ).fetchone()[0]


# ---------- задачи ----------

def add_task(uid: int, title: str) -> int:
    title = title.strip()[:500]
    with db() as c:
        cur = c.execute(
            "INSERT INTO tasks(user_id, title, created_at) VALUES(?, ?, ?)", (uid, title, now().strftime(FMT))
        )
        notify(c, uid, f"📌 Новая задача:\n{title}")
        return cur.lastrowid


def tasks(uid: int) -> list:
    with db() as c:
        rows = c.execute(
            "SELECT * FROM tasks WHERE user_id=? ORDER BY done_at IS NOT NULL, id DESC LIMIT 50", (uid,)
        ).fetchall()
    return [
        {"id": r["id"], "title": r["title"], "created": parse(r["created_at"]).strftime("%d.%m %H:%M"),
         "done": bool(r["done_at"])}
        for r in rows
    ]


def complete_task(task_id: int, uid: int):
    with db() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, uid)).fetchone()
        if not r:
            return False, "Задача не найдена."
        if r["done_at"]:
            return False, "Задача уже выполнена."
        c.execute("UPDATE tasks SET done_at=? WHERE id=?", (now().strftime(FMT), task_id))
        notify_admins(c, f"✔️ {name(uid)} выполнил(а) задачу:\n{r['title']}")
    return True, "Задача выполнена 👍"


def delete_task(task_id: int):
    with db() as c:
        c.execute("DELETE FROM tasks WHERE id=?", (task_id,))


# ---------- команда (для руководителя) ----------

def team() -> list:
    t = now()
    today = t.strftime("%Y-%m-%d")
    out = []
    with db() as c:
        for uid in config.EMPLOYEES:
            emp = employee(uid)
            _close_stale(c, uid)
            s = _open_session(c, uid)
            first = c.execute(
                "SELECT * FROM sessions WHERE user_id=? AND check_in >= ? ORDER BY id LIMIT 1", (uid, today)
            ).fetchone()
            last = c.execute(
                "SELECT * FROM sessions WHERE user_id=? AND check_in >= ? ORDER BY id DESC LIMIT 1", (uid, today)
            ).fetchone()
            if s:
                status, icon = f"на месте с {s['check_in'][11:16]}", "🟢"
            elif first:
                status, icon = f"ушёл(ла) в {last['check_out'][11:16]}", "⚪️"
            elif not has_schedule(emp):
                status, icon = "сегодня не отмечался(ась)", "⚪️"
            elif workday(emp, t):
                status, icon = "не пришёл(ла)", "🔴"
            else:
                status, icon = "выходной", "🌙"
            open_tasks = c.execute(
                "SELECT id, title FROM tasks WHERE user_id=? AND done_at IS NULL ORDER BY id DESC", (uid,)
            ).fetchall()
            out.append({
                "id": uid,
                "name": emp["name"],
                "role": emp.get("role", "Сотрудник"),
                "schedule": schedule_text(emp),
                "shift": emp.get("shift"),
                "icon": icon,
                "status": status,
                "late": fmt_min(first["late_min"]) if first and first["late_min"] else None,
                "penalties_month": penalties_month(c, uid),
                "penalties": penalties_list(c, uid),
                "penalties_total": penalties_total(c, uid),
                "total_hours": round(_hours(c, uid) + emp.get("hours_before", 0), 1),
                "tasks": [{"id": r["id"], "title": r["title"]} for r in open_tasks],
            })
    return out


# ---------- очередь уведомлений (их отправляет бот) ----------

def pending_outbox(limit: int = 20) -> list:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM outbox WHERE sent=0 ORDER BY id LIMIT ?", (limit,))]


def mark_sent(msg_id: int):
    with db() as c:
        c.execute("UPDATE outbox SET sent=1 WHERE id=?", (msg_id,))


# ---------- счётчик альбомов фотографа ----------

def log_relay_post(items: int):
    t = now()
    with db() as c:
        c.execute("INSERT INTO relay_posts(day, created_at, items) VALUES(?, ?, ?)",
                  (t.strftime("%Y-%m-%d"), t.strftime(FMT), items))


def relay_posts_today() -> int:
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM relay_posts WHERE day=?", (now().strftime("%Y-%m-%d"),)).fetchone()[0]


def flag_once(name: str) -> bool:
    """True только в первый раз для этого имени (например 'tease:2026-11-21')."""
    with db() as c:
        cur = c.execute("INSERT OR IGNORE INTO flags(name, value) VALUES(?, ?)", (name, now().strftime(FMT)))
        return cur.rowcount == 1
