"""Обратная связь после покупки: на следующий день после продажи в BILLZ — задача продавцу связаться с клиентом.

- Берутся только НОВЫЕ продажи — с момента включения (старые не трогаем).
- Продавец BILLZ → сотрудник по связи «Продавцы BILLZ ↔ сотрудники» (sellers.py).
- ТЕСТ (по умолчанию): задачи НЕ ставятся, руководителю утром — предпросмотр «кому что поставилось бы».
- ВКЛЮЧЕНО: задача сотруднику в его список; уход НЕ блокирует (это напоминание).
"""
from datetime import datetime, timedelta

import config
import core

HOUR = getattr(config, "FOLLOWUP_HOUR", 10)  # во сколько утром ставить / слать предпросмотр
_tick = {"at": 0}


def init() -> None:
    with core.db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS fu_sales(order_id TEXT PRIMARY KEY, sale_at TEXT, seller TEXT, uid INTEGER, "
                  "customer_id TEXT, customer TEXT, phone TEXT, total REAL, due TEXT, status TEXT, task_id INTEGER, at TEXT)")
        if "soft" not in {r[1] for r in c.execute("PRAGMA table_info(tasks)")}:
            c.execute("ALTER TABLE tasks ADD COLUMN soft INTEGER DEFAULT 0")  # напоминание: не блокирует уход
    if not core.flag_json("followup"):
        core.flag_json("followup", {"mode": "test", "since": core.now().strftime(core.FMT)})


def settings() -> dict:
    return core.flag_json("followup") or {"mode": "test", "since": core.now().strftime(core.FMT)}


def set_mode(mode: str):
    st = settings()
    st["mode"] = "live" if mode == "live" else "test"
    core.flag_json("followup", st)
    return True, ("✅ Включено: задачи «обратная связь» будут ставиться менеджерам" if st["mode"] == "live"
                  else "🧪 Тестовый режим: задачи не ставятся, предпросмотр — только вам")


def _money(n) -> str:
    return f"{int(round(n or 0)):,}".replace(",", " ")


def scan() -> int:
    """Новые продажи из копии BILLZ → в очередь обратной связи (на следующий день)."""
    import sellers
    since = settings()["since"]
    n = 0
    with core.db() as c:
        rows = c.execute("SELECT s.* FROM billz_sales s LEFT JOIN fu_sales f ON f.order_id=s.order_id "
                         "WHERE f.order_id IS NULL AND s.order_type='SALE' AND s.date >= ?", (since,)).fetchall()
        for r in rows:
            seller = (r["sellers"] or "").split(",")[0].strip()
            uid = sellers.employee_of(seller) if seller else None
            cl = c.execute("SELECT * FROM billz_clients WHERE id=?", (r["customer_id"],)).fetchone() if r["customer_id"] else None
            name = (r["customer_name"] or "").strip() or (" ".join(x for x in ((cl["first_name"] if cl else ""),
                                                                                    (cl["last_name"] if cl else "")) if x))
            phone = (cl["phone"] if cl else "") or ""
            due = (datetime.strptime(r["date"][:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            status = "skip_noclient" if not r["customer_id"] else "pending"
            c.execute("INSERT OR IGNORE INTO fu_sales(order_id, sale_at, seller, uid, customer_id, customer, phone, total, due, "
                      "status, at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (r["order_id"], r["date"], seller, uid, r["customer_id"], name or "Клиент", phone, r["total"], due, status,
                       core.now().strftime(core.FMT)))
            n += 1
    return n


def _title(r) -> str:
    return (f"📞 Обратная связь: {r['customer']}" + (f" · {r['phone']}" if r["phone"] else "")
            + f" · покупка {r['sale_at'][8:10]}.{r['sale_at'][5:7]} на {_money(r['total'])}")


def due_rows(day: str = "") -> list:
    day = day or core.now().strftime("%Y-%m-%d")
    with core.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM fu_sales WHERE status='pending' AND due <= ? ORDER BY sale_at",
                                           (day,))]


def preview_text(rows: list, title: str) -> str:
    import sellers
    by = {}
    for r in rows:
        uid = r["uid"] or sellers.employee_of(r["seller"])  # связь могли добавить позже
        by.setdefault(uid, []).append(r)
    if not rows:
        return title + "\n\nПродаж для обратной связи нет."
    lines = [title]
    for uid, items in sorted(by.items(), key=lambda x: (x[0] is None, core.name(x[0]) if x[0] else "")):
        head = f"👤 {core.name(uid)}" if uid else f"❓ Продавец «{items[0]['seller'] or '—'}» не связан с сотрудником"
        lines.append(f"\n{head} — {len(items)}:")
        lines += [f"• {r['customer']}{' · ' + r['phone'] if r['phone'] else ''} · {r['sale_at'][8:10]}.{r['sale_at'][5:7]} · "
                  f"{_money(r['total'])}" for r in items[:30]]
        if len(items) > 30:
            lines.append(f"…и ещё {len(items) - 30}")
    return "\n".join(lines)[:4000]


def run_now():
    """Кнопка «📌 Поставить задачи сейчас»: по всем ожидающим продажам — задачи продавцам сразу (для проверки)."""
    scan()
    with core.db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM fu_sales WHERE status IN ('pending', 'test') ORDER BY sale_at")]
    n = _create_tasks(rows)
    return True, f"📌 Поставлено задач: {n}" + ("" if n else " — нет продаж с продавцом, связанным с сотрудником")


def _create_tasks(rows: list) -> int:
    import sellers
    n = 0
    with core.db() as c:
        for r in rows:
            uid = r["uid"] or sellers.employee_of(r["seller"])
            if not uid or not core.employee(uid):
                c.execute("UPDATE fu_sales SET status='skip_unlinked' WHERE order_id=?", (r["order_id"],))
                continue
            cur = c.execute("INSERT INTO tasks(user_id, title, created_at, due_date, soft) VALUES(?, ?, ?, ?, 1)",
                            (uid, _title(r), core.now().strftime(core.FMT), core.now().strftime("%Y-%m-%d")))
            c.execute("UPDATE fu_sales SET status='task', task_id=?, uid=? WHERE order_id=?", (cur.lastrowid, uid, r["order_id"]))
            core.notify(c, uid, core.st.s(core.staff_lang(uid), "task_new", t=_title(r)))
            n += 1
    return n


def run_due(force_preview: bool = False):
    """Утром: продажи вчерашнего дня (и раньше, если пропущены) → задачи (live) или предпросмотр руководителю (test)."""
    import sellers
    rows = due_rows()
    mode = settings()["mode"]
    if mode == "test" or force_preview:
        if rows or force_preview:
            text = preview_text(rows, "🧪 ТЕСТ · обратная связь после покупки (менеджерам НЕ отправлено)" if mode == "test"
                                else "👁 Предпросмотр · обратная связь после покупки")
            with core.db() as c:
                core.notify_admins(c, text)
                if mode == "test":
                    c.executemany("UPDATE fu_sales SET status='test' WHERE order_id=?", [(r["order_id"],) for r in rows])
        return len(rows)
    return _create_tasks(rows)


def tick() -> None:
    """Из цикла бота: раз в 5 минут — забрать новые продажи; в HOUR:00 и позже — обработать продажи к сегодняшнему дню."""
    import time
    if time.time() - _tick["at"] < 300:
        return
    _tick["at"] = time.time()
    scan()
    if core.now().hour >= HOUR:
        run_due()


def status() -> dict:
    today = core.now().strftime("%Y-%m-%d")
    with core.db() as c:
        cnt = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM fu_sales GROUP BY status")}
        tomorrow = c.execute("SELECT COUNT(*) FROM fu_sales WHERE status='pending' AND due > ?", (today,)).fetchone()[0]
    st = settings()
    return {"mode": st["mode"], "since": st["since"][:16], "pending": cnt.get("pending", 0), "tomorrow": tomorrow,
            "tasks": cnt.get("task", 0), "test": cnt.get("test", 0), "noclient": cnt.get("skip_noclient", 0),
            "unlinked": cnt.get("skip_unlinked", 0), "hour": HOUR}


def fetch_today() -> str:
    """Загрузить сегодняшние продажи из BILLZ прямо сейчас и вернуть строку-диагностику."""
    import erp
    if not erp.enabled():
        return "⚠️ BILLZ не подключён (нет BILLZ_SECRET_TOKEN в Railway)."
    today = core.now().strftime("%Y-%m-%d")
    try:
        n = erp.mirror_sales(today)
    except Exception as e:  # noqa: BLE001
        return f"⚠️ BILLZ не отдал продажи за сегодня: {str(e)[:200]}"
    with core.db() as c:
        sales = c.execute("SELECT COUNT(*) FROM billz_sales WHERE date >= ? AND order_type='SALE'", (today,)).fetchone()[0]
        withs = c.execute("SELECT COUNT(*) FROM billz_sales WHERE date >= ? AND order_type='SALE' AND sellers != ''",
                          (today,)).fetchone()[0]
        withc = c.execute("SELECT COUNT(*) FROM billz_sales WHERE date >= ? AND order_type='SALE' AND customer_id IS NOT NULL "
                          "AND customer_id != ''", (today,)).fetchone()[0]
        last = c.execute("SELECT date, sellers, customer_name FROM billz_sales WHERE date >= ? ORDER BY date DESC LIMIT 1",
                         (today,)).fetchone()
    keys = (core.flag_json("billz_mirror_keys") or {}).get("seller_fields")
    out = f"📥 BILLZ сегодня: строк {n}, продаж {sales} (с продавцом {withs}, с клиентом {withc})."
    if last:
        out += f"\nПоследняя: {last['date'][11:16]} · продавец «{last['sellers'] or '—'}» · клиент «{last['customer_name'] or '—'}»"
    if sales and not withs:
        out += f"\n⚠️ Продавец не найден в данных BILLZ. Поля: {str(keys)[:200]}"
    return out


def preview_now() -> str:
    """Кнопка «👁 Проверить сейчас»: сначала свежие продажи из BILLZ, затем — что получили бы менеджеры."""
    diag = fetch_today()
    scan()
    with core.db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM fu_sales WHERE status IN ('pending', 'test') ORDER BY sale_at")]
    return diag + "\n\n" + preview_text(rows, "👁 Предпросмотр · обратная связь после покупки (никому не отправлено)")
