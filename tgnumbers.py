"""RADI Chat — рабочие номера (главные профили) на сервере и вход сотрудников по ключу.

Руководитель один раз подключает рабочий номер в кабинете (номер → код → облачный пароль). Сервер хранит сеанс
этого аккаунта (Telethon, StringSession) и облачный пароль — оба зашифрованы ключом из BOT_TOKEN.

Вход телефона по ключу (как «Подключить устройство» по QR, только без сканирования):
  1) RADI Chat запрашивает у Telegram токен входа (auth.exportLoginToken) и отправляет его серверу вместе с ключом;
  2) сервер подтверждает токен от имени рабочего аккаунта (auth.acceptLoginToken) — телефон входит в аккаунт;
  3) если на аккаунте облачный пароль, телефон присылает параметры account.getPassword, сервер считает SRP-доказательство
     (пароль на телефон не передаётся), телефон завершает вход auth.checkPassword.
Отключение телефона в кабинете завершает его сеанс Telegram (account.resetAuthorization).

Нужны переменные Railway: TG_API_ID, TG_API_HASH (те же, что у приложения RADI Chat).
"""
import asyncio
import base64
import hashlib
import os
import threading
import time

import core

API_ID = int(os.getenv("TG_API_ID", "0") or 0)
API_HASH = os.getenv("TG_API_HASH", "").strip()

_loop = None
_lock = threading.Lock()
_pending = {}  # number_id → {"client", "phone", "hash", "step", "at"} — подключение номера в процессе


def enabled() -> bool:
    return bool(API_ID and API_HASH)


def init() -> None:
    with core.db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS msgr_numbers(
                id INTEGER PRIMARY KEY, title TEXT, phone TEXT, tg_user_id INTEGER, session_enc TEXT, password_enc TEXT,
                created_at TEXT, status TEXT);
        """)
        for table, col, typ in (("msgr_devices", "number_id", "INTEGER"), ("msgr_devices", "auth_hash", "TEXT"),
                                ("msgr_codes", "number_id", "INTEGER"), ("msgr_staff", "number_id", "INTEGER")):
            if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


# ---------- шифрование (ключ из BOT_TOKEN: та же переменная Railway, отдельная не нужна) ----------

def _fernet():
    from cryptography.fernet import Fernet
    secret = os.getenv("BOT_TOKEN", "") + "|radi-chat-numbers-v1"
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _enc(s: str) -> str:
    return _fernet().encrypt(s.encode()).decode() if s else ""


def _dec(s: str) -> str:
    return _fernet().decrypt(s.encode()).decode() if s else ""


# ---------- свой asyncio-цикл для Telethon (Flask синхронный) ----------

def _run(coro, timeout: float = 60):
    global _loop
    with _lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="tgnumbers", daemon=True).start()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


def _client(session: str = ""):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session), API_ID, API_HASH, device_model="RADI server",
                          system_version="staff-bot", app_version="1.0", receive_updates=False)


def _now() -> str:
    return core.now().strftime(core.FMT)


def _mask(phone: str) -> str:
    d = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return f"+{d[:3]} ** *** {d[-4:-2]} {d[-2:]}" if len(d) >= 9 else (phone or "")


# ---------- рабочие номера: список и подключение ----------

def numbers() -> list:
    with core.db() as c:
        rows = c.execute("SELECT * FROM msgr_numbers ORDER BY id").fetchall()
        cnt = {r["number_id"]: r["n"] for r in c.execute(
            "SELECT number_id, COUNT(*) AS n FROM msgr_devices WHERE disabled=0 GROUP BY number_id")}
    return [{"id": r["id"], "title": r["title"] or _mask(r["phone"]), "phone": _mask(r["phone"]),
             "ready": r["status"] == "ready", "status": r["status"], "since": (r["created_at"] or "")[:16],
             "devices": cnt.get(r["id"], 0), "pending": (_pending.get(r["id"]) or {}).get("step", "")} for r in rows]


def add_start(phone: str, title: str = "") -> dict:
    """Шаг 1: номер → Telegram присылает код (в Telegram этого аккаунта или SMS)."""
    if not enabled():
        return {"ok": False, "error": "На сервере нет TG_API_ID / TG_API_HASH (Railway → Variables)."}
    phone = "+" + "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(phone) < 10:
        return {"ok": False, "error": "Введите номер полностью, с кодом страны: +998…"}
    with core.db() as c:
        r = c.execute("SELECT id FROM msgr_numbers WHERE phone=?", (phone,)).fetchone()
        nid = r["id"] if r else c.execute("INSERT INTO msgr_numbers(title, phone, created_at, status) VALUES(?, ?, ?, 'new')",
                                          ((title or "").strip()[:60], phone, _now())).lastrowid

    async def go():
        cl = _client()
        await cl.connect()
        sent = await cl.send_code_request(phone)
        return cl, sent.phone_code_hash

    try:
        cl, h = _run(go())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Telegram: {e}"}
    _pending[nid] = {"client": cl, "phone": phone, "hash": h, "step": "code", "at": time.time()}
    return {"ok": True, "number_id": nid, "step": "code"}


def add_code(nid: int, code: str) -> dict:
    """Шаг 2: код из Telegram. Если на аккаунте облачный пароль — шаг 3."""
    p = _pending.get(nid)
    if not p:
        return {"ok": False, "error": "Начните заново: введите номер."}
    from telethon.errors import SessionPasswordNeededError

    async def go():
        try:
            await p["client"].sign_in(p["phone"], "".join(ch for ch in str(code) if ch.isdigit()), phone_code_hash=p["hash"])
            return "ready"
        except SessionPasswordNeededError:
            return "password"

    try:
        step = _run(go())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Код не подошёл: {e}"}
    if step == "ready":
        return _finish(nid, "")
    p["step"] = "password"
    return {"ok": True, "number_id": nid, "step": "password"}


def add_password(nid: int, password: str) -> dict:
    """Шаг 3: облачный пароль (сохраняется зашифрованным — нужен, чтобы телефоны сотрудников входили без него)."""
    p = _pending.get(nid)
    if not p:
        return {"ok": False, "error": "Начните заново: введите номер."}

    async def go():
        await p["client"].sign_in(password=password)

    try:
        _run(go())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Пароль не подошёл: {e}"}
    return _finish(nid, password)


def _finish(nid: int, password: str) -> dict:
    p = _pending.pop(nid)
    cl = p["client"]

    async def go():
        me = await cl.get_me()
        s = cl.session.save()
        await cl.disconnect()
        return me, s

    me, session = _run(go())
    title = " ".join(x for x in (me.first_name, me.last_name) if x) or p["phone"]
    with core.db() as c:
        c.execute("UPDATE msgr_numbers SET session_enc=?, password_enc=?, tg_user_id=?, status='ready', "
                  "title=COALESCE(NULLIF(title, ''), ?) WHERE id=?", (_enc(session), _enc(password), me.id, title, nid))
    return {"ok": True, "number_id": nid, "step": "ready", "title": title}


def remove(nid: int) -> None:
    """Удалить номер с сервера (сеанс сервера завершается; телефоны сотрудников остаются в аккаунте до отключения)."""
    with core.db() as c:
        r = c.execute("SELECT session_enc FROM msgr_numbers WHERE id=?", (nid,)).fetchone()
    if r and r["session_enc"]:
        async def go():
            cl = _client(_dec(r["session_enc"]))
            await cl.connect()
            try:
                await cl.log_out()
            finally:
                await cl.disconnect()
        try:
            _run(go())
        except Exception:  # noqa: BLE001
            pass
    with core.db() as c:
        c.execute("DELETE FROM msgr_numbers WHERE id=?", (nid,))


def _session(nid: int):
    with core.db() as c:
        r = c.execute("SELECT * FROM msgr_numbers WHERE id=? AND status='ready'", (nid,)).fetchone()
    return (_dec(r["session_enc"]), _dec(r["password_enc"] or "")) if r else (None, None)


# ---------- вход телефона по ключу ----------

def accept_login_token(nid: int, token_b64: str) -> dict:
    """Подтвердить вход телефона в рабочий аккаунт. Возвращает hash авторизации (чтобы потом завершить сеанс)."""
    session, _pw = _session(nid)
    if not session:
        return {"ok": False, "error": "Рабочий номер не подключён на сервере."}
    from telethon.tl.functions.auth import AcceptLoginTokenRequest
    token = base64.b64decode(token_b64 + "=" * (-len(token_b64) % 4), altchars=b"-_")

    async def go():
        cl = _client(session)
        await cl.connect()
        try:
            auth = await cl(AcceptLoginTokenRequest(token=token))
            return str(auth.hash)
        finally:
            await cl.disconnect()

    try:
        return {"ok": True, "auth_hash": _run(go())}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Telegram не подтвердил вход: {e}"}


def srp_check(nid: int, params: dict) -> dict:
    """Облачный пароль за телефон: из параметров account.getPassword считаем A и M1 (пароль остаётся на сервере)."""
    _s, password = _session(nid)
    if not password:
        return {"ok": False, "error": "Облачный пароль не сохранён на сервере."}
    from telethon.password import compute_check
    from telethon.tl.types import PasswordKdfAlgoSHA256SHA256PBKDF2HMACSHA512iter100000SHA256ModPow
    from telethon.tl.types.account import Password

    def b(k):
        v = params.get(k) or ""
        return base64.b64decode(v + "=" * (-len(v) % 4))

    try:
        algo = PasswordKdfAlgoSHA256SHA256PBKDF2HMACSHA512iter100000SHA256ModPow(
            salt1=b("salt1"), salt2=b("salt2"), g=int(params["g"]), p=b("p"))
        pwd = Password(has_recovery=False, has_secure_values=False, has_password=True, current_algo=algo,
                       srp_B=b("srp_B"), srp_id=int(params["srp_id"]), new_algo=algo, new_secure_algo=None,
                       secure_random=os.urandom(256))
        chk = compute_check(pwd, password)
        return {"ok": True, "srp_id": str(chk.srp_id), "A": base64.b64encode(chk.A).decode(),
                "M1": base64.b64encode(chk.M1).decode()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"SRP: {e}"}


def reset_authorization(nid: int, auth_hash: str) -> bool:
    """Завершить сеанс Telegram телефона сотрудника (при отключении в кабинете)."""
    session, _pw = _session(nid)
    if not session or not auth_hash:
        return False
    from telethon.tl.functions.account import ResetAuthorizationRequest

    async def go():
        cl = _client(session)
        await cl.connect()
        try:
            return bool(await cl(ResetAuthorizationRequest(hash=int(auth_hash))))
        finally:
            await cl.disconnect()

    try:
        return _run(go())
    except Exception:  # noqa: BLE001
        return False
