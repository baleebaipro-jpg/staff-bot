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
NO_NAME = {"", "клиент", "client", "mijoz", "покупатель"}


def init() -> None:
    with core.db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS fu_sales(order_id TEXT PRIMARY KEY, sale_at TEXT, seller TEXT, uid INTEGER, "
                  "customer_id TEXT, customer TEXT, phone TEXT, total REAL, due TEXT, status TEXT, task_id INTEGER, at TEXT)")
        if "soft" not in {r[1] for r in c.execute("PRAGMA table_info(tasks)")}:
            c.execute("ALTER TABLE tasks ADD COLUMN soft INTEGER DEFAULT 0")  # напоминание: не блокирует уход
    if core.flag_once("fu_unskip_words"):  # тестовых клиентов теперь выбирает руководитель вручную
        with core.db() as c:
            c.execute("UPDATE fu_sales SET status='test' WHERE status='skip_test'")
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


def _skip_reason(r) -> str:
    """'noname' — клиент без имени, 'test' — тестовая / розничная карточка, '' — ставить обратную связь."""
    name = " ".join((r.get("customer") or "").split()).lower()
    if name in NO_NAME:
        return "noname"
    if _client_key(r) in {t["key"] for t in test_clients()}:
        return "test"
    return ""


def _client_key(r) -> str:
    return str(r.get("customer_id") or "").strip() or " ".join((r.get("customer") or "").split()).lower()


def test_clients() -> list:
    """Тестовые клиенты, отмеченные руководителем вручную: [{key, name}] — по ним обратная связь не ставится."""
    return core.flag_json("followup_test_clients") or []


def mark_test(key: str, name: str, on: bool):
    key = str(key or "").strip()
    if not key:
        return False, "Не выбран клиент"
    items = [t for t in test_clients() if t["key"] != key]
    if on:
        items.append({"key": key, "name": (name or "Клиент")[:80]})
    core.flag_json("followup_test_clients", items)
    with core.db() as c:
        if on:
            c.execute("UPDATE fu_sales SET status='skip_test' WHERE (customer_id=? OR lower(customer)=?) "
                      "AND status IN ('pending', 'test')", (key, key))
        else:
            c.execute("UPDATE fu_sales SET status=? WHERE (customer_id=? OR lower(customer)=?) AND status='skip_test'",
                      ("pending" if settings()["mode"] == "live" else "test", key, key))
    return True, (f"🧪 «{name}» — тестовый, обратной связи не будет" if on else f"↩️ «{name}» — снова обычный клиент")


def recent_clients(limit: int = 40) -> list:
    """Клиенты последних продаж (для выбора тестовых): ключ, имя, телефон, покупок, тестовый ли."""
    tests = {t["key"] for t in test_clients()}
    with core.db() as c:
        rows = c.execute("SELECT customer_id, customer, phone, COUNT(*) AS n, MAX(sale_at) AS last FROM fu_sales "
                         "WHERE status NOT IN ('skip_noclient', 'skip_noname') GROUP BY COALESCE(NULLIF(customer_id, ''), customer) "
                         "ORDER BY last DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        if _skip_reason(dict(r, customer_id="", customer=r["customer"])) == "noname":
            continue
        k = _client_key(r)
        out.append({"key": k, "name": r["customer"], "phone": r["phone"] or "", "n": r["n"], "test": k in tests})
    return out


def _prepare(rows: list) -> list:
    """Убрать клиентов без имени и тестовых (отметить в базе), несколько покупок одного клиента — одной задачей."""
    out, skip = {}, []
    for r in rows:
        why = _skip_reason(r)
        if why:
            skip.append(("skip_" + why, r["order_id"]))
            continue
        key = (r["seller"] or "", r["customer_id"] or r["customer"].lower())
        m = out.get(key)
        if not m:
            out[key] = dict(r, ids=[r["order_id"]], n=1)
        else:
            m["ids"].append(r["order_id"])
            m["n"] += 1
            m["total"] = (m["total"] or 0) + (r["total"] or 0)
            m["sale_at"] = max(m["sale_at"], r["sale_at"])
            m["uid"] = m["uid"] or r["uid"]
            m["phone"] = m["phone"] or r["phone"]
    if skip:
        with core.db() as c:
            c.executemany("UPDATE fu_sales SET status=? WHERE order_id=?", skip)
    return list(out.values())


def _title(r) -> str:
    n = r.get("n", 1)
    return (f"📞 Обратная связь: {r['customer']}" + (f" · {r['phone']}" if r["phone"] else "")
            + f" · {'покупка' if n == 1 else f'{n} покупки'} {r['sale_at'][8:10]}.{r['sale_at'][5:7]} на {_money(r['total'])}")


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
        by.setdefault(uid or ("?" + (r["seller"] or "")), []).append(r)
    if not rows:
        return title + "\n\nПродаж для обратной связи нет."
    lines = [title]
    for uid, items in sorted(by.items(), key=lambda x: (isinstance(x[0], str), core.name(x[0]) if isinstance(x[0], int) else x[0])):
        head = f"👤 {core.name(uid)}" if isinstance(uid, int) else f"❓ Продавец «{items[0]['seller'] or '—'}» не связан с сотрудником"
        lines.append(f"\n{head} — {len(items)}:")
        lines += [f"• {r['customer']}{' · ' + r['phone'] if r['phone'] else ''} · {r['sale_at'][8:10]}.{r['sale_at'][5:7]} · "
                  f"{_money(r['total'])}{' (' + str(r['n']) + ' покупки)' if r.get('n', 1) > 1 else ''}" for r in items[:30]]
        if len(items) > 30:
            lines.append(f"…и ещё {len(items) - 30}")
    return "\n".join(lines)[:4000]


def run_now():
    """Кнопка «📌 Поставить задачи сейчас»: по всем ожидающим продажам — задачи продавцам сразу (для проверки)."""
    scan()
    with core.db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM fu_sales WHERE status IN ('pending', 'test') ORDER BY sale_at")]
    n = _create_tasks(_prepare(rows))
    return True, f"📌 Поставлено задач: {n}" + ("" if n else " — нет продаж с продавцом, связанным с сотрудником")


def _create_tasks(rows: list) -> int:
    import sellers
    n = 0
    with core.db() as c:
        for r in rows:
            uid = r["uid"] or sellers.employee_of(r["seller"])
            ids = r.get("ids") or [r["order_id"]]
            if not uid or not core.employee(uid):
                c.executemany("UPDATE fu_sales SET status='skip_unlinked' WHERE order_id=?", [(i,) for i in ids])
                continue
            cur = c.execute("INSERT INTO tasks(user_id, title, created_at, due_date, soft) VALUES(?, ?, ?, ?, 1)",
                            (uid, _title(r), core.now().strftime(core.FMT), core.now().strftime("%Y-%m-%d")))
            c.executemany("UPDATE fu_sales SET status='task', task_id=?, uid=? WHERE order_id=?",
                          [(cur.lastrowid, uid, i) for i in ids])
            core.notify(c, uid, core.st.s(core.staff_lang(uid), "task_new", t=_title(r)))
            n += 1
    return n


def run_due(force_preview: bool = False):
    """Утром: продажи вчерашнего дня (и раньше, если пропущены) → задачи (live) или предпросмотр руководителю (test)."""
    import sellers
    rows = _prepare(due_rows())
    mode = settings()["mode"]
    if mode == "test" or force_preview:
        if rows or force_preview:
            text = preview_text(rows, "🧪 ТЕСТ · обратная связь после покупки (менеджерам НЕ отправлено)" if mode == "test"
                                else "👁 Предпросмотр · обратная связь после покупки")
            with core.db() as c:
                core.notify_admins(c, text)
                if mode == "test":
                    c.executemany("UPDATE fu_sales SET status='test' WHERE order_id=?", [(i,) for r in rows for i in r["ids"]])
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
            "unlinked": cnt.get("skip_unlinked", 0), "noname": cnt.get("skip_noname", 0),
            "skiptest": cnt.get("skip_test", 0), "hour": HOUR}


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
    return diag + "\n\n" + preview_text(_prepare(rows), "👁 Предпросмотр · обратная связь после покупки (никому не отправлено)")
