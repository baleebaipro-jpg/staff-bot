"""Общая логика: база данных, приход/уход, часы, задачи, уведомления.
Этот файл используют и бот, и мини-апп."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import staff_text as st

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


def fmt_min(minutes: int, lang: str = None) -> str:
    return st.fmt_min(minutes, lang)


# ---------- язык сотрудника (uz / ru) ----------

def staff_lang(uid: int) -> str:
    """Язык интерфейса сотрудника. Пока не выбран — русский."""
    try:
        with db() as c:
            r = c.execute("SELECT lang FROM staff_lang WHERE user_id=?", (uid,)).fetchone()
    except sqlite3.Error:
        r = None
    return st.lang_of(r[0] if r else None)


def staff_lang_chosen(uid: int) -> bool:
    with db() as c:
        return bool(c.execute("SELECT 1 FROM staff_lang WHERE user_id=?", (uid,)).fetchone())


def set_staff_lang(uid: int, lang: str) -> str:
    lang = st.lang_of(lang)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO staff_lang(user_id, lang) VALUES(?, ?)", (uid, lang))
    return lang


SHIFTS = getattr(config, "SHIFTS", {})
OFF = "off"  # выходной
_cache = {"t": 0.0, "shift": {}, "week": {}}


def _overrides() -> dict:
    """Графики, выбранные руководителем в кабинете (хранятся в базе)."""
    import time
    if time.time() - _cache["t"] > 30:
        try:
            with db() as c:
                _cache["shift"] = {r[0]: r[1] for r in c.execute("SELECT user_id, shift FROM emp_shift")}
                week = {}
                for uid, wd, key in c.execute("SELECT user_id, weekday, shift FROM emp_week"):
                    week.setdefault(uid, {})[wd] = key
                _cache["week"] = week
        except sqlite3.Error:
            pass
        _cache["t"] = time.time()
    return _cache


def employee(uid: int):
    base = config.EMPLOYEES.get(uid)
    if not base:
        return None
    e = dict(base)
    e["id"] = uid
    shift = _overrides()["shift"].get(uid) or e.get("shift")
    e["shift"] = shift if shift in SHIFTS else None
    return e


def week_plan(emp) -> dict:
    """{0: '9-18', 1: '9-18', ..., 6: 'off'} — график на каждый день недели."""
    custom = _overrides()["week"].get(emp["id"], {})
    days = emp.get("days", range(7))
    base = emp.get("shift") or "custom"
    return {d: custom.get(d, base if d in days else OFF) for d in range(7)}


def day_shift(emp, day):
    """График на конкретный день: dict со start/end/title или None (выходной)."""
    key = week_plan(emp)[day.weekday()]
    if key == OFF:
        return None
    if key in SHIFTS:
        return SHIFTS[key]
    # старый формат: start/end прямо у сотрудника
    if emp.get("start") and emp.get("end"):
        return {"title": f"{emp['start']}–{emp['end']}", "short": f"{emp['start']}–{emp['end']}",
                "start": emp["start"], "end": emp["end"]}
    return {"title": "Свободный график", "short": "своб.", "start": None, "end": None}


def shift_start(emp, day) -> str:
    return day_shift(emp, day)["start"]


def shift_end(emp, day) -> str:
    return day_shift(emp, day)["end"]


def workday(emp, day) -> bool:
    """Рабочий день с точным временем. Выходной или свободный график — без окон и штрафов."""
    sh = day_shift(emp, day)
    return bool(sh and sh.get("start") and sh.get("end"))


def is_free_day(emp, day) -> bool:
    sh = day_shift(emp, day)
    return bool(sh) and not (sh.get("start") and sh.get("end"))


def has_schedule(emp, day=None) -> bool:
    return workday(emp, day or now())


def shift_title(sh, lang=None) -> str:
    """Название графика на языке сотрудника (русский — как в config.py)."""
    if sh is None:
        return st.s(lang, "day_off")
    if st.lang_of(lang) == "ru":
        return sh["title"]
    if not (sh.get("start") and sh.get("end")):
        return st.s(lang, "free_schedule")
    return st.s(lang, "range", a=sh["start"], b=sh["end"])


def shift_short(sh, lang=None) -> str:
    if sh is None:
        return "—"
    if not (sh.get("start") and sh.get("end")):
        return st.s(lang, "free_short") if st.lang_of(lang) != "ru" else sh.get("short", "своб.")
    return sh.get("short") or f"{sh['start']}–{sh['end']}"


def schedule_text(emp, day=None, lang=None) -> str:
    return shift_title(day_shift(emp, day or now()), lang)


def week_list(emp, lang=None) -> list:
    """Для кабинета: [{'day': 'Пн', 'key': '9-18', 'short': '9–18'}, ...]"""
    out = []
    for d, key in week_plan(emp).items():
        if key == OFF:
            short = "—"
        elif key in SHIFTS:
            short = shift_short(SHIFTS[key], lang)
        else:
            short = shift_short(day_shift(emp, now() + timedelta(days=(d - now().weekday()) % 7)), lang)
        out.append({"day": st.WEEKDAYS[st.lang_of(lang)][d], "key": key, "short": short})
    return out


def set_week(uid: int, plan: dict):
    """Руководитель задаёт график на каждый день недели."""
    if not config.EMPLOYEES.get(uid):
        return False, "Сотрудник не найден."
    clean = {}
    for d in range(7):
        key = str(plan.get(str(d), plan.get(d, "")))
        if key != OFF and key not in SHIFTS:
            return False, f"Не выбран график на {WEEKDAYS[d]}."
        clean[d] = key
    with db() as c:
        c.execute("DELETE FROM emp_week WHERE user_id=?", (uid,))
        c.executemany("INSERT INTO emp_week(user_id, weekday, shift) VALUES(?, ?, ?)",
                      [(uid, d, k) for d, k in clean.items()])
        L = staff_lang(uid)
        lines = [f"{st.WEEKDAYS[L][d]} — {shift_title(None if k == OFF else SHIFTS[k], L)}" for d, k in clean.items()]
        notify(c, uid, st.s(L, "week_updated") + "\n" + "\n".join(lines))
    _cache["week"][uid] = clean
    return True, f"График сохранён: {name(uid)}"


def set_shift(uid: int, shift: str):
    """Один график на все рабочие дни (старый способ)."""
    if shift not in SHIFTS:
        return False, "Такого графика нет."
    emp = employee(uid)
    if not emp:
        return False, "Сотрудник не найден."
    plan = {d: (shift if k != OFF else OFF) for d, k in week_plan(emp).items()}
    return set_week(uid, plan)


def is_admin(uid: int) -> bool:
    return uid in config.ADMINS


def name(uid: int) -> str:
    e = config.EMPLOYEES.get(uid)
    if e:
        return e["name"]
    admins = getattr(config, "ADMIN_NAMES", {})
    if uid in admins:
        return admins[uid]
    return "Руководитель" if uid in config.ADMINS else str(uid)


WINDOW = getattr(config, "WINDOW_MINUTES", 15)


def window(emp, kind: str, day):
    """Окно, когда можно отметить приход (kind="in") или уход ("out")."""
    base = at(day, shift_start(emp, day) if kind == "in" else shift_end(emp, day))
    return base - timedelta(minutes=WINDOW), base + timedelta(minutes=WINDOW)


def window_text(emp, kind: str, day, lang=None) -> str:
    if not workday(emp, day):
        return st.s(lang, "any_time")
    a, b = window(emp, kind, day)
    return f"{a:%H:%M}–{b:%H:%M}"


def in_window(emp, kind: str, t: datetime) -> bool:
    if not workday(emp, t):
        return True  # в выходной — без ограничений
    a, b = window(emp, kind, t)
    if kind == "out":
        return a <= t  # уйти можно с начала окна (задержаться — можно)
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
            CREATE TABLE IF NOT EXISTS emp_week(user_id INTEGER, weekday INTEGER, shift TEXT,
                PRIMARY KEY(user_id, weekday));
            CREATE TABLE IF NOT EXISTS proof_pending(
                user_id INTEGER PRIMARY KEY, pressed_at TEXT, expires_at TEXT);
            CREATE TABLE IF NOT EXISTS proof_used(
                file_unique_id TEXT PRIMARY KEY, user_id INTEGER, used_at TEXT);
            CREATE TABLE IF NOT EXISTS clients(
                user_id INTEGER PRIMARY KEY, phone TEXT, first_name TEXT, username TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS news(
                id INTEGER PRIMARY KEY, text TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY, chat_id INTEGER, text TEXT, sent INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS investments(
                id INTEGER PRIMARY KEY, user_id INTEGER, amount INTEGER, months INTEGER, rate REAL,
                status TEXT DEFAULT 'pending', created_at TEXT, start_date TEXT, end_date TEXT,
                decided_at TEXT, paid_at TEXT, paid_amount INTEGER);
            CREATE TABLE IF NOT EXISTS invest_months(
                month TEXT PRIMARY KEY, level TEXT, set_at TEXT);
            CREATE TABLE IF NOT EXISTS invest_access(
                user_id INTEGER PRIMARY KEY, status TEXT, plan_amount TEXT, note TEXT,
                created_at TEXT, decided_at TEXT);
            CREATE TABLE IF NOT EXISTS staff_lang(user_id INTEGER PRIMARY KEY, lang TEXT);
            CREATE TABLE IF NOT EXISTS posts(
                id INTEGER PRIMARY KEY, owner_id INTEGER, text TEXT, photo TEXT, button_text TEXT,
                button_url TEXT, audience TEXT, to_news INTEGER DEFAULT 0, status TEXT, send_at TEXT,
                created_at TEXT, sent_at TEXT, total INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS projects(
                id INTEGER PRIMARY KEY, owner_id INTEGER, title TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS ptasks(
                id INTEGER PRIMARY KEY, project_id INTEGER, owner_id INTEGER, title TEXT, note TEXT,
                due_date TEXT, done_at TEXT, created_at TEXT);
            """
        )
        if "status" not in {r[1] for r in c.execute("PRAGMA table_info(tasks)")}:
            c.execute("ALTER TABLE tasks ADD COLUMN status TEXT DEFAULT 'open'")
            c.execute("UPDATE tasks SET status='done' WHERE done_at IS NOT NULL")
        if "due_date" not in {r[1] for r in c.execute("PRAGMA table_info(tasks)")}:
            c.execute("ALTER TABLE tasks ADD COLUMN due_date TEXT")
        ccols = {r[1] for r in c.execute("PRAGMA table_info(clients)")}
        for col, ddl in (("tier", "TEXT DEFAULT 'bronza'"), ("lang", "TEXT"), ("last_name", "TEXT"),
                         ("birth_date", "TEXT"), ("step", "TEXT")):
            if col not in ccols:
                c.execute(f"ALTER TABLE clients ADD COLUMN {col} {ddl}")
        ocols = {r[1] for r in c.execute("PRAGMA table_info(outbox)")}
        if "markup" not in ocols:
            c.execute("ALTER TABLE outbox ADD COLUMN markup TEXT")
        if "thread_id" not in ocols:
            c.execute("ALTER TABLE outbox ADD COLUMN thread_id INTEGER")
        if "photo" not in ocols:
            c.execute("ALTER TABLE outbox ADD COLUMN photo TEXT")
        if "texts" not in {r[1] for r in c.execute("PRAGMA table_info(news)")}:
            c.execute("ALTER TABLE news ADD COLUMN texts TEXT")
        if "texts" not in {r[1] for r in c.execute("PRAGMA table_info(posts)")}:
            c.execute("ALTER TABLE posts ADD COLUMN texts TEXT")
        if "post_id" not in ocols:
            c.execute("ALTER TABLE outbox ADD COLUMN post_id INTEGER")
        if "failed" not in ocols:
            c.execute("ALTER TABLE outbox ADD COLUMN failed INTEGER DEFAULT 0")
        icols = {r[1] for r in c.execute("PRAGMA table_info(investments)")}
        for col in ("early_at", "early_info", "pay_method", "pay_report"):
            if col not in icols:
                c.execute(f"ALTER TABLE investments ADD COLUMN {col} TEXT")
        if "code" not in {r[1] for r in c.execute("PRAGMA table_info(proof_pending)")}:
            c.execute("ALTER TABLE proof_pending ADD COLUMN code TEXT")
        cols = {r[1] for r in c.execute("PRAGMA table_info(penalties)")}
        if "note" not in cols:
            c.execute("ALTER TABLE penalties ADD COLUMN note TEXT")


def notify_admins(c, text: str, buttons=None):
    """buttons — [[("Текст", "callback_data"), ...], ...] — кнопки под сообщением."""
    import json
    markup = json.dumps(buttons, ensure_ascii=False) if buttons else None
    for a in config.ADMINS:
        c.execute("INSERT INTO outbox(chat_id, text, markup) VALUES(?, ?, ?)", (a, text, markup))


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
        return  # сегодняшнюю смену не закрываем: сотрудник может задержаться
    if emp and workday(emp, cin):
        end = at(cin.date(), shift_end(emp, cin))
    else:  # свободный график: считаем максимум 8 часов, но не позже конца дня
        end = min(cin + timedelta(hours=8), at(cin.date(), "23:59"))
    if end <= cin:
        end = cin
    c.execute("UPDATE sessions SET check_out=?, auto_closed=1 WHERE id=?", (end.strftime(FMT), s["id"]))
    notify_admins(
        c,
        f"🤷 {name(uid)} не отметил(а) уход {cin:%d.%m}. Закрыто автоматически в {end:%H:%M}.",
    )


def check_in(uid: int, t: datetime = None, dry_run: bool = False, notify_boss: bool = True):
    """Отметить приход. dry_run — только проверить, можно ли.
    t — время прихода (например, время нажатия кнопки, если ждали кружок)."""
    emp = employee(uid)
    L = staff_lang(uid)
    if not emp:
        return False, st.s(L, "not_staff")
    t = t or now()
    with db() as c:
        _close_stale(c, uid)
        s = _open_session(c, uid)
        if s:
            return False, st.s(L, "already_in", t=s['check_in'][11:16])
        if c.execute("SELECT 1 FROM sessions WHERE user_id=? AND substr(check_in, 1, 10) = ?",
                     (uid, t.strftime("%Y-%m-%d"))).fetchone() and workday(emp, t):
            return False, st.s(L, "shift_done")
        if not in_window(emp, "in", t):
            a, b = window(emp, "in", t)
            if t > b and not dry_run:
                notify_admins(c, f"⛔ {emp['name']} пытался(ась) отметить приход в {t:%H:%M} — окно закрыто ({a:%H:%M}–{b:%H:%M})")
            return False, st.s(L, "in_window_only", a=f"{a:%H:%M}", b=f"{b:%H:%M}", t=f"{t:%H:%M}")
        if dry_run:
            return True, ""
        late = 0
        if workday(emp, t):
            diff = int((t - at(t, shift_start(emp, t))).total_seconds() // 60)
            if diff > config.GRACE_MINUTES:
                late = diff
        c.execute("INSERT INTO sessions(user_id, check_in, late_min) VALUES(?, ?, ?)", (uid, t.strftime(FMT), late))
        if not notify_boss:
            pass
        elif late:
            notify_admins(c, f"⚠️ {emp['name']} пришёл(ла) в {t:%H:%M} — опоздание {fmt_min(late)} (по графику {shift_start(emp, t)})")
        else:
            notify_admins(c, f"✅ {emp['name']} пришёл(ла) в {t:%H:%M}")
    msg = st.s(L, "in_ok", t=f"{t:%H:%M}")
    with db() as c:
        today_tasks = _unfinished(c, uid)
    if today_tasks:
        msg += st.s(L, "tasks_today", n=len(today_tasks), btn=st.s(L, "btn_tasks"))
    if late:
        msg += st.s(L, "late", m=fmt_min(late, L))
    elif workday(emp, t):
        msg += st.s(L, "on_time")
    return True, msg


def check_out(uid: int):
    emp = employee(uid)
    L = staff_lang(uid)
    if not emp:
        return False, st.s(L, "not_staff")
    t = now()
    with db() as c:
        _close_stale(c, uid)
        s = _open_session(c, uid)
        if not s:
            return False, st.s(L, "not_in_yet")
        if not in_window(emp, "out", t):
            a, b = window(emp, "out", t)
            return False, st.s(L, "out_not_before", a=f"{a:%H:%M}", t=f"{t:%H:%M}")
        left = _unfinished(c, uid)
        if left:
            lines = "\n".join(f"• {r['title']}" + (" — " + st.s(L, "task_review") if r["status"] == "review" else "")
                              for r in left)
            return False, st.s(L, "out_blocked", lines=lines)
        cin = parse(s["check_in"])
        worked = int((t - cin).total_seconds() // 60)
        early = 0
        if workday(emp, t):
            diff = int((at(t, shift_end(emp, t)) - t).total_seconds() // 60)
            if diff > 0:
                early = diff
        c.execute("UPDATE sessions SET check_out=?, early_min=? WHERE id=?", (t.strftime(FMT), early, s["id"]))
        text = f"🚪 {emp['name']} ушёл(ла) в {t:%H:%M}, отработал(а) {fmt_min(worked)}"
        if early:
            text += f"\n⚠️ Ранний уход: на {fmt_min(early)} раньше графика ({shift_end(emp, t)})"
        notify_admins(c, text)
    msg = st.s(L, "out_ok", t=f"{t:%H:%M}", w=fmt_min(worked, L))
    if early:
        msg += st.s(L, "early", m=fmt_min(early, L))
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
    L = staff_lang(uid)
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
            "streak": _streak(c, uid, emp),
            "lang": L,
            "role": st.role(emp.get("role", "Сотрудник"), L),
            "schedule": schedule_text(emp, t, L),
            "has_schedule": not is_free_day(emp, t),
            "week": week_list(emp, L),
            "today_wd": t.weekday(),
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
            "penalties": penalties_list(c, uid, L),
            "penalties_total": penalties_total(c, uid),
            "workday": workday(emp, t),
            "in_window": window_text(emp, "in", t, L),
            "out_window": window_text(emp, "out", t, L),
            "can_in": in_window(emp, "in", t) and not s and not (today and workday(emp, t)),
            "unfinished": len(_unfinished(c, uid)),
            "can_out": bool(s) and in_window(emp, "out", t) and not _unfinished(c, uid),
        }
    return res


def history(uid: int, limit: int = 30) -> list:
    L = staff_lang(uid)
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
            "wd": st.WEEKDAYS[L][cin.weekday()],
            "in": f"{cin:%H:%M}",
            "out": f"{cout:%H:%M}" if cout else None,
            "worked": fmt_min(((cout or t) - cin).total_seconds() // 60, L),
            "late": fmt_min(r["late_min"], L) if r["late_min"] else None,
            "early": fmt_min(r["early_min"], L) if r["early_min"] else None,
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
            if c.execute("SELECT 1 FROM proof_pending WHERE user_id=?", (uid,)).fetchone():
                continue  # нажал «Пришёл», ждём кружок — штраф пока не выписываем
            until, start = f"{window(emp, 'in', t)[1]:%H:%M}", shift_start(emp, t)
            reason = st.s("ru", "late_reason", t=until, start=start)
            cur = c.execute(
                "INSERT OR IGNORE INTO penalties(user_id, day, reason, amount, note) VALUES(?, ?, 'late', ?, ?)",
                (uid, today, PENALTY_AMOUNT, reason),
            )
            if not cur.rowcount:
                continue  # штраф за сегодня уже выписан
            _announce(c, uid, "penalty", reason, PENALTY_AMOUNT,
                      reason_emp=st.s(staff_lang(uid), "late_reason", t=until, start=start))


def _announce(c, uid, title_key, reason, amount, reason_emp=None):
    """Сообщение о штрафе: в группу (или руководителям) — по-русски, самому сотруднику — на его языке."""
    text = f"{st.s('ru', title_key)}\n👤 {name(uid)}\n📅 {now():%d.%m.%Y}\nПричина: {reason}"
    if amount:
        text += f"\n💰 Сумма: {amount}"
    if PENALTY_CHAT_ID:
        c.execute("INSERT INTO outbox(chat_id, text) VALUES(?, ?)", (PENALTY_CHAT_ID, text))
    else:
        notify_admins(c, text)
    L = staff_lang(uid)
    mine = st.s(L, "penalty_msg", title=st.s(L, title_key), date=f"{now():%d.%m.%Y}", reason=reason_emp or reason)
    if amount:
        mine += st.s(L, "penalty_sum", amount=amount)
    notify(c, uid, mine)


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
        _announce(c, uid, "penalty", reason, amount)


def delete_penalty(pid: int):
    with db() as c:
        r = c.execute("SELECT * FROM penalties WHERE id=?", (pid,)).fetchone()
        if not r:
            return False, "Штраф не найден."
        c.execute("DELETE FROM penalties WHERE id=?", (pid,))
        _announce(c, r["user_id"], "penalty_cancelled", r["note"] or "—", r["amount"],
                  reason_emp=_penalty_reason(r, staff_lang(r["user_id"])))
    return True, "Штраф отменён."


def penalties_total(c, uid) -> int:
    return c.execute("SELECT COUNT(*) FROM penalties WHERE user_id=?", (uid,)).fetchone()[0]


def _penalty_reason(r, lang=None) -> str:
    """Причина штрафа на языке сотрудника (автоштраф за опоздание переводится, ручной — как написал руководитель)."""
    import re
    note = r["note"] or ""
    if r["reason"] == "late":
        m = re.search(r"до (\d\d:\d\d) \(график (\d\d:\d\d)\)", note)
        if m:
            return st.s(lang, "late_reason", t=m.group(1), start=m.group(2))
        return note or st.s(lang, "late_short")
    return note or st.s(lang, "late_short")


def penalties_list(c, uid, lang=None) -> list:
    rows = c.execute(
        "SELECT * FROM penalties WHERE user_id=? AND day >= ? ORDER BY id DESC",
        (uid, now().strftime("%Y-%m-01")),
    ).fetchall()
    return [
        {"id": r["id"], "date": datetime.strptime(r["day"], "%Y-%m-%d").strftime("%d.%m"),
         "reason": _penalty_reason(r, lang), "amount": r["amount"] or "",
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
        notify(c, uid, st.s(staff_lang(uid), "task_new", t=title))
        return cur.lastrowid


def _unfinished(c, uid) -> list:
    """Незакрытые задачи на сегодня (перенесённые на будущее не считаются)."""
    return c.execute("SELECT id, title, status, due_date FROM tasks WHERE user_id=? AND status IN ('open', 'review') "
                     "AND (due_date IS NULL OR due_date <= ?) ORDER BY id", (uid, now().strftime("%Y-%m-%d"))).fetchall()


def _all_open(c, uid) -> list:
    """Все незакрытые задачи, включая перенесённые на будущие дни."""
    return c.execute("SELECT id, title, status, due_date FROM tasks WHERE user_id=? AND status IN ('open', 'review') "
                     "ORDER BY due_date IS NOT NULL, due_date, id", (uid,)).fetchall()


def _due_label(due: str, lang=None):
    """'2026-11-24' -> 'Вт 24.11', если дата в будущем; иначе None."""
    if not due or due <= now().strftime("%Y-%m-%d"):
        return None
    d = datetime.strptime(due, "%Y-%m-%d")
    return f"{st.WEEKDAYS[st.lang_of(lang)][d.weekday()]} {d:%d.%m}"


def next_workday(uid: int) -> datetime:
    """Следующий рабочий день сотрудника по его графику (выходные пропускаются)."""
    emp = employee(uid)
    d = now() + timedelta(days=1)
    for _ in range(14):
        if not emp or day_shift(emp, d) is not None:
            return d
        d += timedelta(days=1)
    return now() + timedelta(days=1)


def tasks(uid: int) -> list:
    L = staff_lang(uid)
    with db() as c:
        rows = c.execute(
            "SELECT * FROM tasks WHERE user_id=? ORDER BY status='done', id DESC LIMIT 50", (uid,)
        ).fetchall()
    return [
        {"id": r["id"], "title": r["title"], "created": parse(r["created_at"]).strftime("%d.%m %H:%M"),
         "status": r["status"], "done": r["status"] == "done", "due": _due_label(r["due_date"], L)}
        for r in rows
    ]


def complete_task(task_id: int, uid: int):
    """Сотрудник нажал «Готово» — задача уходит руководителю на проверку."""
    L = staff_lang(uid)
    with db() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, uid)).fetchone()
        if not r:
            return False, st.s(L, "task_not_found")
        if r["status"] == "done":
            return False, st.s(L, "task_already_done")
        if r["status"] == "review":
            return False, st.s(L, "task_already_review")
        c.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
        notify_admins(c, f"📝 {name(uid)} выполнил(а) задачу — проверьте:\n{r['title']}",
                      [[("✅ Принять", f"task:ok:{task_id}"), ("↩️ Вернуть", f"task:back:{task_id}")],
                       [("📅 Оставить на завтра", f"task:later:{task_id}")]])
    return True, st.s(L, "task_sent")


def review_task(task_id: int, action):
    """Руководитель: action = "ok" (принять), "back" (вернуть) или "later" (на завтра).
    Для совместимости True/False = принять/вернуть."""
    if action is True:
        action = "ok"
    elif action is False:
        action = "back"
    with db() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not r:
            return False, "Задача не найдена (возможно, удалена)."
        if r["status"] == "done":
            return False, "Задача уже принята."
        if action == "later":
            d = next_workday(r["user_id"])
            label = f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"
            c.execute("UPDATE tasks SET status='open', due_date=? WHERE id=?", (d.strftime("%Y-%m-%d"), task_id))
            left = len(_unfinished(c, r["user_id"]))
            L = staff_lang(r["user_id"])
            extra = st.s(L, "none_left_moved") if not left else st.s(L, "left_today", n=left)
            notify(c, r["user_id"], st.s(L, "task_moved", d=f"{st.WEEKDAYS[L][d.weekday()]} {d:%d.%m}", t=r["title"]) + extra)
            return True, f"📅 Перенесено на {label}: {r['title']}"
        if action == "ok":
            c.execute("UPDATE tasks SET status='done', done_at=? WHERE id=?", (now().strftime(FMT), task_id))
            left = len(_unfinished(c, r["user_id"]))
            L = staff_lang(r["user_id"])
            extra = st.s(L, "none_left") if not left else st.s(L, "left", n=left)
            notify(c, r["user_id"], st.s(L, "task_accepted", t=r["title"]) + extra)
            return True, f"✅ Принято: {r['title']}"
        c.execute("UPDATE tasks SET status='open' WHERE id=?", (task_id,))
        notify(c, r["user_id"], st.s(staff_lang(r["user_id"]), "task_returned", t=r["title"]))
        return True, f"↩️ Возвращено: {r['title']}"


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
            elif is_free_day(emp, t):
                status, icon = "сегодня не отмечался(ась)", "⚪️"
            elif workday(emp, t) and t <= window(emp, "in", t)[1]:
                status, icon = f"смена с {shift_start(emp, t)}", "🕘"
            elif workday(emp, t):
                status, icon = "не пришёл(ла)", "🔴"
            else:
                status, icon = "выходной", "🌙"
            open_tasks = _all_open(c, uid)
            out.append({
                "id": uid,
                "name": emp["name"],
                "role": emp.get("role", "Сотрудник"),
                "schedule": schedule_text(emp, t),
                "week": week_list(emp),
                "icon": icon,
                "status": status,
                "late": fmt_min(first["late_min"]) if first and first["late_min"] else None,
                "penalties_month": penalties_month(c, uid),
                "penalties": penalties_list(c, uid),
                "penalties_total": penalties_total(c, uid),
                "total_hours": round(_hours(c, uid) + emp.get("hours_before", 0), 1),
                "tasks": [{"id": r["id"], "title": r["title"], "status": r["status"],
                           "due": _due_label(r["due_date"])} for r in open_tasks],
            })
    return out


# ---------- очередь уведомлений (их отправляет бот) ----------

def pending_outbox(limit: int = 20) -> list:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM outbox WHERE sent=0 ORDER BY id LIMIT ?", (limit,))]


def mark_sent(msg_id: int, failed: bool = False):
    with db() as c:
        c.execute("UPDATE outbox SET sent=1, failed=? WHERE id=?", (int(failed), msg_id))


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


# ---------- подтверждение прихода кружком ----------

PROOF_REQUIRED = getattr(config, "PROOF_REQUIRED", False)
PROOF_MINUTES = getattr(config, "PROOF_MINUTES", 3)
PROOF_MIN_SECONDS = getattr(config, "PROOF_MIN_SECONDS", 3)
PROOF_PROMPT = (
    "📹 Запишите кружок, чтобы подтвердить приход.\n\n"
    "🔢 Скажите в кружке вслух число: <b>{code}</b>\n\n"
    "Нажмите на 🎤 справа внизу — значок станет 📷. Затем зажмите его и запишите видео "
    "(не короче {min_sec} сек).\n"
    "⏳ У вас {minutes} мин."
)


def start_proof(uid: int):
    """Нажата кнопка «Пришёл»: проверяем, можно ли, и ждём кружок."""
    ok, msg = check_in(uid, dry_run=True)
    if not ok:
        return False, msg
    import secrets
    t = now()
    code = str(secrets.randbelow(90) + 10)  # случайное число 10–99
    with db() as c:
        c.execute("INSERT INTO proof_pending(user_id, pressed_at, expires_at, code) VALUES(?, ?, ?, ?) "
                  "ON CONFLICT(user_id) DO UPDATE SET pressed_at=excluded.pressed_at, "
                  "expires_at=excluded.expires_at, code=excluded.code",
                  (uid, t.strftime(FMT), (t + timedelta(minutes=PROOF_MINUTES)).strftime(FMT), code))
    return True, st.s(staff_lang(uid), "proof_prompt", minutes=PROOF_MINUTES, code=code, min_sec=PROOF_MIN_SECONDS)


def proof_pending(uid: int) -> bool:
    with db() as c:
        r = c.execute("SELECT expires_at FROM proof_pending WHERE user_id=?", (uid,)).fetchone()
    return bool(r) and parse(r[0]) >= now()


def take_proof(uid: int, file_unique_id: str = "", duration: int = 0, forwarded: bool = False):
    """Пришёл кружок: проверяем и засчитываем приход по времени нажатия кнопки.
    Возвращает (ok, сообщение, код)."""
    L = staff_lang(uid)
    btn = st.s(L, "btn_in")
    with db() as c:
        r = c.execute("SELECT pressed_at, expires_at, code FROM proof_pending WHERE user_id=?", (uid,)).fetchone()
        if not r:
            return False, st.s(L, "proof_first", btn=btn), None
        if parse(r[1]) < now():
            c.execute("DELETE FROM proof_pending WHERE user_id=?", (uid,))
            return False, st.s(L, "proof_expired", btn=btn), None
        # дальше — ошибки, которые можно исправить, пока время не вышло (запрос не удаляем)
        if forwarded:
            return False, st.s(L, "proof_forwarded"), None
        if duration < PROOF_MIN_SECONDS:
            return False, st.s(L, "proof_short", sec=PROOF_MIN_SECONDS), None
        if file_unique_id and c.execute("SELECT 1 FROM proof_used WHERE file_unique_id=?", (file_unique_id,)).fetchone():
            notify_admins(c, f"🚨 {name(uid)} отправил(а) кружок, который уже присылался раньше. Приход не засчитан.")
            return False, st.s(L, "proof_reused"), None
        c.execute("DELETE FROM proof_pending WHERE user_id=?", (uid,))
        if file_unique_id:
            c.execute("INSERT OR IGNORE INTO proof_used(file_unique_id, user_id, used_at) VALUES(?, ?, ?)",
                      (file_unique_id, uid, now().strftime(FMT)))
    ok, msg = check_in(uid, t=parse(r[0]), notify_boss=False)
    return ok, msg, r[2]


def last_checkin(uid: int):
    """('09:05', 5) — время последнего прихода и опоздание в минутах (для сообщения руководителю)."""
    with db() as c:
        r = c.execute("SELECT check_in, late_min FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    return (r["check_in"][11:16], r["late_min"] or 0) if r else ("—", 0)


def expired_proofs() -> list:
    """Кто нажал «Пришёл», но не прислал кружок вовремя (удаляем и возвращаем ID)."""
    t = now().strftime(FMT)
    with db() as c:
        ids = [r[0] for r in c.execute("SELECT user_id FROM proof_pending WHERE expires_at < ?", (t,))]
        c.execute("DELETE FROM proof_pending WHERE expires_at < ?", (t,))
    return ids


# ---------- клиенты (все, кто не сотрудник и не руководитель) ----------

def is_client(uid: int) -> bool:
    return not employee(uid) and not is_admin(uid)


def get_client(uid: int):
    with db() as c:
        r = c.execute("SELECT * FROM clients WHERE user_id=?", (uid,)).fetchone()
    return dict(r) if r else None


def save_client(uid: int, phone: str = None, first_name: str = "", username: str = "") -> bool:
    """Запомнить клиента. Возвращает True, если это новый клиент.
    Имя из Telegram записывается, только пока клиент не ввёл своё."""
    phone = ("+" + phone.lstrip("+")) if phone else None
    with db() as c:
        old = c.execute("SELECT phone FROM clients WHERE user_id=?", (uid,)).fetchone()
        if old:
            c.execute("UPDATE clients SET phone=COALESCE(?, phone), "
                      "first_name=CASE WHEN last_name IS NULL AND ?<>'' THEN ? ELSE first_name END, "
                      "username=COALESCE(NULLIF(?, ''), username) WHERE user_id=?",
                      (phone, first_name, first_name, username, uid))
            return False
        c.execute("INSERT INTO clients(user_id, phone, first_name, username, created_at) VALUES(?, ?, ?, ?, ?)",
                  (uid, phone, first_name, username, now().strftime(FMT)))
        return True


def clients_stats() -> dict:
    with db() as c:
        total = c.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        with_phone = c.execute("SELECT COUNT(*) FROM clients WHERE phone IS NOT NULL").fetchone()[0]
    return {"total": total, "with_phone": with_phone}


# ---------- новости для клиентов ----------

def add_news(text: str = "", texts: dict = None) -> int:
    """Новость для клиентов. texts — {'uz': ..., 'ru': ..., 'en': ...}: каждый клиент видит свой язык."""
    import json
    import i18n
    texts = {k: v[:2000] for k, v in i18n.clean_texts(texts).items()}
    if not texts and text.strip():
        texts = {i18n.DEFAULT: text.strip()[:2000]}
    main = i18n.pick(texts, "ru")
    with db() as c:
        return c.execute("INSERT INTO news(text, texts, created_at) VALUES(?, ?, ?)",
                         (main, json.dumps(texts, ensure_ascii=False), now().strftime(FMT))).lastrowid


def news_list(limit: int = 10, lang: str = None) -> list:
    """lang — язык клиента (текст на его языке); без lang — для руководителя (по-русски, если есть)."""
    import json
    import i18n
    with db() as c:
        rows = c.execute("SELECT * FROM news ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        texts = json.loads(r["texts"]) if r["texts"] else {}
        out.append({"id": r["id"], "text": (i18n.pick(texts, lang or "ru") if texts else r["text"]),
                    "langs": [k for k in i18n.LANGS if texts.get(k)],
                    "date": parse(r["created_at"]).strftime("%d.%m.%Y")})
    return out


def delete_news(news_id: int):
    with db() as c:
        c.execute("DELETE FROM news WHERE id=?", (news_id,))


CLIENT_TIERS = getattr(config, "CLIENT_TIERS", {"bronza": {"title": "Bronza", "emoji": "🥉"}})


def tier_of(cl) -> str:
    key = (cl or {}).get("tier") or "bronza"
    return key if key in CLIENT_TIERS else next(iter(CLIENT_TIERS))


def tier_title(key: str) -> str:
    t = CLIENT_TIERS.get(key, {})
    return f"{t.get('emoji', '')} {t.get('title', key)}".strip()


def set_client_tier(uid: int, tier: str):
    import i18n
    if tier not in CLIENT_TIERS:
        return False, "Такого статуса нет."
    with db() as c:
        r = c.execute("SELECT tier, first_name, lang FROM clients WHERE user_id=?", (uid,)).fetchone()
        if not r:
            return False, "Клиент не найден."
        c.execute("UPDATE clients SET tier=? WHERE user_id=?", (tier, uid))
        if (r["tier"] or "bronza") != tier:
            notify(c, uid, i18n.t(r["lang"], "tier_changed", tier=tier_title(tier)))
    return True, f"{r['first_name'] or uid}: {tier_title(tier)}"


CLIENT_FIELDS = {"phone", "first_name", "last_name", "birth_date", "lang", "step", "username"}


def update_client(uid: int, **fields):
    """Обновить поля клиента (создаёт запись, если её нет)."""
    fields = {k: v for k, v in fields.items() if k in CLIENT_FIELDS}
    save_client(uid)
    if fields:
        with db() as c:
            c.execute(f"UPDATE clients SET {', '.join(k + '=?' for k in fields)} WHERE user_id=?",
                      (*fields.values(), uid))


def client_full_name(cl) -> str:
    return " ".join(x for x in ((cl or {}).get("first_name"), (cl or {}).get("last_name")) if x)


def parse_birth(text: str):
    """'15.03.1995' / '15/03/1995' / '15-03-1995' -> '1995-03-15' или None."""
    import re
    m = re.fullmatch(r"\s*(\d{1,2})[./\-](\d{1,2})[./\-](\d{4})\s*", text or "")
    if not m:
        return None
    try:
        d = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    age = (now() - d).days / 365.25
    return d.strftime("%Y-%m-%d") if 5 <= age <= 110 else None


def clients_list(limit: int = 200) -> list:
    with db() as c:
        rows = c.execute("SELECT * FROM clients ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r["user_id"], "name": client_full_name(dict(r)), "username": r["username"] or "",
             "phone": r["phone"], "tier": tier_of(dict(r)), "lang": r["lang"] or "",
             "birth": datetime.strptime(r["birth_date"], "%Y-%m-%d").strftime("%d.%m.%Y") if r["birth_date"] else None,
             "since": parse(r["created_at"]).strftime("%d.%m.%Y")} for r in rows]
