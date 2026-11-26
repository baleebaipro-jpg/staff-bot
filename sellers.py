"""Связь продавцов BILLZ с сотрудниками команды (Махира в BILLZ = Moxira Isxakova в боте).

Имена продавцов берутся из зеркала продаж (billz_sales.sellers). Система сама предлагает сотрудника
по похожему имени (кириллица/латиница, о/а, сокращения), руководитель проверяет и сохраняет.
Нужна для отчётов по менеджерам, задач «связаться с клиентом через неделю после покупки» и KPI.
"""
import difflib
import re
from datetime import timedelta

import config
import core

TR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k",
      "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "ts",
      "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya", "ў": "o", "қ": "q", "ғ": "g",
      "ҳ": "h"}


def init() -> None:
    with core.db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS seller_links(billz_name TEXT PRIMARY KEY, uid INTEGER, updated_at TEXT)")


def _norm(s: str) -> str:
    s = "".join(TR.get(ch, ch) for ch in (s or "").lower())
    s = s.replace("kh", "x").replace("h", "x").replace("'", "").replace("‘", "").replace("ʻ", "")
    return re.sub(r"[^a-z ]+", " ", s).strip()


def _score(billz: str, emp: str) -> float:
    a, b = _norm(billz).split(), _norm(emp).split()
    if not a or not b:
        return 0
    fold = lambda w: w.replace("o", "a").replace("y", "i").replace("u", "o")  # noqa: E731 — Махира = Moxira, Камола = Komola
    first = max(difflib.SequenceMatcher(None, a[0], b[0]).ratio(), difflib.SequenceMatcher(None, fold(a[0]), fold(b[0])).ratio())
    whole = difflib.SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
    # фамилия совпадает хотя бы первой буквой — плюс
    last = 0.1 if len(a) > 1 and len(b) > 1 and a[1][:1] == b[1][:1] else 0
    return max(first * 0.85 + last, whole)


def suggest(name: str):
    """Сотрудник с самым похожим именем (или None, если похожих нет)."""
    best, best_s = None, 0
    for uid, e in config.EMPLOYEES.items():
        s = _score(name, e["name"])
        if s > best_s:
            best, best_s = uid, s
    return best if best_s >= 0.72 else None


def billz_sellers(days: int = 180) -> list:
    """Продавцы из продаж BILLZ за период: имя, сколько продаж, последняя продажа."""
    since = (core.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    stat = {}
    with core.db() as c:
        for r in c.execute("SELECT sellers, date FROM billz_sales WHERE date >= ? AND sellers != '' AND order_type='SALE'",
                           (since,)):
            for n in [x.strip() for x in (r["sellers"] or "").split(",") if x.strip()]:
                s = stat.setdefault(n, {"name": n, "sales": 0, "last": ""})
                s["sales"] += 1
                s["last"] = max(s["last"], r["date"] or "")
    return sorted(stat.values(), key=lambda x: -x["sales"])


def links() -> dict:
    with core.db() as c:
        return {r["billz_name"]: r["uid"] for r in c.execute("SELECT * FROM seller_links")}


def overview() -> list:
    """Для экрана настройки: каждый продавец BILLZ — связанный сотрудник или предложение системы."""
    saved = links()
    out = []
    for s in billz_sellers():
        uid = saved.get(s["name"])
        sug = None if s["name"] in saved else suggest(s["name"])
        out.append(dict(s, uid=uid or 0, suggested=sug or 0, saved=s["name"] in saved))
    return out


def save(mapping: dict):
    """mapping: {имя в BILLZ: ID сотрудника или 0 (не сотрудник / не связывать)}."""
    t = core.now().strftime(core.FMT)
    n = 0
    with core.db() as c:
        for name, uid in (mapping or {}).items():
            uid = int(uid or 0)
            if uid and not core.employee(uid):
                continue
            c.execute("INSERT INTO seller_links(billz_name, uid, updated_at) VALUES(?, ?, ?) "
                      "ON CONFLICT(billz_name) DO UPDATE SET uid=excluded.uid, updated_at=excluded.updated_at",
                      (str(name)[:100], uid, t))
            n += 1
    return True, f"🔗 Сохранено связей: {n}"


def diagnose() -> dict:
    """Что лежит в копии продаж BILLZ: всего строк, за неделю, сколько с продавцом, когда обновлялось."""
    import erp
    week = (core.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    with core.db() as c:
        total = c.execute("SELECT COUNT(*) FROM billz_sales").fetchone()[0]
        w = c.execute("SELECT COUNT(*) FROM billz_sales WHERE date >= ?", (week,)).fetchone()[0]
        ws = c.execute("SELECT COUNT(*) FROM billz_sales WHERE date >= ? AND sellers != ''", (week,)).fetchone()[0]
        last = c.execute("SELECT MAX(date) FROM billz_sales").fetchone()[0] or ""
    m = core.flag_json("billz_mirror") or {}
    return {"total": total, "week": w, "week_sellers": ws, "last_sale": last[:16], "mirror_at": m.get("at", ""),
            "mirror_last": m.get("last", ""), "report_on": bool(erp._watch.get("report", True)),
            "billz_on": erp.enabled(), "keys": core.flag_json("billz_mirror_keys") or {}}


def refresh_week():
    """Загрузить из BILLZ продажи за последние 7 дней (старые не берём)."""
    import erp
    if not erp.enabled():
        return False, "BILLZ не подключён (нет ключа BILLZ_SECRET_TOKEN в Railway)."
    d1 = (core.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    try:
        n = erp.mirror_sales(d1, core.now().strftime("%Y-%m-%d"))
    except erp.BillzError as e:
        return False, f"BILLZ не отдал продажи: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"Ошибка загрузки: {e}"
    return True, f"📥 Загружено строк продаж за неделю: {n}"


def billz_names() -> dict:
    """Имена продавцов для выбора: из BILLZ (список сотрудников) + из продаж; {names, source, error}."""
    import erp
    res = {"names": [], "source": "", "error": ""}
    if erp.enabled():
        try:
            res = erp.billz_users()
        except Exception as e:  # noqa: BLE001
            res["error"] = str(e)[:200]
    names = list(res.get("names") or [])
    for s in billz_sellers():
        if s["name"] not in names:
            names.append(s["name"])
    return {"names": sorted(names), "source": res.get("source") or ("продажи" if names else ""), "error": res.get("error", "")}


def by_employee() -> list:
    """Сотрудники команды и как каждый записан в BILLZ (без продаж — руководитель вписывает сам)."""
    saved = links()
    return [{"id": uid, "name": e["name"], "billz": [n for n, u in saved.items() if u == uid]}
            for uid, e in config.EMPLOYEES.items()]


def save_by_employee(mapping: dict):
    """mapping: {ID сотрудника: "Махира, Махира И."} — имена в BILLZ через запятую (пусто — убрать связь)."""
    t = core.now().strftime(core.FMT)
    n = 0
    with core.db() as c:
        for uid, names in (mapping or {}).items():
            uid = int(uid)
            if not core.employee(uid):
                continue
            c.execute("DELETE FROM seller_links WHERE uid=?", (uid,))
            for name in [x.strip()[:100] for x in str(names or "").split(",") if x.strip()]:
                c.execute("INSERT INTO seller_links(billz_name, uid, updated_at) VALUES(?, ?, ?) "
                          "ON CONFLICT(billz_name) DO UPDATE SET uid=excluded.uid, updated_at=excluded.updated_at",
                          (name, uid, t))
                n += 1
    return True, f"🔗 Сохранено: {n} имён BILLZ"


def employee_of(billz_name: str):
    """ID сотрудника для продавца BILLZ (только подтверждённые связи)."""
    uid = links().get((billz_name or "").strip())
    return uid or None


def names_of(uid: int) -> list:
    return [n for n, u in links().items() if u == uid]
