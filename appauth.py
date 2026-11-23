"""Вход в мобильное приложение RADI Team (Android / iPhone) — без паролей, через бот сотрудников.

1. Приложение просит код: /api/app/login_start → код и ссылка t.me/<бот>?start=applogin_<код>.
2. Сотрудник открывает ссылку, бот проверяет, что это сотрудник или руководитель, и привязывает код к нему.
3. Приложение раз в пару секунд спрашивает /api/app/login_check — как только код подтверждён,
   получает долгоживущий токен сессии и дальше ходит с ним вместо подписи Telegram.
"""
import secrets
from datetime import timedelta

import core

CODE_MINUTES = 10
SESSION_DAYS = 180


def init() -> None:
    with core.db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS app_logins(code TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT, used INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS app_sessions(token TEXT PRIMARY KEY, user_id INTEGER, platform TEXT, created_at TEXT,
                last_seen TEXT);
        """)


def bot_username() -> str:
    return (core.flag_json("staff_bot") or {}).get("username") or ""


def start_login() -> dict:
    code = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    with core.db() as c:
        c.execute("DELETE FROM app_logins WHERE created_at<?",
                  ((core.now() - timedelta(minutes=CODE_MINUTES)).strftime(core.FMT),))
        c.execute("INSERT INTO app_logins(code, created_at) VALUES(?, ?)", (code, core.now().strftime(core.FMT)))
    u = bot_username()
    return {"code": code, "bot": u, "link": f"https://t.me/{u}?start=applogin_{code}" if u else "",
            "tg_link": f"tg://resolve?domain={u}&start=applogin_{code}" if u else ""}


def confirm(code: str, uid: int) -> bool:
    """Бот: пользователь открыл ссылку входа — привязать код к нему (только сотрудники и руководитель)."""
    if not (core.employee(uid) or core.is_admin(uid)):
        return False
    with core.db() as c:
        cur = c.execute("UPDATE app_logins SET user_id=? WHERE code=? AND user_id IS NULL AND used=0 AND created_at>=?",
                        (uid, code, (core.now() - timedelta(minutes=CODE_MINUTES)).strftime(core.FMT)))
    return bool(cur.rowcount)


def check_login(code: str, platform: str = "") -> dict:
    with core.db() as c:
        r = c.execute("SELECT * FROM app_logins WHERE code=?", (str(code or ""),)).fetchone()
        if not r:
            return {"status": "expired"}
        if not r["user_id"]:
            return {"status": "wait"}
        if r["used"]:
            return {"status": "expired"}
        token = secrets.token_urlsafe(32)
        now = core.now().strftime(core.FMT)
        c.execute("UPDATE app_logins SET used=1 WHERE code=?", (code,))
        c.execute("INSERT INTO app_sessions(token, user_id, platform, created_at, last_seen) VALUES(?, ?, ?, ?, ?)",
                  (token, r["user_id"], str(platform or "")[:20], now, now))
    return {"status": "ok", "token": token, "name": core.name(r["user_id"])}


def session_user(token: str):
    """ID пользователя по токену приложения (или None). Сотрудник, которого убрали из команды, — выходит."""
    if not token:
        return None
    with core.db() as c:
        r = c.execute("SELECT * FROM app_sessions WHERE token=?", (str(token),)).fetchone()
        if not r:
            return None
        if r["last_seen"] < (core.now() - timedelta(days=SESSION_DAYS)).strftime(core.FMT):
            c.execute("DELETE FROM app_sessions WHERE token=?", (token,))
            return None
        if not (core.employee(r["user_id"]) or core.is_admin(r["user_id"])):
            return None
        c.execute("UPDATE app_sessions SET last_seen=? WHERE token=?", (core.now().strftime(core.FMT), token))
    return r["user_id"]


def logout(token: str) -> None:
    with core.db() as c:
        c.execute("DELETE FROM app_sessions WHERE token=?", (str(token or ""),))
