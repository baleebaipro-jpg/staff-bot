"""Связь с ERP BILLZ (https://docs.billz.io).

Ключ интеграции задаётся в Railway → Variables:
    BILLZ_SECRET_TOKEN — секретный ключ интеграции из BILLZ
Без ключа всё работает как раньше: клиента не ищем, покупки и бонусы — «скоро».

Что используем:
    POST /v1/auth/login           секретный ключ → access_token (живёт 15 суток)
    GET  /v1/client?phone_number= поиск клиента по телефону: имя, дата рождения,
                                  balance (кешбэк-бонусы), карты
    GET  /v1/customer/{id}        карточка клиента: purchase_amount (сумма покупок), balance (кешбэк)
    POST /v1/client               создать клиента (если включено BILLZ_CREATE_CLIENTS)
    GET  /v1/client?page=&limit=  вся клиентская база — копия для раздела «Клиенты» (раз в сутки и по кнопке)
"""
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

import config
import core

BASE = os.getenv("BILLZ_API_URL", "https://api-admin.billz.ai").rstrip("/")
PLATFORM_ID = "7d4a4c38-dd84-4902-b744-0488b80a4c01"  # статичное значение для интеграций (из документации)
CACHE_MINUTES = getattr(config, "BILLZ_CACHE_MINUTES", 10)
CREATE_CLIENTS = getattr(config, "BILLZ_CREATE_CLIENTS", False)
TIMEOUT = 15
SEARCH_TIMEOUT = 30  # общий поиск по цифрам у BILLZ бывает медленным

_lock = threading.Lock()
_token = {"value": None, "until": 0.0}
last_error = {"text": None, "at": None}  # последняя ошибка — показывает команда /billz


class BillzError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def _secret() -> str:
    return os.getenv("BILLZ_SECRET_TOKEN", "").strip()


def enabled() -> bool:
    return bool(_secret())


def _http(method: str, path: str, params: dict = None, body: dict = None, token: str = None, timeout: int = TIMEOUT):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"platform-id": PLATFORM_ID, "Accept": "application/json",
               # без нормального User-Agent защита сайта (Cloudflare) может отклонять запросы программы
               "User-Agent": "Mozilla/5.0 (compatible; staff-bot/1.0; +https://t.me)"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, (json.loads(raw) if raw else {})
            except ValueError:  # пришёл не JSON (например, страница защиты сайта)
                return 502, {"message": "ответ не JSON: " + raw[:150].decode(errors="replace").replace("\n", " ")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"message": "ответ не JSON: " + raw[:150].decode(errors="replace").replace("\n", " ")}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BillzError(f"BILLZ недоступен: {e}")


def _looks_like_jwt(key: str) -> bool:
    return key.count(".") == 2 and key.startswith("ey")


def _login() -> str:
    key = _secret()
    code, res = _http("POST", "/v1/auth/login", body={"secret_token": key})
    data = (res or {}).get("data") or {}
    if (code != 200 or not data.get("access_token")) and _looks_like_jwt(key):
        # в Railway вставлен не секретный ключ, а готовый токен доступа (JWT) — пробуем его напрямую
        c2, _ = _http("GET", "/v1/client", params={"page": 1, "limit": 1}, token=key)
        if c2 == 200:
            _token["value"], _token["until"] = key, time.time() + 3600  # через час проверим заново
            return key
    if code != 200 or not data.get("access_token"):
        raise BillzError(f"BILLZ: вход не удался ({code}: {(res or {}).get('message')} / {(res or {}).get('error')})")
    _token["value"] = data["access_token"]
    # обновляем заранее: за час до истечения (refresh-токен не используем — повторный вход проще и надёжнее)
    _token["until"] = time.time() + max(60, int(data.get("expires_in") or 3600) - 3600)
    return _token["value"]


def _api(method: str, path: str, params: dict = None, body: dict = None, timeout: int = TIMEOUT):
    if not enabled():
        raise BillzError("BILLZ не подключён (нет BILLZ_SECRET_TOKEN)")
    with _lock:
        tok = _token["value"] if _token["value"] and time.time() < _token["until"] else _login()
    code, res = _http(method, path, params, body, tok, timeout)
    if code == 401:  # токен истёк или отозван — входим заново и повторяем один раз
        with _lock:
            tok = _login()
        code, res = _http(method, path, params, body, tok, timeout)
    if code >= 400:
        raise BillzError(f"BILLZ {method} {path}: {code} {(res or {}).get('message')}", code)
    return res


# ---------- сотрудники (продавцы) BILLZ ----------

_users_cache = {"at": 0, "names": [], "source": ""}
USER_PATHS = ["/v1/user", "/v2/user", "/v1/users", "/v2/users", "/v1/employee", "/v2/employee", "/v1/company/user",
              "/v1/seller", "/v2/seller"]


def billz_users(force: bool = False) -> dict:
    """Список продавцов/сотрудников BILLZ. Путь API у BILLZ точно не задокументирован — пробуем известные варианты,
    рабочий запоминаем. Кэш на час. Возвращает {names, source, error}."""
    if not force and _users_cache["names"] and time.time() - _users_cache["at"] < 3600:
        return dict(_users_cache, error="")
    known = (core.flag_json("billz_users_path") or {}).get("path")
    errors = []
    for path in ([known] if known else []) + [p for p in USER_PATHS if p != known]:
        try:
            res = _api("GET", path, params={"page": 1, "limit": 200}, timeout=20)
        except BillzError as e:
            errors.append(f"{path}: {e.code or ''}")
            continue
        items = res if isinstance(res, list) else next(
            (res.get(k) for k in ("users", "employees", "sellers", "items", "data", "rows", "result") if isinstance(res.get(k), list)), [])
        names = []
        for x in items or []:
            if not isinstance(x, dict):
                continue
            n = x.get("name") or x.get("full_name") or " ".join(str(v) for v in (x.get("first_name"), x.get("last_name")) if v)
            n = (n or "").strip()
            if n and n not in names:
                names.append(n)
        if names:
            core.flag_json("billz_users_path", {"path": path})
            _users_cache.update(at=time.time(), names=sorted(names), source=path)
            return dict(_users_cache, error="")
    return {"names": [], "source": "", "error": "; ".join(errors)[:300]}


# ---------- клиенты ----------

def _phone_variants(phone: str) -> list:
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return []
    out = ["+" + digits, digits]
    if len(digits) == 9:  # местный номер без кода страны
        out = ["+998" + digits, "998" + digits]
    return list(dict.fromkeys(out))


def _date(value) -> str:
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(value or ""))
    return m.group(1) if m and not m.group(1).startswith("0001") else None


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _same_phone(c: dict, phone: str) -> bool:
    """Номер клиента BILLZ совпадает с нашим по последним 9 цифрам (формат записи не важен)."""
    tail = _digits(phone)[-9:]
    return bool(tail) and any(_digits(p).endswith(tail) for p in (c.get("phone_numbers") or []))


def _exact(phone: str) -> dict:
    found = {}
    for p in _phone_variants(phone):
        for c in (_api("GET", "/v1/client", params={"phone_number": p, "page": 1, "limit": 5}) or {}).get("clients") or []:
            found[c.get("id")] = c
    return found


def _search(phone: str) -> dict:
    """Общий поиск BILLZ по цифрам номера (находит записи с пробелами, без кода страны и т.п.).
    Может быть медленным — при таймауте молча пропускается."""
    found = {}
    digits = _digits(phone)
    for q in dict.fromkeys([digits[-9:], digits]):
        if len(q) < 7:
            continue
        try:
            res = _api("GET", "/v1/client", params={"search": q, "page": 1, "limit": 20}, timeout=SEARCH_TIMEOUT)
        except BillzError as e:
            logging.warning("BILLZ: общий поиск «%s» не удался: %s", q, e)
            continue
        for c in (res or {}).get("clients") or []:
            if _same_phone(c, phone):
                found[c.get("id")] = c
        if found:
            break
    return found


def _candidates(phone: str, deep: bool = False) -> list:
    """Записи BILLZ с этим номером: точный поиск; общий — если точный ничего не дал или deep."""
    found = _exact(phone)
    if deep or not found:
        found.update(_search(phone))
    return list(found.values())


def _best(cands: list):
    """Из нескольких записей одного номера — та, где есть история: бонусы, покупки, давность."""
    return max(cands, key=lambda c: (float(c.get("balance") or 0) > 0, bool(c.get("last_transaction_date")),
                                     str(c.get("last_transaction_date") or ""), float(c.get("balance") or 0)),
               default=None)


def find_client(phone: str, deep: bool = False):
    """Найти клиента в BILLZ по номеру (в любом формате записи). Возвращает словарь или None.
    {"billz_id", "first_name", "last_name", "birth_date" (YYYY-MM-DD), "balance", "cards", "duplicates"}"""
    if not enabled() or not _digits(phone):
        return None
    cands = _candidates(phone, deep)
    c = _best(cands)
    if not c:
        return None
    return {"billz_id": c.get("id"), "first_name": (c.get("first_name") or "").strip() or None,
            "last_name": (c.get("last_name") or "").strip() or None, "birth_date": _date(c.get("birth_date")),
            "balance": float(c.get("balance") or 0), "cards": c.get("cards") or [], "duplicates": len(cands)}


def create_client(uid: int):
    """Создать клиента в BILLZ из данных, которые он ввёл в боте (если включено BILLZ_CREATE_CLIENTS)."""
    if not (enabled() and CREATE_CLIENTS):
        return None
    cl = core.get_client(uid) or {}
    if not cl.get("phone") or cl.get("billz_id"):
        return cl.get("billz_id")
    body = {"phone_number": "+" + re.sub(r"\D", "", cl["phone"]), "first_name": cl.get("first_name") or "",
            "last_name": cl.get("last_name") or "", "chat_id": str(uid)}
    if cl.get("birth_date"):
        body["date_of_birth"] = cl["birth_date"]
    res = _api("POST", "/v1/client", body=body)
    bid = (res or {}).get("id")
    if bid:
        core.update_client(uid, billz_id=bid)
    return bid


def sync_client(uid: int, force: bool = False) -> dict:
    """Покупки и бонусы клиента из BILLZ (с кэшем на CACHE_MINUTES минут).
    Возвращает {"linked": bool, "purchases": float|None, "balance": float|None, "synced": "..."} или {} если BILLZ выключен."""
    if not enabled():
        return {}
    cl = core.get_client(uid) or {}
    fresh = cl.get("billz_synced") and core.parse(cl["billz_synced"]) > core.now() - timedelta(minutes=CACHE_MINUTES)
    if fresh and not force and cl.get("billz_balance") is not None:  # бонусы не загрузились — обновим сразу
        return _stats(cl)
    try:
        bid = cl.get("billz_id")
        balance = None
        if not bid and cl.get("phone"):
            found = find_client(cl["phone"])
            if found:
                bid, balance = found["billz_id"], found["balance"]
            elif CREATE_CLIENTS and cl.get("step") == "done":
                # клиент прошёл регистрацию в боте, но в BILLZ его нет — заводим (один раз: дальше есть billz_id)
                bid = create_client(uid)
                balance = 0.0 if bid else None
        purchases = debt = None
        if bid:
            try:
                info = _api("GET", f"/v1/customer/{bid}")
            except BillzError as e:
                if e.code != 404:
                    raise
                info = {}  # клиент только что заведён — покупок ещё нет
            purchases = float((info or {}).get("purchase_amount") or 0)
            debt = float((info or {}).get("debt_amount") or 0)
            lp, visits = _date((info or {}).get("last_purchase_date")), (info or {}).get("visits_count")
            if lp != cl.get("last_purchase") or (visits is not None and visits != cl.get("visits")):
                core.update_client(uid, last_purchase=lp, visits=visits)
            bf, bl = ((info or {}).get("first_name") or "").strip(), ((info or {}).get("last_name") or "").strip()
            if bf and (bf != cl.get("billz_first") or bl != (cl.get("billz_last") or "")):
                core.update_client(uid, billz_first=bf, billz_last=bl or None)  # имя и фамилия как в магазине
            if debt > 0 or float(cl.get("billz_debt") or 0) > 0 or float(cl.get("debt_notified") or 0) > 0:
                # остаток по списку долгов точнее: общая сумма в карточке может обновляться с задержкой
                items = _debt_items(bid)
                if items is not None:
                    debt = round(sum(d["left"] for d in items), 2)
            if (info or {}).get("balance") is not None:  # кешбэк-баланс есть прямо в карточке клиента
                balance = float(info["balance"] or 0)
            if not purchases and not balance and cl.get("phone"):
                # пустая запись (например, бот сам завёл клиента, не найдя его из-за формата номера) —
                # проверяем, нет ли в BILLZ записи с этим номером, где есть покупки или бонусы
                better = find_client(cl["phone"], deep=True)
                if better and better["billz_id"] != bid:
                    other = _api("GET", f"/v1/customer/{better['billz_id']}") or {}
                    o_buy = float(other.get("purchase_amount") or 0)
                    o_bal = float(other.get("balance") if other.get("balance") is not None else better["balance"] or 0)
                    if o_buy or o_bal:
                        logging.info("BILLZ: клиент %s перепривязан %s → %s", uid, bid, better["billz_id"])
                        bid, purchases, balance = better["billz_id"], o_buy, o_bal
            if balance is None:  # запасной путь — поиск по номеру
                for p in _phone_variants(cl.get("phone")):
                    found = (_api("GET", "/v1/client", params={"phone_number": p, "page": 1, "limit": 1}) or {}).get("clients")
                    if found:
                        balance = float(found[0].get("balance") or 0)
                        break
        core.update_client(uid, billz_id=bid, billz_purchases=purchases, billz_balance=balance, billz_debt=debt,
                           billz_synced=core.now().strftime(core.FMT))
        if purchases is not None:
            core.auto_tier(uid, purchases)
            _notify_purchase(uid, purchases, balance)
        if bid and not cl.get("billz_id"):  # клиент только что связан с магазином — статус, выставленный заранее
            core.apply_store_tier(uid, bid)
        if debt is not None:
            _notify_debt(uid, debt)
    except BillzError as e:
        logging.warning("BILLZ: не удалось обновить клиента %s: %s", uid, e)
        last_error.update(text=f"клиент {uid}: {e}", at=core.now().strftime("%d.%m %H:%M"))
        return dict(_stats(cl), error=True)
    return _stats(core.get_client(uid) or {})


# ---------- вся клиентская база ----------

IMPORT_PAGE = 100
IMPORT_HOUR = 4  # каждый день около 04:00 база обновляется сама
_import = {"running": False}


def _import_status(**kw):
    st = dict(core.flag_json("billz_import") or {}, **kw)
    core.flag_json("billz_import", st)
    return st


def import_all() -> dict:
    """Скачать всю клиентскую базу BILLZ постранично в таблицу billz_clients."""
    started = core.now().strftime(core.FMT)
    _import_status(running=True, started=started, loaded=0, error=None)
    loaded, page, total = 0, 1, None
    try:
        while page <= 2000:
            res = _api("GET", "/v1/client", params={"page": page, "limit": IMPORT_PAGE}, timeout=SEARCH_TIMEOUT) or {}
            batch = res.get("clients") or []
            if total is None and res.get("count") is not None:
                total = int(res["count"])
            rows = []
            for c in batch:
                if not c.get("id"):
                    continue
                phones = [p for p in (c.get("phone_numbers") or []) if p]
                first = phones[0] if phones else ""
                rows.append((c["id"], (c.get("first_name") or "").strip() or None, (c.get("last_name") or "").strip() or None,
                             first, " ".join(_digits(p) for p in phones), _digits(first)[-9:] or None,
                             _date(c.get("birth_date") or c.get("date_of_birth")), float(c.get("balance") or 0),
                             _date(c.get("last_transaction_date")), started))
            with core.db() as db:
                db.executemany("INSERT OR REPLACE INTO billz_clients(id, first_name, last_name, phone, phones, phone_tail, "
                               "birth_date, balance, last_tx, synced) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            loaded += len(rows)
            _import_status(loaded=loaded, total=total)
            if len(batch) < IMPORT_PAGE or (total is not None and page * IMPORT_PAGE >= total):
                break
            page += 1
            time.sleep(0.2)
        with core.db() as db:  # удалённые в BILLZ — убираем из копии (только после полной загрузки)
            db.execute("DELETE FROM billz_clients WHERE synced<?", (started,))
        return _import_status(running=False, loaded=loaded, total=loaded, at=core.now().strftime("%d.%m.%Y %H:%M"),
                              day=core.now().strftime("%Y-%m-%d"))
    except BillzError as e:
        logging.warning("BILLZ: загрузка базы клиентов прервана: %s", e)
        last_error.update(text=f"загрузка базы: {e}", at=core.now().strftime("%d.%m %H:%M"))
        return _import_status(running=False, error=str(e)[:200], day=core.now().strftime("%Y-%m-%d"))


def start_import() -> bool:
    """Запустить загрузку базы в фоне. False — уже идёт или BILLZ не подключён."""
    if not enabled() or _import["running"]:
        return False
    _import["running"] = True

    def run():
        try:
            import_all()
        except Exception:  # noqa: BLE001 — фоновая загрузка не должна ронять бота
            logging.exception("BILLZ: загрузка базы клиентов упала")
            _import_status(running=False, error="внутренняя ошибка")
        finally:
            _import["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True


def import_due() -> None:
    """Из цикла бота: раз в сутки (после IMPORT_HOUR) и сразу, если базы ещё нет."""
    if not enabled() or _import["running"]:
        return
    st = core.flag_json("billz_import") or {}
    today = core.now().strftime("%Y-%m-%d")
    if not st.get("day") or (st.get("day") != today and core.now().hour >= IMPORT_HOUR):
        start_import()


# ---------- «спасибо за покупку» ----------
# Покупку замечаем по росту суммы покупок клиента (purchase_amount) относительно purchases_notified —
# суммы, о которой мы уже сказали «спасибо». Поэтому обновление данных где угодно (кабинет, кнопки)
# не «съедает» покупку: при любом обновлении рост будет замечен и сообщение уйдёт один раз.
# Отчёт по продажам (каждые 2 минуты) лишь подсказывает, чью карточку обновить прямо сейчас,
# а фоновый обход клиентов бота страхует, если отчёт недоступен.

WATCH_SECONDS = 30  # как часто спрашиваем магазин о новых покупках и оплатах долга
WATCH_BATCH = 20
_watch = {"started": False, "report": True}


def _money(v, lang) -> str:
    import i18n
    return f"{int(round(v or 0)):,}".replace(",", " ") + " " + i18n.t(lang, "currency")


def _notify_purchase(uid: int, purchases, balance) -> None:
    """Вызывается из sync_client: сумма покупок выросла — «спасибо» и бонусы (один раз на рост)."""
    if purchases is None or not getattr(config, "THANK_AFTER_PURCHASE", False):
        return
    cl = core.get_client(uid) or {}
    before = cl.get("purchases_notified")
    if before is None or purchases < before - 0.5:  # первый раз — запоминаем; возврат — просто обновляем
        core.update_client(uid, purchases_notified=purchases)
        return
    if purchases <= before + 0.5:
        return
    import i18n
    lang = i18n.lang_of(cl.get("lang"))
    name = core.client_first_name(cl) or {"uz": "Hurmatli mijoz", "ru": "Дорогой клиент", "en": "Dear customer"}[lang]
    text = i18n.t(lang, "thanks_purchase", name=name, sum=_money(purchases - before, lang), bonus=_money(balance, lang))
    core.update_client(uid, purchases_notified=purchases)
    with core.db() as c:
        c.execute("INSERT INTO outbox(chat_id, text, via) VALUES(?, ?, 'client')", (uid, text))
    st = core.flag_json("purchase_watch") or {}
    core.flag_json("purchase_watch", dict(st, sent=int(st.get("sent") or 0) + 1, last_sent=core.now().strftime("%d.%m %H:%M")))
    logging.info("Спасибо за покупку → %s (+%s)", uid, purchases - before)


def _notify_debt(uid: int, debt: float) -> None:
    """Долг уменьшился (оплата) — сообщение клиенту с остатком. Новый долг просто запоминаем."""
    cl = core.get_client(uid) or {}
    before = cl.get("debt_notified")
    if before is None or debt > before - 0.5:
        if before is None or abs(debt - before) > 0.5:
            core.update_client(uid, debt_notified=debt)
        return
    import i18n
    lang = i18n.lang_of(cl.get("lang"))
    paid = before - debt
    key = "debt_paid_full" if debt <= 0.5 else "debt_paid"
    text = i18n.t(lang, key, paid=_money(paid, lang), left=_money(debt, lang))
    core.update_client(uid, debt_notified=debt)
    with core.db() as c:
        c.execute("INSERT INTO outbox(chat_id, text, via) VALUES(?, ?, 'client')", (uid, text))
    logging.info("Оплата долга → %s (−%s, остаток %s)", uid, paid, debt)


def _bot_client_for(billz_id: str):
    """Клиент бота по ID клиента магазина (напрямую или по номеру из копии базы)."""
    with core.db() as c:
        r = c.execute("SELECT user_id FROM clients WHERE billz_id=? AND phone IS NOT NULL", (billz_id,)).fetchone()
        if r:
            return r["user_id"]
        b = c.execute("SELECT phone_tail FROM billz_clients WHERE id=?", (billz_id,)).fetchone()
        if b and b["phone_tail"]:
            r = c.execute("SELECT user_id FROM clients WHERE phone LIKE ? AND billz_id IS NULL",
                          ("%" + b["phone_tail"],)).fetchone()
            if r:
                core.update_client(r["user_id"], billz_id=billz_id)
                return r["user_id"]
    return None


def _sales_rows() -> list:
    """Продажи за вчера и сегодня из отчёта по транзакциям."""
    today = core.now().date()
    rows, page = [], 1
    while page <= 20:
        res = _api("GET", "/v1/transaction_report_table", params={
            "start_date": (today - timedelta(days=1)).strftime("%Y-%m-%d"), "end_date": today.strftime("%Y-%m-%d"),
            "order_types": "SALE", "page": page, "limit": 100}, timeout=SEARCH_TIMEOUT) or {}
        batch = res.get("rows") or []
        rows += batch
        if len(batch) < 100:
            break
        page += 1
    return [r for r in rows if (r.get("order_type") or "SALE").upper() == "SALE"]


# ---------- копия продаж BILLZ (для ERP: транзакции и сверка) ----------

def _seller_names(r: dict) -> list:
    """Продавцы строки отчёта BILLZ — поле называется по-разному: seller_names / sellers / seller_name / seller …"""
    out = []
    for key in ("seller_names", "sellers", "seller_name", "seller", "sellers_names", "employee_name", "employees"):
        v = r.get(key)
        if not v:
            continue
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, dict):
                n = x.get("name") or x.get("full_name") or " ".join(
                    str(p) for p in (x.get("first_name"), x.get("last_name")) if p)
            else:
                n = str(x)
            n = (n or "").strip()
            if n and n not in out:
                out.append(n)
        if out:
            break
    return out


def mirror_sales(day_from: str, day_to: str = None) -> int:
    """Скопировать транзакции BILLZ (продажи, возвраты, обмены) за период в таблицу billz_sales."""
    day_to = day_to or day_from
    rows, page = [], 1
    while page <= 50:
        res = _api("GET", "/v1/transaction_report_table", params={
            "start_date": day_from, "end_date": day_to, "page": page, "limit": 100}, timeout=SEARCH_TIMEOUT) or {}
        batch = res.get("rows") or []
        rows += batch
        if len(batch) < 100:
            break
        page += 1
    now = core.now().strftime(core.FMT)
    if rows:  # для диагностики: какие поля приходят из BILLZ (только названия + поля продавца)
        r0 = rows[0]
        core.flag_json("billz_mirror_keys", {"keys": sorted(r0.keys())[:80], "seller_fields": {
            k: r0.get(k) for k in r0 if "sell" in k.lower() or "employee" in k.lower() or "cashier" in k.lower()}})
    with core.db() as c:
        for r in rows:
            oid = r.get("order_id") or r.get("row_id")
            if not oid:
                continue
            c.execute("INSERT OR REPLACE INTO billz_sales(order_id, external_id, date, shop_id, shop_name, customer_name, customer_id, "
                      "sellers, cashier, order_type, total, gross, discount_pct, units, payments, comment, synced) "
                      "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (oid, str(r.get("external_id") or ""), str(r.get("date") or "")[:19].replace("T", " "), r.get("shop_id"),
                       r.get("shop_name"), r.get("customer_name"), r.get("customer_id"),
                       ", ".join(_seller_names(r)), r.get("cashier_name"), (r.get("order_type") or "SALE").upper(),
                       float(r.get("total_price") or 0), float(r.get("gross_sales") or 0), float(r.get("discount_percent") or 0),
                       float(r.get("sold_measurement_value") or 0) - float(r.get("returned_measurement_value") or 0),
                       json.dumps([{"name": p.get("payment_type_name"), "sum": float(p.get("sum") or 0)}
                                   for p in (r.get("payments") or [])], ensure_ascii=False), r.get("comment") or "", now))
    st = core.flag_json("billz_mirror") or {}
    core.flag_json("billz_mirror", dict(st, at=core.now().strftime("%d.%m %H:%M"), last=f"{day_from}…{day_to}: {len(rows)}"))
    return len(rows)


_history = {"running": False}


def history_backfill(days: int = 365) -> bool:
    """Загрузить историю продаж BILLZ за год (в фоне, по месяцам) — для истории покупок в карточке клиента."""
    if not enabled() or _history["running"]:
        return False
    _history["running"] = True

    def run():
        try:
            end = core.now().date()
            start = end - timedelta(days=days)
            d = start
            while d <= end:
                to = min(end, d + timedelta(days=29))
                try:
                    mirror_sales(d.strftime("%Y-%m-%d"), to.strftime("%Y-%m-%d"))
                except BillzError as e:
                    logging.warning("BILLZ: история продаж %s…%s: %s", d, to, e)
                core.flag_json("billz_history", {"from": start.strftime("%Y-%m-%d"), "upto": to.strftime("%Y-%m-%d"),
                                                 "running": to < end, "at": core.now().strftime("%d.%m %H:%M")})
                d = to + timedelta(days=1)
                time.sleep(0.5)
        except Exception:  # noqa: BLE001
            logging.exception("История продаж BILLZ")
        finally:
            _history["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True


def customer_card(billz_id: str) -> dict:
    """Карточка клиента BILLZ (статистика, заметки, группы)."""
    c = _api("GET", f"/v1/customer/{billz_id}") or {}
    keys = ("first_name", "last_name", "phone_numbers", "date_of_birth", "gender", "purchase_amount", "debt_amount", "balance",
            "visits_count", "average_purchase_amount", "average_discount", "average_products_count", "top_transaction",
            "first_purchase_date", "last_purchase_date", "registered_shop_name", "group_names", "tag_names", "client_type")
    out = {k: c.get(k) for k in keys}
    out["notes"] = [{"text": n.get("value") or "", "at": str(n.get("created_at") or "")[:16].replace("T", " "),
                     "by": n.get("created_by") or ""} for n in (c.get("notes") or []) if n.get("value")]
    out["cards"] = [x.get("code") for x in (c.get("cards") or []) if x.get("code")]
    return out


def check_purchases() -> int:
    """Новые продажи из отчёта → обновить карточки этих клиентов бота (рост суммы даст «спасибо»)."""
    rows = _sales_rows()
    with core.db() as c:
        seen = {r["order_id"] for r in c.execute("SELECT order_id FROM purchase_seen")}
    found = 0
    for r in rows:
        oid = r.get("order_id") or r.get("row_id")
        if not oid or oid in seen:
            continue
        seen.add(oid)
        with core.db() as c:
            c.execute("INSERT OR IGNORE INTO purchase_seen(order_id, seen_at) VALUES(?, ?)", (oid, core.now().strftime(core.FMT)))
        uid = _bot_client_for(r["customer_id"]) if r.get("customer_id") else None
        if uid:
            found += 1
            sync_client(uid, force=True)
            # карточка в магазине иногда обновляется с задержкой — проверим этого клиента ещё раз в следующем круге
            core.update_client(uid, billz_synced="2000-01-01 00:00:00")
    with core.db() as c:
        c.execute("DELETE FROM purchase_seen WHERE seen_at<?", ((core.now() - timedelta(days=7)).strftime(core.FMT),))
    return found


def check_cards(batch: int = WATCH_BATCH) -> None:
    """Обход клиентов бота по кругу (давно не обновлённые — первыми)."""
    with core.db() as c:
        ids = [r["user_id"] for r in c.execute(
            "SELECT user_id FROM clients WHERE billz_id IS NOT NULL AND phone IS NOT NULL "
            "ORDER BY COALESCE(billz_synced,'') LIMIT ?", (batch,))]
    for uid in ids:
        sync_client(uid, force=True)


def watch_status() -> str:
    st = core.flag_json("purchase_watch") or {}
    if not getattr(config, "THANK_AFTER_PURCHASE", False):
        return "🙏 «Спасибо за покупку»: выключено"
    return ("🙏 «Спасибо за покупку»: " + ("работает" if _watch["started"] else "не запущено") +
            f"\n   отчёт продаж: {'доступен' if _watch['report'] else 'нет доступа — проверяю карточки клиентов'}" +
            (f"\n   последняя проверка: {st['last_run']}" if st.get("last_run") else "") +
            (f"\n   отправлено: {st.get('sent', 0)}" + (f", последнее {st['last_sent']}" if st.get("last_sent") else "")) +
            (f"\n   ⚠️ {st['error']}" if st.get("error") else ""))


def start_purchase_watch() -> None:
    """Фоновая проверка новых покупок каждые WATCH_SECONDS (запускается один раз из цикла бота)."""
    if _watch["started"] or not enabled() or not getattr(config, "THANK_AFTER_PURCHASE", False):
        return
    _watch["started"] = True

    def run():
        while True:
            err = None
            try:
                if _watch["report"]:
                    try:
                        check_purchases()
                        _watch["n"] = _watch.get("n", 0) + 1
                        if _watch["n"] % 4 == 1:  # примерно раз в 2 минуты — копия продаж за сегодня для ERP
                            mirror_sales(core.now().strftime("%Y-%m-%d"))
                    except BillzError as e:
                        if e.code in (400, 403, 404):
                            logging.warning("BILLZ: отчёт продаж недоступен (%s) — только обход карточек", e)
                            _watch["report"] = False
                        else:
                            err = str(e)[:150]
                check_cards()
            except Exception as e:  # noqa: BLE001 — фоновая проверка не должна падать навсегда
                logging.exception("Проверка новых покупок")
                err = str(e)[:150]
            st = core.flag_json("purchase_watch") or {}
            core.flag_json("purchase_watch", dict(st, last_run=core.now().strftime("%d.%m %H:%M"), error=err))
            time.sleep(WATCH_SECONDS)
    threading.Thread(target=run, daemon=True).start()


# ---------- начисление бонусов (из постинга) ----------

BONUS_MAX = 1_000_000  # не больше этой суммы одному клиенту за один пост


def set_balance(billz_id: str, balance: float) -> None:
    """Установить кешбэк-баланс клиента в магазине (PATCH /v1/customer/set_balance/{id}, ждём результат)."""
    res = _api("PATCH", f"/v1/customer/set_balance/{billz_id}", params={"Billz-Response-Channel": "HTTP"},
               body={"balance": round(float(balance), 2)}, timeout=60) or {}
    code = res.get("status_code")
    err = (res.get("error") or {}).get("message") if isinstance(res.get("error"), dict) else None
    if code not in (None, 200) or err:
        raise BillzError(f"BILLZ: бонус не начислен ({code}: {err})", code)


def grant_bonus(post_id: int, uid: int) -> str:
    """Начислить бонус клиенту по записи bonus_grants (один раз). Возвращает статус."""
    with core.db() as c:
        g = c.execute("SELECT * FROM bonus_grants WHERE post_id=? AND user_id=?", (post_id, uid)).fetchone()
    if not g or g["status"] != "pending":
        return g["status"] if g else "none"
    status, before, after, bid = "fail", None, None, None
    try:
        sync_client(uid, force=True)  # свяжет с магазином по номеру, если ещё не связан
        bid = (core.get_client(uid) or {}).get("billz_id")
        if not bid:
            status = "no_store"
        else:
            card = _api("GET", f"/v1/customer/{bid}") or {}
            before = float(card.get("balance") or 0)
            after = before + float(g["amount"])
            set_balance(bid, after)
            core.update_client(uid, billz_balance=after)
            status = "ok"
    except BillzError as e:
        logging.warning("Бонус клиенту %s (пост %s) не начислен: %s", uid, post_id, e)
    with core.db() as c:
        c.execute("UPDATE bonus_grants SET status=?, billz_id=?, before=?, after=?, at=? WHERE post_id=? AND user_id=?",
                  (status, bid, before, after, core.now().strftime(core.FMT), post_id, uid))
    return status


def start_grants(post_id: int) -> None:
    """Начислить бонусы получателям поста в фоне."""
    def run():
        with core.db() as c:
            uids = [r["user_id"] for r in c.execute(
                "SELECT user_id FROM bonus_grants WHERE post_id=? AND status='pending'", (post_id,))]
        for uid in uids:
            try:
                grant_bonus(post_id, uid)
            except Exception:  # noqa: BLE001
                logging.exception("Начисление бонуса %s", uid)
            time.sleep(0.2)
    threading.Thread(target=run, daemon=True).start()


def import_running() -> bool:
    return _import["running"]


def diagnose(phone: str = None) -> str:
    """Проверка подключения по шагам — для руководителя (команда /billz). Ключ в отчёт не попадает."""
    lines = [f"🔌 BILLZ: {BASE}"]
    if not enabled():
        return lines[0] + "\n❌ Ключ не задан: нет переменной BILLZ_SECRET_TOKEN в Railway."
    key = _secret()
    lines.append(f"🔑 Ключ: задан, длина {len(key)} симв." + (" ⚠️ есть пробелы/кавычки по краям?" if key != key.strip(" '\"") else ""))

    def timed(fn):
        t = time.time()
        try:
            return fn(), None, time.time() - t
        except BillzError as e:
            return None, e, time.time() - t
    # 1. вход
    (code_res), err, dt = timed(lambda: _http("POST", "/v1/auth/login", body={"secret_token": key}))
    if err:
        return "\n".join(lines + [f"❌ 1. Вход: нет ответа за {dt:.0f} с — {err}"])
    code, res = code_res
    data = (res or {}).get("data") or {}
    if code == 200 and data.get("access_token"):
        _token.update(value=data["access_token"], until=time.time() + max(60, int(data.get("expires_in") or 3600) - 3600))
        lines.append(f"✅ 1. Вход по секретному ключу — успешно ({dt:.1f} с)")
    elif _looks_like_jwt(key):
        c2, r2 = _http("GET", "/v1/client", params={"page": 1, "limit": 1}, token=key)
        if c2 != 200:
            return "\n".join(lines + [f"❌ 1. Вход: {code} — {(res or {}).get('message')} / {(res or {}).get('error')}; "
                                       f"как токен (JWT) тоже не подходит: {c2}"])
        _token.update(value=key, until=time.time() + 3600)
        lines.append("✅ 1. Ключ — готовый токен доступа (JWT), работает (лучше поставить секретный ключ интеграции)")
    else:
        return "\n".join(lines + [f"❌ 1. Вход: {code} — {(res or {}).get('message')} / {(res or {}).get('error')}",
                                   "Ключ не принят BILLZ: проверьте, что это секретный ключ интеграции, вставлен целиком."])
    # 2. список клиентов
    (code_res), err, dt = timed(lambda: _http("GET", "/v1/client", params={"page": 1, "limit": 1}, token=_token["value"]))
    if err:
        lines.append(f"⚠️ 2. Чтение клиентов: нет ответа за {dt:.0f} с — {err}")
    elif code_res[0] != 200:
        return "\n".join(lines + [f"❌ 2. Чтение клиентов: {code_res[0]} — {(code_res[1] or {}).get('message')}",
                                   "У роли ключа нет права на клиентов — включите его в BILLZ (права роли ключа)."])
    else:
        lines.append(f"✅ 2. Чтение клиентов — доступно, в базе {(code_res[1] or {}).get('count')} ({dt:.1f} с)")
    # 3. поиск по номеру: точный и общий
    if phone:
        exact, err, dt = timed(lambda: _exact(phone))
        if err:
            lines.append(f"⚠️ 3а. Точный поиск: нет ответа за {dt:.0f} с — {err}")
            exact = {}
        else:
            lines.append(f"{'✅' if exact else 'ℹ️'} 3а. Точный поиск ({', '.join(_phone_variants(phone))}): "
                         f"записей {len(exact)} ({dt:.1f} с)")
        found, err, dt = timed(lambda: _search(phone))
        found = found or {}
        if not found and dt >= SEARCH_TIMEOUT * 0.9:
            lines.append(f"⚠️ 3б. Общий поиск по цифрам: BILLZ не ответил за {SEARCH_TIMEOUT} с — пропущен")
        else:
            lines.append(f"{'✅' if found else 'ℹ️'} 3б. Общий поиск по цифрам: записей {len(found)} ({dt:.1f} с)")
        cands = list({**exact, **found}.values())
        best = _best(cands)
        if cands:
            lines.append(f"Всего записей с этим номером: {len(cands)}" + (" ⚠️ дубли" if len(cands) > 1 else ""))
        for c in cands:
            lines.append(("👉 " if c is best else "   ") + f"{(c.get('first_name') or '')} {(c.get('last_name') or '')}".strip() +
                         f" · тел. {', '.join(c.get('phone_numbers') or [])} · кешбэк " +
                         f"{float(c.get('balance') or 0):,.0f}".replace(",", " ") +
                         f" · посл. покупка {str(c.get('last_transaction_date') or '—')[:10]}")
        if best:
            try:
                card = _api("GET", f"/v1/customer/{best['id']}") or {}
                items = _debt_items(best["id"])
                lines.append(f"💳 Долг: в карточке {float(card.get('debt_amount') or 0):,.0f}".replace(",", " ") +
                             (f" · по списку долгов {sum(d['left'] for d in items):,.0f} ({len(items)} шт.)".replace(",", " ")
                              if items is not None else " · список долгов недоступен"))
            except BillzError as e:
                lines.append(f"⚠️ Долг: не удалось получить — {e}")
            tail = _digits(phone)[-9:]
            with core.db() as c:
                bc = c.execute("SELECT * FROM clients WHERE phone LIKE ?", ("%" + tail,)).fetchone()
            if bc:
                lines.append(f"🤖 В боте: id {bc['user_id']} · бот помнит долг {bc['debt_notified']} · "
                             f"покупки (сказали спасибо) {bc['purchases_notified']} · обновлён {bc['billz_synced'] or '—'}")
            else:
                lines.append("🤖 Клиента с этим номером в боте нет")
    lines.append(f"➕ Заводить новых клиентов в BILLZ: {'да' if CREATE_CLIENTS else 'нет'}")
    if last_error["text"]:
        lines.append(f"⚠️ Последняя ошибка ({last_error['at']}): {last_error['text']}")
    return "\n".join(lines)


def debts(uid: int) -> list:
    """Непогашенные долги клиента: [{"left", "amount", "paid", "due" (YYYY-MM-DD|None), "overdue", "order"}].
    Если подробный список недоступен — пусто (общая сумма есть в карточке клиента)."""
    cl = core.get_client(uid) or {}
    if not enabled() or not cl.get("billz_id"):
        return []
    return _debt_items(cl["billz_id"]) or []


def _debt_items(billz_id: str):
    """Список непогашенных долгов из магазина; None — список недоступен."""
    try:
        res = _api("GET", "/v1/debt", params={"customer_id": billz_id, "page": 1, "limit": 100}) or {}
    except BillzError as e:
        logging.warning("BILLZ: список долгов %s недоступен: %s", billz_id, e)
        return None
    out = []
    for d in res.get("data") or []:
        if d.get("deleted_at") or d.get("status") == "fully_paid":
            continue
        amount, paid = float(d.get("amount") or 0), float(d.get("paid_amount") or 0)
        left = max(0.0, amount - paid)
        if left <= 0:
            continue
        due = _date(d.get("repayment_at_tz") or d.get("repayment_date"))
        out.append({"left": left, "amount": amount, "paid": paid, "due": due, "order": d.get("order_number") or "",
                    "overdue": d.get("status") == "overdue" or bool(due and due < core.now().strftime("%Y-%m-%d"))})
    return sorted(out, key=lambda x: (x["due"] or "9999"))


def _stats(cl: dict) -> dict:
    return {"linked": bool(cl.get("billz_id")), "purchases": cl.get("billz_purchases"), "debt": cl.get("billz_debt"),
            "balance": cl.get("billz_balance"), "synced": cl.get("billz_synced")}
