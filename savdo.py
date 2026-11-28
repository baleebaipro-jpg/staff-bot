"""Продажа из чата в BILLZ: менеджер пишет в тему группы — бот разбирает, руководитель подтверждает, бот пробивает чек.

Сообщение менеджера (порядок строк любой):
    mohinur (uzum)              ← продавец (как в BILLZ) и тип оплаты в скобках (uzum → Uzum Nasiya)
    pardayeva marjona           ← клиент: фамилия и имя
    10.09.2002                  ← дата рождения
    shartnoma: 67257165         ← номер договора
    6.100.000 dumi yoq          ← общая сумма
    bonus omagan                ← бонусами не платил (или «bonus 200.000» — оплачено бонусами)
    884759191                   ← телефон (+998)

После «✅ Пробить» руководителя:
  1) клиент находится в BILLZ по телефону (или создаётся: имя, фамилия, дата рождения);
  2) продажа: товар SALE_PRODUCT по свободной цене = сумма, продавец, клиент, тип оплаты;
     комментарий к продаже — текст менеджера как есть; стандартный кешбэк BILLZ не начисляется;
  3) клиенту начисляется бонус BONUS_PCT % от суммы (кешбэк-баланс BILLZ);
  4) текст менеджера — в заметку клиента в кабинете.
"""
import json
import logging
import re
import time

import config
import core

BONUS_PCT = getattr(config, "SALE_BONUS_PCT", 29)
PAY_ALIASES = getattr(config, "SALE_PAY_ALIASES", {
    "uzum": ["uzum nasiya", "uzum"], "nasiya": ["nasiya"], "naqd": ["naqd", "наличн", "cash"], "nal": ["наличн", "naqd", "cash"],
    "karta": ["karta", "карт", "plastik", "uzcard", "humo", "terminal"], "plastik": ["plastik", "karta", "карт", "terminal"],
    "click": ["click"], "payme": ["payme"], "alif": ["alif"], "perechisleniye": ["перечисл", "o'tkazma"],
})
_cache = {}


def init() -> None:
    with core.db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS chat_sales(id INTEGER PRIMARY KEY, chat_id INTEGER, thread_id INTEGER, msg_id INTEGER, "
                  "poster INTEGER, poster_name TEXT, text TEXT, plan TEXT, status TEXT, order_id TEXT, order_number TEXT, "
                  "result TEXT, created_at TEXT, done_at TEXT)")
        # бот перезапустился посреди «Пробиваю…» — чем кончилось, неизвестно: пометить, чтобы проверить в BILLZ
        c.execute("UPDATE chat_sales SET status='stuck', result='прервано перезапуском бота' WHERE status='run'")


# ---------- настройки: тема и товар ----------
def check() -> str:
    """/savdo — проверка: тема, товар, связки сотрудников с продавцами BILLZ, кассы и типы оплат."""
    import sellers
    t, pr = topic(), product()
    out = [f"🧾 Продажи из чата\nТема: {t.get('chat')} / {t.get('thread') or 'все'}",
           f"Товар: {pr.get('name') + ' · ' + (pr.get('sku') or pr.get('barcode') or '') if pr else '❌ не задан — /savdo_tovar <штрихкод>'}",
           f"Бонус клиенту: {BONUS_PCT}%"]
    try:
        _cache.clear()
        users = _users()
        keys = {sellers._key(u["name"]): u for u in users}
        out.append(f"\n👥 Связка сотрудников с BILLZ (пользователей BILLZ: {len(users)}):")
        for uid, e in config.EMPLOYEES.items():
            names = sellers.names_of(uid)
            ok = [keys[sellers._key(n)]["name"] for n in names if sellers._key(n) in keys]
            mark = "✅ " + ", ".join(ok) if ok else ("⚠️ имя «" + ", ".join(names) + "» не найдено среди пользователей BILLZ"
                                                    if names else "— не связан")
            out.append(f"• {e['name']}: {mark}")
        out.append("\n💳 Типы оплат: " + ", ".join(p["name"] for p in _pay_types()))
        out.append("🏬 Кассы: " + ", ".join(f"{b['shop']} · {b['name']}" for b in _cashboxes()))
    except Exception as e:  # noqa: BLE001
        out.append(f"⚠️ BILLZ: {str(e)[:200]}")
    return "\n".join(out)[:4000]


def topic() -> dict:
    t = core.flag_json("savdo_topic") or {}
    if not t and getattr(config, "SALE_CHAT_ID", None):
        t = {"chat": config.SALE_CHAT_ID, "thread": getattr(config, "SALE_THREAD_ID", None)}
    return t


def set_topic(chat: int, thread) -> None:
    core.flag_json("savdo_topic", {"chat": chat, "thread": thread})


def is_sale_topic(chat: int, thread) -> bool:
    t = topic()
    return bool(t) and t.get("chat") == chat and (not t.get("thread") or t.get("thread") == thread)


def product() -> dict:
    """Товар для продаж по свободной цене. По умолчанию — штрихкод SALE_PRODUCT_BARCODE из config (находится в BILLZ сам)."""
    pr = core.flag_json("savdo_product") or {}
    code = str(getattr(config, "SALE_PRODUCT_BARCODE", "") or "")
    if code and pr.get("barcode") != code and pr.get("sku") != code and not pr.get("manual"):
        try:
            set_product(code, manual=False)
            pr = core.flag_json("savdo_product") or {}
        except Exception as e:  # noqa: BLE001
            logging.warning("Товар %s для продаж из чата не найден: %s", code, e)
    return pr


def set_product(query: str, manual: bool = True):
    """Товар, которым пробивается продажа по свободной цене (штрихкод, артикул или название в BILLZ)."""
    import erp
    q = (query or "").strip()
    if not q:
        return False, "Напишите штрихкод, артикул или название товара: /savdo_tovar 123456"
    res = erp._api("GET", "/v2/products", params={"page": 1, "limit": 10, "search": q, "include_empty_stocks": "true"}) or {}
    items = res.get("products") or []
    if not items:
        return False, f"Товар «{q}» в BILLZ не найден"
    exact = [p for p in items if q in (str(p.get("barcode") or ""), str(p.get("sku") or ""))]
    if q.isdigit() and not exact:
        return False, f"Товар со штрихкодом {q} в BILLZ не найден"
    exact = exact or items
    p = exact[0]
    core.flag_json("savdo_product", {"id": p.get("id"), "name": p.get("name") or "", "sku": p.get("sku") or "",
                                     "barcode": str(p.get("barcode") or q), "manual": manual})
    more = f"\n(найдено {len(items)}, выбран первый — уточните штрихкодом, если не тот)" if len(exact) > 1 else ""
    return True, f"✅ Товар для продаж из чата: {p.get('name')} · {p.get('sku') or p.get('barcode') or ''}{more}"


# ---------- разбор сообщения ----------
def is_debt_case(text: str) -> bool:
    """«5.000.000 dumi bor» — остаток долга у клиента: такие продажи бот пока не пробивает."""
    return bool(re.search(r"dumi?\s*bor|dum\s*bor|думи?\s*бор", (text or "").lower()))


SKIP_REPLY = getattr(config, "SALE_SKIP_REPLY", "Hojayin sizsiz Eplomayman!!")


def pay_allowed(p: dict) -> bool:
    """Пока пробиваем только оплату SALE_ONLY_PAY (Uzum Nasiya)."""
    only = str(getattr(config, "SALE_ONLY_PAY", "") or "").lower()
    if not only:
        return True
    word = (p.get("pay") or "").lower()
    return bool(word) and (word.startswith(only.split()[0]) or only.split()[0] in word)


def _is_pay(word: str) -> bool:
    w = (word or "").lower().strip()
    return bool(w) and any(w.startswith(k) or k in w.split() for k in PAY_ALIASES)


def _num(s: str) -> float:
    m = re.search(r"\d[\d\s.,']*", s or "")
    if not m:
        return 0
    raw = m.group(0).strip()
    raw = re.sub(r"[\s.,']", "", raw)
    return float(raw) if raw.isdigit() else 0


def parse(text: str) -> dict:
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    out = {"seller": "", "pay": "", "last_name": "", "first_name": "", "birth": "", "contract": "", "total": 0.0,
           "bonus_used": 0.0, "bonus": None, "bonus_line": "", "phone": "", "problems": []}
    rest = []
    for i, ln in enumerate(lines):
        low = ln.lower()
        digits = re.sub(r"\D", "", ln)
        if i == 0 and re.search(r"[a-zа-яўқғҳ]", low):
            m = re.match(r"\s*([^()]+?)\s*\(\s*([^)]*)\)?", ln)
            if m:
                out["seller"], out["pay"] = m.group(1).strip(), m.group(2).strip(" )")
            else:
                parts = ln.split()
                out["seller"], out["pay"] = parts[0], " ".join(parts[1:])
            if _is_pay(out["seller"]) and not _is_pay(out["pay"]):  # «Uzum ( diyora )» — оплата и продавец наоборот
                out["seller"], out["pay"] = out["pay"], out["seller"]
            continue
        if re.search(r"shartnoma|shartnama|договор|dogovor|contract", low):
            out["contract"] = digits or ln.split(":", 1)[-1].strip()
            continue
        if "bonus" in low or "бонус" in low:
            # «bonus omagan / olmagan / berilmagan» — бонус ещё не получал → начислить 29%;
            # «bonus berilgan / ogan / olgan» — уже получил → ничего не добавлять
            out["bonus_line"] = ln
            if re.match(r"\s*\d", ln) and _num(ln) >= 1000 and not out["total"]:  # «800.000 dum yoq bonus omagan»
                out["total"] = _num(ln)
            if re.search(r"ma.?g?.?an\b|magan|yo.?q|не\s*получ|нет", low):
                out["bonus"] = True
            elif re.search(r"berilgan|o.?l?gan|oldi|bor|получ|дали", low):
                out["bonus"] = False
            continue
            continue
        b = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", ln)
        if b and not out["birth"]:
            d, mth = int(b.group(1)), int(b.group(2))
            if mth > 12 and d <= 12:  # «03.23.1999» — месяц и день наоборот
                d, mth = mth, d
            out["birth"] = f"{b.group(3)}-{mth:02d}-{d:02d}"
            left = (ln[:b.start()] + " " + ln[b.end():]).strip(" .,-")
            if re.search(r"[A-Za-zА-Яа-я]", left):  # «Lutfullayeva Sabina 03.08.2006» — имя в той же строке
                rest.insert(0, left)
            continue
        if re.fullmatch(r"[+\d\s()-]+", ln) and len(digits) in (9, 12) and (len(digits) == 9 or digits.startswith("998")):
            out["phone"] = "+998" + digits[-9:]
            continue
        if digits and re.match(r"\s*\d", ln) and _num(ln) >= 1000 and not out["total"]:
            out["total"] = _num(ln)
            continue
        rest.append(ln)
    for ln in rest:  # клиент: строка из слов
        words = [w for w in re.split(r"\s+", ln) if re.search(r"[A-Za-zА-Яа-яЎўҚқҒғҲҳ']", w)]
        if len(words) >= 2 and not re.search(r"\d", ln):
            fam = [w for w in words if re.search(r"(ova|eva|yeva|ov|ev|yev|iy|aya|ая|ова|ева|ов|ев)$", w.lower())]
            last = fam[0] if fam else words[0]
            first = " ".join(w for w in words if w != last)
            cap = lambda x: x[:1].upper() + x[1:].lower()  # noqa: E731 — «Sug'diyona», а не «Sug'Diyona»
            out["last_name"], out["first_name"] = cap(last), " ".join(cap(x) for x in first.split())
            out["words"] = [cap(w) for w in words]
            break
    # имя клиента в BILLZ — как написал менеджер + номер договора: «Pardayeva Marjona 67257165»
    w = out.get("words") or []
    out["client_name"] = " ".join(w + ([out["contract"]] if out["contract"] else []))
    out["bz_first"] = w[0] if w else ""
    out["bz_last"] = " ".join(w[1:] + ([out["contract"]] if out["contract"] else []))
    for k, why in (("seller", "продавец (1-я строка)"), ("total", "сумма"), ("phone", "телефон")):
        if not out[k]:
            out["problems"].append(f"не нашёл: {why}")
    return out


# ---------- справочники BILLZ ----------
def _cached(key: str, fn, ttl: int = 3600):
    v = _cache.get(key)
    if v and time.time() - v[0] < ttl:
        return v[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def _users() -> list:
    import erp
    res = erp._api("GET", "/v1/user", params={"page": 1, "limit": 100, "show_sellers": "true"}) or {}
    return [{"id": u.get("id"), "name": " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip()}
            for u in (res.get("users") or []) if u.get("id")]


def _pay_types() -> list:
    import erp
    res = erp._api("GET", "/v1/company-payment-type", params={"page": 1, "limit": 100}) or {}
    return [{"id": p.get("id"), "name": p.get("name") or (p.get("payment_type") or {}).get("name") or "",
             "cash": bool(p.get("is_cash_payment_type"))} for p in (res.get("company_payment_types") or []) if p.get("id")]


def _cashboxes() -> list:
    import erp
    res = erp._api("GET", "/v1/cash-box", params={"page": 1, "limit": 100}) or {}
    return [{"id": b.get("id"), "name": b.get("name") or "", "shop_id": b.get("shop_id") or (b.get("shop") or {}).get("id"),
             "shop": (b.get("shop") or {}).get("name") or ""} for b in (res.get("cash_boxes") or []) if b.get("id")]


def _match_seller(word: str, poster: int):
    """Продавец BILLZ: «mohinur» → сотрудник Moxinur Yoldasheva → её имя в BILLZ по связке «Продавцы BILLZ ↔ сотрудники»
    → ID пользователя BILLZ. Если связки нет — ищем по имени среди пользователей BILLZ."""
    import sellers
    key = sellers._key(word)
    if not key:
        return None
    users = _cached("users", _users)
    emps = [u for u, e in config.EMPLOYEES.items() if sellers._key(e["name"].split()[0]).startswith(key)
            or key.startswith(sellers._key(e["name"].split()[0]))]
    if len(emps) > 1 and poster in emps:
        emps = [poster]
    for emp in emps:
        linked = {sellers._key(n) for n in sellers.names_of(emp)}
        hit = [u for u in users if sellers._key(u["name"]) in linked]
        if hit:
            return dict(hit[0], emp=emp)
    cands = [u for u in users if any(sellers._key(w).startswith(key) or key.startswith(sellers._key(w) or "#")
                                     for w in u["name"].split()[:1]) or sellers._key(u["name"]).startswith(key)]
    if len(cands) > 1 and poster:  # несколько похожих — тот, кто связан с написавшим сотрудником
        mine = {sellers._key(n) for n in sellers.names_of(poster)}
        cands = [u for u in cands if sellers._key(u["name"]) in mine] or cands
    return cands[0] if cands else None


def _match_pay(word: str):
    import sellers
    types = _cached("pays", _pay_types)
    w = sellers._norm(word)
    wants = []
    for k, al in PAY_ALIASES.items():
        if k in w.split() or w.startswith(k):
            wants += al
    wants += [w] if w else []
    for want in wants:
        hit = [p for p in types if want in p["name"].lower() or sellers._norm(want) in sellers._norm(p["name"])]
        if hit:
            return hit[0]
    return None


def _shops() -> dict:
    import erp
    res = erp._api("GET", "/v1/shop", params={"page": 1, "limit": 100}) or {}
    return {x["id"]: x.get("name") or "" for x in (res.get("shops") or []) if x.get("id")}


def _cashbox(seller_id: str = ""):
    boxes = _cached("boxes", _cashboxes)
    only = str(getattr(config, "SALE_SHOP", "") or "").lower()
    if only:  # пробиваем только в этом магазине — без запасных вариантов
        names = _cached("shops", _shops)
        for b in boxes:
            b["shop"] = b["shop"] or names.get(b["shop_id"], "")
        pat = re.compile(r"(?<![0-9a-zа-я])" + re.escape(only) + r"(?![0-9a-zа-я])")
        hit = [b for b in boxes if pat.search(b["shop"].lower())] or [b for b in boxes if pat.search(b["name"].lower())]
        return hit[0] if hit else None
    want = (getattr(config, "SALE_CASHBOX", "") or "").lower()
    if want:
        hit = [b for b in boxes if want in (b["name"] + " " + b["shop"]).lower()]
        if hit:
            return hit[0]
    with core.db() as c:  # магазин, где этот продавец чаще продаёт
        shops = [r["shop_id"] for r in c.execute("SELECT shop_id, COUNT(*) n FROM billz_sales WHERE shop_id != '' "
                                                 "GROUP BY shop_id ORDER BY n DESC")]
    for sid in shops:
        hit = [b for b in boxes if b["shop_id"] == sid]
        if hit:
            return hit[0]
    return boxes[0] if boxes else None


def plan(text: str, poster: int = 0) -> dict:
    """Что будет пробито: разбор + поиск в BILLZ. Ничего не создаёт."""
    import erp
    p = parse(text)
    p["text"] = text
    p["problems"] = list(p["problems"])
    try:
        s = _match_seller(p["seller"], poster) if p["seller"] else None
        p["seller_id"], p["seller_name"] = (s["id"], s["name"]) if s else ("", "")
        p["seller_emp"] = core.name(s["emp"]) if s and s.get("emp") else ""
        if p["seller"] and not s:
            p["problems"].append(f"продавец «{p['seller']}» не найден в BILLZ")
        pay = _match_pay(p["pay"] or "naqd")
        only = str(getattr(config, "SALE_ONLY_PAY", "") or "").lower()
        if pay and only and only not in pay["name"].lower():
            hit = [x for x in _cached("pays", _pay_types) if only in x["name"].lower()]
            pay = hit[0] if hit else None
        p["pay_id"], p["pay_name"] = (pay["id"], pay["name"]) if pay else ("", "")
        if not pay:
            p["problems"].append(f"тип оплаты «{p['pay']}» не найден в BILLZ")
        box = _cashbox()
        p["cashbox_id"], p["shop_id"], p["shop_name"] = (box["id"], box["shop_id"], f"{box['shop']} · {box['name']}") if box else ("", "", "")
        if not box:
            p["problems"].append(f"касса магазина «{getattr(config, 'SALE_SHOP', '')}» в BILLZ не найдена"
                                 if getattr(config, "SALE_SHOP", "") else "в BILLZ не найдено ни одной кассы")
        if not product().get("id"):
            p["problems"].append("не задан товар для продажи — руководитель: /savdo_tovar <штрихкод>")
        cl = erp.find_client(p["phone"]) if p["phone"] else None
        p["client_id"] = (cl or {}).get("billz_id") or ""
        p["client_now"] = " ".join(x for x in ((cl or {}).get("first_name"), (cl or {}).get("last_name")) if x)
        p["client_balance"] = (cl or {}).get("balance") or 0
        if not p["client_id"] and not p.get("words"):
            p["problems"].append("не нашёл имя клиента (строка «Фамилия Имя»)")
    except Exception as e:  # noqa: BLE001
        p["problems"].append(f"BILLZ: {str(e)[:150]}")
    p["bonus_add"] = round(p["total"] * BONUS_PCT / 100) if p.get("bonus") else 0
    return p


def money(n) -> str:
    return f"{int(round(n or 0)):,}".replace(",", " ")


def preview(p: dict, poster_name: str = "") -> str:
    client = p.get("client_name") or "—"
    lines = [f"🧾 <b>Продажа из чата</b>{' · от ' + poster_name if poster_name else ''}",
             f"👤 Продавец: {p.get('seller_name') or '❓ ' + (p['seller'] or '—')}"
             + (f" ↔ {p['seller_emp']} ✅" if p.get("seller_emp") else (" (без связки с сотрудником)" if p.get("seller_name") else "")),
             f"💳 Оплата: {p.get('pay_name') or '❓ ' + (p['pay'] or '—')}",
             f"🙍 Клиент: {client} · {p['phone'] or '❓'}"
             + (f"\n    в BILLZ: {p['client_now'] or 'есть'}" if p.get("client_id") else "\n    в BILLZ нет — будет создан"),
             f"🎂 {p['birth'][8:10]}.{p['birth'][5:7]}.{p['birth'][:4]}" if p["birth"] else "🎂 —",
             f"📄 Договор: {p['contract'] or '—'}",
             f"💰 Сумма: <b>{money(p['total'])}</b>" + (f" (из них бонусами {money(p['bonus_used'])})" if p["bonus_used"] else ""),
             (f"🎁 Бонус клиенту: +{money(p['bonus_add'])} ({BONUS_PCT}%)" if p.get("bonus")
              else "🎁 Бонус: не начисляется" + (f" («{p['bonus_line']}»)" if p.get("bonus") is False
                                                 else " — ⚠️ в сообщении нет строки «bonus omagan / berilgan»")),
             f"🏬 {p.get('shop_name') or '—'}"]
    if p["problems"]:
        lines.append("\n⚠️ " + "\n⚠️ ".join(p["problems"]))
    return "\n".join(lines)


def can_run(p: dict) -> bool:
    return bool(p.get("seller_id") and p.get("pay_id") and p.get("cashbox_id") and p.get("total") and p.get("phone")
                and product().get("id")) and not any("бонусами" in x for x in p.get("problems", []))


# ---------- очередь ----------
def save(chat, thread, msg_id, poster, poster_name, text, p) -> int:
    with core.db() as c:
        cur = c.execute("INSERT INTO chat_sales(chat_id, thread_id, msg_id, poster, poster_name, text, plan, status, created_at) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, 'wait', ?)", (chat, thread, msg_id, poster, poster_name, text,
                                                                   json.dumps(p, ensure_ascii=False), core.now().strftime(core.FMT)))
        return cur.lastrowid


def already_done(chat: int, msg_id: int) -> bool:
    with core.db() as c:
        return bool(c.execute("SELECT 1 FROM chat_sales WHERE chat_id=? AND msg_id=? AND status IN ('ok', 'run')",
                              (chat, msg_id)).fetchone())


def was_stuck(chat: int, msg_id: int) -> bool:
    with core.db() as c:
        return bool(c.execute("SELECT 1 FROM chat_sales WHERE chat_id=? AND msg_id=? AND status='stuck'",
                              (chat, msg_id)).fetchone())


def get(sid: int) -> dict:
    with core.db() as c:
        r = c.execute("SELECT * FROM chat_sales WHERE id=?", (sid,)).fetchone()
    if not r:
        return {}
    d = dict(r)
    d["plan"] = json.loads(d["plan"] or "{}")
    return d


def take(sid: int, status: str) -> bool:
    """Атомарно перевести из 'wait' (двойное нажатие не пробьёт дважды)."""
    with core.db() as c:
        return c.execute("UPDATE chat_sales SET status=? WHERE id=? AND status='wait'", (status, sid)).rowcount == 1


def finish(sid: int, status: str, result: str, order_id: str = "", number: str = "") -> None:
    with core.db() as c:
        c.execute("UPDATE chat_sales SET status=?, result=?, order_id=?, order_number=?, done_at=? WHERE id=?",
                  (status, result[:2000], order_id, number, core.now().strftime(core.FMT), sid))


# ---------- пробить ----------
def _env(res, what: str) -> dict:
    import erp
    res = res or {}
    code = res.get("status_code")
    err = res.get("error") if isinstance(res.get("error"), dict) else {}
    if (code not in (None, 200)) or (err and err.get("message")):
        raise erp.BillzError(f"{what}: {code} {err.get('message') or res.get('data') or ''}"[:300], code)
    return res


def run(sid: int):
    """Пробить продажу в BILLZ (после подтверждения). Возвращает (ok, текст)."""
    import erp
    s = get(sid)
    p = plan(s["text"], s["poster"])  # свежие данные на момент подтверждения
    if not can_run(p):
        msg = "Не пробито:\n⚠️ " + "\n⚠️ ".join(p["problems"] or ["не хватает данных"])
        finish(sid, "fail", msg)
        return False, msg
    q = {"Billz-Response-Channel": "HTTP"}
    step = "клиент"
    order_id = ""
    try:
        cid = p["client_id"]
        if not cid:
            body = {"phone_number": p["phone"], "first_name": p.get("bz_first") or "Mijoz", "last_name": p.get("bz_last") or ""}
            if p["birth"]:
                body["date_of_birth"] = p["birth"]
            cid = (erp._api("POST", "/v1/client", body=body) or {}).get("id")
            if not cid:
                raise erp.BillzError("клиент не создан")
        step = "создание продажи"
        res = _env(erp._api("POST", "/v2/order", params=q, body={"shop_id": p["shop_id"], "cashbox_id": p["cashbox_id"]},
                            timeout=60), step)
        order_id = (res.get("data") or {}).get("id") or res.get("id")
        number = str((res.get("data") or {}).get("order_number") or "")
        if not order_id:
            raise erp.BillzError("BILLZ не вернул ID продажи")
        step = "товар"
        _env(erp._api("POST", f"/v2/order-product/{order_id}", params=q, timeout=60, body={
            "product_id": product()["id"], "sold_measurement_value": 1, "use_free_price": True, "free_price": p["total"],
            "seller_ids": [p["seller_id"]], "is_manual": True}), step)
        step = "привязка клиента"
        _env(erp._api("PUT", f"/v2/order-customer-new/{order_id}", params=q, timeout=60,
                      body={"customer_id": cid, "check_auth_code": False}), step)
        step = "оплата"
        paid = p["total"] - (p["bonus_used"] or 0)
        erp._api("POST", f"/v2/order-payment/{order_id}", timeout=60, body={
            "payments": [{"company_payment_type_id": p["pay_id"], "paid_amount": paid, "returned_amount": 0}],
            "with_cashback": p["bonus_used"] or 0, "without_cashback": True, "comment": s["text"][:1000],
            "cashbox_id": p["cashbox_id"]})
    except erp.BillzError as e:
        msg = f"❌ Не пробито (шаг: {step}): {e}" + (f"\nЧерновик продажи в BILLZ: {order_id}" if order_id else "")
        finish(sid, "fail", msg, order_id)
        return False, msg
    notes = []
    try:  # бонус клиенту — только если «bonus omagan»
        if not p["bonus_add"]:
            raise StopIteration
        card = erp._api("GET", f"/v1/customer/{cid}") or {}
        before = float(card.get("balance") or 0)
        erp.set_balance(cid, before + p["bonus_add"])
        notes.append(f"🎁 Бонус +{money(p['bonus_add'])} (было {money(before)}, стало {money(before + p['bonus_add'])})")
    except StopIteration:
        notes.append("🎁 Бонус не начислялся")
    except Exception as e:  # noqa: BLE001
        notes.append(f"⚠️ Бонус не начислен: {str(e)[:150]}")
    try:  # заметка клиента в кабинете
        import shop
        with core.db() as c:
            ccid = shop._client_id(c, p["phone"], p.get("client_name") or "")
            old = (c.execute("SELECT note FROM shop_clients WHERE id=?", (ccid,)).fetchone() or {"note": ""})["note"] or ""
            add = f"[{core.now().strftime('%d.%m.%Y')}] {s['text']}"
            c.execute("UPDATE shop_clients SET note=? WHERE id=?", ((old + "\n\n" + add).strip()[-3000:], ccid))
    except Exception as e:  # noqa: BLE001
        logging.warning("Заметка продажи из чата: %s", e)
    msg = (f"✅ Пробито в BILLZ{' · чек №' + number if number else ''}\n{p.get('client_name')} · "
           f"{money(p['total'])} · {p['pay_name']} · продавец {p['seller_name']}\n" + "\n".join(notes))
    finish(sid, "ok", msg, order_id, number)
    return True, msg
