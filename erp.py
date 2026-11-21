"""Связь с ERP BILLZ (https://docs.billz.io).

Ключ интеграции задаётся в Railway → Variables:
    BILLZ_SECRET_TOKEN — секретный ключ интеграции из BILLZ
Без ключа всё работает как раньше: клиента не ищем, покупки и бонусы — «скоро».

Что используем:
    POST /v1/auth/login           секретный ключ → access_token (живёт 15 суток)
    GET  /v1/client?phone_number= поиск клиента по телефону: имя, дата рождения,
                                  balance (кешбэк-бонусы), карты
    GET  /v1/customer/{id}        карточка клиента: purchase_amount (сумма покупок)
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

_lock = threading.Lock()
_token = {"value": None, "until": 0.0}


class BillzError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def _secret() -> str:
    return os.getenv("BILLZ_SECRET_TOKEN", "").strip()


def enabled() -> bool:
    return bool(_secret())


def _http(method: str, path: str, params: dict = None, body: dict = None, token: str = None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"platform-id": PLATFORM_ID, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"message": raw[:200].decode(errors="replace")}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BillzError(f"BILLZ недоступен: {e}")


def _login() -> str:
    code, res = _http("POST", "/v1/auth/login", body={"secret_token": _secret()})
    data = (res or {}).get("data") or {}
    if code != 200 or not data.get("access_token"):
        raise BillzError(f"BILLZ: вход не удался ({code}: {(res or {}).get('message')})")
    _token["value"] = data["access_token"]
    # обновляем заранее: за час до истечения (refresh-токен не используем — повторный вход проще и надёжнее)
    _token["until"] = time.time() + max(60, int(data.get("expires_in") or 3600) - 3600)
    return _token["value"]


def _api(method: str, path: str, params: dict = None, body: dict = None):
    if not enabled():
        raise BillzError("BILLZ не подключён (нет BILLZ_SECRET_TOKEN)")
    with _lock:
        tok = _token["value"] if _token["value"] and time.time() < _token["until"] else _login()
    code, res = _http(method, path, params, body, tok)
    if code == 401:  # токен истёк или отозван — входим заново и повторяем один раз
        with _lock:
            tok = _login()
        code, res = _http(method, path, params, body, tok)
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


def find_client(phone: str):
    """Найти клиента в BILLZ по номеру. Возвращает словарь или None.
    {"billz_id", "first_name", "last_name", "birth_date" (YYYY-MM-DD), "balance", "cards"}"""
    if not enabled():
        return None
    for p in _phone_variants(phone):
        res = _api("GET", "/v1/client", params={"phone_number": p, "page": 1, "limit": 1})
        clients = (res or {}).get("clients") or []
        if clients:
            c = clients[0]
            return {"billz_id": c.get("id"), "first_name": (c.get("first_name") or "").strip() or None,
                    "last_name": (c.get("last_name") or "").strip() or None, "birth_date": _date(c.get("birth_date")),
                    "balance": float(c.get("balance") or 0), "cards": c.get("cards") or []}
    return None


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
    if fresh and not force:
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
            if balance is None:
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
        return dict(_stats(cl), error=True)
    return _stats(core.get_client(uid) or {})


def _stats(cl: dict) -> dict:
    return {"linked": bool(cl.get("billz_id")), "purchases": cl.get("billz_purchases"),
            "balance": cl.get("billz_balance"), "synced": cl.get("billz_synced")}
