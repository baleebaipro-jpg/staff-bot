"""«👤 Мой менеджер» — персональный менеджер клиента (тарифы Silver и Gold).

Руководитель назначает менеджера в карточке клиента в кабинете (рядом со статусом). Назначение хранится и по
клиенту бота, и по клиенту BILLZ — не потеряется, если клиент ещё не в боте или войдёт заново.
Клиент видит раздел в боте; на Bronza — «доступно с тарифа Silver».
"""
import html

import config
import core

TIERS_ALLOWED = getattr(config, "MANAGER_TIERS", ("silver", "gold"))


def init() -> None:
    with core.db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS client_managers(key TEXT PRIMARY KEY, uid INTEGER, set_at TEXT)")


def _keys(bot_id=None, store_id=None) -> list:
    """Все ключи клиента: b:<ID в боте>, s:<ID в BILLZ> (связанные друг с другом)."""
    keys = []
    with core.db() as c:
        if bot_id:
            keys.append(f"b:{int(bot_id)}")
            r = c.execute("SELECT billz_id FROM clients WHERE user_id=?", (int(bot_id),)).fetchone()
            if r and r["billz_id"]:
                store_id = store_id or r["billz_id"]
        if store_id:
            keys.append(f"s:{store_id}")
            if not bot_id:
                r = c.execute("SELECT user_id FROM clients WHERE billz_id=?", (str(store_id),)).fetchone()
                if r:
                    keys.insert(0, f"b:{r['user_id']}")
    return keys


def get(bot_id=None, store_id=None):
    keys = _keys(bot_id, store_id)
    if not keys:
        return None
    with core.db() as c:
        for k in keys:
            r = c.execute("SELECT uid FROM client_managers WHERE key=?", (k,)).fetchone()
            if r and r["uid"] and core.employee(r["uid"]):
                return r["uid"]
    return None


def assign(bot_id=None, store_id=None, uid=0):
    uid = int(uid or 0)
    if uid and not core.employee(uid):
        return False, "Такого сотрудника нет"
    keys = _keys(bot_id, store_id)
    if not keys:
        return False, "Клиент не найден"
    old = get(bot_id, store_id)
    t = core.now().strftime(core.FMT)
    with core.db() as c:
        bot = next((int(k[2:]) for k in keys if k.startswith("b:")), None)
        if uid and uid != old and bot:  # клиенту в бот — кто его личный менеджер (только Silver / Gold)
            cl = core.get_client(bot) or {}
            if core.tier_of(cl) in TIERS_ALLOWED:
                import i18n
                core.notify(c, bot, i18n.t(cl.get("lang"), "manager_assigned", name=core.name(uid)))
        for k in keys:
            if uid:
                c.execute("INSERT OR REPLACE INTO client_managers(key, uid, set_at) VALUES(?, ?, ?)", (k, uid, t))
            else:
                c.execute("DELETE FROM client_managers WHERE key=?", (k,))
    return True, (f"👤 Менеджер: {core.name(uid)}" if uid else "Менеджер снят")


def team() -> list:
    return [{"id": u, "name": e["name"]} for u, e in config.EMPLOYEES.items()]


def contact(uid: int) -> dict:
    """Как написать менеджеру: @username рабочего аккаунта (подключён к контролю переписок) или ссылка по ID."""
    username = ""
    with core.db() as c:
        try:
            r = c.execute("SELECT username FROM biz_connections WHERE user_id=? AND username != '' "
                          "ORDER BY enabled DESC, updated_at DESC LIMIT 1", (uid,)).fetchone()
            username = r["username"] if r else ""
        except Exception:  # noqa: BLE001 — таблицы контроля может не быть
            pass
    username = username or (config.EMPLOYEES.get(uid, {}).get("username") or "").lstrip("@")
    return {"name": core.name(uid), "username": username, "id": uid}


def client_text(client_uid: int, lang: str):
    """(текст, url кнопки или '') для раздела «Мой менеджер» в клиентском боте."""
    import i18n
    cl = core.get_client(client_uid) or {}
    tier = core.tier_of(cl)
    if tier not in TIERS_ALLOWED:
        return i18n.t(lang, "manager_locked", tier=core.tier_title(tier)), ""
    mid = get(client_uid)
    if not mid:
        return i18n.t(lang, "manager_none"), ""
    m = contact(mid)
    name = html.escape(m["name"])
    who = f'<a href="tg://user?id={mid}">{name}</a>' if not m["username"] else f"{name} (@{html.escape(m['username'])})"
    return i18n.t(lang, "manager_info", name=who, tier=core.tier_title(tier)), (f"https://t.me/{m['username']}" if m["username"] else "")
