"""Личные проекты руководителя: проекты → задачи.

Видит и меняет только тот руководитель, который создал проект.
У задачи может быть дата (дедлайн). Задачи с датой попадают в «Мой день»:
на сегодня, просроченные и ближайшие. Задачи без даты просто хранятся
в проекте, пока руководитель их дорабатывает.
"""
from datetime import datetime, timedelta

import config
import core

DAY = "%Y-%m-%d"
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DIGEST_TIME = getattr(config, "PROJECTS_DIGEST_TIME", "08:30")  # None — не присылать утренний список
UPCOMING_DAYS = 7


def _today():
    return core.now().date()


def _due(value):
    """'' / None → None; 'YYYY-MM-DD' → та же строка; иначе ValueError."""
    value = str(value or "").strip()
    if not value:
        return None
    return datetime.strptime(value, DAY).strftime(DAY)


def _label(due: str) -> str:
    d = datetime.strptime(due, DAY).date()
    t = _today()
    if d == t:
        return "сегодня"
    if d == t + timedelta(days=1):
        return "завтра"
    if d == t - timedelta(days=1):
        return "вчера"
    return f"{WD[d.weekday()]} {d:%d.%m}" + ("" if d.year == t.year else f".{d.year}")


def _own_project(c, owner: int, pid: int):
    return c.execute("SELECT * FROM projects WHERE id=? AND owner_id=?", (pid, owner)).fetchone()


def _own_task(c, owner: int, tid: int):
    return c.execute("SELECT * FROM ptasks WHERE id=? AND owner_id=?", (tid, owner)).fetchone()


# ---------- проекты ----------

def add_project(owner: int, title: str):
    title = str(title or "").strip()[:100]
    if not title:
        return False, "Введите название проекта."
    with core.db() as c:
        c.execute("INSERT INTO projects(owner_id, title, created_at) VALUES(?, ?, ?)",
                  (owner, title, core.now().strftime(core.FMT)))
    return True, f"📁 Проект создан: {title}"


def rename_project(owner: int, pid: int, title: str):
    title = str(title or "").strip()[:100]
    if not title:
        return False, "Введите название проекта."
    with core.db() as c:
        if not _own_project(c, owner, pid):
            return False, "Проект не найден."
        c.execute("UPDATE projects SET title=? WHERE id=?", (title, pid))
    return True, "Название сохранено."


def delete_project(owner: int, pid: int):
    with core.db() as c:
        p = _own_project(c, owner, pid)
        if not p:
            return False, "Проект не найден."
        c.execute("DELETE FROM ptasks WHERE project_id=?", (pid,))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
    return True, f"Проект удалён: {p['title']}"


# ---------- задачи ----------

def add_task(owner: int, pid: int, title: str, due: str = ""):
    title = str(title or "").strip()[:300]
    if not title:
        return False, "Введите задачу."
    try:
        due = _due(due)
    except ValueError:
        return False, "Неверная дата."
    with core.db() as c:
        if not _own_project(c, owner, pid):
            return False, "Проект не найден."
        c.execute("INSERT INTO ptasks(project_id, owner_id, title, note, due_date, created_at) VALUES(?, ?, ?, '', ?, ?)",
                  (pid, owner, title, due, core.now().strftime(core.FMT)))
    return True, f"📅 Задача на {_label(due)}" if due else "📝 Задача сохранена (без даты)"


def update_task(owner: int, tid: int, title: str, note: str, due: str):
    title = str(title or "").strip()[:300]
    if not title:
        return False, "Введите задачу."
    try:
        due = _due(due)
    except ValueError:
        return False, "Неверная дата."
    with core.db() as c:
        if not _own_task(c, owner, tid):
            return False, "Задача не найдена."
        c.execute("UPDATE ptasks SET title=?, note=?, due_date=? WHERE id=?",
                  (title, str(note or "").strip()[:3000], due, tid))
    return True, f"Сохранено · дедлайн: {_label(due)}" if due else "Сохранено · без даты"


def set_done(owner: int, tid: int, done: bool):
    with core.db() as c:
        t = _own_task(c, owner, tid)
        if not t:
            return False, "Задача не найдена."
        c.execute("UPDATE ptasks SET done_at=? WHERE id=?", (core.now().strftime(core.FMT) if done else None, tid))
    return True, ("✅ Выполнено: " if done else "↩️ Снова в работе: ") + t["title"]


def delete_task(owner: int, tid: int):
    with core.db() as c:
        if not _own_task(c, owner, tid):
            return False, "Задача не найдена."
        c.execute("DELETE FROM ptasks WHERE id=?", (tid,))
    return True, "Задача удалена."


# ---------- для кабинета ----------

def overview(owner: int) -> dict:
    today = _today()
    t, soon = today.strftime(DAY), (today + timedelta(days=UPCOMING_DAYS)).strftime(DAY)
    month_ago = (core.now() - timedelta(days=30)).strftime(core.FMT)
    with core.db() as c:
        projects = [dict(r) for r in c.execute(
            "SELECT * FROM projects WHERE owner_id=? ORDER BY id DESC", (owner,))]
        tasks = [dict(r) for r in c.execute(
            "SELECT * FROM ptasks WHERE owner_id=? AND (done_at IS NULL OR done_at>=?) ORDER BY id",
            (owner, month_ago))]
    names = {p["id"]: p["title"] for p in projects}
    for x in tasks:
        x["project"] = names.get(x["project_id"], "")
        x["done"] = bool(x["done_at"])
        x["due_label"] = _label(x["due_date"]) if x["due_date"] else None
        x["overdue"] = bool(x["due_date"] and not x["done"] and x["due_date"] < t)
    for p in projects:
        mine = [x for x in tasks if x["project_id"] == p["id"]]
        p["open"] = sum(1 for x in mine if not x["done"])
        p["undated"] = sum(1 for x in mine if not x["done"] and not x["due_date"])
        p["done"] = sum(1 for x in mine if x["done"])
        p["overdue"] = sum(1 for x in mine if x["overdue"])
    by_due = lambda x: (x["due_date"], x["id"])
    open_dated = [x for x in tasks if not x["done"] and x["due_date"]]
    return {
        "projects": projects,
        "tasks": tasks,
        "today": today.strftime("%d.%m"),
        "day": {
            "overdue": sorted([x for x in open_dated if x["due_date"] < t], key=by_due),
            "today": [x for x in open_dated if x["due_date"] == t],
            "done_today": [x for x in tasks if x["done"] and x["due_date"] == t],
            "upcoming": sorted([x for x in open_dated if t < x["due_date"] <= soon], key=by_due),
        },
    }


# ---------- утренний список в бот ----------

def morning_digest() -> None:
    """Один раз в день в DIGEST_TIME присылает каждому руководителю его задачи на день."""
    if not DIGEST_TIME:
        return
    now = core.now()
    if now.strftime("%H:%M") < DIGEST_TIME:
        return
    for owner in config.ADMINS:
        day = overview(owner)["day"]
        if not (day["today"] or day["overdue"]):
            continue
        if not core.flag_once(f"proj_digest:{owner}:{now:%Y-%m-%d}"):
            continue
        lines = [f"☀️ Мой день · {WD[now.weekday()]} {now:%d.%m}"]
        if day["overdue"]:
            lines.append("\n🔴 Просрочено:")
            lines += [f"• {x['title']} — {x['due_label']} ({x['project']})" for x in day["overdue"]]
        if day["today"]:
            lines.append("\n📅 Сегодня:")
            lines += [f"• {x['title']} ({x['project']})" for x in day["today"]]
        lines.append("\nОтметить выполненное — в кабинете → 📁 Проекты.")
        with core.db() as c:
            core.notify(c, owner, "\n".join(lines))
