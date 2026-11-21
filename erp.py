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
        purchases = None
        if bid:
            try:
                info = _api("GET", f"/v1/customer/{bid}")
            except BillzError as e:
                if e.code != 404:
                    raise
                info = {}  # клиент только что заведён — покупок ещё нет
            purchases = float((info or {}).get("purchase_amount") or 0)
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
        core.update_client(uid, billz_id=bid, billz_purchases=purchases, billz_balance=balance,
                           billz_synced=core.now().strftime(core.FMT))
        if purchases is not None:
            core.auto_tier(uid, purchases)
    except BillzError as e:
        logging.warning("BILLZ: не удалось обновить клиента %s: %s", uid, e)
        last_error.update(text=f"клиент {uid}: {e}", at=core.now().strftime("%d.%m %H:%M"))
        return dict(_stats(cl), error=True)
    return _stats(core.get_client(uid) or {})


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
    lines.append(f"➕ Заводить новых клиентов в BILLZ: {'да' if CREATE_CLIENTS else 'нет'}")
    if last_error["text"]:
        lines.append(f"⚠️ Последняя ошибка ({last_error['at']}): {last_error['text']}")
    return "\n".join(lines)


def _stats(cl: dict) -> dict:
    return {"linked": bool(cl.get("billz_id")), "purchases": cl.get("billz_purchases"),
            "balance": cl.get("billz_balance"), "synced": cl.get("billz_synced")}
