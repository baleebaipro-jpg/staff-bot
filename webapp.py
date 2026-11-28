"""Мини-апп: личный кабинет сотрудника и панель руководителя."""
import base64
from datetime import timedelta
import hashlib
import hmac
import json
import time
from functools import wraps
from urllib.parse import parse_qsl

import os

from flask import Flask, abort, g, jsonify, request, send_file

import config
import core
import i18n
import erp
import shop
import appauth
import invest
import kpi
import sellers
import followup
import crm
import msgr
import tgnumbers
import posts
import projects
import staff_text as st

AVATAR_DIR = getattr(config, "AVATAR_DIR", "/home/abdullohbe/avatars")

app = Flask(__name__)
core.init_db()


def _signed_by(token: str, check: str, received: str) -> bool:
    if not token:
        return False
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), received)


def telegram_user_id():
    """Проверить подпись Telegram и вернуть ID пользователя (или None).
    Запоминает в g.via, из какого бота открыт кабинет: "staff" (BOT_TOKEN) или "client" (CLIENT_BOT_TOKEN).
    В мобильном приложении вместо подписи — токен сессии (appToken)."""
    body = request.get_json(silent=True) or {}
    if body.get("appToken") and not body.get("initData"):
        uid = appauth.session_user(body["appToken"])
        if uid:
            g.via = "staff"
        return uid
    init_data = body.get("initData", "")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    if _signed_by(config.BOT_TOKEN, check, received):
        g.via = "staff"
    elif _signed_by(getattr(config, "CLIENT_BOT_TOKEN", ""), check, received):
        g.via = "client"
    else:
        return None
    if time.time() - int(pairs.get("auth_date", 0)) > 86400:
        return None
    return json.loads(pairs.get("user", "{}")).get("id")


def as_client(uid) -> bool:
    """Показывать ли кабинет клиента: открыт из бота клиентов — всегда да; иначе — если это не сотрудник."""
    return getattr(g, "via", "staff") == "client" or core.is_client(uid)


def need_user(admin=False, staff=False):
    """admin — только руководитель; staff — сотрудник или руководитель; иначе — любой (в т.ч. клиент)."""
    def deco(fn):
        @wraps(fn)
        def wrapper():
            uid = telegram_user_id()
            if uid is None:
                return jsonify(error="Откройте кабинет через Telegram-бота."), 401
            if (admin or staff) and g.via == "client":  # служебные действия — только из бота сотрудников
                return jsonify(error="Откройте кабинет через бота сотрудников."), 403
            if admin and not core.is_admin(uid):
                return jsonify(error="Только для руководителя."), 403
            if staff and not (core.employee(uid) or core.is_admin(uid)):
                return jsonify(error="Только для сотрудников."), 403
            if g.via == "client" and not core.client_allowed(uid):  # закрытый клиентский бот
                return jsonify(error="🔒 Faqat XUPING mijozlari uchun · Только для клиентов XUPING"), 403
            return fn(uid, request.get_json(silent=True) or {})
        return wrapper
    return deco


@app.before_request
def invest_closed():
    """Пока раздел «Инвестиции» выключен (INVEST_ENABLED = False) — все его действия закрыты."""
    if not invest.ENABLED and request.path.startswith("/api/invest"):
        return jsonify(ok=False, msg=invest.SOON_TEXT)


@app.route("/")
def index():
    return PAGE


# ---------- мобильное приложение ----------

@app.post("/api/app/login_start")
def api_app_login_start():
    return jsonify(appauth.start_login())


@app.post("/api/app/login_check")
def api_app_login_check():
    d = request.get_json(silent=True) or {}
    return jsonify(appauth.check_login(d.get("code"), d.get("platform")))


# ---------- RADI Chat (приложение Telegram для рабочих аккаунтов) ----------

def need_device(fn):
    """Запрос от подключённого телефона RADI Chat: заголовок X-Radi-Token."""
    @wraps(fn)
    def wrapper():
        dev = msgr.device(request.headers.get("X-Radi-Token", ""), request.headers.get("X-Radi-Version", ""))
        if not dev:
            return jsonify(ok=False, error="Телефон не подключён или отключён руководителем.", unpaired=True), 401
        return fn(dev, request.get_json(silent=True) or {})
    return wrapper


@app.post("/api/msgr/pair")
def api_msgr_pair():
    d = request.get_json(silent=True) or {}
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    r = msgr.pair(str(d.get("code") or ""), str(d.get("device") or ""), ip)
    return jsonify(r), (200 if r.get("ok") else 400)


@app.post("/api/msgr/gate")
def api_msgr_gate():
    """Проверка кода владельца перед входом по номеру в приложении (без токена — это шлагбаум)."""
    d = request.get_json(silent=True) or {}
    return jsonify(msgr.check_owner_code(str(d.get("code") or "")))


@app.post("/api/msgr/keylogin")
def api_msgr_keylogin():
    """RADI Chat: вход в рабочий аккаунт только по ключу (login_token — из auth.exportLoginToken телефона)."""
    d = request.get_json(silent=True) or {}
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    r = msgr.keylogin(str(d.get("key") or ""), str(d.get("login_token") or ""), str(d.get("device") or ""), ip)
    return jsonify(r), (200 if r.get("ok") else 400)


@app.post("/api/msgr/login")
def api_msgr_login():
    """RADI Chat: вход менеджера по логину/паролю (login_token — из auth.exportLoginToken телефона)."""
    d = request.get_json(silent=True) or {}
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    r = msgr.mgr_login(str(d.get("login") or ""), str(d.get("password") or ""),
                       str(d.get("login_token") or ""), str(d.get("device") or ""), ip)
    return jsonify(r), (200 if r.get("ok") else 400)


@app.post("/api/msgr/funnel")
@need_device
def api_msgr_funnel(dev, data):
    """Менеджер перемещает клиента в свою воронку 1/2/3."""
    return jsonify(msgr.set_chat_funnel(dev, int(data.get("peer") or 0), int(data.get("funnel") or 1)))


@app.post("/api/msgr/srp")
@need_device
def api_msgr_srp(dev, data):
    """Облачный пароль рабочего аккаунта — только в первые 15 минут после входа по ключу; пароль телефону не отдаётся."""
    if not dev.get("number_id") or core.now() - core.parse(dev["created_at"]) > timedelta(minutes=15):
        return jsonify(ok=False, error="Время входа истекло. Получите новый ключ."), 403
    return jsonify(tgnumbers.srp_check(dev["number_id"], data.get("params") or {}))


@app.post("/api/msgr/config")
@need_device
def api_msgr_config(dev, data):
    s = msgr.settings()
    if isinstance(data.get("groups"), list):
        msgr.report_groups(data["groups"])
    return jsonify(ok=True, name=msgr.dev_name(dev), funnel=msgr.dev_funnel(dev),
                   perms=msgr.staff_perms(dev.get("staff_uid") or 0)["perms"],
                   whitelist=s["whitelist"], keepalive=s["keepalive"], assign=msgr.assignments_for(dev))


@app.post("/api/msgr/claim")
@need_device
def api_msgr_claim(dev, data):
    return jsonify(msgr.claim(dev, int(data.get("peer") or 0)))


@app.post("/api/msgr/client")
@need_device
def api_msgr_client(dev, data):
    return jsonify(ok=True, **msgr.card(int(data.get("tg_id") or 0), str(data.get("phone") or "")))


@app.post("/api/msgr/search")
@need_device
def api_msgr_search(dev, data):
    return jsonify(ok=True, items=msgr.search(str(data.get("q") or "")))


@app.post("/api/msgr/link")
@need_device
def api_msgr_link(dev, data):
    tg_id = int(data.get("tg_id") or 0)
    if not tg_id:
        return jsonify(ok=False, error="Нет клиента."), 400
    msgr.link(tg_id, str(data.get("billz_id") or ""), dev)
    return jsonify(ok=True, **msgr.card(tg_id))


@app.post("/api/msgr/violation")
@need_device
def api_msgr_violation(dev, data):
    msgr.violation(dev, str(data.get("kind") or ""), int(data.get("peer") or 0), str(data.get("detail") or ""))
    return jsonify(ok=True)


@app.post("/api/msgr/deleted")
@need_device
def api_msgr_deleted(dev, data):
    return jsonify(ok=True, items=msgr.deleted(int(data.get("chat_id") or 0)))


@app.post("/api/msgr/admin")
@need_user(admin=True)
def api_msgr_admin(uid, data):
    """Кабинет руководителя → RADI Chat: код подключения, телефоны, разрешения, белый список, журнал."""
    a, msg = data.get("action"), ""
    if a == "code":
        r = msgr.new_code(uid, int(data.get("staff_uid") or 0), str(data.get("name") or ""), int(data.get("number_id") or 0))
        if r.get("ok") and data.get("send") and data.get("staff_uid"):
            snd = msgr.send_key_to_staff(int(data["staff_uid"]), r["code"], str(data.get("number_title") or ""))
            r["sent"] = snd.get("ok")
        return jsonify(r), (200 if r.get("ok") else 400)
    if a in ("num_add", "num_code", "num_password"):  # подключение рабочего номера: номер → код → облачный пароль
        nid = int(data.get("number_id") or 0)
        r = (tgnumbers.add_start(str(data.get("phone") or ""), str(data.get("title") or "")) if a == "num_add" else
             tgnumbers.add_code(nid, str(data.get("code") or "")) if a == "num_code" else
             tgnumbers.add_password(nid, str(data.get("password") or "")))
        r["numbers"] = tgnumbers.numbers()
        return jsonify(r), (200 if r.get("ok") else 400)
    if a == "num_remove":
        tgnumbers.remove(int(data.get("number_id") or 0))
        msg = "Рабочий номер удалён с сервера"
    if a == "num_rename":
        rr = tgnumbers.set_title(int(data.get("number_id") or 0), str(data.get("title") or ""))
        if not rr.get("ok"):
            return jsonify(rr), 400
        msg = "✅ Название воронки сохранено"
    if a == "login_set":
        rr = msgr.set_login(uid, int(data.get("staff_uid") or 0), int(data.get("number_id") or 0),
                            str(data.get("login") or ""), str(data.get("password") or ""))
        if not rr.get("ok"):
            return jsonify(rr), 400
        msg = "✅ Логин менеджера сохранён"
    if a == "login_del":
        msgr.del_login(int(data.get("staff_uid") or 0))
        msg = "Логин удалён"
    if a == "funnel_names":
        msgr.set_funnel_names(int(data.get("staff_uid") or 0), data.get("names") or [])
        msg = "✅ Названия воронок сохранены"
    if a == "owner_code":
        msgr.set_owner_code(str(data.get("code") or ""))
        msg = "✅ Код входа по номеру сохранён" if (data.get("code") or "").strip() else "Код снят — вход по номеру открыт"
    if a in ("disable", "enable", "kick"):
        msgr.set_disabled(str(data.get("id") or ""), a != "enable")
        msg = {"disable": "🚫 Телефон отключён", "enable": "✅ Телефон снова подключён",
               "kick": "🚪 Сессия закрыта — сотрудник вышел из аккаунта"}[a]
    if a == "save":
        msgr.save_settings(data.get("perms"), data.get("whitelist"), data.get("keepalive"), data.get("assign"))
        msg = "✅ Сохранено. Телефоны получат настройки в течение 5 минут."
    if a == "assign":
        msgr.assign(int(data.get("peer") or 0), int(data.get("staff_uid") or 0))
        msg = "✅ Назначено" if data.get("staff_uid") else "Чат снова свободный"
    if a == "staff":
        msgr.save_staff(int(data.get("staff_uid") or 0), data.get("perms"), data.get("see_others"), bool(data.get("reset")))
        msg = "✅ Доступы сотрудника сохранены"
    s = msgr.settings()
    fcount = msgr.funnels_count()
    staff = [dict(x, **msgr.staff_perms(x["uid"]), funnels=fcount.get(x["uid"], 0),
                  funnel_names=msgr.funnel_names(x["uid"])) for x in msgr.staff()]
    return jsonify(ok=True, msg=msg, devices=msgr.devices(), staff=staff, perms=s["perms"], perm_titles=msgr.PERMS,
                   whitelist="\n".join(s["whitelist"]), keepalive=s["keepalive"], violations=msgr.violations(50),
                   sessions=msgr.sessions_by_staff(), owner_code_set=msgr.owner_code_set(), logins=msgr.logins(),
                   assign={k: s[k] for k in ("assign_mode", "unassigned_visible", "timeout_min", "timeout_action")},
                   assign_modes=msgr.ASSIGN_MODES, timeout_actions=msgr.TIMEOUT_ACTIONS,
                   chats=msgr.chats_admin(str(data.get("q") or "")), numbers=tgnumbers.numbers(),
                   numbers_enabled=tgnumbers.enabled())


@app.post("/api/app/logout")
def api_app_logout():
    appauth.logout((request.get_json(silent=True) or {}).get("appToken"))
    return jsonify(ok=True)


APK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "radi-team.apk")


@app.route("/app/radi-team.apk")
def apk_download():
    """Android-приложение RADI Team — скачать и установить."""
    if not os.path.exists(APK_PATH):
        return "APK ещё не собран", 404
    return send_file(APK_PATH, mimetype="application/vnd.android.package-archive", as_attachment=True,
                     download_name="RADI-Team.apk")


@app.after_request
def no_cache(resp):
    """Telegram запоминает страницу мини-приложения — без этого после обновления бота
    у людей может открываться старая версия кабинета (например, без бонусов)."""
    if request.path == "/" or request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.route("/avatar/<int:uid>.jpg")
def avatar(uid):
    path = os.path.join(AVATAR_DIR, f"{uid}.jpg")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, max_age=86400)


def ava_version(uid) -> int:
    path = os.path.join(AVATAR_DIR, f"{uid}.jpg")
    return int(os.path.getmtime(path)) if os.path.exists(path) else 0


@app.post("/api/avatar")
@need_user()
def api_avatar(uid, data):
    """Сотрудник загружает своё фото (уже сжатое в браузере до JPEG)."""
    L = core.staff_lang(uid)  # язык сотрудника (для клиента ответ ниже — на его языке)
    raw = str(data.get("image", ""))
    if "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        img = base64.b64decode(raw, validate=True)
    except Exception:
        return jsonify(ok=False, msg=st.s(L, "photo_bad"))
    if len(img) > 2_000_000:
        return jsonify(ok=False, msg=st.s(L, "photo_big"))
    if not img.startswith(b"\xff\xd8\xff"):
        return jsonify(ok=False, msg=st.s(L, "photo_jpeg"))
    os.makedirs(AVATAR_DIR, exist_ok=True)
    tmp = os.path.join(AVATAR_DIR, f"{uid}.tmp")
    with open(tmp, "wb") as f:
        f.write(img)
    os.replace(tmp, os.path.join(AVATAR_DIR, f"{uid}.jpg"))
    # отметка, что фото своё — бот не будет заменять его фото из Telegram
    open(os.path.join(AVATAR_DIR, f"{uid}.custom"), "w").close()
    if not as_client(uid):
        return jsonify(ok=True, msg=st.s(L, "photo_updated"))
    lang = i18n.lang_of((core.get_client(uid) or {}).get("lang"))
    return jsonify(ok=True, msg={"uz": "Rasm yangilandi 📸", "ru": "Фото обновлено 📸", "en": "Photo updated 📸"}[lang])


def _ranks(uid):
    """Названия званий: для сотрудника, выбравшего узбекский, — по-узбекски."""
    ranks = getattr(config, "RANKS", None)
    if core.employee(uid) and core.staff_lang(uid) == "uz" and (not ranks or list(ranks) == st.RANKS["ru"]):
        return st.RANKS["uz"]
    return ranks


@app.post("/api/client_debts")
@need_user()
def api_client_debts(uid, data):
    """Клиент: каждый непогашенный долг отдельно."""
    if not as_client(uid):
        return jsonify(ok=False, items=[])
    return jsonify(ok=True, items=erp.debts(uid))


@app.post("/api/client_lang")
@need_user()
def api_client_lang(uid, data):
    """Клиент меняет язык (uz / ru / en) — панель и бот переключаются сразу."""
    if not as_client(uid):
        return jsonify(ok=False, msg="—")
    lang = i18n.lang_of(str(data.get("lang", "")))
    core.update_client(uid, lang=lang)
    with core.db() as c:  # бот пришлёт клавиатуру на новом языке
        c.execute("INSERT INTO outbox(chat_id, text, markup) VALUES(?, ?, 'client_kb')", (uid, i18n.t(lang, "lang_set")))
    return jsonify(ok=True, msg=i18n.t(lang, "lang_set"))


@app.post("/api/staff_lang")
@need_user(staff=True)
def api_staff_lang(uid, data):
    """Сотрудник меняет язык кабинета и бота (uz / ru)."""
    if not core.employee(uid):
        return jsonify(ok=False, msg="Только для сотрудников.")
    L = core.set_staff_lang(uid, str(data.get("lang", "")))
    with core.db() as c:  # бот пришлёт кнопки на новом языке
        c.execute("INSERT INTO outbox(chat_id, text, markup) VALUES(?, ?, 'staff_kb')", (uid, st.s(L, "lang_set")))
    return jsonify(ok=True, msg=st.s(L, "lang_set"))


# Разделы кабинета, которые руководитель может скрыть в приложении RADI Team (Android / iPhone).
# В Telegram-боте видно всё. Ключ → название (порядок = порядок в настройке).
APP_SECTIONS = {
    "shop": "🧾 Продажи", "clients": "💎 Клиенты", "staff": "👥 Команда", "projects": "📁 Проекты",
    "crm": "💬 Чаты · CRM", "earn": "💰 Мой доход", "earn-admin": "📈 Foyda сотрудников", "history": "🕘 История посещений",
    "posting": "📣 Постинг", "invest": "💼 Инвестиции", "msgr": "📱 RADI Chat",
}
APP_SECTIONS_DEFAULT = {"hidden": ["posting", "invest", "msgr"], "admin_too": False}


def app_sections() -> dict:
    s = core.flag_json("app_sections") or {}
    return {"hidden": [k for k in s.get("hidden", APP_SECTIONS_DEFAULT["hidden"]) if k in APP_SECTIONS],
            "admin_too": bool(s.get("admin_too", APP_SECTIONS_DEFAULT["admin_too"]))}


@app.post("/api/app_sections")
@need_user(admin=True)
def api_app_sections(uid, data):
    """Руководитель: какие разделы скрыть в приложении RADI Team (без data.hidden — просто прочитать)."""
    msg = ""
    if isinstance(data.get("hidden"), list):
        core.flag_json("app_sections", {"hidden": [k for k in data["hidden"] if k in APP_SECTIONS],
                                        "admin_too": bool(data.get("admin_too"))})
        msg = "✅ Сохранено. В приложении изменения видны после обновления страницы."
    return jsonify(ok=True, msg=msg, titles=APP_SECTIONS, **app_sections())


@app.post("/api/me")
@need_user()
def api_me(uid, data):
    staff_view = g.via == "staff"  # из бота клиентов — всегда кабинет клиента, даже у сотрудника
    res = {"is_admin": staff_view and core.is_admin(uid), "is_employee": staff_view and bool(core.employee(uid)),
           "branding": core.flag_json("branding") or {},
           "shop_access": staff_view and shop.has_access(uid),
           "goal": config.GOAL_HOURS,
           "ranks": _ranks(uid),
           "today_wd": core.now().weekday()}
    sec = app_sections()  # что скрыть, если кабинет открыт в приложении RADI Team (в боте — всё)
    res["app_hidden"] = sec["hidden"] if (not res["is_admin"] or sec["admin_too"]) else []
    if res["is_employee"]:
        res["profile"] = core.profile(uid)
        res["profile"]["ava_v"] = ava_version(uid)
        res["tasks"] = core.tasks(uid)
        res["history"] = core.history(uid)
        res["earnings"] = core.earnings_of(uid, core.staff_lang(uid))
        if can_assign(uid) and not core.is_admin(uid):  # может ставить задачи всем
            res["assign_team"] = [{"id": e, "name": core.name(e)} for e in config.EMPLOYEES if e != uid]
        res["kpi_me"] = kpi.summary_for_employee(uid)  # без выручки
    res["invest_enabled"] = invest.ENABLED
    if invest.ENABLED and (res["is_employee"] or res["is_admin"]):
        res["invest"] = invest.mine(uid)  # руководители тоже могут инвестировать (по заявке)
    res["is_client"] = not res["is_admin"] and not res["is_employee"]
    if res["is_client"]:
        # покупки и бонусы из BILLZ: при открытии кабинета и по «🔄» — свежие (fresh), при автообновлении — из кэша
        billz = erp.sync_client(uid, force=bool(data.get("fresh"))) if erp.enabled() else {}
        cl = core.get_client(uid) or {}
        tier = core.tier_of(cl)
        birth = cl.get("birth_date")
        res["client"] = {"id": uid, "name": core.client_full_name(cl), "first": core.client_first_name(cl),
                         "phone": cl.get("phone"),
                         "tier": tier, "tier_title": core.CLIENT_TIERS[tier]["title"],
                         "ava_v": ava_version(uid), "lang": i18n.lang_of(cl.get("lang")),
                         "registered": cl.get("step") == "done",
                         "birth": f"{birth[8:10]}.{birth[5:7]}" if birth else None,
                         "billz": dict(billz, enabled=bool(billz) or erp.enabled()),
                         "erp": shop.client_wallet_for_bot(uid),
                         # «📢 Канал» — личная ссылка-заявка (как кнопка в боте клиентов)
                         "channel": i18n.t(i18n.lang_of(cl.get("lang")), "btn_channel") if _channel_on() else "",
                         # «🥈 Стать Silver» — клиентам Bronza: ссылка на бот подписки
                         "upgrade": (getattr(config, "SILVER_UPGRADE_URL", "") or "") if tier == "bronza" else "",
                         "upgrade_soon": bool(getattr(config, "SILVER_UPGRADE_SOON", False))}
        res["news"] = core.news_list(10, i18n.lang_of(cl.get("lang")))  # каждый клиент — на своём языке
    if invest.ENABLED and (res["is_employee"] or res["is_admin"]):
        res["invest_cfg"] = {"levels": invest.levels_info(), "term": invest.TERM, "min": invest.MIN_AMOUNT,
                             "max": invest.MAX_AMOUNT, "currency": invest.CURRENCY,
                             "annual_min": invest.ANNUAL_MIN, "annual_max": invest.ANNUAL_MAX,
                             "history": invest.history_public()}
    if res["is_admin"] or (staff_view and is_poster(uid)):
        aud = posts.audience_info()
        if not res["is_admin"]:
            aud["bonus_on"] = False  # бонусы в постах начисляет только руководитель
        res["posting"] = {"audience": aud, "history": posts.history(),
                          "client_bot": (core.flag_json("client_bot") or {}).get("username", "")}
    if res["is_admin"]:
        res["projects"] = projects.overview(uid)
        if not res["is_employee"]:
            res["boss_day"] = core.boss_day(uid)  # «Пришёл / Ушёл» руководителя
            res["boss_day"]["video"] = core.PROOF_REQUIRED
        res["earnings_team"] = core.earnings_team()
        res["kpi"] = {str(k): v for k, v in kpi.team_summary().items()}
        if invest.ENABLED:
            res["invest_admin"] = invest.overview()
        res["news"] = core.news_list(10)
        res["clients"] = core.clients_stats()
        base = core.billz_base("", 0, 0)  # единый список: бот + база магазина без дублей
        res["clients"].update(all=base["total"], store_at=(base["status"] or {}).get("at"))
        res["client_list"] = core.clients_list()
        res["tiers"] = {k: v["title"] for k, v in core.CLIENT_TIERS.items()}
        res["shifts"] = {k: v["title"] for k, v in core.SHIFTS.items()}
        res["shifts"][core.OFF] = "Выходной"
        res["team"] = core.team()
        bl = sellers.links()
        for e in res["team"]:
            e["ava_v"] = ava_version(e["id"])
            e["billz"] = [n for n, u in bl.items() if u == e["id"]]  # как он записан продавцом в BILLZ
    return jsonify(res)


@app.post("/api/checkin")
@need_user(staff=True)
def api_checkin(uid, data):
    if core.PROOF_REQUIRED:
        ok, msg = core.start_proof(uid)
        if not ok:
            return jsonify(ok=False, msg=msg)
        # руководитель и мобильное приложение — видео записывается прямо в кабинете
        if not core.employee(uid) or data.get("video"):
            return jsonify(ok=True, video=True, code=core.proof_code(uid), min_sec=core.PROOF_MIN_SECONDS,
                           minutes=core.PROOF_MINUTES)
        with core.db() as c:
            core.notify(c, uid, "HTML:" + msg)  # бот пришлёт просьбу о кружке в чат
        return jsonify(ok=True, close=True, msg=st.s(core.staff_lang(uid), "proof_closed"))
    ok, msg = core.check_in(uid)
    return jsonify(ok=ok, msg=msg)


PROOF_DIR = os.path.join(config.DATA_DIR, "proofs")
PROOF_KEEP_DAYS = 30


@app.post("/api/proof_video")
@need_user(staff=True)
def api_proof_video(uid, data):
    """Видео-подтверждение прихода, записанное прямо в кабинете (вместо кружка в чате)."""
    raw = str(data.get("video") or "")
    mime = raw[5:raw.find(";")] if raw.startswith("data:") and ";" in raw else ""
    if "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        blob = base64.b64decode(raw, validate=True)
    except Exception:
        return jsonify(ok=False, msg="Видео не загрузилось, запишите ещё раз.")
    if len(blob) < 2000 or len(blob) > 25 * 1024 * 1024:
        return jsonify(ok=False, msg="Видео не подходит, запишите ещё раз (3–10 секунд).")
    try:
        dur = float(data.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0
    os.makedirs(PROOF_DIR, exist_ok=True)
    old = time.time() - PROOF_KEEP_DAYS * 86400
    for f in os.listdir(PROOF_DIR):  # старые видео — удаляем
        fp = os.path.join(PROOF_DIR, f)
        try:
            if os.path.getmtime(fp) < old:
                os.remove(fp)
        except OSError:
            pass
    sha = hashlib.sha256(blob).hexdigest()
    ok, msg, code = core.take_proof(uid, file_unique_id="app:" + sha, duration=int(round(dur)))
    if not ok:
        return jsonify(ok=False, msg=msg)
    ext = "mp4" if "mp4" in mime or "quicktime" in mime else "webm"
    path = os.path.join(PROOF_DIR, f"{uid}_{core.now():%Y%m%d_%H%M%S}.{ext}")
    with open(path, "wb") as f:
        f.write(blob)
    with core.db() as c:  # в чат бота самому человеку — уведомление «✅ Приход отмечен в …» (без видео)
        core.notify(c, uid, msg)
    if core.employee(uid):  # видео сотрудника — руководителю / в группу проверки, как кружок
        cin, late = core.last_checkin(uid)
        note = f"📹 {core.name(uid)}: приход отмечен в {cin} (видео из приложения)\n🔢 Код: {code} — проверьте, что назвал(а) его"
        if late:
            note += f"\n⚠️ Опоздание: {core.fmt_min(late)}"
        chat = getattr(config, "PROOF_CHAT_ID", None)
        targets = [(chat, getattr(config, "PROOF_THREAD_ID", None))] if chat else [(a, None) for a in config.ADMINS]
        with core.db() as c:
            for chat_id, thread in targets:
                c.execute("INSERT INTO outbox(chat_id, text, photo, thread_id) VALUES(?, ?, ?, ?)",
                          (chat_id, note, "video:" + path, thread))
    return jsonify(ok=True, msg=msg)


def _channel_on() -> bool:
    return bool(getattr(config, "CLIENT_CHANNEL_ID", None) and (getattr(config, "CLIENT_BOT_TOKEN", "") or config.BOT_TOKEN))


def _tg(method: str, **params):
    """Вызов Telegram Bot API от бота клиентов (он админ канала и одобряет заявки)."""
    import urllib.request
    token = getattr(config, "CLIENT_BOT_TOKEN", "") or config.BOT_TOKEN
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.loads(r.read().decode())
    if not j.get("ok"):
        raise RuntimeError(j.get("description") or "telegram error")
    return j["result"]


@app.post("/api/client_channel")
@need_user()
def api_client_channel(uid, data):
    """«📢 Канал» в мини-аппе клиента: покупателю — личная ссылка-заявка на 24 часа (одна на человека)."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    lang = i18n.lang_of((core.get_client(uid) or {}).get("lang"))
    chat = getattr(config, "CLIENT_CHANNEL_ID", None)
    if not _channel_on():
        return jsonify(ok=False, msg=i18n.t(lang, "channel_error"))
    try:
        m = _tg("getChatMember", chat_id=chat, user_id=uid)
        if m.get("status") in ("member", "administrator", "creator"):
            return jsonify(ok=True, status="already", msg=i18n.t(lang, "channel_already"))
    except Exception:  # noqa: BLE001 — не смогли проверить, продолжаем
        pass
    stats = erp.sync_client(uid, True) if erp.enabled() else {}
    if float((stats or {}).get("purchases") or 0) < getattr(config, "CLIENT_CHANNEL_MIN_PURCHASES", 1):
        return jsonify(ok=False, status="no_purchase", msg=i18n.t(lang, "channel_no_purchase"))
    link = core.channel_link_get(uid, chat)
    if not link:
        hours = getattr(config, "CLIENT_CHANNEL_LINK_HOURS", 24)
        try:
            inv = _tg("createChatInviteLink", chat_id=chat, name=f"client {uid}"[:32], creates_join_request=True,
                      expire_date=int((_dt.now(_tz.utc) + _td(hours=hours)).timestamp()))
        except Exception as e:  # noqa: BLE001
            app.logger.warning("Канал (мини-апп): не удалось создать ссылку для %s: %s", uid, e)
            return jsonify(ok=False, msg=i18n.t(lang, "channel_error"))
        link = inv["invite_link"]
        core.channel_link_add(link, uid, chat, core.now() + _td(hours=hours))
    return jsonify(ok=True, status="link", link=link, msg=i18n.t(lang, "channel_link"), btn=i18n.t(lang, "channel_btn"))


@app.post("/api/sellers")
@need_user(admin=True)
def api_sellers(uid, data):
    """Продавцы BILLZ ↔ сотрудники: список с предложениями системы; action=save {links: {имя BILLZ: ID}}."""
    ok, msg = True, ""
    if data.get("action") == "save":
        ok, msg = sellers.save(data.get("links") or {})
    elif data.get("action") == "refresh":
        ok, msg = sellers.refresh_week()
    elif data.get("action") == "save_emp":
        ok, msg = sellers.save_by_employee(data.get("links") or {})
    if data.get("view") == "billz":  # список реальных продавцов BILLZ для кнопки выбора
        return jsonify(ok=True, emps=sellers.by_employee(), **sellers.billz_names())
    if data.get("view") == "emp":  # связь без продаж: сотрудник → как записан в BILLZ
        return jsonify(ok=ok, msg=msg, emps=sellers.by_employee())
    return jsonify(ok=ok, msg=msg, items=sellers.overview(), diag=sellers.diagnose(),
                   team=[{"id": e, "name": core.name(e)} for e in config.EMPLOYEES],
                   mirror=(core.flag_json("billz_mirror") or {}).get("at", ""))


@app.post("/api/followup")
@need_user(admin=True)
def api_followup(uid, data):
    """Обратная связь после покупки: статус, предпросмотр (preview), режим (mode = test / live)."""
    a, ok, msg, text = data.get("action"), True, "", ""
    if a == "mode":
        ok, msg = followup.set_mode(str(data.get("mode") or "test"))
    elif a == "preview":
        text = followup.preview_now()
    elif a == "now":
        ok, msg = followup.run_now()
    elif a == "test":  # отметить / снять тестового клиента
        ok, msg = followup.mark_test(str(data.get("key") or ""), str(data.get("name") or ""), bool(data.get("on")))
    return jsonify(ok=ok, msg=msg, text=text, clients=followup.recent_clients(), **followup.status())


@app.post("/api/direct_message")
@need_user(admin=True)
def api_direct_message(uid, data):
    """Личное сообщение одному человеку в бот: клиенту — через бот клиентов, сотруднику — через бот сотрудников.
    {name} / {ism} — подставится имя."""
    to, kind = int(data.get("to") or 0), data.get("kind")
    text = str(data.get("text") or "").strip()[:4000]
    if not to or not text:
        return jsonify(ok=False, msg="Напишите сообщение.")
    with core.db() as c:
        if kind == "client":
            cl = core.get_client(to)
            if not cl:
                return jsonify(ok=False, msg="Клиент не найден в боте — написать ему нельзя.")
            first = core.client_first_name(cl) or ""
            text = text.replace("{name}", first).replace("{ism}", first).replace("{имя}", first)
            c.execute("INSERT INTO outbox(chat_id, text, via) VALUES(?, ?, 'client')", (to, text))
        else:
            if not core.employee(to):
                return jsonify(ok=False, msg="Сотрудник не найден.")
            first = core.name(to).split()[0]
            text = text.replace("{name}", first).replace("{ism}", first).replace("{имя}", first)
            core.notify(c, to, text)
    return jsonify(ok=True, msg="✉️ Сообщение отправлено в бот")


@app.post("/api/cancel_late_fines")
@need_user(admin=True)
def api_cancel_late_fines(uid, data):
    """Отменить штрафы за опоздание за сегодня (например, бот не работал) и не выписывать новые сегодня."""
    ok, msg = core.cancel_late_fines()
    return jsonify(ok=ok, msg=msg)


@app.post("/api/admin_checkout")
@need_user(admin=True)
def api_admin_checkout(uid, data):
    """Руководитель закрывает смену сотрудника, который забыл нажать «Ушёл»."""
    ok, msg = core.admin_checkout(int(data.get("user_id") or 0), str(data.get("time") or ""))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/checkout")
@need_user(staff=True)
def api_checkout(uid, data):
    ok, msg = core.check_out(uid)
    if ok:
        with core.db() as c:
            core.notify(c, uid, msg)  # «Уход отмечен» — и в чат бота
    return jsonify(ok=ok, msg=msg)


@app.post("/api/task_done")
@need_user(staff=True)
def api_task_done(uid, data):
    ok, msg = core.complete_task(int(data.get("id", 0)), uid)
    return jsonify(ok=ok, msg=msg)


# ---------- ERP: учёт продаж ----------

def _shop_guard(uid):
    if not shop.has_access(uid):
        return jsonify(ok=False, msg="Нет доступа к продажам — попросите руководителя выдать роль."), 403
    return None


@app.post("/api/shop/state")
@need_user(staff=True)
def api_shop_state(uid, data):
    bad = _shop_guard(uid)
    if bad:
        return bad
    me = shop.staff_row(uid)
    owner_view = me["role"] == "owner" or "settings" in me["perms"]
    source = "billz" if data.get("source") == "billz" and owner_view else "erp"
    args = (data.get("from") or None, data.get("to") or None)
    erp_rows = shop.sales_list(uid, *args, q=data.get("q") or "", point_id=data.get("point_id") or None)
    rows = shop.billz_sales_list(*args, q=data.get("q") or "", point_id=data.get("point_id") or None) if source == "billz" else erp_rows
    res = {"me": {"id": uid, "role": me["role"], "role_title": shop.ROLES[me["role"]], "perms": me["perms"],
                  "point_id": me.get("point_id")},
           "settings": shop.settings(), "points": shop.points(), "pay_types": shop.pay_types(),
           "pay_kinds": shop.PAY_KINDS, "roles": shop.ROLES, "perm_titles": shop.PERMS,
           "sellers": [{"id": s["id"], "name": s["name"], "point_id": s["point_id"]} for s in shop.staff_list()
                       if s["role"] != "none"] + [{"id": a, "name": core.name(a) + " (руководитель)", "point_id": None}
                                                  for a in config.ADMINS],
           "sales": rows, "summary": shop.summary(rows), "drafts": shop.drafts(uid) if "sell" in me["perms"] else [],
           "billz_on": erp.enabled(), "source": source, "owner_view": owner_view}
    if owner_view and erp.enabled():  # сверка: BILLZ и наш учёт за тот же период и точку
        b = shop.summary(shop.billz_sales_list(*args, point_id=data.get("point_id") or None)) if source != "billz" else res["summary"]
        e = shop.summary(erp_rows)
        res["recon"] = {"billz_count": b["count"], "billz_total": b["total"], "erp_count": e["count"], "erp_total": e["total"],
                        "mirror": core.flag_json("billz_mirror") or {}}
    if me["role"] == "owner" or "settings" in me["perms"]:
        res["staff"] = shop.staff_list()
    if "unfreeze" in me["perms"]:
        res["bonuses"] = shop.bonuses_due()
    return jsonify(res)


@app.post("/api/shop/client")
@need_user(staff=True)
def api_shop_client(uid, data):
    bad = _shop_guard(uid)
    if bad:
        return bad
    if data.get("q") is not None:  # гибкий поиск: часть номера или имя
        return jsonify(items=shop.search_clients(data.get("q")))
    return jsonify(shop.find_client(data.get("phone")))


@app.post("/api/shop/sale")
@need_user(staff=True)
def api_shop_sale(uid, data):
    bad = _shop_guard(uid)
    if bad:
        return bad
    ok, msg, sid = shop.create_sale(uid, data)
    return jsonify(ok=ok, msg=msg, id=sid)


@app.post("/api/shop/draft")
@need_user(staff=True)
def api_shop_draft(uid, data):
    if data.get("action") == "delete":
        ok, msg = shop.delete_draft(uid, int(data.get("id") or 0))
        return jsonify(ok=ok, msg=msg)
    ok, msg, did = shop.save_draft(uid, data.get("data") or {}, data.get("id"))
    return jsonify(ok=ok, msg=msg, id=did)


@app.post("/api/shop/unfreeze")
@need_user(staff=True)
def api_shop_unfreeze(uid, data):
    ok, msg = shop.unfreeze(uid, int(data.get("id") or 0))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/shop/settings")
@need_user(staff=True)
def api_shop_settings(uid, data):
    if not shop.can(uid, "settings"):
        return jsonify(ok=False, msg="Нет права менять настройки ERP.")
    act = data.get("action")
    if act == "settings":
        ok, msg = shop.save_settings(data)
    elif act == "point":
        ok, msg = shop.save_point(data.get("id"), data.get("title"), data.get("address"), data.get("active", True))
    elif act == "pay_type":
        ok, msg = shop.save_pay_type(data.get("id"), data.get("title"), data.get("icon"), data.get("kind"), data.get("active", True))
    elif act == "import_billz":
        ok, msg = shop.import_from_billz()
    elif act == "mirror_billz":
        try:
            n = erp.mirror_sales(str(data.get("from") or core.now().strftime("%Y-%m-%d"))[:10],
                                 str(data.get("to") or data.get("from") or core.now().strftime("%Y-%m-%d"))[:10])
            ok, msg = True, f"🅱️ Из BILLZ загружено транзакций: {n}"
        except erp.BillzError as e:
            ok, msg = False, f"Не удалось загрузить продажи из BILLZ: {e}"
    elif act == "staff":
        ok, msg = shop.set_staff(uid, int(data.get("user_id") or 0), data.get("role"), data.get("point_id"), data.get("kpi_pct"),
                                 data.get("senior_id"), data.get("senior_pct"), data.get("perms"))
    else:
        ok, msg = False, "Неизвестное действие."
    return jsonify(ok=ok, msg=msg)


@app.post("/api/my_task")
@need_user(staff=True)
def api_my_task(uid, data):
    """Сотрудник: своя задача на сегодня/завтра (action=add) или удалить свою (action=delete)."""
    if data.get("action") == "delete":
        ok, msg = core.delete_self_task(uid, int(data.get("id") or 0))
    else:
        ok, msg = core.add_self_task(uid, data.get("title"), str(data.get("day") or "today"))
    return jsonify(ok=ok, msg=msg)


def can_assign(uid) -> bool:
    return core.is_admin(uid) or uid in getattr(config, "TASK_ASSIGNERS", [])


@app.post("/api/add_task")
@need_user(staff=True)
def api_add_task(uid, data):
    """Задача одному или сразу нескольким сотрудникам (user_ids) — каждому своя.
    Ставят руководитель и сотрудники из TASK_ASSIGNERS."""
    if not can_assign(uid):
        return jsonify(ok=False, msg="Нет доступа: ставить задачи может руководитель."), 403
    raw = data.get("user_ids") or ([data.get("user_id")] if data.get("user_id") else [])
    targets = []
    for t in raw:
        try:
            t = int(t)
        except (TypeError, ValueError):
            continue
        if core.employee(t) and t not in targets:
            targets.append(t)
    title = str(data.get("title", "")).strip()
    if not targets or not title:
        return jsonify(ok=False, msg="Выберите сотрудников и введите задачу.")
    repeat = str(data.get("repeat") or "once")
    if repeat in ("daily", "weekly"):  # повторяющаяся: каждый рабочий день / каждую неделю
        results = [core.add_recurring(t, title, repeat, data.get("weekday")) for t in targets]
        if not all(ok for ok, _ in results):
            return jsonify(ok=False, msg=next(m for ok, m in results if not ok))
        if len(targets) == 1:
            return jsonify(ok=True, msg=results[0][1])
        when = "каждый рабочий день" if repeat == "daily" else "каждую неделю"
        return jsonify(ok=True, msg=f"🔁 Задача {when} поставлена {len(targets)} сотрудникам: " +
                                    ", ".join(core.name(t) for t in targets))
    for t in targets:
        core.add_task(t, title, by=uid)
    who = ", ".join(core.name(t) for t in targets)
    if not core.is_admin(uid):  # руководителю — кто кому что поставил
        with core.db() as c:
            core.notify_admins(c, f"📌 {core.name(uid)} поставил(а) задачу «{title}»: {who}")
    return jsonify(ok=True, msg="Задача поставлена: " + who)


@app.post("/api/crm/chats")
@need_user(admin=True)
def api_crm_chats(uid, data):
    """CRM: рабочие аккаунты и список чатов (поиск, фильтры: waiting / closing / deleted)."""
    return jsonify(ok=True, accounts=crm.accounts(),
                   chats=crm.chats(str(data.get("account") or ""), str(data.get("q") or ""), str(data.get("only") or "")))


@app.post("/api/crm/messages")
@need_user(admin=True)
def api_crm_messages(uid, data):
    return jsonify(ok=True, **crm.messages(str(data.get("conn") or ""), int(data.get("chat") or 0), int(data.get("before") or 0)))


@app.post("/api/crm/send")
@need_user(admin=True)
def api_crm_send(uid, data):
    """Ответ клиенту из CRM: текст и/или фото (base64) — от имени рабочего аккаунта."""
    photo = b""
    raw = str(data.get("photo") or "")
    if raw:
        try:
            photo = base64.b64decode(raw.split(",", 1)[-1], validate=True)
        except Exception:  # noqa: BLE001
            return jsonify(ok=False, msg="Фото не загрузилось, выберите ещё раз.")
    ok, msg = crm.send(uid, str(data.get("conn") or ""), int(data.get("chat") or 0), str(data.get("text") or ""), photo)
    return jsonify(ok=ok, msg=msg)


@app.post("/api/crm/voice")
@need_user(admin=True)
def api_crm_voice(uid, data):
    """Голосовое из CRM: запись из браузера (base64) → OGG/Opus → голосовое от рабочего аккаунта."""
    raw = str(data.get("audio") or "")
    mime = raw[5:raw.find(";")] if raw.startswith("data:") and ";" in raw else str(data.get("mime") or "")
    try:
        blob = base64.b64decode(raw.split(",", 1)[-1], validate=True)
    except Exception:  # noqa: BLE001
        return jsonify(ok=False, msg="Запись не загрузилась, попробуйте ещё раз.")
    if len(blob) > 20 * 1024 * 1024:
        return jsonify(ok=False, msg="Слишком длинная запись.")
    try:
        dur = float(data.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0
    ok, msg = crm.send_voice(uid, str(data.get("conn") or ""), int(data.get("chat") or 0), blob, mime, dur)
    return jsonify(ok=ok, msg=msg)


@app.post("/api/crm/circle")
@need_user(admin=True)
def api_crm_circle(uid, data):
    """Кружок из CRM: видео из браузера (base64) → кружок Telegram от рабочего аккаунта."""
    raw = str(data.get("video") or "")
    mime = raw[5:raw.find(";")] if raw.startswith("data:") and ";" in raw else ""
    try:
        blob = base64.b64decode(raw.split(",", 1)[-1], validate=True)
    except Exception:  # noqa: BLE001
        return jsonify(ok=False, msg="Видео не загрузилось, запишите ещё раз.")
    if len(blob) > 40 * 1024 * 1024:
        return jsonify(ok=False, msg="Слишком большое видео.")
    try:
        dur = float(data.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0
    ok, msg = crm.send_circle(uid, str(data.get("conn") or ""), int(data.get("chat") or 0), blob, mime, dur)
    return jsonify(ok=ok, msg=msg)


@app.post("/api/crm/info")
@need_user(admin=True)
def api_crm_info(uid, data):
    """Заметки и задачи по клиенту: action = note / del_note / task (иначе — просто прочитать)."""
    conn, chat, a = str(data.get("conn") or ""), int(data.get("chat") or 0), data.get("action")
    ok, msg = True, ""
    if a == "note":
        ok, msg = crm.add_note(uid, conn, chat, str(data.get("text") or ""))
    elif a == "del_note":
        ok, msg = crm.delete_note(int(data.get("id") or 0))
    elif a == "task":
        ok, msg = crm.add_task(uid, conn, chat, str(data.get("title") or ""), str(data.get("day") or "today"),
                               int(data.get("assignee") or 0))
    return jsonify(ok=ok, msg=msg, **crm.info(conn, chat))


@app.post("/api/crm/templates")
@need_user(admin=True)
def api_crm_templates(uid, data):
    a = data.get("action")
    if a == "save":
        ok, msg = crm.save_template(int(data.get("id") or 0), str(data.get("title") or ""), str(data.get("text") or ""))
    elif a == "delete":
        ok, msg = crm.delete_template(int(data.get("id") or 0))
    else:
        ok, msg = True, ""
    return jsonify(ok=ok, msg=msg, items=crm.templates())


@app.get("/crm/ava/<int:chat>")
def crm_ava(chat):
    """Фото профиля клиента (ссылка подписана сервером)."""
    path = crm.avatar(chat, request.args.get("s", ""))
    if not path:
        abort(404)
    return send_file(path, mimetype="image/jpeg", max_age=6 * 3600)


@app.get("/crm/media/<conn>/<int:chat>/<int:msg>")
def crm_media(conn, chat, msg):
    """Фото / видео / голосовые из переписки (ссылка подписана сервером — открывается только из CRM)."""
    try:
        path, mime = crm.media_file(conn, chat, msg, request.args.get("s", ""), request.args.get("f", ""))
    except Exception as e:  # noqa: BLE001
        app.logger.warning("CRM медиа %s/%s/%s: %s", conn, chat, msg, e)
        abort(404)
    if not path:
        abort(404)
    return send_file(path, mimetype=mime, max_age=86400)


@app.post("/api/kpi")
@need_user(admin=True)
def api_kpi(uid, data):
    """Касса и KPI сотрудника: тип продаж (save_type/delete_type), продажа (add/delete), «Оплачено» (pay)."""
    a, emp = data.get("action"), int(data.get("user_id") or 0)
    if a == "save_type":
        ok, msg = kpi.save_type(emp, str(data.get("name") or ""), data.get("pct"), int(data.get("id") or 0))
    elif a == "delete_type":
        ok, msg = kpi.delete_type(int(data.get("id") or 0))
    elif a == "add":
        ok, msg = kpi.add_sale(emp, int(data.get("type_id") or 0), data.get("amount"), str(data.get("day") or ""), uid)
    elif a == "delete":
        ok, msg = kpi.delete_sale(int(data.get("id") or 0))
    elif a == "pay":
        ok, msg = kpi.pay(emp, int(data.get("id") or 0))
    else:
        ok, msg = False, "Неизвестное действие."
    return jsonify(ok=ok, msg=msg)


@app.post("/api/earning")
@need_user(admin=True)
def api_earning(uid, data):
    """Заработок сотрудника: добавить (action=add) или удалить (action=delete) запись."""
    if data.get("action") == "delete":
        ok, msg = core.delete_earning(int(data.get("id") or 0))
    else:
        ok, msg = core.add_earning(int(data.get("user_id") or 0), data.get("amount", ""), str(data.get("month") or ""),
                                   str(data.get("note") or ""), str(data.get("date_from") or ""),
                                   str(data.get("date_to") or ""))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/stop_recurring")
@need_user(admin=True)
def api_stop_recurring(uid, data):
    ok, msg = core.stop_recurring(int(data.get("id") or 0))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/add_penalty")
@need_user(admin=True)
def api_add_penalty(uid, data):
    target = int(data.get("user_id", 0))
    reason = str(data.get("reason", "")).strip()
    if not core.employee(target) or not reason:
        return jsonify(ok=False, msg="Выберите сотрудника и укажите причину.")
    core.add_penalty(target, reason, str(data.get("amount", "")))
    return jsonify(ok=True, msg=f"Штраф выписан: {core.name(target)}")


@app.post("/api/pay_penalty")
@need_user(admin=True)
def api_pay_penalty(uid, data):
    ok, msg = core.pay_penalty(int(data.get("id", 0)))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/delete_penalty")
@need_user(admin=True)
def api_delete_penalty(uid, data):
    ok, msg = core.delete_penalty(int(data.get("id", 0)))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/set_week")
@need_user(admin=True)
def api_set_week(uid, data):
    week = data.get("week") or {}
    ok, msg = core.set_week(int(data.get("user_id", 0)), {str(k): v for k, v in week.items()})
    return jsonify(ok=ok, msg=msg)


@app.post("/api/set_shift")
@need_user(admin=True)
def api_set_shift(uid, data):
    ok, msg = core.set_shift(int(data.get("user_id", 0)), str(data.get("shift", "")))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/task_review")
@need_user(admin=True)
def api_task_review(uid, data):
    act = data.get("action") or ("ok" if data.get("approve") else "back")
    if act not in ("ok", "back", "later"):
        return jsonify(ok=False, msg="Неизвестное действие.")
    ok, msg = core.review_task(int(data.get("id", 0)), act)
    return jsonify(ok=ok, msg=msg)


@app.post("/api/billz_base")
@need_user(admin=True)
def api_billz_base(uid, data):
    """Клиентская база BILLZ (копия): поиск и постраничный список; action=refresh — обновить сейчас."""
    if data.get("action") == "refresh":
        if not erp.enabled():
            return jsonify(ok=False, msg="BILLZ не подключён: нет BILLZ_SECRET_TOKEN в Railway.")
        started = erp.start_import()
        return jsonify(ok=True, msg="🔄 Загружаю базу из BILLZ — это займёт до пары минут." if started
                       else "Загрузка уже идёт…")
    res = core.billz_base(data.get("q", ""), max(0, int(data.get("offset") or 0)))
    res["status"]["running"] = erp.import_running()
    res["enabled"] = erp.enabled()
    return jsonify(res)


@app.post("/api/export_clients")
@need_user(admin=True)
def api_export_clients(uid, data):
    """Выгрузить ID всех клиентов бота — бот пришлёт файл CSV руководителю в личку."""
    path = core.export_clients()
    with core.db() as c:
        n = c.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        c.execute("INSERT INTO outbox(chat_id, text, photo) VALUES(?, ?, ?)",
                  (uid, f"📤 Клиенты бота: {n} (Telegram ID, имя, телефон, @username)", "doc:" + path))
    return jsonify(ok=True, msg=f"📤 Файл с {n} клиентами придёт вам в бот через несколько секунд")


@app.post("/api/import_clients")
@need_user(admin=True)
def api_import_clients(uid, data):
    text = str(data.get("text") or "")
    if data.get("xlsx"):  # файл Excel — читаем на сервере
        try:
            text = core.xlsx_to_text(base64.b64decode(str(data["xlsx"]).split(",", 1)[-1]))
        except Exception as e:  # noqa: BLE001
            return jsonify(ok=False, msg=f"Не удалось прочитать Excel: {e}")
    if len(text) > 3_000_000:
        return jsonify(ok=False, msg="Файл слишком большой.")
    r = core.import_clients(text)
    msg = f"📥 Добавлено: {r['added']} · уже были: {r['existing']}"
    if r["bad_count"]:
        msg += f"\nНе понял строк: {r['bad_count']} (например: {r['bad'][0]})"
    return jsonify(ok=True, msg=msg, **r)


@app.post("/api/client_card")
@need_user(admin=True)
def api_client_card(uid, data):
    """Карточка клиента: профиль, статистика BILLZ, заметки, долги, история покупок."""
    if data.get("action") == "note":
        ok, msg = shop.save_note(data.get("phone"), data.get("note"), data.get("name") or "")
        return jsonify(ok=ok, msg=msg)
    if data.get("action") == "history":
        started = erp.history_backfill()
        return jsonify(ok=True, msg="📥 Загружаю историю продаж из BILLZ за год — это займёт несколько минут." if started
                       else "Загрузка истории уже идёт…")
    if erp.enabled() and not (core.flag_json("billz_history") or {}).get("from"):
        erp.history_backfill()  # первый раз — история продаж за год загрузится сама (в фоне)
    import managers
    card = shop.client_card(data.get("bot_id") or None, data.get("store_id") or None)
    card["manager"] = managers.get(card.get("bot_id"), card.get("store_id")) or 0
    card["managers"] = managers.team()
    return jsonify(card)


@app.post("/api/branding")
@need_user(admin=True)
def api_branding(uid, data):
    """Надписи кабинета: название вверху, подпись, приветствие на главной (пусто — по умолчанию)."""
    keys = {"title": 40, "sub": 60, "welcome": 60, "welcome_sub": 120}
    b = {k: str(data.get(k) or "").strip()[:n] for k, n in keys.items()}
    core.flag_json("branding", {k: v for k, v in b.items() if v})
    return jsonify(ok=True, msg="✏️ Надписи сохранены")


@app.post("/api/client_manager")
@need_user(admin=True)
def api_client_manager(uid, data):
    """Назначить клиенту личного менеджера (раздел «👤 Мой менеджер» в клиентском боте, Silver / Gold)."""
    import managers
    ok, msg = managers.assign(data.get("user_id") or None, data.get("store_id") or None, data.get("manager") or 0)
    return jsonify(ok=ok, msg=msg)


@app.post("/api/set_tier")
@need_user(admin=True)
def api_set_tier(uid, data):
    if data.get("store_id"):  # клиент из базы магазина (ещё не в боте)
        ok, msg = core.set_store_tier(str(data["store_id"]), str(data.get("tier", "")))
    else:
        ok, msg = core.set_client_tier(int(data.get("user_id", 0)), str(data.get("tier", "")))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/add_news")
@need_user(admin=True)
def api_add_news(uid, data):
    texts = i18n.clean_texts(data.get("texts")) or i18n.clean_texts({"uz": data.get("text", "")})
    if not texts:
        return jsonify(ok=False, msg="Напишите текст новости хотя бы на одном языке.")
    core.add_news(texts=texts)
    return jsonify(ok=True, msg="📰 Новость опубликована для клиентов")


@app.post("/api/delete_news")
@need_user(admin=True)
def api_delete_news(uid, data):
    core.delete_news(int(data.get("id", 0)))
    return jsonify(ok=True, msg="Новость удалена.")


@app.post("/api/delete_task")
@need_user(admin=True)
def api_delete_task(uid, data):
    core.delete_task(int(data.get("id", 0)))
    return jsonify(ok=True, msg="Задача удалена.")



# ---------- постинг ----------

def is_poster(uid) -> bool:
    return core.is_admin(uid) or uid in getattr(config, "POSTERS", [])


@app.post("/api/post")
@need_user(staff=True)
def api_post(uid, data):
    if not is_poster(uid):
        return jsonify(ok=False, msg="Постинг доступен только руководителю."), 403
    if not core.is_admin(uid):
        data["bonus"] = 0  # бонусы в постах начисляет только руководитель
    act = data.get("action")
    if act == "cancel":
        ok, msg = posts.cancel(uid, int(data.get("id") or 0))
        return jsonify(ok=ok, msg=msg)
    photo = None
    raw = str(data.get("image") or "")
    if raw:
        if "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            img = base64.b64decode(raw, validate=True)
        except Exception:
            img = b""
        if not img.startswith(b"\xff\xd8\xff") or len(img) > 5_000_000:
            return jsonify(ok=False, msg="Не удалось прочитать фото.")
        photo = posts.save_photo(img)
    args = (data.get("texts"), photo, data.get("button_text"), data.get("button_url"))
    if act == "test":
        ok, msg = posts.test(uid, *args, text=data.get("text", ""), audience=data.get("audience") or [])
    elif act == "create":
        ok, msg = posts.create(uid, *args, data.get("audience") or [], bool(data.get("to_news")),
                               data.get("send_at") or "", text=data.get("text", ""), bonus=data.get("bonus") or 0)
    else:
        ok, msg = False, "Неизвестное действие."
    return jsonify(ok=ok, msg=msg)


# ---------- личные проекты руководителя ----------

@app.post("/api/proj")
@need_user(admin=True)
def api_proj(uid, data):
    """Одна точка для всех действий с проектами: data.action = ..."""
    act, i = data.get("action"), int(data.get("id") or 0)
    if act == "add_project":
        ok, msg = projects.add_project(uid, data.get("title"))
    elif act == "rename_project":
        ok, msg = projects.rename_project(uid, i, data.get("title"))
    elif act == "delete_project":
        ok, msg = projects.delete_project(uid, i)
    elif act == "add_task":
        ok, msg = projects.add_task(uid, i, data.get("title"), data.get("due"))
    elif act == "update_task":
        ok, msg = projects.update_task(uid, i, data.get("title"), data.get("note"), data.get("due"))
    elif act == "plan":
        ok, msg = projects.plan(uid, i, str(data.get("when") or ""))
    elif act == "done":
        ok, msg = projects.set_done(uid, i, bool(data.get("done")))
    elif act == "delete_task":
        ok, msg = projects.delete_task(uid, i)
    else:
        ok, msg = False, "Неизвестное действие."
    return jsonify(ok=ok, msg=msg)


@app.post("/api/invest_request")
@need_user(staff=True)
def api_invest_request(uid, data):
    ok, msg = invest.request(uid, data.get("amount", ""))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/invest_cancel")
@need_user(staff=True)
def api_invest_cancel(uid, data):
    ok, msg = invest.cancel(uid, int(data.get("id", 0)))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/invest_decide")
@need_user(admin=True)
def api_invest_decide(uid, data):
    ok, msg = invest.decide(int(data.get("id", 0)), bool(data.get("approve")))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/invest_early")
@need_user(staff=True)
def api_invest_early(uid, data):
    if data.get("cancel"):
        ok, msg = invest.early_cancel(uid, int(data.get("id", 0)))
    else:
        ok, msg = invest.early_request(uid, int(data.get("id", 0)), data.get("name", ""),
                                       data.get("phone", ""), data.get("requisites", ""))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/invest_early_decide")
@need_user(admin=True)
def api_invest_early_decide(uid, data):
    inv_id = int(data.get("id", 0))
    if not data.get("approve"):
        ok, msg = invest.early_reject(inv_id)
        return jsonify(ok=ok, msg=msg)
    method = str(data.get("method", ""))
    if method not in invest.PAY_METHODS:
        return jsonify(ok=False, msg="Выберите: онлайн или офлайн оплата.")
    raw = str(data.get("image", ""))
    if "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        img = base64.b64decode(raw, validate=True)
    except Exception:
        img = b""
    if not img.startswith(b"\xff\xd8\xff"):
        return jsonify(ok=False, msg="Прикрепите фото-отчёт о выплате.")
    if len(img) > 5_000_000:
        return jsonify(ok=False, msg="Фото слишком большое.")
    ok, msg = invest.early_pay(inv_id, method, invest.save_report(inv_id, img))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/invest_access")
@need_user(staff=True)
def api_invest_access(uid, data):
    ok, msg = invest.access_request(uid, data.get("plan", ""), data.get("note", ""))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/invest_access_decide")
@need_user(admin=True)
def api_invest_access_decide(uid, data):
    ok, msg = invest.access_decide(int(data.get("user_id", 0)), bool(data.get("approve")))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/invest_rate")
@need_user(admin=True)
def api_invest_rate(uid, data):
    ok, msg = invest.set_level(str(data.get("month", "")), str(data.get("level", "")))
    return jsonify(ok=ok, msg=msg)


@app.post("/api/invest_paid")
@need_user(admin=True)
def api_invest_paid(uid, data):
    ok, msg = invest.mark_paid(int(data.get("id", 0)))
    return jsonify(ok=ok, msg=msg)


PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>Личный кабинет</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: #0d0e1c; --card: #171a2e; --card2: #1e2140; --line: rgba(255,255,255,.07);
    --text: #f4f5ff; --hint: #9a9fbd; --accent: #7c5cff; --accent2: #5b8cff;
    --green: #34d399; --red: #f87171; --orange: #fbbf24;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin: 0; background: var(--bg); color: var(--text); font-size: 15px;
         font-family: -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
         background-image: radial-gradient(600px 300px at 90% -5%, rgba(124,92,255,.28), transparent 70%);
         background-repeat: no-repeat; min-height: 100vh; }
  .wrap { padding: 16px 14px 36px; max-width: 560px; margin: 0 auto; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 20px; padding: 16px; margin-bottom: 14px; }
  .hint { color: var(--hint); }
  .hidden { display: none !important; }
  h3 { margin: 0 0 14px; font-size: 18px; }

  /* профиль */
  .profile { display: flex; gap: 16px; align-items: center; margin: 4px 0 18px; position: relative; }
  .ava { position: relative; width: 96px; height: 96px; border-radius: 50%; flex: none; padding: 3px;
         background: linear-gradient(135deg, var(--accent), var(--accent2)); box-shadow: 0 0 24px rgba(124,92,255,.45); }
  .ava > div, .ava > img, #me-ava > div, #me-ava > img { width: 100%; height: 100%; border-radius: 50%; object-fit: cover; background: var(--card2);
         display: flex; align-items: center; justify-content: center; font-size: 34px; font-weight: 700; border: 3px solid var(--bg); }
  .pname { font-size: 22px; font-weight: 800; line-height: 1.15; }
  .prole { color: var(--hint); margin: 2px 0 8px; }
  .lvl-pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px 4px 4px; border-radius: 99px;
              background: linear-gradient(90deg, rgba(124,92,255,.35), rgba(124,92,255,.12)); font-weight: 700; font-size: 14px; }
  .lvl-pill svg { width: 26px; height: 26px; }
  .sparkle { position: absolute; right: 6px; top: -4px; color: var(--accent); font-size: 22px; opacity: .8; }

  .goal { font-size: 32px; font-weight: 800; font-variant-numeric: tabular-nums; }
  .goal small { font-size: 22px; color: var(--hint); font-weight: 600; }
  .bar-row { display: flex; align-items: center; gap: 10px; margin: 10px 0 18px; }
  .bar { flex: 1; height: 16px; background: var(--card2); border-radius: 99px; overflow: hidden; }
  .bar > div { height: 100%; min-width: 6px; border-radius: 99px; transition: width .8s;
               background: linear-gradient(90deg, var(--accent), var(--accent2)); box-shadow: 0 0 14px rgba(124,92,255,.6); }
  .pct { padding: 4px 10px; border-radius: 99px; border: 1px solid var(--line); background: var(--card); font-weight: 700; font-size: 14px; }

  /* статистика */
  .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 14px; }
  .stat { border-radius: 18px; padding: 14px 6px; text-align: center; border: 1px solid var(--line); }
  .stat .ic { font-size: 24px; }
  .stat b { display: block; font-size: 20px; margin: 6px 0 2px; }
  .stat span { color: var(--hint); font-size: 13px; }
  .s1 { background: linear-gradient(180deg, #172540, #141a30); }
  .s2 { background: linear-gradient(180deg, #33231f, #1d1a26); }
  .s3 { background: linear-gradient(180deg, #2a2150, #1b1a36); }

  /* статус и кнопка */
  .status { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; }
  button { font: inherit; border: none; cursor: pointer; color: var(--text); }
  .btn { border-radius: 14px; padding: 13px 16px; font-weight: 700; white-space: nowrap; }
  .btn-main { background: linear-gradient(90deg, var(--accent), var(--accent2)); color: #fff; }
  .btn-soft { background: var(--card2); }
  .tier-ring.bronza { background: linear-gradient(135deg, #f0b27a, #8a4f24); box-shadow: 0 0 22px rgba(205,127,50,.45); }
  .tier-ring.silver { background: linear-gradient(135deg, #f4f6fa, #8e98a8); box-shadow: 0 0 22px rgba(200,210,225,.45); }
  .tier-ring.gold   { background: linear-gradient(135deg, #ffe680, #c58a10); box-shadow: 0 0 24px rgba(255,200,60,.55); }
  .tier-ring #cl-ava > div, .tier-ring #cl-ava > img { width: 100%; height: 100%; border-radius: 50%; object-fit: cover;
         background: var(--card2); display: flex; align-items: center; justify-content: center; font-size: 34px;
         font-weight: 700; border: 3px solid var(--bg); }
  .cl-edit { right: auto; left: -2px; width: 30px; height: 30px; font-size: 14px; }
  .tier-badge { position: absolute; right: -2px; bottom: -2px; width: 34px; height: 34px; border-radius: 50%;
                border: 3px solid var(--bg); display: flex; align-items: center; justify-content: center; font-size: 17px;
                background: var(--card2); }
  .tier-pill { display: inline-flex; align-items: center; gap: 6px; padding: 5px 14px; border-radius: 99px;
               font-weight: 800; font-size: 14px; letter-spacing: .02em; color: #1a1306; }
  .tier-pill.bronza { background: linear-gradient(90deg, #f0b27a, #b26a32); }
  .tier-pill.silver { background: linear-gradient(90deg, #ffffff, #a9b3c2); }
  /* «🥈 Стать Silver» — блок для клиентов Bronza */
  .silver-up { position: relative; overflow: hidden; width: 100%; text-align: left; display: block; margin: 0 0 14px; padding: 16px;
    border-radius: 22px; border: 1px solid rgba(226,232,240,.55); color: #fff; cursor: pointer;
    background: radial-gradient(120% 90% at 100% 0%, rgba(226,232,240,.28), transparent 55%),
                linear-gradient(140deg, #2a2d3a 0%, #4b5163 45%, #1c1e28 100%);
    box-shadow: 0 10px 34px rgba(203,213,225,.18), inset 0 1px 0 rgba(255,255,255,.25); animation: suGlow 3s ease-in-out infinite; }
  .silver-up:active { transform: scale(.985); }
  .silver-up.su-off { cursor: default; }
  .silver-up.su-off:active { transform: none; }
  .silver-up.su-off .su-cta { animation: none; background: rgba(255,255,255,.16); color: #e2e8f0; border: 1px dashed rgba(226,232,240,.5); }
  .su-shine { position: absolute; top: 0; left: 0; width: 40%; height: 100%; pointer-events: none;
    background: linear-gradient(100deg, transparent, rgba(255,255,255,.28), transparent); animation: suShine 3.2s ease-in-out infinite; }
  .su-top { display: flex; align-items: center; gap: 12px; }
  .su-medal { width: 52px; height: 52px; flex: none; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 28px;
    background: linear-gradient(135deg, #f8fafc, #94a3b8); box-shadow: 0 0 20px rgba(226,232,240,.55); }
  .su-kicker { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: #cbd5e1; }
  .su-title { font-size: 19px; line-height: 1.2; background: linear-gradient(90deg, #fff, #cbd5e1); -webkit-background-clip: text; background-clip: text; color: transparent; }
  .su-perks { display: grid; gap: 6px; margin: 12px 0; font-size: 13.5px; color: #e2e8f0; }
  .su-cta { text-align: center; padding: 11px; border-radius: 14px; font-weight: 800; font-size: 15.5px; color: #1e293b;
    background: linear-gradient(90deg, #ffffff, #cbd5e1, #ffffff); background-size: 200% 100%; animation: suCta 2.4s linear infinite; }
  @keyframes suShine { 0% { transform: translateX(-120%) skewX(-12deg); } 60%, 100% { transform: translateX(320%) skewX(-12deg); } }
  @keyframes suGlow { 0%, 100% { box-shadow: 0 10px 34px rgba(203,213,225,.14), inset 0 1px 0 rgba(255,255,255,.25); }
    50% { box-shadow: 0 10px 40px rgba(226,232,240,.32), inset 0 1px 0 rgba(255,255,255,.35); } }
  @keyframes suCta { from { background-position: 0 0; } to { background-position: 200% 0; } }
  .tier-pill.gold   { background: linear-gradient(90deg, #fff0a8, #e0a21c); }
  .cl-row { display: flex; align-items: center; gap: 10px; padding: 9px 0; border-top: 1px solid var(--line); }
  .cl-row:first-child { border-top: none; }
  .cl-row select { width: auto; margin: 0; padding: 7px 8px; font-size: 13px; }
  .soon-stat { position: relative; opacity: .7; }
  .soon-stat em { position: absolute; top: 8px; right: 10px; font-style: normal; font-size: 11px; color: var(--hint); }
  textarea { width: 100%; font: inherit; color: var(--text); background: var(--card2); border: 1px solid var(--line);
             border-radius: 12px; padding: 12px; margin-bottom: 10px; outline: none; resize: vertical; }
  .news-item { padding: 10px 0; border-top: 1px solid var(--line); white-space: pre-wrap; word-break: break-word; }
  .news-item:first-child { border-top: none; }
  .soon { display: flex; align-items: center; gap: 12px; opacity: .6; cursor: not-allowed; user-select: none;
          background: linear-gradient(90deg, rgba(52,211,153,.12), rgba(124,92,255,.08)); }
  .soon-ic { font-size: 28px; filter: grayscale(.3); }
  .soon-pill { padding: 5px 11px; border-radius: 99px; font-size: 12.5px; font-weight: 700; white-space: nowrap;
               background: var(--card2); border: 1px solid var(--line); color: var(--hint); }
  .btn-danger { background: linear-gradient(90deg, #ef4444, #f97316); color: #fff; }
  .pen { display: flex; gap: 10px; align-items: center; padding: 8px 0; border-top: 1px solid var(--line); font-size: 14px; }
  .pen .tt { flex: 1; word-break: break-word; }
  .pen.paid .tt { opacity: .55; }
  .kpi-types { display: flex; flex-wrap: wrap; gap: 8px; margin: 4px 0 12px; }
  .kpi-types button { background: rgba(255,255,255,.06); border: 1px solid var(--line); border-radius: 999px; padding: 7px 12px;
    font-size: 13.5px; color: inherit; }
  .kpi-types button b { color: #c4b5fd; }
  .kpi-form .f-lbl { margin: 12px 0 6px; font-size: 13px; color: var(--muted); }
  .kpi-seg { display: flex; flex-wrap: wrap; gap: 8px; }
  .kpi-seg > * { flex: 1 1 0; min-width: 0; position: relative; background: rgba(255,255,255,.05); border: 1px solid var(--line);
    border-radius: 14px; padding: 10px 8px; font-size: 14px; color: inherit; text-align: center; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 2px; cursor: pointer; }
  .kpi-seg > * b { color: #c4b5fd; font-size: 15px; }
  .kpi-seg > .on { background: linear-gradient(150deg, rgba(167,139,250,.35), rgba(124,58,237,.35)); border-color: #a78bfa; }
  .kpi-seg > .on b { color: #fff; }
  .kpi-other input { position: absolute; inset: 0; opacity: 0; width: 100%; height: 100%; margin: 0; cursor: pointer; }
  .kpi-sum { display: flex; align-items: center; background: rgba(255,255,255,.05); border: 1px solid var(--line);
    border-radius: 14px; padding: 0 14px; }
  .kpi-sum input { flex: 1; margin: 0; background: none; border: 0; font-size: 22px; font-weight: 800; padding: 12px 0;
    font-variant-numeric: tabular-nums; }
  .kpi-sum span { color: var(--muted); font-weight: 600; }
  .kpi-res { margin: 10px 0; padding: 10px 14px; border-radius: 14px; background: rgba(52,211,153,.08); border: 1px solid rgba(52,211,153,.25);
    font-size: 14px; display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; }
  .kpi-res b { color: var(--green); font-size: 18px; }
  .kpi-res span { color: var(--muted); font-size: 12.5px; margin-left: auto; }
  .kpi-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; padding: 9px 0; border-top: 1px solid var(--line); font-size: 14px; }
  .kpi-row .tt { flex: 1 1 60%; min-width: 0; }
  .kpi-row .k { font-weight: 800; color: var(--green); white-space: nowrap; }
  .kpi-row.paid .tt, .kpi-row.paid .k { opacity: .55; }
  .pen-act { flex-wrap: wrap; } .pen-act .tt { flex: 1 1 100%; }
  .pen-btns { display: flex; gap: 8px; width: 100%; } .pen-btns .btn { flex: 1; }
  .pen-tag { font-size: 12px; color: var(--green); white-space: nowrap; }
  .pen-hist summary { cursor: pointer; color: var(--hint); font-size: 14px; padding: 10px 0 4px; list-style: none; }
  .pen-stats.three { grid-template-columns: 1fr 1fr 1fr; }
  .pen-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 8px; text-align: center; }
  .pen-stats > div { background: var(--card2); border-radius: 14px; padding: 10px 6px; }
  .pen-stats b { display: block; font-size: 24px; }
  .pen-stats span { color: var(--hint); font-size: 13px; }
  .btn:disabled { opacity: .35; cursor: default; }
  .btn-sm { padding: 8px 12px; font-size: 13px; border-radius: 11px; }

  /* уровни */
  .levels { display: flex; align-items: flex-start; justify-content: space-between; }
  .lv { flex: 1; text-align: center; min-width: 0; }
  .lv svg { width: 48px; height: 48px; display: block; margin: 0 auto 6px; }
  .lv b { display: block; font-size: 11px; letter-spacing: -.2px; white-space: nowrap; }
  .lv span { color: var(--hint); font-size: 10.5px; }
  .lv.locked { opacity: .38; filter: grayscale(.6); }
  .lv.cur svg { filter: drop-shadow(0 0 10px rgba(124,92,255,.9)); }
  .arrow { color: var(--hint); padding-top: 14px; font-size: 14px; width: 8px; flex: none; text-align: center; }

  /* списки */
  .head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .head h3 { margin: 0; display: flex; align-items: center; gap: 8px; }
  .link { background: none; color: #8b7bff; font-weight: 600; padding: 0; }
  .visit { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--line); }
  .visit:last-child { border-bottom: none; }
  .visit .t { display: flex; gap: 6px 16px; flex-wrap: wrap; font-variant-numeric: tabular-nums; }
  .visit.short .t { flex-wrap: nowrap; white-space: nowrap; gap: 10px; font-size: 13.5px; }
  .visit.short .t > span { gap: 4px; }
  .visit.short .ico { width: 15px; height: 15px; }
  .visit.short .dur { font-size: 12.5px; padding: 4px 9px; }
  @media (max-width: 400px) {
    .visit.short .yr { display: none; }
    .lv b { font-size: 10px; }
    .lv svg { width: 42px; height: 42px; }
  }
  .visit .t > span { display: inline-flex; align-items: center; gap: 6px; }
  .ico { width: 18px; height: 18px; color: #8b7bff; flex: none; }
  .dur { background: rgba(124,92,255,.22); color: #cfc6ff; padding: 5px 11px; border-radius: 99px; font-weight: 700; font-size: 14px; white-space: nowrap; }
  .tag { font-size: 12px; margin-top: 3px; }
  .task { display: flex; gap: 10px; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--line); }
  .task:last-child { border-bottom: none; }
  .task .tt { flex: 1; word-break: break-word; }
  .task.done .tt { color: var(--hint); text-decoration: line-through; }
  .empty { color: var(--hint); text-align: center; padding: 10px 0; }

  .motiv { display: flex; align-items: center; gap: 14px; background: linear-gradient(90deg, #2b2160, #1c1c3a); }
  .motiv .cup { font-size: 40px; }
  .motiv b { display: block; font-size: 17px; }

  /* переключатель и команда */
  .seg { display: flex; background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 4px; margin-bottom: 14px; }
  .seg button { flex: 1; background: none; padding: 9px; border-radius: 10px; font-weight: 600; color: var(--hint); }
  .seg button.on { background: var(--card2); color: var(--text); }
  .member { display: flex; gap: 12px; align-items: center; }
  .ava-edit { position: absolute; right: 0; bottom: 0; width: 32px; height: 32px; border-radius: 50%;
              background: var(--accent); border: 3px solid var(--bg); display: flex; align-items: center;
              justify-content: center; font-size: 15px; cursor: pointer; }
  .member .ava { width: 52px; height: 52px; padding: 2px; box-shadow: none; }
  .member .ava > div, .member .ava > img { font-size: 20px; border-width: 2px; }
  .mini-bar { height: 7px; margin-top: 8px; }
  .rank-sm svg { width: 20px; height: 20px; vertical-align: -5px; }
  .week { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; margin-top: 8px; }
  .week div { background: var(--card2); border-radius: 9px; padding: 5px 2px; text-align: center; font-size: 11px; line-height: 1.25; }
  .week div b { display: block; font-size: 11px; color: var(--hint); font-weight: 600; }
  .week div.today { outline: 1.5px solid var(--accent); }
  .week div.off { opacity: .45; }
  .name-btn { background: none; padding: 0; font-weight: 700; text-align: left; color: var(--text); text-decoration: underline dotted rgba(255,255,255,.35); text-underline-offset: 4px; }
  .sheet-bg { position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 20; display: flex; align-items: flex-end; }
  .sheet { background: var(--card); border-top: 1px solid var(--line); border-radius: 22px 22px 0 0; width: 100%;
           max-width: 560px; margin: 0 auto; padding: 18px 16px 24px; max-height: 92vh; overflow-y: auto; }
  .sheet .row-day { display: grid; grid-template-columns: 40px 1fr; align-items: center; gap: 10px; margin-bottom: 8px; }
  .sheet .row-day select { margin: 0; }
  select, input { width: 100%; font: inherit; color: var(--text); background: var(--card2); border: 1px solid var(--line);
                  border-radius: 12px; padding: 12px; margin-bottom: 10px; outline: none; }

  /* инвестиции */
  .inv-tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 12px; }
  .inv-tiles > div { background: var(--card2); border-radius: 14px; padding: 10px 6px; text-align: center; min-width: 0; }
  .inv-tiles b { display: block; font-size: 15px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
  .inv-tiles span { color: var(--hint); font-size: 11.5px; }
  .inv { padding: 11px 0; border-top: 1px solid var(--line); }
  .inv-top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .inv-top b { font-variant-numeric: tabular-nums; }
  .chip { font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 99px; white-space: nowrap; }
  .chip.pending { background: rgba(251,191,36,.16); color: var(--orange); }
  .chip.active { background: rgba(124,92,255,.22); color: #cfc6ff; }
  .chip.matured { background: rgba(52,211,153,.18); color: var(--green); }
  .chip.paid, .chip.rejected { background: var(--card2); color: var(--hint); }
  .inv .mini-bar > div { background: linear-gradient(90deg, var(--green), #10b981); box-shadow: none; }
  .inv-sub { font-size: 12.5px; margin-top: 4px; display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
  .inv-act { display: flex; gap: 8px; margin-top: 8px; }
  .inv-act .btn { flex: 1; }
  .plans { display: grid; grid-template-columns: repeat(auto-fit, minmax(90px, 1fr)); gap: 8px; margin-bottom: 10px; }
  .plans button { background: var(--card2); border: 1.5px solid var(--line); border-radius: 12px; padding: 9px 4px; line-height: 1.3; }
  .plans button b { display: block; font-size: 15px; }
  .plans button span { font-size: 12px; color: var(--hint); }
  .plans button.on { border-color: var(--accent); background: rgba(124,92,255,.18); }
  .inv-calc { background: var(--card2); border-radius: 12px; padding: 10px 12px; margin-bottom: 10px; font-size: 14px; line-height: 1.6; }
  .inv-calc b { color: var(--green); }
  .inv-h { font-size: 13px; color: var(--hint); font-weight: 600; margin: 12px 0 2px; }
  .lvl-row { padding: 10px 0; border-top: 1px solid var(--line); }
  .lvl-row:first-of-type { border-top: none; }
  .lvl-btns { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-top: 8px; }
  .lvl-btns button { background: var(--card2); border: 1.5px solid var(--line); border-radius: 11px; padding: 8px 2px;
                     font-size: 13px; font-weight: 700; line-height: 1.25; }
  .lvl-btns button span { display: block; font-size: 11px; font-weight: 500; color: var(--hint); }
  .lvl-btns button.on { border-color: var(--accent); background: rgba(124,92,255,.2); }
  .mchips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .mchips span { background: var(--card2); border-radius: 9px; padding: 4px 8px; font-size: 12px; white-space: nowrap; }
  .chip.rating { background: rgba(251,191,36,.16); color: var(--orange); }
  details.inv-months summary { font-size: 12.5px; color: #8b7bff; cursor: pointer; margin-top: 6px; }
  details.inv-form summary { list-style: none; cursor: pointer; }
  .emonth summary::-webkit-details-marker { display: none; }
  .emonth[open] summary b:first-child { color: #cfc6ff; }
  .staff-act { padding: 14px 16px; }
  /* проекты руководителя */
  .ptask { display: flex; gap: 12px; align-items: flex-start; padding: 11px 0; border-top: 1px solid var(--line); }
  .ptask:first-child { border-top: none; }
  .pcheck { width: 26px; height: 26px; flex: none; border-radius: 50%; border: 2px solid var(--hint); background: none;
            padding: 0; display: flex; align-items: center; justify-content: center; font-size: 14px; margin-top: 1px; }
  .ptask.done .pcheck { background: var(--green); border-color: var(--green); color: #06281c; }
  .ptask .pbody { flex: 1; min-width: 0; cursor: pointer; }
  .take-today { flex: none; align-self: center; border-color: rgba(167,139,250,.45) !important; color: #e9e3ff; }
  .ptask .pt { word-break: break-word; }
  .ptask.done .pt { color: var(--hint); text-decoration: line-through; }
  .pmeta { font-size: 12.5px; color: var(--hint); margin-top: 3px; display: flex; gap: 6px 10px; flex-wrap: wrap; }
  .pmeta .late { color: var(--red); font-weight: 600; }
  .pmeta .dl { color: #cfc6ff; }
  .prow { display: flex; align-items: center; gap: 12px; padding: 12px 0; border-top: 1px solid var(--line); cursor: pointer; }
  .prow:first-child { border-top: none; }
  .prow .f-ic { width: 40px; height: 40px; font-size: 20px; border-radius: 12px; flex: none; display: flex; align-items: center;
                justify-content: center; background: rgba(251,191,36,.14); }
  .add-row { display: flex; gap: 8px; }
  .add-row input { margin-bottom: 0; }
  .add-row .btn { flex: none; }
  .quick { display: flex; gap: 6px; flex-wrap: wrap; margin: 10px 0 0; }
  .quick button { background: var(--card2); border: 1.5px solid var(--line); border-radius: 99px; padding: 6px 12px; font-size: 13px; }
  .quick button.on { border-color: var(--accent); background: rgba(124,92,255,.2); }
  .day-h { font-size: 13px; font-weight: 700; margin: 12px 0 2px; }
  /* переключатель языка в профиле */
  .lang-switch { display: inline-flex; gap: 4px; margin-top: 10px; background: var(--card2); border: 1px solid var(--line);
                 border-radius: 11px; padding: 3px; }
  .lang-switch button { background: none; border-radius: 8px; padding: 5px 10px; font-size: 12.5px; font-weight: 700;
                        color: var(--hint); white-space: nowrap; }
  .lang-switch button.on { background: var(--accent); color: #fff; }
  /* тексты на трёх языках */
  .ltabs { display: flex; gap: 6px; margin-bottom: 8px; }
  .ltabs button { flex: 1; background: var(--card2); border: 1.5px solid var(--line); border-radius: 11px; padding: 8px 4px;
                  font-size: 13.5px; font-weight: 700; }
  .ltabs button.on { border-color: var(--accent); background: rgba(124,92,255,.2); }
  .ltabs button .ok { color: var(--green); margin-left: 3px; }
  .lang-badges { display: inline-flex; gap: 4px; margin-left: 6px; }
  .lang-badges span { font-size: 10.5px; font-weight: 700; padding: 1px 6px; border-radius: 6px; background: var(--card2); color: var(--hint); }
  /* постинг */
  .po-lbl { font-size: 13px; color: var(--hint); margin: 12px 0 6px; }
  .po-post { padding: 11px 0; border-top: 1px solid var(--line); }
  .po-post:first-child { border-top: none; }
  .po-post .pt { white-space: pre-wrap; word-break: break-word; display: -webkit-box; -webkit-line-clamp: 3;
                 -webkit-box-orient: vertical; overflow: hidden; margin: 4px 0; }
  .chk { display: flex; gap: 10px; align-items: center; margin-top: 12px; font-size: 14px; cursor: pointer; }
  .chk input { width: 20px; height: 20px; margin: 0; flex: none; accent-color: var(--accent); }
  .day-h:first-child { margin-top: 0; }
  .staff-act summary { list-style: none; cursor: pointer; display: flex; justify-content: space-between; align-items: center; }
  .staff-act summary::-webkit-details-marker { display: none; }
  .staff-act summary .hint { font-size: 22px; transition: transform .2s; }
  .staff-act[open] summary .hint { transform: rotate(90deg); }
  .folder { display: flex; align-items: center; gap: 14px; width: 100%; text-align: left; color: var(--text);
            background: linear-gradient(110deg, rgba(52,211,153,.16), rgba(124,92,255,.14) 60%, var(--card));
            border: 1px solid rgba(52,211,153,.25); }
  .folder:active { transform: scale(.985); }
  .folder .f-ic { width: 48px; height: 48px; border-radius: 14px; flex: none; display: flex; align-items: center;
                  justify-content: center; font-size: 26px; background: rgba(52,211,153,.16); }
  .folder b { display: block; font-size: 16.5px; }
  .folder .f-sub { color: var(--hint); font-size: 13px; margin-top: 2px; }
  .folder .f-arrow { margin-left: auto; font-size: 26px; color: var(--hint); flex: none; }
  .f-badge { background: var(--red); color: #fff; font-size: 12px; font-weight: 800; border-radius: 99px;
             padding: 2px 8px; margin-left: 6px; vertical-align: 2px; }
  .page-title { font-size: 22px; font-weight: 800; margin: 4px 0 4px; }
  .page-sub { color: var(--hint); font-size: 13.5px; margin-bottom: 14px; }
  details.inv-form summary::-webkit-details-marker { display: none; }

  .toast { position: fixed; left: 14px; right: 14px; bottom: 18px; background: #2a2d4d; border: 1px solid var(--line);
           padding: 13px 15px; border-radius: 14px; white-space: pre-line; display: none; z-index: 9; box-shadow: 0 10px 30px rgba(0,0,0,.4); }
  /* стрелка свайпа «назад» от левого края */
  #swipe-back { position: fixed; left: 0; top: 50%; width: 40px; height: 40px; border-radius: 50%; z-index: 50;
                display: flex; align-items: center; justify-content: center; font-size: 26px; line-height: 1;
                color: #fff; background: rgba(42,45,77,.92); border: 1px solid rgba(168,140,255,.35);
                opacity: 0; pointer-events: none; transform: translate(-40px, -50%); transition: background .15s; }
  #swipe-back.ready { background: #7c5cff; }
  .app-hide { display: none !important; }

  /* ===== Тема RADI: чёрный + фиолетовое свечение, стеклянные карточки ===== */
  :root { --bg: #06050c; --card: #15112a; --card2: rgba(255,255,255,.06); --line: rgba(168,140,255,.16);
          --text: #f5f3ff; --hint: #a39dc6; --accent: #8b5cf6; --accent2: #6d5dfc;
          --glass: linear-gradient(160deg, rgba(44,33,82,.66), rgba(13,10,26,.82));
          --icon: linear-gradient(150deg, #b9a2ff 0%, #8b5cf6 45%, #5b21b6 100%); }
  body { background: #06050c; background-image: none; }
  body::before { content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
    background: radial-gradient(460px 320px at 105% -4%, rgba(124,58,237,.42), transparent 70%),
                radial-gradient(420px 360px at -10% 104%, rgba(91,33,182,.32), transparent 70%),
                url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 800' preserveAspectRatio='none'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%23a78bfa' stop-opacity='.55'/%3E%3Cstop offset='1' stop-color='%234c1d95' stop-opacity='0'/%3E%3C/linearGradient%3E%3C/defs%3E%3Cpath d='M250 0C300 70 330 120 400 150V0Z' fill='url(%23g)' opacity='.55'/%3E%3Cpath d='M230 0C290 80 320 140 400 175' fill='none' stroke='%23a78bfa' stroke-opacity='.35' stroke-width='1.2'/%3E%3Cpath d='M270 0C315 60 345 100 400 120' fill='none' stroke='%23c4b5fd' stroke-opacity='.25'/%3E%3Cpath d='M400 600C340 660 310 740 230 800H400Z' fill='url(%23g)' opacity='.45'/%3E%3Cpath d='M400 570C330 640 300 730 205 800' fill='none' stroke='%23a78bfa' stroke-opacity='.3' stroke-width='1.2'/%3E%3C/svg%3E"); background-size: auto, auto, 100% 100%; }
  .card, .folder, .soon { background: var(--glass); border: 1px solid var(--line); border-radius: 24px;
    box-shadow: inset 0 1px 0 rgba(255,255,255,.07), 0 12px 30px rgba(0,0,0,.35);
    -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
  .soon { border-color: rgba(168,140,255,.42); opacity: .8; }
  .f-ic, .folder .f-ic, .prow .f-ic { background: var(--icon) !important; border-radius: 16px !important;
    box-shadow: inset 0 1px 0 rgba(255,255,255,.4), inset 0 -6px 12px rgba(46,16,101,.5), 0 6px 18px rgba(124,58,237,.42); }
  .folder .f-arrow { color: #c4b5fd; }
  .btn-main { background: linear-gradient(135deg, #a78bfa, #7c3aed 60%, #6366f1); box-shadow: 0 8px 22px rgba(124,58,237,.45); }
  .btn-soft { background: rgba(255,255,255,.06); border: 1px solid var(--line); }
  select, input, textarea { background: rgba(255,255,255,.045); border-color: var(--line); }
  select:focus, input:focus, textarea:focus { border-color: rgba(167,139,250,.7); }
  .seg { background: rgba(255,255,255,.04); border-color: var(--line); border-radius: 16px; }
  .seg button.on { background: linear-gradient(135deg, #8b5cf6, #6d28d9); color: #fff; box-shadow: 0 4px 14px rgba(124,58,237,.4); }
  .quick button.on, .plans button.on, .lvl-btns button.on, .ltabs button.on { border-color: #a78bfa; background: rgba(139,92,246,.22); }
  .s1, .s2, .s3 { background: var(--glass); }
  .stat { border-radius: 20px; }
  .motiv { background: linear-gradient(110deg, rgba(91,33,182,.55), rgba(20,15,40,.85)); }
  .sheet { background: linear-gradient(180deg, #1b1533, #0c0917); border-top-color: rgba(168,140,255,.3); }
  .toast { background: rgba(32,24,60,.96); border-color: rgba(168,140,255,.3); }
  .inv-tiles > div, .pen-stats > div, .week div { background: rgba(255,255,255,.05); }
  .inv-tiles > div { background: var(--glass); border: 1px solid var(--line); border-radius: 18px; }
  .link { color: #b9a2ff; }

  /* шапка: логотип RADI & CO. по центру */
  .brand-c { flex-direction: column; justify-content: center; gap: 4px; margin: 6px 0 16px; text-align: center; }
  .brand-logo { width: 62%; max-width: 280px; height: auto; display: block; filter: drop-shadow(0 2px 14px rgba(167,139,250,.35)); }
  .brand-c #br-sub { color: var(--hint); font-size: 11.5px; letter-spacing: .28em; text-transform: uppercase; }
  .load-logo { width: 55%; max-width: 240px; opacity: .9; animation: spPulse 1.6s ease-in-out infinite; margin: 18px auto; display: block; }
  #splash { position: fixed; inset: 0; z-index: 100; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 26px;
    background: radial-gradient(420px 320px at 85% 0%, rgba(124,58,237,.35), transparent 70%),
                radial-gradient(380px 320px at 0% 100%, rgba(91,33,182,.28), transparent 70%), #06050c;
    transition: opacity .6s ease, visibility .6s; }
  .bday .bd-top { display: flex; align-items: center; gap: 12px; }
  .bday .bd-top span { display: block; font-size: 13px; color: var(--muted); }
  .bday .bd-top b { display: block; font-size: 22px; font-weight: 800; margin-top: 2px; font-variant-numeric: tabular-nums; }
  .bday #bd-btn { flex: none; padding: 12px 18px; }
  .bd-goal { height: 8px; border-radius: 8px; background: rgba(255,255,255,.07); margin: 14px 0 6px; overflow: hidden; }
  .bd-goal i { display: block; height: 100%; width: 0; border-radius: 8px; background: linear-gradient(90deg, #7c3aed, #c4b5fd); transition: width .6s; }
  .bd-goal-t { font-size: 12.5px; color: var(--muted); }
  .bd-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 12px; }
  .bd-stats div { background: rgba(255,255,255,.04); border: 1px solid var(--line); border-radius: 14px; padding: 10px; text-align: center; }
  .bd-stats b { display: block; font-size: 17px; font-variant-numeric: tabular-nums; }
  .bd-stats span { font-size: 11.5px; color: var(--muted); }
  .bd-hist { margin-top: 12px; }
  .bd-hist summary { cursor: pointer; font-size: 14px; color: var(--muted); list-style: none; }
  .bd-row { display: flex; justify-content: space-between; padding: 9px 2px; border-bottom: 1px solid var(--line); font-size: 14px; font-variant-numeric: tabular-nums; }
  .bd-row:last-child { border-bottom: 0; }
  #rec { position: fixed; inset: 0; z-index: 90; display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 18px; padding: 24px; background: radial-gradient(420px 320px at 50% 30%, rgba(124,58,237,.3), transparent 70%), #06050c; }
  #rec.hidden { display: none; }
  .rec-code { text-align: center; color: var(--muted); font-size: 15px; }
  .rec-code b { display: block; font-size: 56px; color: #fff; font-weight: 800; letter-spacing: 2px; margin-top: 4px; }
  .rec-ring { position: relative; width: min(72vw, 300px); aspect-ratio: 1; }
  .rec-ring svg { position: absolute; inset: -8px; width: calc(100% + 16px); height: calc(100% + 16px); transform: rotate(-90deg); }
  .rec-ring circle { fill: none; stroke: #a78bfa; stroke-width: 2.4; stroke-dasharray: 301.6; stroke-dashoffset: 301.6; stroke-linecap: round; }
  .rec-ring video { width: 100%; height: 100%; object-fit: cover; border-radius: 50%; transform: scaleX(-1); background: #120f22;
    border: 1px solid var(--line); }
  .rec-ph { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 54px; pointer-events: none; }
  .rec-hint { color: var(--muted); font-size: 14px; text-align: center; min-height: 20px; }
  .rec-btn { width: min(80vw, 320px); }
  .rec-x { background: none; border: 0; color: var(--muted); font-size: 15px; padding: 8px; }
  .crm-item { display: flex; gap: 12px; align-items: center; padding: 11px 0; border-bottom: 1px solid var(--line); cursor: pointer; }
  .crm-item:last-child { border-bottom: 0; }
  .crm-ava { width: 46px; height: 46px; border-radius: 50%; flex: none; display: flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 17px; background: linear-gradient(150deg, #a78bfa, #6d28d9); color: #fff; }
  .crm-item .mid { flex: 1; min-width: 0; }
  .crm-ava { position: relative; overflow: hidden; }
  .crm-ava img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  .crm-item .nm { display: flex; justify-content: space-between; gap: 8px; }
  .crm-item .nm b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .crm-item .nm span { color: var(--muted); font-size: 12px; white-space: nowrap; }
  .crm-item .pv { color: var(--muted); font-size: 13.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 2px; }
  .crm-tags { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 4px; }
  .crm-tag { font-size: 11px; padding: 2px 7px; border-radius: 999px; background: rgba(255,255,255,.07); color: var(--muted); }
  .crm-tag.wait { background: rgba(251,191,36,.15); color: #fbbf24; }
  .crm-tag.del { background: rgba(248,113,113,.15); color: var(--red); }
  .crm-tag.win { background: rgba(52,211,153,.12); color: var(--green); }
  body.crm-open { background: radial-gradient(circle at 20% 20%, rgba(124,58,237,.10) 0 2px, transparent 3px) 0 0/34px 34px,
    radial-gradient(circle at 70% 60%, rgba(196,181,253,.07) 0 1.5px, transparent 2.5px) 0 0/26px 26px, var(--bg); }
  body.crm-open .wrap, body.crm-open main { padding-top: 0 !important; }
  .crm-bar { position: sticky; top: 0; z-index: 7; display: flex; align-items: center; gap: 8px; margin: 0 -16px 8px;
    padding: 8px 12px; background: rgba(12,10,24,.86); backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    border-bottom: 1px solid var(--line); }
  .crm-back { background: none; border: 0; color: #c4b5fd; font-size: 34px; line-height: 1; padding: 0 6px 4px; }
  .crm-head { flex: 1; min-width: 0; display: flex; align-items: center; gap: 10px; }
  .crm-head .crm-ava { width: 40px; height: 40px; font-size: 15px; }
  .crm-head b { display: block; font-size: 16px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .crm-head .st { font-size: 12.5px; color: var(--muted); }
  .crm-head .st.on { color: #86efac; }
  .crm-pin { display: flex; gap: 10px; align-items: center; margin: -8px -16px 8px; padding: 8px 16px; font-size: 13px;
    background: rgba(12,10,24,.7); border-bottom: 1px solid var(--line); cursor: pointer; }
  .crm-pin i { width: 3px; align-self: stretch; background: #a78bfa; border-radius: 2px; }
  .crm-pin div { min-width: 0; flex: 1; }
  .crm-pin b { color: #c4b5fd; font-size: 12.5px; display: block; }
  .crm-pin span { color: var(--muted); display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #crm-feed { display: flex; flex-direction: column; gap: 6px; padding-bottom: 130px; }
  .crm-day { align-self: center; font-size: 12px; color: var(--muted); background: rgba(255,255,255,.06); padding: 3px 10px;
    border-radius: 999px; margin: 8px 0 2px; }
  .crm-msg { max-width: 82%; padding: 6px 10px 5px 12px; border-radius: 18px 18px 18px 6px; font-size: 15px; line-height: 1.35; word-wrap: break-word;
    background: rgba(255,255,255,.07); border: 1px solid var(--line); align-self: flex-start; }
  .crm-msg.out { align-self: flex-end; border-radius: 18px 18px 6px 18px; background: linear-gradient(150deg, #7c3aed, #5b21b6);
    border-color: transparent; }
  .crm-msg.out .meta { color: rgba(237,233,254,.75); }
  .crm-msg { background: #1c1830; border-color: transparent; }
  .crm-msg.del { border: 1.5px dashed var(--red) !important; background: rgba(127,29,29,.35) !important; }
  .crm-msg .crm-tx { white-space: pre-wrap; }
  .crm-msg .meta { font-size: 11px; color: var(--muted); text-align: right; margin: 2px 0 0 12px; float: right; white-space: nowrap;
    position: relative; top: 4px; }
  .crm-msg::after { content: ""; display: block; clear: both; }
  .crm-msg .who { font-size: 12px; font-weight: 700; color: #c4b5fd; margin-bottom: 1px; white-space: normal; }
  .crm-msg.out .who { color: #e9d5ff; }
  .crm-msg img, .crm-msg video { max-width: 100%; border-radius: 12px; display: block; margin-bottom: 4px; }
  .crm-msg audio { width: 230px; max-width: 100%; }
  .vp { display: flex; align-items: center; gap: 10px; width: 250px; max-width: 100%; padding: 4px 0 2px; }
  .vp-btn { width: 42px; height: 42px; flex: none; border-radius: 50%; border: 0; font-size: 16px; color: #fff; padding-left: 3px;
    background: linear-gradient(150deg, #a78bfa, #7c3aed); box-shadow: 0 4px 14px rgba(124,58,237,.45); }
  .crm-msg.out .vp-btn { background: #fff; color: #6d28d9; }
  .vp-btn.on { padding-left: 0; }
  .vp-mid { flex: 1; min-width: 0; }
  .vp-bar { -webkit-appearance: none; appearance: none; width: 100%; height: 4px; margin: 8px 0 6px; border-radius: 4px; padding: 0;
    border: 0; background: linear-gradient(90deg, #c4b5fd var(--p, 0%), rgba(255,255,255,.22) var(--p, 0%)); }
  .vp-bar::-webkit-slider-thumb { -webkit-appearance: none; width: 14px; height: 14px; border-radius: 50%; background: #fff;
    box-shadow: 0 1px 4px rgba(0,0,0,.4); }
  .vp-bar::-moz-range-thumb { width: 14px; height: 14px; border-radius: 50%; background: #fff; border: 0; }
  .crm-msg.out .vp-bar { background: linear-gradient(90deg, #fff var(--p, 0%), rgba(255,255,255,.3) var(--p, 0%)); }
  .vp-t { display: flex; justify-content: space-between; font-size: 11.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .crm-msg.out .vp-t { color: rgba(237,233,254,.8); }
  .vp-sp { flex: none; border: 0; border-radius: 10px; padding: 3px 7px; font-size: 12px; font-weight: 700; color: inherit;
    background: rgba(255,255,255,.12); }
  .crm-msg video.circle { width: 220px; height: 220px; border-radius: 50%; object-fit: cover; }
  .crm-msg .old { font-size: 12.5px; color: var(--muted); border-left: 2px solid #fbbf24; padding-left: 6px; margin-top: 4px; white-space: pre-wrap; }
  .crm-foot { position: fixed; left: 0; right: 0; bottom: 0; padding: 10px 16px calc(10px + env(safe-area-inset-bottom));
    background: linear-gradient(180deg, transparent, var(--bg) 30%); z-index: 6; }
  .crm-foot > * { max-width: 600px; margin-left: auto; margin-right: auto; }
  .crm-note { text-align: center; font-size: 13px; color: var(--muted); background: rgba(255,255,255,.06);
    border: 1px solid var(--line); border-radius: 14px; padding: 10px; margin-bottom: 8px; }
  .crm-comp { display: flex; align-items: flex-end; gap: 6px; background: var(--card); border: 1px solid var(--line);
    border-radius: 22px; padding: 6px; }
  .crm-comp.off { opacity: .45; pointer-events: none; }
  .crm-comp textarea { flex: 1; margin: 0; border: 0; background: none; resize: none; max-height: 140px; min-height: 22px;
    padding: 8px 4px; font-size: 16px; line-height: 1.35; font-family: inherit; color: inherit; }
  .crm-ib { width: 38px; height: 38px; flex: none; border-radius: 50%; display: flex; align-items: center; justify-content: center;
    font-size: 19px; background: rgba(255,255,255,.06); border: 0; color: inherit; cursor: pointer; }
  .crm-send { width: 42px; height: 42px; flex: none; border-radius: 50%; border: 0; color: #fff; font-size: 18px;
    background: linear-gradient(150deg, #a78bfa, #6d28d9); }
  .crm-send:disabled { opacity: .5; }
  .crm-recbar { display: flex; align-items: center; gap: 10px; background: var(--card); border: 1px solid var(--line);
    border-radius: 22px; padding: 6px 6px 6px 16px; }
  .crm-recbar .dot { width: 10px; height: 10px; border-radius: 50%; background: #ef4444; animation: recBlink 1s infinite; }
  .crm-recbar b { font-variant-numeric: tabular-nums; }
  @keyframes recBlink { 50% { opacity: .2; } }
  .crm-sug { background: var(--card); border: 1px solid var(--line); border-radius: 16px; margin-bottom: 8px; max-height: 40vh; overflow: auto; }
  .crm-sug div { padding: 10px 14px; border-bottom: 1px solid var(--line); cursor: pointer; font-size: 14px; }
  .crm-sug div:last-child { border-bottom: 0; }
  .crm-sug div b { display: block; font-size: 13px; color: #c4b5fd; }
  .crm-sug div span { color: var(--muted); display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .crm-photo { display: flex; align-items: center; gap: 10px; background: var(--card); border: 1px solid var(--line);
    border-radius: 14px; padding: 6px; margin-bottom: 8px; font-size: 12.5px; color: var(--muted); }
  .crm-photo img { width: 46px; height: 46px; object-fit: cover; border-radius: 10px; }
  .crm-photo span { flex: 1; }
  .crm-photo button { background: none; border: 0; color: inherit; font-size: 18px; }
  #splash.out { opacity: 0; visibility: hidden; }
  #splash img { width: 70%; max-width: 360px; animation: spIn 1.1s cubic-bezier(.2,.8,.2,1) both, spGlow 2.4s ease-in-out 1.1s infinite; }
  .sp-line { width: 120px; height: 2px; border-radius: 2px; background: rgba(255,255,255,.08); overflow: hidden; }
  .sp-line i { display: block; width: 40%; height: 100%; background: linear-gradient(90deg, transparent, #c4b5fd, transparent);
    animation: spBar 1.2s ease-in-out infinite; }
  @keyframes spIn { from { opacity: 0; transform: scale(.94); filter: blur(8px); } to { opacity: 1; transform: none; filter: blur(0); } }
  @keyframes spGlow { 0%, 100% { filter: drop-shadow(0 0 0 rgba(167,139,250,0)); } 50% { filter: drop-shadow(0 0 18px rgba(167,139,250,.55)); } }
  @keyframes spPulse { 0%, 100% { opacity: .45; } 50% { opacity: 1; } }
  @keyframes spBar { from { transform: translateX(-120%); } to { transform: translateX(320%); } }
  /* шапка */
  .brand { display: flex; align-items: center; gap: 14px; margin: 2px 0 16px; }
  .logo-mark { width: 58px; height: 58px; border-radius: 18px; flex: none; display: flex; align-items: center; justify-content: center;
    background: radial-gradient(circle at 35% 25%, #2c2546, #0e0b1a 75%); border: 1px solid rgba(255,255,255,.1);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.12), 0 8px 22px rgba(0,0,0,.5); }
  .logo-mark svg { width: 74%; }
  .brand-sep { width: 1px; align-self: stretch; margin: 6px 0; background: linear-gradient(transparent, rgba(196,181,253,.6), transparent); }
  .brand b { display: block; font-size: 19px; letter-spacing: .06em; font-weight: 700; }
  .brand span { color: var(--hint); font-size: 13.5px; }
  /* заголовок страницы */
  .hero { position: relative; overflow: hidden; display: flex; align-items: center; gap: 16px; padding: 18px; margin-bottom: 14px;
    border-radius: 24px; border: 1px solid rgba(168,140,255,.32);
    background: linear-gradient(115deg, rgba(88,60,170,.6), rgba(28,20,56,.85) 60%, rgba(12,9,24,.9)); }
  .hero::after { content: ""; position: absolute; right: -40px; top: -30px; width: 180px; height: 160px; pointer-events: none;
    background: radial-gradient(closest-side, rgba(167,139,250,.45), transparent); }
  .hero .logo-mark { width: 56px; height: 56px; background: none; border: none; box-shadow: none; }
  .hero .page-title { margin: 0; font-size: 21px; }
  .hero .page-sub { margin: 3px 0 0; }
  /* главная руководителя */
  .welcome { display: flex; align-items: center; gap: 14px; border-color: rgba(168,140,255,.38); }
  .welcome b { display: block; font-size: 18px; }
  .welcome .f-sub { color: var(--hint); font-size: 13.5px; margin-top: 3px; }
  .dash { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 14px; }
  .dtile { display: flex; align-items: center; gap: 10px; padding: 14px 12px; text-align: left; position: relative; margin: 0; min-width: 0; }
  .dtile .f-ic { width: 44px; height: 44px; font-size: 22px; flex: none; display: flex; align-items: center; justify-content: center; }
  .dtile b { display: block; font-size: 21px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .dtile span { color: var(--hint); font-size: 12.5px; display: block; line-height: 1.2; }
  .dtile i { position: absolute; right: 10px; bottom: 6px; font-style: normal; color: #c4b5fd; font-size: 18px; }
  .dtile > div:nth-child(2) { min-width: 0; }
  .dayc { width: 100%; text-align: left; border-color: rgba(168,140,255,.38); }
  .dayc .top { display: flex; align-items: center; gap: 14px; }
  .dayc .f-ic { width: 52px; height: 52px; font-size: 24px; display: flex; align-items: center; justify-content: center; flex: none; }
  .dayc .top span { color: var(--hint); font-size: 14px; }
  .dayc .top b { display: block; font-size: 28px; line-height: 1.1; }
  .day-pill { margin-left: auto; padding: 7px 12px; border-radius: 99px; background: rgba(0,0,0,.35); border: 1px solid var(--line);
    font-size: 13px; white-space: nowrap; color: var(--text) !important; }
  .dayc .bottom { display: flex; align-items: center; gap: 10px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line);
    color: var(--hint); font-size: 14px; }
  .sec-h { font-size: 13px; color: var(--hint); font-weight: 700; letter-spacing: .04em; text-transform: uppercase; margin: 4px 4px 10px; }
  /* постинг: выбор аудитории в боте */
  .po-bot { width: 100%; margin-bottom: 4px; }
  .po-bot-head { width: 100%; display: flex; justify-content: space-between; align-items: center; gap: 8px; text-align: left;
    background: rgba(255,255,255,.05) !important; border: 1.5px solid var(--line) !important; border-radius: 14px !important;
    padding: 12px 14px !important; font-size: 14px !important; font-weight: 700; }
  .po-bot-head.on { border-color: #a78bfa !important; background: rgba(139,92,246,.18) !important; }
  .po-bot-cur { color: #d8ccff; font-weight: 600; font-size: 13px; white-space: nowrap; display: flex; align-items: center; gap: 6px; }
  .po-caret { display: inline-block; transition: transform .2s; color: #c4b5fd; }
  .po-bot-list { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
  .po-bot-list button { display: flex; justify-content: space-between; align-items: center; text-align: left; width: 100%;
    background: rgba(255,255,255,.03) !important; border: 1px solid var(--line) !important; border-radius: 12px !important;
    padding: 11px 14px !important; font-size: 14px !important; }
  .po-bot-list button.on { border-color: #a78bfa !important; background: rgba(139,92,246,.2) !important; }
  /* команда: список сотрудников */
  .emp-row { display: flex; align-items: center; gap: 12px; width: 100%; background: none; padding: 12px 0; border-top: 1px solid var(--line); }
  .emp-row:first-child { border-top: none; }
  .emp-row .ava { width: 50px; height: 50px; padding: 2px; box-shadow: none; flex: none; }
  .emp-row .ava > div, .emp-row .ava > img { font-size: 18px; border-width: 2px; }
  .emp-row b { display: block; font-size: 15.5px; }
  .emp-badges { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
  .emp-badges:empty { display: none; }
  /* ERP: продажи — экраны в стиле кассы */
  .sh-head { display: flex; align-items: center; justify-content: space-between; margin: 2px 2px 14px; }
  .sh-title { font-size: 30px; font-weight: 800; letter-spacing: -.5px; }
  .sh-icon { width: 48px; height: 48px; border-radius: 50%; background: rgba(255,255,255,.07) !important; border: 1px solid var(--line);
    font-size: 20px; display: flex; align-items: center; justify-content: center; }
  .chips { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 4px; margin-bottom: 12px; scrollbar-width: none; }
  .chips::-webkit-scrollbar { display: none; }
  .chips button { flex: none; padding: 9px 14px; border-radius: 99px; background: rgba(255,255,255,.05); border: 1.5px solid var(--line);
    font-weight: 600; font-size: 13.5px; white-space: nowrap; }
  .chips button.on { border-color: #a78bfa; background: rgba(139,92,246,.22); color: #e9e3ff; }
  .big-sum { font-size: 34px; font-weight: 800; color: #b9a2ff; font-variant-numeric: tabular-nums; }
  .big-sum small { font-size: 18px; color: var(--hint); font-weight: 700; }
  .bars { display: flex; align-items: flex-end; gap: 3px; height: 110px; margin-top: 12px; }
  .bars div { flex: 1; background: linear-gradient(180deg, #a78bfa, #6d28d9); border-radius: 4px 4px 0 0; min-height: 2px; opacity: .9; }
  .bars-x { display: flex; justify-content: space-between; font-size: 10.5px; color: var(--hint); margin-top: 4px; }
  .kv { display: flex; justify-content: space-between; align-items: center; padding: 11px 0; border-top: 1px solid var(--line); gap: 10px; }
  .kv:first-of-type { border-top: none; }
  .kv b { color: #b9a2ff; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .dot-c { width: 12px; height: 12px; border-radius: 50%; display: inline-block; margin-right: 8px; vertical-align: -1px; }
  .pad-sum { text-align: center; font-size: 46px; font-weight: 800; padding: 22px 0 6px; font-variant-numeric: tabular-nums; }
  .pad-sum small { font-size: 22px; color: var(--hint); }
  .pad-parts { text-align: center; color: var(--hint); font-size: 13px; min-height: 18px; margin-bottom: 12px; }
  .numpad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .numpad button { height: 68px; border-radius: 20px; background: rgba(255,255,255,.06); border: 1px solid var(--line);
    font-size: 28px; font-weight: 700; }
  .numpad button:active { background: rgba(139,92,246,.3); }
  .numpad .fn { color: var(--hint); font-size: 24px; }
  .row-btn { display: flex; align-items: center; gap: 14px; width: 100%; text-align: left; padding: 18px 16px !important; margin-bottom: 10px; }
  .row-btn .ri { font-size: 22px; width: 28px; text-align: center; flex: none; }
  .row-btn .rt { flex: 1; min-width: 0; }
  .row-btn .rt b { display: block; font-size: 16.5px; }
  .row-btn .rt span { color: #b9a2ff; font-size: 14px; font-weight: 600; }
  .row-btn .ra { color: #b9a2ff; font-weight: 700; font-size: 14px; white-space: nowrap; }
  .bottom-bar { position: sticky; bottom: 88px; display: grid; grid-template-columns: 1fr 1.3fr; gap: 10px; z-index: 5;
    margin: 6px -14px 0; padding: 18px 14px 10px; background: linear-gradient(to top, #06050c 78%, rgba(6,5,12,0)); }
  .bottom-bar .btn-soft { background: #1d1833 !important; }
  .bottom-bar .btn { padding: 17px 10px; border-radius: 20px; font-size: 16px; }
  .pay-tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .pay-tile { border-radius: 20px; padding: 20px 10px 16px !important; text-align: center; background: rgba(255,255,255,.05) !important;
    border: 1.5px solid var(--line) !important; }
  .pay-tile .pi { font-size: 26px; display: block; margin-bottom: 8px; }
  .pay-tile b { font-size: 15.5px; }
  .pay-tile.on { border-color: #a78bfa !important; background: rgba(139,92,246,.24) !important; box-shadow: 0 0 0 3px rgba(139,92,246,.18); }
  .pay-tile input { margin: 10px 0 0; padding: 9px; text-align: center; }
  .to-pay { text-align: center; font-weight: 700; margin: 4px 0 2px; }
  .tx-date { display: flex; justify-content: space-between; font-weight: 800; font-size: 17px; margin: 14px 4px 8px; }
  .tx { padding: 14px 0; border-top: 1px solid var(--line); cursor: pointer; }
  .tx:first-child { border-top: none; }
  .tx-a { color: #b9a2ff; font-weight: 800; }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 99px; background: rgba(255,255,255,.07); font-size: 13px; font-weight: 600; }
  .menu-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .menu-grid button { padding: 22px 10px !important; text-align: center; font-weight: 700; font-size: 15px; }
  .menu-grid button span { display: block; font-size: 26px; margin-bottom: 6px; }
  /* ERP: продажи */
  .sw { position: relative; width: 50px; height: 30px; flex: none; display: inline-block; }
  .sw input { opacity: 0; width: 0; height: 0; position: absolute; }
  .sw span { position: absolute; inset: 0; border-radius: 99px; background: rgba(255,255,255,.14); transition: .2s; cursor: pointer; }
  .sw span::before { content: ""; position: absolute; left: 3px; top: 3px; width: 24px; height: 24px; border-radius: 50%;
    background: #fff; transition: .2s; box-shadow: 0 2px 6px rgba(0,0,0,.4); }
  .sw input:checked + span { background: linear-gradient(135deg, #34d399, #10b981); }
  .sw input:checked + span::before { transform: translateX(20px); }
  .sw-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 0; border-top: 1px solid var(--line); }
  .sw-row:first-child { border-top: none; }
  .f-lbl { font-size: 12.5px; color: var(--hint); margin: 10px 0 5px; }
  .two { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .pay-row { display: grid; grid-template-columns: 1.3fr 1fr auto; gap: 6px; align-items: center; margin-bottom: 6px; }
  .pay-row select, .pay-row input { margin: 0; }
  .cl-card { background: rgba(139,92,246,.14); border: 1px solid rgba(167,139,250,.35); border-radius: 14px; padding: 10px 12px; margin: 8px 0; }
  .tot { background: rgba(255,255,255,.05); border-radius: 12px; padding: 10px 12px; margin-top: 10px; font-size: 13.5px; line-height: 1.7; }
  .sale { padding: 11px 0; border-top: 1px solid var(--line); }
  .sale:first-child { border-top: none; }
  .badge { font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 7px; background: rgba(255,255,255,.08); margin-right: 4px; }
  .st-row { padding: 10px 0; border-top: 1px solid var(--line); }
  .st-row:first-child { border-top: none; }
  .perm-chk { display: flex; flex-wrap: wrap; gap: 6px 12px; font-size: 12.5px; margin-top: 6px; }
  .perm-chk label { display: flex; align-items: center; gap: 5px; }
  .perm-chk input { width: 16px; height: 16px; margin: 0; accent-color: var(--accent); }
  /* мобильное приложение: без выделения и копирования текста */
  html.app body { -webkit-user-select: none; user-select: none; -webkit-touch-callout: none; }
  html.app input, html.app textarea { -webkit-user-select: text; user-select: text; }
  .app-only { display: none; }
  html.app .app-only { display: block; }
  /* нижнее меню */
  .tabbar { position: fixed; left: 12px; right: 12px; bottom: 12px; max-width: 536px; margin: 0 auto; z-index: 8;
    display: grid; grid-template-columns: repeat(4, 1fr); padding: 8px 6px; border-radius: 26px;
    background: rgba(18,14,34,.86); border: 1px solid rgba(168,140,255,.25);
    -webkit-backdrop-filter: blur(18px); backdrop-filter: blur(18px); box-shadow: 0 12px 34px rgba(0,0,0,.55); }
  .tabbar button { background: none; display: flex; flex-direction: column; align-items: center; gap: 4px; padding: 6px 2px;
    font-size: 12px; color: var(--hint); font-weight: 600; }
  .tabbar svg { width: 24px; height: 24px; }
  .tabbar button.on { color: #b9a2ff; }
  .tabbar button.on svg { filter: drop-shadow(0 0 8px rgba(167,139,250,.8)); }
  body.has-tabs .wrap { padding-bottom: 110px; }
  body.has-tabs .toast { bottom: 96px; }
</style>
</head>
<body>
<div id="rec" class="hidden">
  <div class="rec-code"><span id="rec-say">Скажите вслух число</span><b id="rec-code">—</b></div>
  <div class="rec-ring"><svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="48" id="rec-prog"/></svg>
    <video id="rec-v" playsinline muted autoplay></video><div class="rec-ph" id="rec-ph">📹</div></div>
  <div class="rec-hint" id="rec-hint">Включаю камеру…</div>
  <button class="btn btn-main rec-btn" id="rec-btn" disabled>● Записать</button>
  <label class="btn btn-soft rec-btn hidden" id="rec-file"><span id="rec-file-t">📹 Открыть камеру</span><input type="file" accept="video/*" capture="user" hidden id="rec-input"></label>
  <button class="rec-x" id="rec-x">Отмена</button>
</div>
<div id="splash"><img src="/static/radi-logo.png" alt="RADI &amp; CO."><div class="sp-line"><i></i></div></div>
<div class="wrap">
  <div class="brand brand-c hidden" id="brand">
    <img class="brand-logo" src="/static/radi-logo.png" alt="RADI &amp; CO.">
    <span id="br-sub">мини-приложение</span><b id="br-title" class="hidden"></b>
    <div class="logo-mark hidden"><svg viewBox="0 0 64 40"><defs><linearGradient id="lgR" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff"/><stop offset=".55" stop-color="#c9cbe0"/><stop offset="1" stop-color="#7d8199"/></linearGradient></defs><text x="31" y="31" text-anchor="middle" font-family="Didot,Georgia,serif" font-style="italic" font-weight="700" font-size="31" letter-spacing="-4" fill="url(#lgR)">RD</text></svg></div></div>
  <div id="loading" class="card empty"><img class="load-logo" src="/static/radi-logo.png" alt=""></div>
  <div id="error" class="card empty hidden"></div>

  <div id="seg" class="seg hidden">
    <button data-view="me" class="on">Мой кабинет</button>
    <button data-view="team">👥 Команда</button>
  </div>

  <!-- ЛИЧНЫЙ КАБИНЕТ -->
  <div id="view-me" class="hidden">
    <div class="profile">
      <label class="ava">
        <span id="me-ava" style="display:contents"></span>
        <span class="ava-edit">📷</span>
        <input type="file" id="ava-file" accept="image/*" hidden>
      </label>
      <div style="min-width:0">
        <div class="pname" id="p-name"></div>
        <div class="prole" id="p-role"></div>
        <div class="lvl-pill" id="p-lvl"></div>
        <div><div class="lang-switch" id="me-langsw"></div></div>
      </div>
    </div>

    <div class="goal"><span id="g-have"></span> <small>/ <span id="g-goal"></span> <span data-sx="h"></span></small></div>
    <div class="hint" id="g-to"></div>
    <div class="bar-row"><div class="bar"><div id="g-bar" style="width:0"></div></div><div class="pct" id="g-pct"></div></div>

    <div class="stats">
      <div class="stat s1"><div class="ic">📅</div><b id="s-month"></b><span data-sx="per_month"></span></div>
      <div class="stat s2"><div class="ic">🔥</div><b id="s-streak"></b><span data-sx="in_row"></span></div>
      <div class="stat s3"><div class="ic">📊</div><b id="s-level"></b><span data-sx="cur_rank"></span></div>
    </div>

    <button class="card folder" id="go-done" style="cursor:pointer">
      <div class="f-ic">✅</div>
      <div style="min-width:0;flex:1"><b data-sx="done_title"></b><div class="f-sub" data-sx="done_sub"></div></div>
      <div style="display:flex;gap:14px;text-align:center;flex:none">
        <div><b id="done-month" style="font-size:22px;color:var(--green)">0</b><div class="hint" style="font-size:11px" data-sx="done_month"></div></div>
        <div><b id="done-total" style="font-size:22px">0</b><div class="hint" style="font-size:11px" data-sx="done_total"></div></div>
      </div>
    </button>
    <button class="card folder hidden" id="go-posting-me">
      <div class="f-ic">📣</div>
      <div style="min-width:0"><b data-sx="posting_title"></b><div class="f-sub" data-sx="posting_sub"></div></div>
      <div class="f-arrow">›</div>
    </button>
    <button class="card folder hidden" id="go-shop-me">
      <div class="f-ic">🧾</div>
      <div style="min-width:0"><b data-sx="shop_title"></b><div class="f-sub" data-sx="shop_sub"></div></div>
      <div class="f-arrow">›</div>
    </button>
    <button class="card folder" id="go-earn">
      <div class="f-ic" style="background:rgba(52,211,153,.16)">💵</div>
      <div style="min-width:0"><b data-sx="earn_title"></b><div class="f-sub" id="earn-folder-sub"></div></div>
      <div class="f-arrow">›</div>
    </button>
    <button class="card folder" id="go-inv">
      <div class="f-ic">💰</div>
      <div style="min-width:0"><b data-sx="invest">Инвестиции</b><div class="f-sub" id="inv-folder-sub"></div></div>
      <div class="f-arrow">›</div>
    </button>
    <div class="card status">
      <div>
        <div id="st-text"></div>
        <div class="hint" style="font-size:13px;margin-top:2px" id="st-sub"></div>
      </div>
      <button class="btn btn-main" id="btn-mark"></button>
    </div>
    <div class="card" style="padding:12px 14px">
      <div class="hint" style="font-size:13px" data-sx="my_week"></div>
      <div class="week" id="my-week"></div>
    </div>
    <div class="card hidden" id="kpi-card"></div>
    <div class="card" id="pen-card"></div>

    <div class="card">
      <h3 data-sx="ranks"></h3>
      <div class="levels" id="levels"></div>
    </div>

    <div class="card hidden" id="assign-card">
      <div class="head"><h3 id="as-title"></h3></div>
      <div class="quick" id="as-users" style="margin:0 0 10px"></div>
      <div class="add-row"><input id="as-text" maxlength="500" placeholder="…">
        <button class="btn btn-main" id="as-add">＋</button></div>
    </div>
    <div class="card" id="my-tasks-card">
      <div class="head"><h3 data-sx="my_tasks"></h3></div>
      <div class="add-row" style="margin-bottom:6px"><input id="mt-title" maxlength="300" data-sxp="mt_ph">
        <button class="btn btn-main" id="mt-add">＋</button></div>
      <div class="quick" id="mt-day" style="margin:0 0 6px"><button data-day="today" class="on" data-sx="mt_today"></button>
        <button data-day="tomorrow" data-sx="mt_tomorrow"></button><button data-day="pick" data-sx="mt_pick"></button></div>
      <input type="date" id="mt-date" class="hidden" style="margin-bottom:6px">
      <div id="tasks"></div>
    </div>

    <div class="card">
      <div class="head">
        <h3><svg class="ico" style="width:22px;height:22px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg><span data-sx="last_visits"></span></h3>
        <button class="link" id="all-btn" data-sx="all"></button>
      </div>
      <div id="visits"></div>
    </div>

    <div class="card motiv">
      <div class="cup">🏆</div>
      <div><b id="m-title"></b><span class="hint" id="m-sub"></span></div>
    </div>
  </div>

  <!-- ИНВЕСТИЦИИ (отдельная страница сотрудника) -->
  <div id="view-invest" class="hidden">
    <div class="page-title">💰 Мои инвестиции</div>
    <div class="page-sub" id="inv-page-sub"></div>
    <div class="card hidden" id="inv-access">
      <h3>🙋 Заявка на участие</h3>
      <div id="inv-access-body"></div>
      <div id="inv-access-form">
        <input id="ia-plan" inputmode="numeric" autocomplete="off" maxlength="20" placeholder="Планируемая сумма (необязательно)">
        <textarea id="ia-note" rows="3" maxlength="500" placeholder="Комментарий или вопрос руководителю (необязательно)"></textarea>
        <button class="btn btn-main" style="width:100%" id="ia-send">Подать заявку на участие</button>
      </div>
    </div>
    <div class="card" id="inv-main">
      <div id="inv-me"></div>
      <details class="inv-form" id="inv-form">
        <summary class="btn btn-main" style="display:block;text-align:center;margin-top:10px">➕ Инвестировать в товар</summary>
        <div style="margin-top:12px">
          <input id="inv-amount" inputmode="numeric" autocomplete="off" placeholder="Сумма">
          <div class="inv-calc" id="inv-calc"></div>
          <button class="btn btn-main" style="width:100%" id="inv-send">Подать заявку</button>
          <div class="hint" style="font-size:12.5px;margin-top:10px;line-height:1.45" id="inv-rules"></div>
        </div>
      </details>
    </div>
  </div>

  <!-- МОИ ПРОЕКТЫ (личное — только для руководителя) -->
  <div id="view-projects" class="hidden">
    <div class="page-title">📁 Мои проекты</div>
    <div class="page-sub">Личные проекты и задачи — видите только вы</div>
    <!-- «Мой день» показывается на главной — здесь скрыт, чтобы не дублировать -->
    <div class="card hidden" id="proj-day-card"><div class="head"><h3 id="day-title">☀️ Мой день</h3></div><div id="proj-day"></div></div>
    <div class="card">
      <div class="head"><h3>Проекты</h3><span class="hint" style="font-size:13px" id="proj-count"></span></div>
      <div class="add-row"><input id="np-title" placeholder="Название нового проекта" maxlength="100">
        <button class="btn btn-main" id="np-add">＋</button></div>
      <div id="proj-list" style="margin-top:6px"></div>
    </div>
  </div>

  <!-- ЗАРАБОТОК (сотрудник видит свой) -->
  <div id="view-earn" class="hidden">
    <div class="page-title" data-sx="earn_page"></div>
    <div class="page-sub" data-sx="earn_sub"></div>
    <div class="inv-tiles" style="grid-template-columns:1fr 1fr" id="earn-tiles"></div>
    <div class="card"><div id="earn-list"></div></div>
  </div>

  <!-- ЗАРАБОТОК СОТРУДНИКОВ (руководитель вводит вручную) -->
  <div id="view-earn-admin" class="hidden">
    <div class="page-title">💵 Foyda сотрудников</div>
    <div class="page-sub">Сколько прибыли получил каждый за всё время в компании. Пока вводится вручную — нажмите на сотрудника.</div>
    <div class="card"><div id="earn-team"></div></div>
  </div>

  <!-- КАРТОЧКА КЛИЕНТА -->
  <div id="view-ccard" class="hidden"><div id="cc-body"></div></div>

  <!-- CRM: переписки рабочих Telegram-аккаунтов -->
  <div id="view-crm" class="hidden">
    <div class="page-title">💬 Чаты · CRM</div>
    <div class="page-sub">Переписки рабочих Telegram-аккаунтов: удалённые и изменённые сообщения сохраняются.</div>
    <select id="crm-acc" class="hidden" style="margin-bottom:10px"></select>
    <input id="crm-q" placeholder="🔎 Поиск: имя, @username или текст" style="margin-bottom:10px">
    <div class="quick" id="crm-only" style="margin:0 0 12px"><button data-o="" class="on">Все</button><button data-o="waiting">⏳ Ждут ответа</button>
      <button data-o="closing">⏰ Окно закрывается</button><button data-o="deleted">🗑 Удалённые</button></div>
    <div class="card" id="crm-list" style="padding:6px 14px"></div>
  </div>
  <div id="view-crmchat" class="hidden">
    <div class="crm-bar"><button class="crm-back" id="crm-back">‹</button><div id="crm-head" class="crm-head"></div>
      <button class="crm-ib" id="crm-notes" title="Заметки">📝</button><button class="crm-ib" id="crm-task" title="Задача">📌</button></div>
    <div class="crm-pin hidden" id="crm-pin"></div>
    <div id="crm-feed"></div>
    <div class="crm-foot" id="crm-foot">
      <div class="crm-sug hidden" id="crm-sug"></div>
      <div class="crm-photo hidden" id="crm-photo"><img id="crm-photo-img"><span>Фото будет отправлено с текстом как подпись</span><button id="crm-photo-x">✕</button></div>
      <div class="crm-note hidden" id="crm-note"></div>
      <div class="crm-comp" id="crm-comp">
        <button class="crm-ib" id="crm-tpl" title="Быстрые ответы">⚡</button>
        <label class="crm-ib" title="Фото">📷<input type="file" accept="image/*" id="crm-file" hidden></label>
        <button class="crm-ib" id="crm-circle" title="Кружок">⭕</button>
        <textarea id="crm-input" rows="1" placeholder="Сообщение"></textarea>
        <button class="crm-send" id="crm-send">🎤</button></div>
      <div class="crm-recbar hidden" id="crm-recbar"><span class="dot"></span><b id="crm-rect">0:00</b>
        <span class="hint" style="flex:1;font-size:13px">Запись голосового…</span>
        <button class="btn btn-soft btn-sm" id="crm-recx">✕</button><button class="crm-send" id="crm-recok">➤</button></div>
    </div>
  </div>

  <!-- КАРТОЧКА СОТРУДНИКА -->
  <div id="view-emp" class="hidden"><div id="emp-body"></div></div>

  <!-- ERP: ПРОДАЖИ -->
  <div id="view-shop" class="hidden">
    <div class="sh-head"><div><div class="sh-title" id="sh-title">Главная</div><div class="hint" style="font-size:12.5px" id="shop-sub"></div></div>
      <button class="sh-icon" id="sh-head-btn" style="visibility:hidden"></button></div>
    <div id="shop-body"></div>
  </div>

  <!-- ПОСТИНГ -->
  <div id="view-appsec" class="hidden">
    <div class="page-title">📲 Разделы в приложении</div>
    <div class="page-sub">Что видно в приложении RADI Team (Android и iPhone). В Telegram-боте видно всё — редкие разделы можно оставить только там.</div>
    <div class="card"><div id="as-list"></div>
      <div style="display:flex;align-items:center;gap:10px;padding:12px 0 2px;border-top:1px solid var(--line);margin-top:6px">
        <span style="flex:1">👑 Скрывать и у руководителя<div class="hint" style="font-size:12px">выключено — вы видите в приложении всё</div></span>
        <label class="sw"><input type="checkbox" id="as-admin"><span></span></label></div>
    </div>
    <button class="btn btn-main" id="as-save" style="width:100%">💾 Сохранить</button>
  </div>

  <div id="view-msgr" class="hidden">
    <div class="page-title">📱 RADI Chat</div>
    <div class="page-sub">Telegram рабочего аккаунта на телефонах менеджеров</div>
    <div class="card"><b>Рабочие номера</b>
      <div class="hint" style="margin:6px 0 8px">Воронки компании (рабочие номера, SIM). Подключите номер один раз — сотрудники входят по ключу. ✏️ — задать название воронки (напр. «Воронка 1 — Новые»): оно видно сотруднику в профиле приложения.</div>
      <div id="ms-nums"></div>
      <div id="ms-num-add" style="margin-top:8px">
        <input id="ms-num-phone" placeholder="+998 90 123 45 67" inputmode="tel" style="margin-bottom:8px">
        <button class="btn btn-soft" id="ms-num-go" style="width:100%">➕ Подключить номер</button></div>
      <div id="ms-num-step" class="hidden" style="margin-top:8px">
        <div class="hint" id="ms-num-hint" style="margin-bottom:6px"></div>
        <input id="ms-num-val" style="margin-bottom:8px">
        <button class="btn btn-main" id="ms-num-next" style="width:100%">Дальше</button></div>
      <div style="border-top:1px solid var(--line);margin-top:12px;padding-top:12px">
        <b style="font-size:14px">🔒 Код входа по номеру</b>
        <div class="hint" style="font-size:12px;margin:4px 0 8px">В приложении перед входом по номеру спросит этот код. Сотрудники входят по ключу и код не видят. Пусто — вход по номеру без кода.</div>
        <div style="display:flex;gap:8px"><input id="ms-owner-code" placeholder="Придумайте код" style="flex:1">
          <button class="btn btn-soft" id="ms-owner-save" style="padding:10px 14px">Сохранить</button></div>
        <div class="hint" id="ms-owner-state" style="font-size:12px;margin-top:6px"></div>
      </div>
    </div>
    <div class="card">
      <b>Менеджер: вход по логину + воронки</b>
      <div class="hint" style="margin:6px 0 10px">Создайте менеджеру логин и пароль — он войдёт в приложение (без ключа) и получит свои 3 воронки. Новые клиенты попадают в Воронку 1; менеджер переключается между воронками и перемещает в них клиентов.</div>
      <select id="ml-staff" style="margin-bottom:8px;width:100%"><option value="">— Выберите сотрудника —</option></select>
      <select id="ml-number" style="margin-bottom:8px;width:100%"></select>
      <input id="ml-login" placeholder="Логин (латиница, напр. manager_aziz)" autocomplete="off" autocapitalize="none" style="margin-bottom:8px">
      <input id="ml-pass" type="text" placeholder="Пароль (пусто — оставить прежний)" autocomplete="off" style="margin-bottom:10px">
      <div class="hint" style="font-size:12px;margin:0 0 6px">Названия 3 воронок этого менеджера:</div>
      <input id="ml-f1" placeholder="Воронка 1 — Новые" style="margin-bottom:6px">
      <input id="ml-f2" placeholder="Воронка 2 — Повторные" style="margin-bottom:6px">
      <input id="ml-f3" placeholder="Воронка 3 — Постоянные" style="margin-bottom:10px">
      <button class="btn btn-main" id="ml-save" style="width:100%">💾 Сохранить менеджера</button>
      <div id="ml-list" style="margin-top:12px"></div>
    </div>
    <div class="card">
      <b>Ключ доступа сотрудника <span class="hint" style="font-size:11px;font-weight:500">(старый способ, разовый вход)</span></b>
      <div class="hint" style="margin:6px 0 10px">Выберите сотрудника и получите ключ (8 цифр, 10 минут, один раз). Сотрудник вводит его в RADI Chat и сразу входит в рабочий аккаунт — номер, код и облачный пароль ему не нужны. Одному сотруднику можно выдать ключи на несколько воронок.</div>
      <select id="ms-staff" style="margin-bottom:8px;width:100%"><option value="">— Чей телефон? Выберите сотрудника —</option></select>
      <select id="ms-number" class="hidden" style="margin-bottom:8px;width:100%"></select>
      <button class="btn btn-main" id="ms-code-btn" style="width:100%">🔑 Получить ключ</button>
      <div id="ms-code" class="hidden" style="text-align:center;margin-top:12px"><b id="ms-code-v" style="font-size:34px;letter-spacing:6px"></b>
        <div class="hint" id="ms-code-h"></div>
        <button class="btn btn-soft" id="ms-code-send" style="width:100%;margin-top:10px">📩 Отправить сотруднику в бот</button></div>
    </div>
    <div class="card"><b>Активные сессии</b>
      <div class="hint" style="margin:6px 0 8px">Менеджеры в приложении сейчас. Внутри — их воронки (рабочий номер + ключ). «Выкинуть» завершает сессию: сотрудник выходит из аккаунта.</div>
      <div id="ms-sessions"></div></div>
    <div class="card"><b>Все телефоны</b><div class="hint" style="font-size:12px;margin:4px 0 8px">включая отключённые</div><div id="ms-devs" style="margin-top:4px"></div></div>
    <div class="card"><b>Доступы для всех (по умолчанию)</b>
      <div class="hint" style="margin:6px 0 4px">Удалять сообщения и чаты, выходить из аккаунта, менять номер и отключать бизнес-бота нельзя никогда.</div>
      <div id="ms-perms"></div>
      <div class="card row-btn" style="cursor:default;margin:8px 0 0;padding-left:0;padding-right:0;background:none;border:none">
        <span class="ri">🟢</span><span class="rt"><b>Постоянное подключение</b><span style="color:var(--hint);font-weight:500">всегда онлайн — ловит сообщения до удаления</span></span>
        <label class="sw"><input type="checkbox" id="ms-keep"><span></span></label></div>
    </div>
    <div class="card"><b>Назначение чатов</b>
      <div class="hint" style="margin:6px 0 8px">Как новый клиент закрепляется за менеджером. Телефон должен быть закреплён за сотрудником.</div>
      <select id="ms-mode" style="width:100%;margin-bottom:8px"></select>
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0">
        <span style="flex:1">👀 Свободные чаты видны всем<div class="hint" style="font-size:12px">выключено — свободных видит только руководитель</div></span>
        <label class="sw"><input type="checkbox" id="ms-unass"><span></span></label></div>
      <div style="display:flex;align-items:center;gap:8px;padding:6px 0;flex-wrap:wrap">
        <span style="flex:1;min-width:150px">⏱ Нет ответа клиенту, мин<div class="hint" style="font-size:12px">0 — не следить</div></span>
        <input id="ms-tmin" type="number" min="0" max="1440" style="width:80px">
        <select id="ms-tact" style="width:100%"></select></div>
      <div class="hint" style="font-size:12px">Группы и каналы по умолчанию не видит никто — назначьте их ниже.</div>
    </div>
    <div class="card"><b>Сотрудники</b>
      <div class="hint" style="margin:6px 0 4px">Свои доступы для каждого; без своих — как «для всех» выше. «Чужие чаты» — например, стажёрам для обучения.</div>
      <div id="ms-staff-list"></div></div>
    <div class="card"><b>Чаты</b>
      <input id="ms-q" placeholder="Поиск клиента или группы" style="margin:8px 0">
      <div id="ms-chats"></div></div>
    <div class="card"><b>Белый список</b>
      <div class="hint" style="margin:6px 0 8px">Номера, @юзернеймы и ссылки компании, которые менеджерам можно отправлять клиентам. По одному в строке.</div>
      <textarea id="ms-wl" rows="5" placeholder="+998 71 200 00 00&#10;@radi_shop&#10;t.me/xuping_uz"></textarea>
    </div>
    <button class="btn btn-main" id="ms-save" style="width:100%;margin-bottom:12px">💾 Сохранить настройки, доступы и белый список</button>
    <div class="card"><b>Журнал нарушений</b><div class="hint" style="margin:4px 0 8px">Попытки удалить, отправить номер или ссылку (за 90 дней)</div><div id="ms-viol"></div></div>
  </div>

  <div id="view-posting" class="hidden">
    <div class="page-title">📣 Постинг</div>
    <div class="page-sub">Один пост — всем в боте XUPING Bonus, а также в группы и каналы</div>
    <div class="card">
      <div id="po-langs"></div>
      <div class="hint" style="font-size:12px;display:flex;justify-content:space-between;margin:-6px 0 8px">
        <span id="po-lang-hint"></span><span id="po-count"></span></div>
      <label class="btn btn-soft" style="display:block;text-align:center" id="po-photo-btn">📷 Добавить фото
        <input type="file" id="po-file" accept="image/*" hidden></label>
      <div id="po-photo-box" class="hidden" style="position:relative;margin-top:10px">
        <img id="po-prev" style="width:100%;max-height:300px;object-fit:contain;border-radius:12px;background:var(--card2)">
        <button class="btn btn-soft btn-sm" id="po-photo-del" style="position:absolute;top:8px;right:8px">✕ Убрать</button>
      </div>
      <details class="staff-act" style="padding:0;margin-top:12px">
        <summary><span style="font-size:14px">🔗 Кнопка-ссылка под постом</span><span class="hint">›</span></summary>
        <div style="margin-top:10px">
          <input id="po-btn-text" maxlength="60" placeholder="Текст кнопки, например: Заказать">
          <input id="po-btn-url" maxlength="500" placeholder="Ссылка: https://t.me/xupingSilver" inputmode="url">
          <button class="btn btn-soft btn-sm" style="width:100%" id="po-btn-channel">📢 Кнопка «Kanal» — личная ссылка в канал</button>
        </div>
      </details>
      <div class="po-lbl">Где опубликовать <span style="font-size:11.5px">(можно несколько)</span></div>
      <div class="quick" id="po-aud" style="margin:0"></div>
      <div class="hint" style="font-size:12px;margin-top:8px">💡 Напишите в тексте <b>{name}</b> — каждый клиент увидит своё имя,
        <b>{bonus}</b> — сумму начисленного бонуса.</div>
      <div id="po-bonus-row" class="hidden" style="margin-top:12px">
        <div class="po-lbl" style="margin-top:0">🎁 Начислить бонус каждому получателю (необязательно)</div>
        <input id="po-bonus" inputmode="numeric" autocomplete="off" placeholder="Сумма бонуса, например 50 000" style="margin-bottom:0">
      </div>
      <label class="chk"><input type="checkbox" id="po-news">📰 Показать и в «Новостях» в панели клиентов</label>
      <div class="po-lbl">Когда</div>
      <div class="quick" id="po-when" style="margin:0"><button data-w="now" class="on">Сейчас</button><button data-w="later">🕒 Запланировать</button></div>
      <input type="datetime-local" id="po-at" class="hidden" style="margin-top:10px">
      <button class="btn btn-main" style="width:100%;margin-top:14px" id="po-send">📣 Опубликовать</button>
    </div>
    <div class="card"><h3>История постов</h3><div id="po-history"></div></div>
  </div>

  <!-- ОДИН ПРОЕКТ -->
  <div id="view-project" class="hidden">
    <div style="display:flex;align-items:flex-start;gap:10px">
      <div class="page-title" id="pj-title" style="flex:1;word-break:break-word"></div>
      <button class="btn btn-soft btn-sm" id="pj-rename" style="margin-top:4px">✏️</button>
    </div>
    <div class="page-sub" id="pj-sub"></div>
    <div class="card">
      <input id="nt-title" placeholder="Новая задача" maxlength="300">
      <div class="quick" id="nt-quick">
        <button data-q="">📝 Без даты</button><button data-q="0">Сегодня</button><button data-q="1">Завтра</button>
        <button data-q="pick">📅 Дата…</button>
      </div>
      <input type="date" id="nt-date" class="hidden" style="margin-top:10px">
      <button class="btn btn-main" style="width:100%;margin-top:10px" id="nt-add">Добавить задачу</button>
    </div>
    <div class="card" id="pj-tasks"></div>
    <button class="link" id="pj-delete" style="color:var(--red);display:block;margin:4px auto 0">Удалить проект</button>
  </div>

  <!-- КЛИЕНТЫ (отдельная страница руководителя: список клиентов и новости) -->
  <div id="view-clients" class="hidden">
    <div class="page-title">💎 Клиенты</div>
    <div class="page-sub">Список клиентов, статусы и новости для клиентов</div>
    <div class="card">
      <div class="head"><h3>💎 Клиенты</h3><span class="hint" style="font-size:13px" id="cl-total"></span></div>
      <div class="hint hidden" style="font-size:12.5px;margin:-2px 0 10px;display:flex;gap:8px;align-items:center" id="bz-status"></div>
      <details class="staff-act" style="padding:0;margin:0 0 10px">
        <summary><span style="font-size:14px">📥 Загрузить прежних подписчиков бота</span><span class="hint">›</span></summary>
        <div style="margin-top:10px">
          <div class="hint" style="font-size:12.5px;margin-bottom:8px">Файл .txt или .csv из прежней системы, или вставьте список:
            в каждой строке Telegram ID (можно с номером и именем). Повторы и уже известные пропускаются.</div>
          <label class="btn btn-soft" style="display:block;text-align:center;margin-bottom:8px">📎 Выбрать файл
            <input type="file" id="imp-file" accept=".txt,.csv,.xlsx,text/plain,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" hidden></label>
          <textarea id="imp-text" rows="4" placeholder="6834141071&#10;745848319, +998901234567, Aziza"></textarea>
          <button class="btn btn-main" style="width:100%" id="imp-go">Загрузить</button>
        </div>
      </details>
      <button class="btn btn-soft" style="width:100%;margin:0 0 10px" id="exp-go">📤 Выгрузить ID клиентов бота (файл)</button>
      <input id="cl-search" placeholder="Поиск: имя или номер">
      <div id="cl-list"></div>
    </div>
    <div class="card">
      <div class="head"><h3>📰 Новости для клиентов</h3><span class="hint" style="font-size:13px" id="cl-count"></span></div>
      <div id="n-langs"></div>
      <button class="btn btn-main" style="width:100%" id="n-add">Опубликовать</button>
      <div id="n-list" style="margin-top:8px"></div>
    </div>
  </div>

  <!-- ИНВЕСТИЦИИ (отдельная страница руководителя) -->
  <div id="view-invest-admin" class="hidden">
    <div class="page-title">💰 Инвестиции сотрудников</div>
    <div class="page-sub">Заявки, итоги продаж за месяц и выплаты</div>
    <div class="card"><div id="inv-admin"></div></div>
  </div>

  <!-- ПАНЕЛЬ КЛИЕНТА -->
  <div id="view-client" class="hidden">
    <div class="profile">
      <label class="ava tier-ring" id="cl-ring">
        <span id="cl-ava" style="display:contents"></span>
        <span class="tier-badge" id="cl-badge"></span>
        <span class="ava-edit cl-edit">📷</span>
        <input type="file" id="cl-file" accept="image/*" hidden>
      </label>
      <div style="min-width:0">
        <div class="hint hidden" style="font-size:14px;margin-bottom:2px" id="cl-hello"></div>
        <div class="pname" id="cl-name"></div>
        <div class="prole" id="cl-phone"></div>
        <div class="hint hidden" style="font-size:13px;margin:-4px 0 8px" id="cl-birth"></div>
        <div class="tier-pill" id="cl-tier"></div>
        <div><div class="lang-switch" id="cl-langsw"></div></div>
      </div>
    </div>
    <div class="card hidden" id="cl-nophone" style="border-color:rgba(251,191,36,.4)" data-t="finish_reg"></div>
    <button class="silver-up hidden" id="cl-silver">
      <span class="su-shine"></span>
      <div class="su-top"><div class="su-medal">🥈</div>
        <div style="flex:1;min-width:0"><div class="su-kicker" data-t="su_kicker"></div><b class="su-title" data-t="su_title"></b></div></div>
      <div class="su-perks"><span data-t="su_p1"></span><span data-t="su_p2"></span><span data-t="su_p3"></span></div>
      <div class="su-cta" data-t="su_cta"></div></button>
    <div class="stats" style="grid-template-columns:1fr 1fr">
      <div class="stat s1 soon-stat" id="cl-st-buy"><div class="ic">🛍</div><b id="cl-buy">—</b><span data-t="purchases"></span><em data-t="soon"></em></div>
      <div class="stat s3 soon-stat" id="cl-st-bonus"><div class="ic">🎁</div><b id="cl-bonus">—</b><div class="hint hidden" style="font-size:11.5px" id="cl-frozen"></div><span data-t="bonuses"></span><em data-t="soon"></em></div>
    </div>
    <div class="card hidden" id="cl-debt" style="border-color:rgba(248,113,113,.45);cursor:pointer">
      <div style="display:flex;align-items:center;gap:14px">
        <div class="f-ic" style="width:48px;height:48px;font-size:24px;display:flex;align-items:center;justify-content:center;flex:none;background:linear-gradient(150deg,#fca5a5,#ef4444 55%,#7f1d1d)!important">💳</div>
        <div style="flex:1"><div class="hint" style="font-size:13px" data-t="debt"></div><b id="cl-debt-sum" style="font-size:20px;color:var(--red)"></b></div>
        <div class="f-arrow" id="cl-debt-arrow" style="font-size:26px;color:#c4b5fd;transition:transform .2s">›</div></div>
      <div id="cl-debt-list" class="hidden" style="margin-top:12px"></div></div>
    <button class="card hidden" id="cl-channel" style="width:100%;text-align:left;display:flex;align-items:center;gap:14px">
      <div class="f-ic" style="width:48px;height:48px;font-size:24px;display:flex;align-items:center;justify-content:center;flex:none">📢</div>
      <div style="flex:1;min-width:0"><b id="cl-channel-t" style="font-size:17px"></b><div class="hint" style="font-size:13px" id="cl-channel-s"></div></div>
      <div class="f-arrow" style="font-size:26px;color:#c4b5fd">›</div></button>
    <div class="hint hidden" style="font-size:12.5px;text-align:center;margin:-6px 0 12px" id="cl-billz-note"></div>
    <div style="text-align:center;margin:-6px 0 12px" class="hidden" id="cl-refresh-row">
      <button class="link" id="cl-refresh" style="font-size:13px"></button></div>
    <div style="display:none">
    </div>
    <!-- «Инвестиции» клиентам не показываем -->
    <div class="card"><h3 data-t="news"></h3><div id="cl-news"></div></div>
  </div>

  <!-- ВСЯ ИСТОРИЯ -->
  <div id="view-history" class="hidden">
    <div class="card"><h3 data-sx="all_visits">Все посещения</h3><div id="history-all"></div></div>
  </div>

  <!-- КОМАНДА -->
  <div id="view-team" class="hidden">
    <div class="card welcome"><div class="f-ic" style="width:56px;height:56px;font-size:26px;display:flex;align-items:center;justify-content:center;flex:none">👥</div>
      <div style="flex:1;min-width:0"><b id="wl-title">Добро пожаловать!</b><div class="f-sub" id="wl-sub">Ваш личный кабинет команды RADI</div></div>
      <button class="sh-icon" id="wl-edit" title="Изменить надписи" style="width:40px;height:40px;font-size:16px;flex:none">✏️</button></div>
    <div class="dash">
      <button class="card dtile" data-dgo="projects"><div class="f-ic">📁</div><div><b id="d-proj">—</b><span>Мои проекты</span></div><i>›</i></button>
      <button class="card dtile" data-dgo="staff"><div class="f-ic">👥</div><div><b id="d-team">—</b><span>Команда</span></div><i>›</i></button>
      <button class="card dtile" data-dgo="earn-admin"><div class="f-ic">📈</div><div><b id="d-earn" style="font-size:17px">—</b><span>Foyda сотрудников</span></div><i>›</i></button>
      <button class="card dtile" data-dgo="clients"><div class="f-ic">💎</div><div><b id="d-cl">—</b><span>Клиенты</span></div><i>›</i></button>
      <button class="card dtile" id="d-shop-tile"><div class="f-ic">🧾</div><div><b style="font-size:16px">Продажи</b><span>ERP · касса</span></div><i>›</i></button>
      <button class="card dtile" data-dgo="posting"><div class="f-ic">📣</div><div><b style="font-size:16px">Постинг</b><span id="d-post">посты</span></div><i>›</i></button>
      <button class="card dtile" data-dgo="crm" style="grid-column:1/-1"><div class="f-ic">💬</div><div><b style="font-size:16px">Чаты · CRM</b><span id="d-crm">переписки рабочих аккаунтов</span></div><i>›</i></button>
      <button class="card dtile" data-dgo="msgr" style="grid-column:1/-1"><div class="f-ic">📱</div><div><b style="font-size:16px">RADI Chat</b><span>телефоны, доступы, белый список</span></div><i>›</i></button>
      <button class="card dtile" id="go-appsec" style="grid-column:1/-1"><div class="f-ic">📲</div><div><b style="font-size:16px">Разделы в приложении</b><span>что видно сотрудникам в RADI Team</span></div><i>›</i></button>
    </div>
    <div class="card bday hidden" id="bday">
      <div class="bd-top"><div class="f-ic">🕘</div><div style="flex:1;min-width:0"><span id="bd-lbl">Мой рабочий день</span>
        <b id="bd-state">—</b></div><button class="btn btn-main" id="bd-btn">Пришёл</button></div>
      <div class="bd-goal"><i id="bd-bar"></i></div><div class="bd-goal-t" id="bd-goal-t"></div>
      <div class="bd-stats"><div><b id="bd-week">0</b><span>неделя</span></div><div><b id="bd-month">0</b><span>месяц</span></div>
        <div><b id="bd-avg">0</b><span>в среднем</span></div></div>
      <details class="bd-hist"><summary>История смен</summary><div id="bd-list"></div></details>
    </div>
    <div class="card dayc" style="cursor:default">
      <div class="top"><div class="f-ic">📅</div><div><span>Мои задачи на сегодня</span><b id="d-date"></b></div>
        <button class="day-pill" data-dgo="projects">📁 Все проекты ›</button></div>
      <div id="d-tasks" style="margin-top:10px"></div>
      <div class="add-row" id="d-add-row" style="margin-top:10px"><input id="d-add" placeholder="Быстрая задача на сегодня…" maxlength="300" style="margin:0">
        <button class="btn btn-main" id="d-add-btn">＋</button></div>
      <select id="d-add-proj" style="margin:8px 0 0"></select>
    </div>
    <div class="hidden" id="old-sections">
    <button class="card folder" id="go-shop">
      <div class="f-ic">🧾</div>
      <div style="min-width:0"><b>Продажи (ERP)</b><div class="f-sub">Новая продажа, баллы, бонусы, настройки</div></div>
      <div class="f-arrow">›</div>
    </button>
    <button class="card folder" id="go-projects">
      <div class="f-ic" style="background:rgba(251,191,36,.16)">📁</div>
      <div style="min-width:0"><b>Мои проекты<span id="proj-badge"></span></b><div class="f-sub" id="proj-folder-sub"></div></div>
      <div class="f-arrow">›</div>
    </button>
    <button class="card folder" id="go-staff">
      <div class="f-ic" style="background:rgba(124,92,255,.2)">👥</div>
      <div style="min-width:0"><b>Команда<span id="staff-badge"></span></b><div class="f-sub" id="staff-folder-sub"></div></div>
      <div class="f-arrow">›</div>
    </button>
    <button class="card folder" id="go-earn-admin">
      <div class="f-ic" style="background:rgba(52,211,153,.16)">💵</div>
      <div style="min-width:0"><b>Foyda сотрудников</b><div class="f-sub" id="earn-admin-sub"></div></div>
      <div class="f-arrow">›</div>
    </button>
    <button class="card folder" id="go-posting">
      <div class="f-ic" style="background:rgba(52,211,153,.16)">📣</div>
      <div style="min-width:0"><b>Постинг</b><div class="f-sub" id="posting-folder-sub"></div></div>
      <div class="f-arrow">›</div>
    </button>
    <button class="card folder" id="go-clients">
      <div class="f-ic" style="background:rgba(91,140,255,.18)">💎</div>
      <div style="min-width:0"><b>Клиенты</b><div class="f-sub" id="clients-folder-sub"></div></div>
      <div class="f-arrow">›</div>
    </button>
    <button class="card folder hidden" id="go-inv-own">
      <div class="f-ic">💼</div>
      <div style="min-width:0"><b>Мои инвестиции</b><div class="f-sub" id="inv-folder-sub-own"></div></div>
      <div class="f-arrow">›</div>
    </button>
    <button class="card folder" id="go-inv-admin">
      <div class="f-ic">💰</div>
      <div style="min-width:0"><b>Инвестиции сотрудников<span id="inv-admin-badge"></span></b>
        <div class="f-sub" id="inv-admin-folder-sub"></div></div>
      <div class="f-arrow">›</div>
    </button>
  </div></div>

  <!-- КОМАНДА: сотрудники, задачи и штрафы (отдельная страница руководителя) -->
  <div id="view-staff" class="hidden">
    <div class="page-title">👥 Команда</div>
    <div class="page-sub" id="staff-page-sub"></div>
    <div class="staff-actions">
      <button class="card" id="go-followup" style="width:100%;text-align:left;font-size:14.5px">📞 Обратная связь после покупки
        <div class="hint" style="font-size:12.5px;margin-top:2px" id="fu-sub">На следующий день — задача продавцу связаться с клиентом</div></button>
      <button class="card" id="go-sellers" style="width:100%;text-align:left;font-size:14.5px">🔗 Продавцы BILLZ ↔ сотрудники
        <div class="hint" style="font-size:12.5px;margin-top:2px">Махира в BILLZ = Moxira в команде — для отчётов и задач по клиентам</div></button>
      <button class="card" id="cancel-late" style="width:100%;text-align:left;font-size:14.5px">🚫 Отменить сегодняшние штрафы за опоздание
        <div class="hint" style="font-size:12.5px;margin-top:2px">Если бот не работал — отменит выписанные и не даст выписать новые до конца дня</div></button>
      <details class="card staff-act">
        <summary><b>📌 Новая задача</b><span class="hint">›</span></summary>
        <div style="margin-top:12px">
          <div class="hint" style="font-size:13px;margin-bottom:6px">Кому (можно несколько):</div>
          <div class="quick" id="t-users" style="margin:0 0 10px"></div>
          <input id="t-title" placeholder="Что нужно сделать?" maxlength="500">
          <div class="quick" id="t-repeat" style="margin:0 0 10px">
            <button data-rep="once" class="on">Один раз</button><button data-rep="daily">🔁 Каждый день</button>
            <button data-rep="weekly">🔁 Каждую неделю</button></div>
          <select id="t-wd" class="hidden">
            <option value="0">по понедельникам</option><option value="1">по вторникам</option><option value="2">по средам</option>
            <option value="3">по четвергам</option><option value="4">по пятницам</option><option value="5">по субботам</option>
            <option value="6">по воскресеньям</option></select>
          <div class="hint hidden" style="font-size:12.5px;margin:-4px 0 10px" id="t-rep-hint"></div>
          <button class="btn btn-main" style="width:100%" id="t-add">Поставить задачу</button>
        </div>
      </details>
      <details class="card staff-act">
        <summary><b>🔴 Выписать штраф</b><span class="hint">›</span></summary>
        <div style="margin-top:12px">
          <select id="f-user"></select>
          <input id="f-amount" placeholder="Сумма, например 50 000 сум (можно пусто)" maxlength="50">
          <input id="f-reason" placeholder="Причина" maxlength="300">
          <button class="btn btn-danger" style="width:100%" id="f-add">Выписать штраф</button>
        </div>
      </details>
    </div>
    <div id="team"></div>
  </div>
</div>
<div class="app-only" style="text-align:center;padding:4px 0 130px"><button class="link" style="color:var(--hint);font-size:13px" onclick="appLogout()">🚪 Ilovadan chiqish / Выйти из приложения</button></div>
<nav class="tabbar hidden" id="tabbar">
  <button data-tab="team"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3 2.5 11h2.7v9h5.3v-6h3v6h5.3v-9h2.7z"/></svg>Главная</button>
  <button data-tab="projects"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h5l2 2h8A1.5 1.5 0 0 1 21 8.5v10a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z"/></svg>Проекты</button>
  <button data-tab="staff"><svg viewBox="0 0 24 24" fill="currentColor"><circle cx="9" cy="8" r="3.5"/><circle cx="17" cy="9" r="2.8"/><path d="M2.5 19c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6zM15.5 19c0-1.9-.6-3.6-1.7-4.8 3.6-.6 7.7 1.2 7.7 4.8z"/></svg>Команда</button>
  <button data-tab="clients"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 3h10l4 6-9 12L3 9zm-1.2 6h12.4L15.7 5H8.3z"/></svg>Клиенты</button>
</nav>
<nav class="tabbar hidden" id="shop-nav" style="grid-template-columns:repeat(4,1fr)">
  <button data-snav="home"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3 2.5 11h2.7v9h5.3v-6h3v6h5.3v-9h2.7z"/></svg>Главная</button>
  <button data-snav="sell"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7V5a1 1 0 0 1 1-1h2M17 4h2a1 1 0 0 1 1 1v2M20 17v2a1 1 0 0 1-1 1h-2M7 20H5a1 1 0 0 1-1-1v-2"/><rect x="6" y="9" width="12" height="7" rx="2" fill="currentColor" stroke="none"/></svg>Продажа</button>
  <button data-snav="list"><svg viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="3" width="16" height="18" rx="3"/><path d="M8 9h8M8 13h8M8 17h5" stroke="#15112a" stroke-width="2"/></svg>Транзакции</button>
  <button data-snav="menu"><svg viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="3" width="8" height="8" rx="2.5"/><rect x="13" y="3" width="8" height="8" rx="2.5" opacity=".55"/><rect x="3" y="13" width="8" height="8" rx="2.5"/><rect x="13" y="13" width="8" height="8" rx="2.5"/></svg>Меню</button>
</nav>
<div class="sheet-bg hidden" id="sheet-bg">
  <div class="sheet">
    <h3 id="sheet-title">График</h3>
    <div class="hint" style="font-size:13px;margin:-8px 0 12px">Выберите график на каждый день недели</div>
    <div class="row-day"><b style="font-size:13px">Все</b><select id="sheet-all"><option value="">Заполнить все дни…</option></select></div>
    <div id="sheet-days"></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px">
      <button class="btn btn-soft" id="sheet-cancel">Отмена</button>
      <button class="btn btn-main" id="sheet-save">Сохранить</button>
    </div>
  </div>
</div>
<div class="sheet-bg hidden" id="inv-sheet-bg"><div class="sheet" id="inv-sheet"></div></div>
<div class="toast" id="toast"></div>

<script>
/* ---------- мобильное приложение RADI Team (Android / iPhone) ----------
   В приложении библиотека Telegram не работает (и её свойства нельзя подменить) — используем свою замену. */
const APP = new URLSearchParams(location.search).get("app") || (window.RadiApp ? "android" : "");
const realTg = window.Telegram && window.Telegram.WebApp;
const IS_APP = !!APP && !(realTg && realTg.initData);
const appToken = { get: () => { try { return localStorage.getItem("radi_app_token") || ""; } catch (e) { return ""; } },
  set: v => { try { v ? localStorage.setItem("radi_app_token", v) : localStorage.removeItem("radi_app_token"); } catch (e) {} } };
const appBackState = {on: false, cb: null};
const vib = ms => { try { navigator.vibrate && navigator.vibrate(ms); } catch (e) {} };
const tg = IS_APP ? {
  initData: "", ready() {}, expand() {}, close() {}, setHeaderColor() {}, setBackgroundColor() {},
  showConfirm: (t, cb) => cb(window.confirm(t)),
  showAlert: (t, cb) => { window.alert(t); cb && cb(); },
  HapticFeedback: {notificationOccurred: () => vib(30), selectionChanged: () => vib(8), impactOccurred: () => vib(15)},
  BackButton: {show() { appBackState.on = true; }, hide() { appBackState.on = false; }, onClick(cb) { appBackState.cb = cb; }},
} : realTg;
/* «Назад» — одно действие для кнопки Telegram, кнопки Android и свайпа от левого края.
   true — обработали (закрыли окно / шаг назад / вышли из раздела), false — назад некуда. */
function radiBack() {
  try {
    if (!document.getElementById("rec").classList.contains("hidden")) { recClose(); return true; }
    const bg = document.getElementById("inv-sheet-bg");
    if (bg && !bg.classList.contains("hidden")) { invSheetClose(); return true; }
    const sb = document.getElementById("sheet-bg");
    if (sb && !sb.classList.contains("hidden")) { sb.classList.add("hidden"); return true; }
    if (view === "shop") {
      if (shopTab === "sell" && cart.step !== "amount") { cart.step = cart.step === "pay" ? "details" : "amount"; shopSell(); return true; }
      if (shopTab === "menu" && shopSub) { shopSub = null; renderShop(); return true; }
      if (shopTab !== "home" && data && data.is_admin) { shopTab = "home"; shopSub = null; loadShop(); return true; }
    }
    if (view in SUBPAGES) { show(SUBPAGES[view] || "me"); return true; }
  } catch (e) {}
  return false;
}

/* Свайп от левого края вправо = «назад» (как в мини-приложении BotFather) */
(function edgeSwipeBack() {
  const EDGE = 28, NEED = 70;
  let sx = 0, sy = 0, active = false, dx = 0;
  const arrow = document.createElement("div");
  arrow.id = "swipe-back";
  arrow.textContent = "‹";
  if (document.body) document.body.appendChild(arrow);
  else document.addEventListener("DOMContentLoaded", () => document.body.appendChild(arrow));
  const paint = () => {
    const p = Math.min(1, dx / NEED);
    arrow.style.opacity = String(p);
    arrow.style.transform = `translate(${Math.min(dx, NEED) * 0.6 - 40}px, -50%) scale(${0.7 + 0.3 * p})`;
    arrow.classList.toggle("ready", p >= 1);
  };
  document.addEventListener("touchstart", e => {
    const t = e.touches[0];
    active = e.touches.length === 1 && t.clientX <= EDGE;
    if (!active) return;
    sx = t.clientX; sy = t.clientY; dx = 0;
    arrow.style.top = t.clientY + "px";
  }, {passive: true});
  document.addEventListener("touchmove", e => {
    if (!active) return;
    const t = e.touches[0];
    dx = t.clientX - sx;
    if (Math.abs(t.clientY - sy) > Math.max(40, dx)) { active = false; dx = 0; }  // это вертикальная прокрутка
    paint();
  }, {passive: true});
  const end = () => {
    if (active && dx >= NEED && radiBack()) {
      if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred("light");
    }
    active = false; dx = 0; paint();
  };
  document.addEventListener("touchend", end, {passive: true});
  document.addEventListener("touchcancel", end, {passive: true});
})();

/* Старое Android-приложение (до 1.2): камера в кабинете не работает, «Открыть камеру» открывает галерею —
   просим обновить. */
const APP_MIN_VERSION = "1.2";
function appOutdated() {
  try {
    const m = navigator.userAgent.match(/RadiTeamApp\/([\d.]+)/);  // версия приложения — в User-Agent (с 1.0)
    const v = m ? m[1] : "";
    if (!v) return false;
    const a = v.split(".").map(Number), b = APP_MIN_VERSION.split(".").map(Number);
    for (let i = 0; i < 3; i++) { if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) < (b[i] || 0); }
  } catch (e) {}
  return false;
}
function appUpdatePrompt() {
  const url = location.origin + "/app/radi-team.apk";
  tg.showConfirm("Обновите приложение RADI Team — в старой версии не работает камера.\nIlovani yangilang — eski versiyada kamera ishlamaydi.\n\nСкачать новую версию?",
    ok => { if (ok) { try { window.RadiApp.openExternal(url); } catch (e) { location.href = url; } } });
}

if (IS_APP) {
  document.documentElement.classList.add("app");
  window.appBack = radiBack;
  if (appOutdated()) setTimeout(appUpdatePrompt, 1500);  // кнопка «Назад» Android: true — обработали, false — свернуть приложение
  document.addEventListener("contextmenu", e => { if (!/INPUT|TEXTAREA/.test(e.target.tagName)) e.preventDefault(); });
  document.addEventListener("copy", e => { if (!/INPUT|TEXTAREA/.test((e.target || {}).tagName || "")) e.preventDefault(); });
}
tg.ready(); tg.expand();
try { tg.setHeaderColor("#0d0e1c"); tg.setBackgroundColor("#0d0e1c"); } catch (e) {}

let data = null, view = "me";
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = n => Number(n).toLocaleString("ru-RU", {maximumFractionDigits: 1});
const plural = (n, a, b, c) => { n = Math.abs(n) % 100; const d = n % 10;
  return n > 10 && n < 20 ? c : d > 1 && d < 5 ? b : d === 1 ? a : c; };

/* ---------- звания (бейджи) ---------- */
function starPts(cx, cy, R) {
  const pts = [];
  for (let i = 0; i < 10; i++) {
    const r = i % 2 ? R * 0.45 : R, a = Math.PI / 2 * -1 + i * Math.PI / 5;
    pts.push((cx + r * Math.cos(a)).toFixed(1) + "," + (cy + r * Math.sin(a)).toFixed(1));
  }
  return pts.join(" ");
}
const grad = (id, c1, c2) => `<defs><linearGradient id="${id}" x1="0" y1="0" x2="1" y2="1">
  <stop offset="0" stop-color="${c1}"/><stop offset="1" stop-color="${c2}"/></linearGradient></defs>`;
const SHIELD = "M32 4 L56 12 V31 C56 46 45 56 32 61 C19 56 8 46 8 31 V12 Z";
const BOARD = '<rect x="15" y="3" width="34" height="58" rx="9"';
function chevrons(n) {
  return Array.from({length: n}, (_, i) => `<polyline points="18,${44 - i * 9} 32,${34 - i * 9} 46,${44 - i * 9}"
    fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round" opacity=".92"/>`).join("");
}
const RANK_ICONS = [
  // 0 Солдат: щит, одна лычка
  () => `<svg viewBox="0 0 64 64">${grad("r0", "#c9a66b", "#6b5330")}
    <path d="${SHIELD}" fill="url(#r0)" stroke="#e9d3a4" stroke-width="2"/>${chevrons(1)}</svg>`,
  // 1 Сержант: щит, три лычки
  () => `<svg viewBox="0 0 64 64">${grad("r1", "#dfe4ee", "#6f7a8c")}
    <path d="${SHIELD}" fill="url(#r1)" stroke="#f4f6fb" stroke-width="2"/>${chevrons(3)}</svg>`,
  // 2 Лейтенант: погон, две звезды
  () => `<svg viewBox="0 0 64 64">${grad("r2", "#3a4a7a", "#1c2547")}
    ${BOARD} fill="url(#r2)" stroke="#8fa2d8" stroke-width="2"/>
    <line x1="32" y1="6" x2="32" y2="58" stroke="#ffd35c" stroke-width="2.5"/>
    <polygon points="${starPts(23, 40, 7)}" fill="#ffd35c"/><polygon points="${starPts(41, 40, 7)}" fill="#ffd35c"/></svg>`,
  // 3 Полковник: погон с двумя просветами, три звезды
  () => `<svg viewBox="0 0 64 64">${grad("r3", "#4b2f86", "#241447")}
    ${BOARD} fill="url(#r3)" stroke="#b49cff" stroke-width="2"/>
    <line x1="26" y1="6" x2="26" y2="58" stroke="#ffd35c" stroke-width="2.5"/>
    <line x1="38" y1="6" x2="38" y2="58" stroke="#ffd35c" stroke-width="2.5"/>
    <polygon points="${starPts(32, 18, 7)}" fill="#ffe08a"/><polygon points="${starPts(22, 36, 7)}" fill="#ffe08a"/>
    <polygon points="${starPts(42, 36, 7)}" fill="#ffe08a"/></svg>`,
  // 4 Генерал: золотой погон с большой звездой
  () => `<svg viewBox="0 0 64 64">${grad("r4", "#ffe58a", "#c98a12")}
    ${BOARD} fill="url(#r4)" stroke="#fff3c4" stroke-width="2"/>
    <path d="M18 12 L24 18 L30 12 L36 18 L42 12 L46 16" fill="none" stroke="#a8700c" stroke-width="2" opacity=".6"/>
    <polygon points="${starPts(32, 38, 14)}" fill="#fff6d6" stroke="#a8700c" stroke-width="1.5"/></svg>`,
];
const DEFAULT_RANKS = ["Солдат", "Сержант", "Лейтенант", "Полковник", "Генерал"];
function levels(goal) {
  const names = (data && data.ranks) || DEFAULT_RANKS;
  const q = goal / 4;
  const bounds = [[0, q - 1], [q, 2 * q - 1], [2 * q, 3 * q - 1], [3 * q, goal - 1], [goal, null]];
  return bounds.map(([from, to], i) => ({name: names[i] || DEFAULT_RANKS[i], from, to, icon: RANK_ICONS[i]}));
}
const levelOf = (h, goal) => { const L = levels(goal); let i = 0; L.forEach((l, k) => { if (h >= l.from) i = k; }); return [L[i], i, L]; };

/* ---------- иконки и аватар ---------- */
const icCal = `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M8 3v4M16 3v4M3 10h18"/></svg>`;
const icClock = `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`;
function avatar(uid, name, v) {
  const init = esc((name || "?").trim().split(/\s+/).map(w => w[0]).slice(0, 2).join("").toUpperCase());
  if (!v) return `<div>${init}</div>`;
  return `<img src="/avatar/${uid}.jpg?v=${v}" onerror="this.outerHTML='<div>${init}</div>'">`;
}

/* Сжать фото в квадрат 512×512 JPEG прямо на телефоне */
function squareJpeg(file) {
  return new Promise((ok, fail) => {
    const img = new Image();
    img.onload = () => {
      const s = Math.min(img.width, img.height), size = Math.min(512, s);
      const c = document.createElement("canvas"); c.width = c.height = size;
      c.getContext("2d").drawImage(img, (img.width - s) / 2, (img.height - s) / 2, s, s, 0, 0, size, size);
      URL.revokeObjectURL(img.src);
      ok(c.toDataURL("image/jpeg", 0.85));
    };
    img.onerror = () => fail(new Error("Не удалось открыть фото"));
    img.src = URL.createObjectURL(file);
  });
}

/* ---------- запросы ---------- */
function toast(t) { const el = $("toast"); el.textContent = t; el.style.display = "block";
  clearTimeout(el._h); el._h = setTimeout(() => el.style.display = "none", 3000); }

async function api(path, body = {}) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(Object.assign({initData: tg.initData}, IS_APP ? {appToken: appToken.get()} : {}, body))});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || "Ошибка");
  return j;
}
async function action(path, body) {
  try { const r = await api(path, body);
    if (r.video) return openRecorder(r);
    toast(r.msg);
    if (r.close) { setTimeout(() => tg.close(), 1800); }
    if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(r.ok ? "success" : "warning");
    await load();
  } catch (e) { toast(e.message); }
}

/* ---------- экраны ---------- */
const SUBPAGES = {crm: "team", crmchat: "crm", history: "me", invest: "me", "invest-admin": "team", clients: "team", staff: "team", shop: "team", emp: "staff", ccard: "clients",
                  projects: "team", project: "projects", posting: "team", earn: "me", "earn-admin": "team", msgr: "team", appsec: "team"};  // страница → куда ведёт «назад»
/* Разделы, скрытые руководителем в приложении RADI Team (в Telegram-боте видно всё) */
const APP_SEC_OF = {project: "projects", emp: "staff", ccard: "clients", crmchat: "crm", "invest-admin": "invest"};
const APP_SEC_BTN = {"go-shop": "shop", "d-shop-tile": "shop", "go-clients": "clients", "go-staff": "staff",
  "go-projects": "projects", "go-posting": "posting", "go-posting-me": "posting", "go-earn": "earn",
  "go-earn-admin": "earn-admin", "go-inv": "invest", "go-inv-own": "invest", "go-inv-admin": "invest", "all-btn": "history"};
const appHiddenSec = sec => IS_APP && !!data && (data.app_hidden || []).includes(sec);
function applyAppSections() {
  if (!IS_APP) return;
  document.querySelectorAll("[data-dgo], #tabbar [data-tab]").forEach(b =>
    b.classList.toggle("app-hide", appHiddenSec(APP_SEC_OF[b.dataset.dgo || b.dataset.tab] || b.dataset.dgo || b.dataset.tab)));
  Object.entries(APP_SEC_BTN).forEach(([id, sec]) => { const el = $(id); if (el) el.classList.toggle("app-hide", appHiddenSec(sec)); });
}
function show(v, refresh = false) {
  if (appHiddenSec(APP_SEC_OF[v] || v)) { toast("Этот раздел открывается в Telegram-боте\nBu bo'lim Telegram-botda ochiladi"); return; }
  const same = refresh && v === view;  // обновление того же экрана после действия — без прокрутки наверх
  view = v;
  ["me", "history", "team", "client", "invest", "invest-admin", "clients", "staff", "projects", "project", "posting", "earn", "earn-admin", "shop", "emp", "ccard", "crm", "crmchat", "msgr", "appsec"].forEach(n => $("view-" + n).classList.toggle("hidden", n !== v));
  $("seg").classList.toggle("hidden", !(data && data.is_admin && data.is_employee) || v in SUBPAGES);
  document.querySelectorAll("#seg button").forEach(b => b.classList.toggle("on", b.dataset.view === v));
  if (v in SUBPAGES) tg.BackButton.show(); else tg.BackButton.hide();
  $("brand").classList.toggle("hidden", v === "client" || v === "crmchat" || !data);
  document.body.classList.toggle("crm-open", v === "crmchat");
  const tabs = !!(data && data.is_admin) && ["team", "projects", "staff", "clients"].includes(v);
  $("tabbar").classList.toggle("hidden", !tabs);
  $("shop-nav").classList.toggle("hidden", v !== "shop");
  document.body.classList.toggle("has-tabs", tabs || v === "shop");
  document.querySelectorAll("#tabbar [data-tab]").forEach(b => b.classList.toggle("on", b.dataset.tab === v));
  if (!same) {
    window.scrollTo(0, 0);
    crmOnShow(v);
    if (v === "msgr") msgrLoad();
    if (v === "appsec") appSecLoad();
  }
  applyAppSections();
}
tg.BackButton.onClick(() => { if (!radiBack()) show(SUBPAGES[view] || "me"); });

/* ---------- тексты кабинета сотрудника (uz / ru) ---------- */
const SX = {
  ru: {h: "ч", per_month: "за месяц", in_row: "подряд", cur_rank: "текущее звание", invest: "Инвестиции",
       my_week: "🗓 Мой график на неделю", ranks: "Звания", my_tasks: "📋 Мои задачи", posting_title: "Постинг", posting_sub: "Посты в бот, группы и каналы", shop_title: "Продажи", shop_sub: "Новая продажа и мои продажи", done_title: "Выполненные задачи", done_sub: "Так держать! 💪",
       done_month: "в этом месяце", done_total: "за всё время", mt_ph: "Добавить себе задачу…", mt_today: "Сегодня", mt_tomorrow: "Завтра", mt_pick: "📅 Дата…", mt_mine: "моя", last_visits: "Последние посещения",
       all: "Все →", all_visits: "Все посещения", now: "сейчас", late: "опоздание", early: "ранний уход",
       auto: "уход закрыт автоматически", by_boss: "уход отметил руководитель", here_since: "На месте с {t}", not_here: "Не на месте",
       close_tasks: "⛔ Сначала закройте задачи ({n})", free_here: "Свободный график · отметьте уход, когда закончите",
       free_any: "Свободный график · отмечайтесь в любое время", day_off: "Сегодня выходной", out_at: "Уход: {w}",
       rec_say: "Скажите вслух число", rec_cam: "Включаю камеру…", rec_btn: "● Записать", rec_again: "● Записать ещё раз", rec_done: "■ Готово", rec_open: "📹 Открыть камеру", rec_cancel: "Отмена", rec_send: "Отправляю…", rec_go: "Нажмите «Записать» и назовите число. {a}–{b} сек.", rec_sys: "Запишите видео {a}–{b} сек и назовите число. У вас {m} мин.", rec_rec: "Запись… {s}", rec_can: "Можно нажать «Готово»", rec_short: "Не короче {s} сек",
       shift_done: "Смена завершена · график {s}", in_at: "Приход: {w}", late_now: "⚠️ Вы опаздываете — отметьте приход", btn_out: "🚪 Ушёл", btn_in: "✅ Пришёл",
       kpi_soon: "⏳ Ожидаемый доход — скоро оплатим 🙌", kpi_title: "Мой доход · KPI", kpi_exp: "ожидаемый доход", kpi_paidm: "выплачено за месяц", kpi_sales: "продажи за месяц", kpi_paid: "✅ выплачено", kpi_wait: "⏳ ожидается", kpi_empty: "Продаж в этом месяце пока нет",
       fines: "🔴 Штрафы", fines_active: "активные", fine_paid: "✅ оплачен {d}", total: "всего", this_month: "за этот месяц", no_fines_month: "В этом месяце штрафов нет 👍",
       no_fines: "Штрафов нет — так держать! 👍", due: "📅 на {d}", review: "⏳ на проверке", done: "Готово",
       no_tasks: "Задач нет 🎉", no_visits: "Посещений пока нет", to_rank: "до звания «{r}»",
       keep_going: "Продолжайте в том же духе!", you_are: "Вы — {r}! 🏆", left_to: "До звания «{r}» осталось {h} ч 💪",
       goal_done: "Цель {h} часов достигнута", soon: "Раздел скоро появится", soon_pill: "Скоро ⏳",
       invest_soon: "💰 Раздел «Инвестиции» скоро появится ⏳", lang_btn: "🌐 O'zbekchaga o'tish", photo: "Загружаю фото…",
       earn_title: "Foydam", earn_page: "Foydam", earn_sub: "Ваша прибыль в компании",
       earn_by_month: "По месяцам", earn_total: "за всё время", earn_year: "в этом году", earn_empty: "Пока нет записей",
       earn_folder: "За всё время: {s}", cur: "сум"},
  uz: {h: "soat", per_month: "shu oyda", in_row: "ketma-ket", cur_rank: "joriy unvon", invest: "Investitsiyalar",
       my_week: "🗓 Haftalik grafigim", ranks: "Unvonlar", my_tasks: "📋 Vazifalarim", posting_title: "Posting", posting_sub: "Bot, guruh va kanallarga postlar", shop_title: "Savdo", shop_sub: "Yangi savdo va savdolarim", done_title: "Bajarilgan vazifalar", done_sub: "Shunday davom eting! 💪",
       done_month: "shu oyda", done_total: "jami", mt_ph: "O'zingizga vazifa qo'shing…", mt_today: "Bugun", mt_tomorrow: "Ertaga", mt_pick: "📅 Sana…", mt_mine: "o'zimniki", last_visits: "Oxirgi tashriflar",
       all: "Hammasi →", all_visits: "Barcha tashriflar", now: "hozir", late: "kechikish", early: "erta ketish",
       auto: "ketish avtomatik yopildi", by_boss: "ketishni rahbar belgiladi", here_since: "{t} dan beri ishda", not_here: "Ishda emas",
       close_tasks: "⛔ Avval vazifalarni yoping ({n})", free_here: "Erkin grafik · tugatganingizda ketishni belgilang",
       free_any: "Erkin grafik · istalgan vaqtda belgilang", day_off: "Bugun dam olish kuni", out_at: "Ketish: {w}",
       rec_say: "Raqamni ovoz chiqarib ayting", rec_cam: "Kamera yoqilmoqda…", rec_btn: "● Yozish", rec_again: "● Qayta yozish", rec_done: "■ Tayyor", rec_open: "📹 Kamerani ochish", rec_cancel: "Bekor qilish", rec_send: "Yuborilmoqda…", rec_go: "«Yozish»ni bosing va raqamni ayting. {a}–{b} soniya.", rec_sys: "{a}–{b} soniyalik video yozing va raqamni ayting. {m} daqiqa vaqtingiz bor.", rec_rec: "Yozilmoqda… {s}", rec_can: "«Tayyor»ni bosish mumkin", rec_short: "Kamida {s} soniya",
       shift_done: "Smena tugadi · grafik {s}", in_at: "Kelish: {w}", late_now: "⚠️ Kechikyapsiz — kelganingizni belgilang", btn_out: "🚪 Ketdim", btn_in: "✅ Keldim",
       kpi_soon: "⏳ Kutilayotgan daromad — tez orada to'laymiz 🙌", kpi_title: "Daromadim · KPI", kpi_exp: "kutilayotgan daromad", kpi_paidm: "shu oy to'langan", kpi_sales: "shu oy savdo", kpi_paid: "✅ to'langan", kpi_wait: "⏳ kutilmoqda", kpi_empty: "Bu oyda hali savdo yo'q",
       fines: "🔴 Jarimalar", fines_active: "faol", fine_paid: "✅ to'langan {d}", total: "jami", this_month: "shu oyda", no_fines_month: "Bu oyda jarima yo'q 👍",
       no_fines: "Jarima yo'q — shunday davom eting! 👍", due: "📅 {d} ga", review: "⏳ tekshiruvda", done: "Bajarildi",
       no_tasks: "Vazifa yo'q 🎉", no_visits: "Hozircha tashriflar yo'q", to_rank: "«{r}» unvonigacha",
       keep_going: "Shunday davom eting!", you_are: "Siz — {r}! 🏆", left_to: "«{r}» unvonigacha {h} soat qoldi 💪",
       goal_done: "{h} soatlik maqsadga erishildi", soon: "Bo'lim tez orada ochiladi", soon_pill: "Tez orada ⏳",
       invest_soon: "💰 «Investitsiyalar» bo'limi tez orada ochiladi ⏳", lang_btn: "🌐 Перейти на русский", photo: "Rasm yuklanmoqda…",
       earn_title: "Foydam", earn_page: "Foydam", earn_sub: "Kompaniyada olgan foydangiz",
       earn_by_month: "Oylar bo'yicha", earn_total: "butun vaqt davomida", earn_year: "shu yili", earn_empty: "Hozircha yozuvlar yo'q",
       earn_folder: "Jami: {s}", cur: "so'm"},
};
const meLang = () => (data && data.profile && data.profile.lang === "uz") ? "uz" : "ru";
const sx = (k, v = {}) => (SX[meLang()][k] || SX.ru[k] || k).replace(/\{(\w+)\}/g, (_, n) => v[n] ?? "");
const daysSx = n => meLang() === "uz" ? `${n} kun` : n + " " + plural(n, "день", "дня", "дней");

function visitRow(h, full) {
  return `<div class="visit ${full ? "" : "short"}">
    <div class="t">
      <span>${icCal}${h.date.slice(0, 5)}<span class="yr">${h.date.slice(5)}</span>${full ? ` <span class="hint">${h.wd}</span>` : ""}</span>
      <span>${icClock}${h.in} – ${h.out || sx("now")}</span>
      ${full && (h.late || h.early || h.auto || h.by_boss) ? `<div class="tag" style="width:100%">
        ${h.late ? `<span style="color:var(--orange)">${sx("late")} ${h.late}</span> ` : ""}
        ${h.early ? `<span style="color:var(--orange)">${sx("early")} ${h.early}</span> ` : ""}
        ${h.auto ? `<span class="hint">${sx("auto")}</span>` : ""}${h.by_boss ? `<span class="hint">${sx("by_boss")}</span>` : ""}</div>` : ""}
    </div>
    <div class="dur">${h.worked}</div></div>`;
}

function renderMe() {
  const p = data.profile;
  document.querySelectorAll("[data-sx]").forEach(el => el.textContent = sx(el.dataset.sx));
  langSwitch("me-langsw", [["uz", "🇺🇿 UZ"], ["ru", "🇷🇺 RU"]], meLang(), l => action("/api/staff_lang", {lang: l}));
  const [lvl, idx, L] = levelOf(p.total_hours, p.goal);
  $("me-ava").innerHTML = avatar(p.id, p.name, p.ava_v);
  $("p-name").textContent = p.name;
  $("p-role").textContent = p.role;
  $("p-lvl").innerHTML = lvl.icon() + esc(lvl.name);
  $("g-have").textContent = num(p.total_hours);
  $("g-goal").textContent = num(p.goal);
  const pct = Math.min(100, p.total_hours / p.goal * 100);
  $("g-bar").style.width = pct + "%";
  $("g-pct").textContent = (pct < 1 && pct > 0 ? pct.toFixed(1) : Math.floor(pct)) + "%";
  $("s-month").textContent = num(p.month_hours) + " " + sx("h");
  $("s-streak").textContent = daysSx(p.streak);
  $("s-level").textContent = lvl.name;
  $("done-month").textContent = num(p.tasks_done_month || 0);
  $("done-total").textContent = num(p.tasks_done_total || 0);

  $("my-week").innerHTML = weekCells(p.week, p.today_wd);
  $("st-text").innerHTML = p.on_site
    ? `<span class="dot" style="background:var(--green)"></span>${sx("here_since", {t: p.since})}`
    : `<span class="dot" style="background:var(--hint)"></span>${sx("not_here")}`;
  const can = p.on_site ? p.can_out : p.can_in;
  $("st-sub").textContent = p.on_site && p.unfinished ? sx("close_tasks", {n: p.unfinished})
    : !p.has_schedule ? (p.on_site ? sx("free_here") : sx("free_any"))
    : !p.workday ? sx("day_off")
    : p.on_site ? sx("out_at", {w: p.out_window})
    : p.came_today ? sx("shift_done", {s: p.schedule})
    : p.late_now ? sx("late_now")
    : sx("in_at", {w: p.in_window});
  $("btn-mark").textContent = p.on_site ? sx("btn_out") : sx("btn_in");
  $("btn-mark").className = "btn " + (p.on_site ? "btn-soft" : "btn-main");
  $("btn-mark").disabled = !can;
  $("pen-card").style.borderColor = p.penalties_active ? "rgba(248,113,113,.35)" : "";
  $("pen-card").innerHTML = `
    <div class="head"><h3>${sx("fines")}</h3></div>
    <div class="pen-stats three">
      <div><b style="color:${p.penalties_active ? "var(--red)" : "var(--green)"}">${p.penalties_active || 0}</b><span>${sx("fines_active")}</span></div>
      <div><b>${p.penalties_month}</b><span>${sx("this_month")}</span></div>
      <div><b>${p.penalties_total}</b><span>${sx("total")}</span></div>
    </div>` + (p.penalties.length ? p.penalties.map(x => `
    <div class="pen ${x.paid ? "paid" : ""}"><div class="tt">${x.date} · ${esc(x.reason)}</div>
    ${x.paid ? `<span class="pen-tag">${sx("fine_paid", {d: x.paid_at})}</span>` : ""}
    ${x.amount ? `<b style="color:${x.paid ? "var(--hint)" : "var(--red)"}">${esc(x.amount)}</b>` : ""}</div>`).join("")
    : `<div class="empty">${p.penalties_total ? sx("no_fines_month") : sx("no_fines")}</div>`);

  $("levels").innerHTML = L.map((l, k) => (k ? `<div class="arrow">›</div>` : "") + `
    <div class="lv ${k > idx ? "locked" : ""} ${k === idx ? "cur" : ""}">${l.icon()}<b>${l.name}</b>
    <span>${l.to === null ? num(l.from) + "+" : num(l.from) + " – " + num(l.to)}</span></div>`).join("");

  const open = data.tasks.filter(t => !t.done), done = data.tasks.filter(t => t.done).slice(0, 3);
  $("tasks").innerHTML = (open.length || done.length) ? [...open, ...done].map(t => `
    <div class="task ${t.done ? "done" : ""}"><div class="tt">${t.repeat ? "🔁 " : t.self ? "✍️ " : ""}${esc(t.title)}<div class="hint" style="font-size:12px">${t.self ? sx("mt_mine") + " · " : ""}${t.created}</div></div>

    ${t.done ? "✔️" : t.due ? `<span class="hint" style="font-size:13px;white-space:nowrap">${sx("due", {d: t.due})}</span>`
      : t.status === "review" ? `<span class="hint" style="font-size:13px;white-space:nowrap">${sx("review")}</span>`
      : `<button class="btn btn-main btn-sm" data-done="${t.id}">${sx("done")}</button>`}</div>`).join("")
    : `<div class="empty">${sx("no_tasks")}</div>`;
  document.querySelectorAll("[data-done]").forEach(b => b.onclick = () => action("/api/task_done", {id: +b.dataset.done}));
  document.querySelectorAll("[data-mtdel]").forEach(b => b.onclick = () => action("/api/my_task", {action: "delete", id: +b.dataset.mtdel}));
  $("mt-title").placeholder = sx("mt_ph");

  $("visits").innerHTML = data.history.length ? data.history.slice(0, 4).map(h => visitRow(h, false)).join("")
    : `<div class="empty">${sx("no_visits")}</div>`;
  $("history-all").innerHTML = data.history.length ? data.history.map(h => visitRow(h, true)).join("")
    : `<div class="empty">${sx("no_visits")}</div>`;

  const next = L[idx + 1];
  const top = L[L.length - 1];
  $("g-to").textContent = sx("to_rank", {r: top.name});
  $("m-title").textContent = next ? sx("keep_going") : sx("you_are", {r: top.name});
  $("m-sub").textContent = next ? sx("left_to", {r: next.name, h: num(Math.max(0, next.from - p.total_hours))})
                                : sx("goal_done", {h: num(p.goal)});
}

function weekCells(week, todayWd) {
  const t = todayWd ?? data.today_wd;
  return (week || []).map((d, i) =>
    `<div class="${d.key === "off" ? "off" : ""} ${i === t ? "today" : ""}"><b>${d.day}</b>${esc(d.short)}</div>`).join("");
}
function weekChips(week, todayWd) {
  return week ? `<div class="week">${weekCells(week, todayWd)}</div>` : "";
}

let sheetUser = null;
function openWeek(uid) {
  const e = data.team.find(x => x.id === uid);
  if (!e) return;
  sheetUser = uid;
  const opts = Object.entries(data.shifts || {});
  $("sheet-title").textContent = "🗓 " + e.name;
  $("sheet-all").innerHTML = `<option value="">Заполнить все дни…</option>` +
    opts.map(([k, t]) => `<option value="${k}">${esc(t)}</option>`).join("");
  $("sheet-days").innerHTML = e.week.map((d, i) => `
    <div class="row-day"><b>${d.day}</b><select data-day="${i}">${opts.map(([k, t]) =>
      `<option value="${k}" ${k === d.key ? "selected" : ""}>${esc(t)}</option>`).join("")}
      ${opts.some(([k]) => k === d.key) ? "" : `<option value="" selected disabled>${esc(d.short)}</option>`}</select></div>`).join("");
  $("sheet-bg").classList.remove("hidden");
}
function closeWeek() { $("sheet-bg").classList.add("hidden"); sheetUser = null; }
$("sheet-cancel").onclick = closeWeek;
$("sheet-bg").onclick = ev => { if (ev.target.id === "sheet-bg") closeWeek(); };
$("sheet-all").onchange = ev => {
  if (!ev.target.value) return;
  document.querySelectorAll("#sheet-days select").forEach(s => s.value = ev.target.value);
  ev.target.value = "";
};
$("sheet-save").onclick = async () => {
  const week = {};
  for (const s of document.querySelectorAll("#sheet-days select")) {
    if (!s.value) return toast(`Выберите график на ${s.closest(".row-day").querySelector("b").textContent}`);
    week[s.dataset.day] = s.value;
  }
  const uid = sheetUser;
  closeWeek();
  await action("/api/set_week", {user_id: uid, week});
};

function renderStaffFolder() {
  const t = data.team, here = t.filter(e => e.icon === "🟢").length, absent = t.filter(e => e.icon === "🔴").length;
  const review = t.reduce((n, e) => n + e.tasks.filter(x => x.status === "review" && !x.due).length, 0);
  const txt = `${t.length} ${plural(t.length, "сотрудник", "сотрудника", "сотрудников")} · на месте: ${here}` +
    (absent ? ` · не пришли: ${absent}` : "") + (review ? ` · на проверке: ${review}` : "");
  $("staff-folder-sub").textContent = txt;
  $("staff-page-sub").textContent = txt;
  $("staff-badge").innerHTML = review ? `<span class="f-badge">${review}</span>` : "";
}
let empId = null;
function renderTeam() {
  renderStaffFolder();
  $("f-user").innerHTML = data.team.map(e => `<option value="${e.id}">${esc(e.name)}</option>`).join("");
  renderTaskUsers();
  // компактный список: нажатие — карточка сотрудника
  $("team").innerHTML = `<div class="card" style="padding:4px 14px">${data.team.map(e => {
    const review = e.tasks.filter(t => t.status === "review" && !t.due).length, open = e.tasks.filter(t => !t.due).length;
    return `<button class="emp-row" data-emp="${e.id}"><div class="ava">${avatar(e.id, e.name, e.ava_v)}</div>
      <div style="flex:1;min-width:0;text-align:left"><b>${esc(e.name)}</b>
        <div class="hint" style="font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(e.status)}${e.late ? ` · <span style="color:var(--red)">опоздание ${e.late}</span>` : ""}</div>
        <div class="emp-badges">${review ? `<span class="badge" style="color:var(--orange)">⏳ ${review} на проверке</span>` : ""}${open ? `<span class="badge">📌 ${open}</span>` : ""}${e.penalties_active ? `<span class="badge" style="color:var(--red)">🔴 ${e.penalties_active}</span>` : ""}</div></div>
      <span style="font-size:18px">${e.icon}</span><span class="f-arrow" style="font-size:22px;color:#c4b5fd">›</span></button>`; }).join("")}</div>`;
  document.querySelectorAll("[data-emp]").forEach(b => b.onclick = () => { empId = +b.dataset.emp; show("emp"); renderEmp(); });
  if (view === "emp") renderEmp();
}
/* Карточка сотрудника: всё о нём и все действия */
/* Касса и KPI сотрудника (руководитель): типы продаж с процентом, продажи за день, «Оплачено» → в Foyda */
const pctT = p => String(p).replace(".", ",");
function kpiRowsHtml(k, admin, T) {
  return k.items.length ? k.items.map(x => `<div class="kpi-row ${x.paid ? "paid" : ""}">
    <div class="tt">${x.day} · ${esc(x.type)}${admin ? `<div class="hint" style="font-size:12.5px">${money0(x.amount, T.cur)} × ${pctT(x.pct)}%</div>` : ""}</div>
    <span class="k">+${money0(x.kpi, T.cur)}</span>
    ${x.paid ? `<span class="pen-tag">${T.paid} ${x.paid_at}</span>` : admin ? `<span style="display:flex;gap:6px;width:100%">
      <button class="btn btn-main btn-sm" style="flex:1" data-kpay="${x.id}">💸 Оплачено</button>
      <button class="btn btn-soft btn-sm" data-kdel="${x.id}">✕</button></span>` : `<span class="hint" style="font-size:12.5px">${T.wait}</span>`}
    </div>`).join("") : `<div class="empty">${T.empty}</div>`;
}
function kpiTiles(k, T) {
  const sales = k.sales_month !== undefined;  // выручку видит только руководитель
  return `<div class="pen-stats ${sales ? "three" : ""}">
    <div><b style="color:#fbbf24;font-size:18px">${money0(k.expected, "")}</b><span>${T.exp}</span></div>
    <div><b style="color:var(--green);font-size:18px">${money0(k.paid_month, "")}</b><span>${T.paidm}</span></div>
    ${sales ? `<div><b style="font-size:18px">${money0(k.sales_month, "")}</b><span>${T.sales}</span></div>` : ""}</div>`;
}
const KPI_T_RU = {cur: "сум", paid: "✅ выплачено", wait: "⏳ ожидается", empty: "Продаж в этом месяце пока нет",
                  exp: "ожидаемый доход", paidm: "выплачено за месяц", sales: "продажи за месяц"};
function kpiShift(iso, days) {
  const d = new Date(iso + "T12:00:00"); d.setDate(d.getDate() + days); return d.toISOString().slice(0, 10);
}
function kpiCardAdmin(e) {
  const k = (data.kpi || {})[e.id]; if (!k) return "";
  const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  return `<div class="card" id="kpi-admin"><div class="head"><h3>🧾 Касса · KPI</h3>
      <span style="display:flex;gap:6px">${k.expected ? `<button class="btn btn-main btn-sm" id="kpi-payall">💸 Оплатить всё</button>` : ""}
      <button class="btn btn-soft btn-sm" id="kpi-settings" title="Виды продаж и проценты">⚙️</button></span></div>
    ${kpiTiles(k, KPI_T_RU)}
    ${k.types.length ? `<div class="kpi-form">
      <div class="f-lbl">1. Вид продаж</div>
      <div class="kpi-seg" id="kpi-type">${k.types.map((t, i) => `<button data-kt="${t.id}" class="${i ? "" : "on"}">${esc(t.name)}<b>${pctT(t.pct)}%</b></button>`).join("")}</div>
      <div class="f-lbl">2. Дата</div>
      <div class="kpi-seg kpi-days" id="kpi-days"><button data-d="${today}" class="on">Сегодня</button>
        <button data-d="${kpiShift(today, -1)}">Вчера</button>
        <label class="kpi-other"><span id="kpi-other-t">Другая дата</span><input type="date" id="kpi-day-pick" max="${today}"></label></div>
      <input type="hidden" id="kpi-day" value="${today}">
      <div class="f-lbl">3. Сумма продаж за день</div>
      <div class="kpi-sum"><input id="kpi-amount" inputmode="numeric" placeholder="0"><span>сум</span></div>
      <div class="kpi-res" id="kpi-preview">KPI сотрудника: <b>0 сум</b></div>
      <button class="btn btn-main" id="kpi-addbtn" style="width:100%">＋ Записать продажи</button></div>`
      : `<button class="btn btn-main" style="width:100%" id="kpi-first">⚙️ Добавить вид продаж и процент KPI</button>`}
    <div style="margin-top:6px">${kpiRowsHtml(k, true, KPI_T_RU)}</div></div>`;
}
function kpiTypeSheet(e, t) {
  invSheet(`<h3>${t ? "✏️ Тип продаж" : "＋ Тип продаж"} — ${esc(e.name)}</h3>
    <input id="kt-name" maxlength="40" placeholder="Название, например Optom / Chakana" value="${t ? esc(t.name) : ""}">
    <input id="kt-pct" inputmode="decimal" maxlength="6" placeholder="Процент KPI, например 5" value="${t ? pctT(t.pct) : ""}">
    <div class="quick" style="margin:-4px 0 10px">${[3, 5, 8, 10, 15, 18].map(p => `<button data-kp="${p}">${p}%</button>`).join("")}</div>
    ${sheetBtns(t ? "Сохранить" : "Добавить")}
    ${t ? `<button class="btn btn-soft" style="width:100%;margin-top:8px" id="kt-del">Убрать этот тип</button>` : ""}`);
  document.querySelectorAll("[data-kp]").forEach(b => b.onclick = () => { $("kt-pct").value = b.dataset.kp; });
  $("is-cancel").onclick = invSheetClose;
  $("is-ok").onclick = () => { const body = {action: "save_type", user_id: e.id, name: $("kt-name").value, pct: $("kt-pct").value};
    if (t) body.id = t.id; invSheetClose(); action("/api/kpi", body); };
  if (t) $("kt-del").onclick = () => tg.showConfirm("Убрать тип продаж? Записанные продажи останутся.", ok => {
    if (ok) { invSheetClose(); action("/api/kpi", {action: "delete_type", id: t.id}); } });
}
function kpiSettings(e) {
  const k = (data.kpi || {})[e.id] || {types: []};
  invSheet(`<h3>⚙️ Виды продаж — ${esc(e.name)}</h3>
    <div class="hint" style="font-size:12.5px;margin:-6px 0 10px">Вид продаж и процент KPI. Настраивается один раз; новый процент
      действует на новые продажи.</div>
    ${k.types.length ? k.types.map(t => `<div class="kpi-row" style="cursor:pointer" data-ktype="${t.id}">
      <div class="tt"><b>${esc(t.name)}</b></div><b style="color:#c4b5fd">${pctT(t.pct)}%</b><span>✏️</span></div>`).join("")
      : `<div class="empty">Видов продаж пока нет</div>`}
    <button class="btn btn-main" style="width:100%;margin-top:12px" id="kpi-newtype">＋ Вид продаж</button>
    <button class="btn btn-soft" style="width:100%;margin-top:8px" id="kpi-close">Готово</button>`);
  $("kpi-close").onclick = invSheetClose;
  $("kpi-newtype").onclick = () => kpiTypeSheet(e, null);
  document.querySelectorAll("[data-ktype]").forEach(b => b.onclick = () => kpiTypeSheet(e, k.types.find(t => t.id === +b.dataset.ktype)));
}
function bindKpiAdmin(e) {
  const k = (data.kpi || {})[e.id]; if (!k || !$("kpi-admin")) return;
  $("kpi-settings").onclick = () => kpiSettings(e);
  if ($("kpi-first")) $("kpi-first").onclick = () => kpiTypeSheet(e, null);
  if ($("kpi-addbtn")) {
    let typeId = k.types[0].id;
    const prev = () => {
      const t = k.types.find(x => x.id === typeId), a = +$("kpi-amount").value.replace(/\D/g, "");
      $("kpi-preview").innerHTML = `KPI сотрудника: <b>+${money0(t && a ? a * t.pct / 100 : 0, "сум")}</b>` +
        (t && a ? `<span>${money0(a, "").trim()} × ${pctT(t.pct)}%</span>` : "");
      $("kpi-addbtn").textContent = a ? `＋ Записать: ${money0(a, "сум")}` : "＋ Записать продажи";
    };
    document.querySelectorAll("#kpi-type [data-kt]").forEach(b => b.onclick = () => {
      typeId = +b.dataset.kt; document.querySelectorAll("#kpi-type [data-kt]").forEach(x => x.classList.toggle("on", x === b)); prev(); });
    const setDay = (d, el) => { $("kpi-day").value = d; document.querySelectorAll("#kpi-days > *").forEach(x => x.classList.toggle("on", x === el)); };
    document.querySelectorAll("#kpi-days [data-d]").forEach(b => b.onclick = () => { setDay(b.dataset.d, b); $("kpi-other-t").textContent = "Другая дата"; });
    $("kpi-day-pick").onchange = () => { const v = $("kpi-day-pick").value; if (!v) return;
      setDay(v, $("kpi-day-pick").parentElement); $("kpi-other-t").textContent = v.split("-").reverse().slice(0, 2).join("."); };
    $("kpi-amount").oninput = () => {  // 100000 → 100 000
      const n = $("kpi-amount").value.replace(/\D/g, "").slice(0, 13);
      $("kpi-amount").value = n ? (+n).toLocaleString("ru-RU") : ""; prev(); };
    prev();
    $("kpi-addbtn").onclick = () => {
      const amount = $("kpi-amount").value.replace(/\D/g, ""); if (!+amount) return toast("Введите сумму продаж");
      action("/api/kpi", {action: "add", user_id: e.id, type_id: typeId, amount, day: $("kpi-day").value});
    };
  }
  if ($("kpi-payall")) $("kpi-payall").onclick = () => tg.showConfirm(
    `Выплатить весь ожидаемый KPI: ${money0(k.expected, "сум")}? Сумма перейдёт в «Foyda».`,
    ok => ok && action("/api/kpi", {action: "pay", user_id: e.id}));
  document.querySelectorAll("#kpi-admin [data-kpay]").forEach(b => b.onclick = () => tg.showConfirm("Отметить KPI как оплаченный?",
    ok => ok && action("/api/kpi", {action: "pay", user_id: e.id, id: +b.dataset.kpay})));
  document.querySelectorAll("#kpi-admin [data-kdel]").forEach(b => b.onclick = () => tg.showConfirm("Удалить эту продажу?",
    ok => ok && action("/api/kpi", {action: "delete", id: +b.dataset.kdel})));
}
function renderKpiMe() {
  const k = data.kpi_me, box = $("kpi-card");
  const show = !!(k && (k.types.length || k.items.length));
  box.classList.toggle("hidden", !show); if (!show) return;
  const T = {cur: sx("cur"), paid: sx("kpi_paid"), wait: sx("kpi_wait"), empty: sx("kpi_empty"), exp: sx("kpi_exp"),
             paidm: sx("kpi_paidm"), sales: sx("kpi_sales")};
  box.innerHTML = `<div class="head"><h3>📈 ${sx("kpi_title")}</h3></div>${kpiTiles(k, T)}
    ${k.expected ? `<div class="hint" style="font-size:13px;margin:-2px 0 8px">${sx("kpi_soon")}</div>` : ""}
    ${kpiRowsHtml(k, false, T)}`;
}
/* Сотрудник с правом ставить задачи всем (TASK_ASSIGNERS) */
let asSel = new Set();
function renderAssign() {
  const list = data.assign_team, box = $("assign-card");
  box.classList.toggle("hidden", !list); if (!list) return;
  const uz = meLang() === "uz";
  $("as-title").textContent = uz ? "📌 Xodimlarga vazifa berish" : "📌 Поставить задачу сотрудникам";
  $("as-text").placeholder = uz ? "Vazifa matni…" : "Что нужно сделать?";
  const all = list.length && list.every(e => asSel.has(e.id));
  $("as-users").innerHTML = `<button data-asu="all" class="${all ? "on" : ""}">${uz ? "Hammaga" : "Всем"}</button>` +
    list.map(e => `<button data-asu="${e.id}" class="${asSel.has(e.id) ? "on" : ""}">${esc(e.name)}</button>`).join("");
  document.querySelectorAll("[data-asu]").forEach(b => b.onclick = () => {
    if (b.dataset.asu === "all") { if (all) asSel.clear(); else list.forEach(e => asSel.add(e.id)); }
    else { const id = +b.dataset.asu; asSel.has(id) ? asSel.delete(id) : asSel.add(id); }
    renderAssign();
  });
}
$("as-add").onclick = () => {
  const title = $("as-text").value.trim(), uz = meLang() === "uz";
  if (!asSel.size) return toast(uz ? "Kimga — xodimni tanlang" : "Выберите, кому");
  if (!title) return toast(uz ? "Vazifani yozing" : "Напишите задачу");
  action("/api/add_task", {user_ids: [...asSel], title}).then(() => { $("as-text").value = ""; asSel.clear(); renderAssign(); });
};
function renderEmp() {
  const e = data.team.find(x => x.id === empId);
  if (!e) { $("emp-body").innerHTML = `<div class="card empty">Сотрудник не найден</div>`; return; }
  const [lvl] = levelOf(e.total_hours, data.goal), pct = Math.min(100, e.total_hours / data.goal * 100);
  const earn = (data.earnings_team || []).find(x => x.id === e.id);
  $("emp-body").innerHTML = `
    <div class="card"><div class="member"><div class="ava" style="width:72px;height:72px">${avatar(e.id, e.name, e.ava_v)}</div>
      <div style="flex:1;min-width:0"><div style="display:flex;justify-content:space-between;gap:8px"><b style="font-size:20px">${esc(e.name)}</b><span style="font-size:20px">${e.icon}</span></div>
        <div class="hint" style="font-size:13px">${esc(e.role)} · сегодня: ${esc(e.schedule)}</div>
        <button class="link" id="ec-billz" style="font-size:12.5px;padding:2px 0">🔗 ${(e.billz || []).length ? "В BILLZ: " + e.billz.map(esc).join(", ") + " ✏️" : "Связать с продавцом BILLZ"}</button>
        <div style="font-size:14px;margin-top:2px">${esc(e.status)}${e.late ? ` <span style="color:var(--red)">· опоздание ${e.late}</span>` : ""}</div></div></div>
      ${e.on_site_since ? `<button class="btn btn-soft" style="width:100%;margin-top:12px" id="ec-out">🚪 Закончить смену за сотрудника</button>` : ""}
      <div class="bar mini-bar" style="margin-top:12px"><div style="width:${pct}%"></div></div>
      <div class="hint" style="font-size:13px;margin-top:6px"><span class="rank-sm">${lvl.icon()}</span> ${esc(lvl.name)} · ${num(e.total_hours)} / ${num(data.goal)} ч</div></div>
    <div class="menu-grid" style="margin-bottom:14px">
      <button class="card" id="ec-task"><span>📌</span>Задача</button>
      <button class="card" id="ec-week"><span>🗓</span>График</button>
      <button class="card" id="ec-fine"><span>🔴</span>Штраф</button>
      <button class="card" id="ec-earn"><span>💵</span>Foyda${earn ? `<div class="hint" style="font-size:12px;font-weight:600">${money0(earn.total, "сум")}</div>` : ""}</button></div>
    <div class="card"><div class="head"><h3>🗓 График на неделю</h3><button class="link" id="ec-week2">Изменить</button></div>${weekChips(e.week, data.today_wd)}</div>
    ${kpiCardAdmin(e)}
    ${dmCard("staff", e.id, "")}
    <div class="card"><div class="head"><h3>📋 Задачи</h3><span class="hint" style="font-size:13px">${e.tasks.length}</span></div>
      ${e.tasks.length ? e.tasks.map(t => `<div class="task"><div class="tt">${t.repeat ? "🔁" : t.self ? "✍️" : "📌"} ${esc(t.title)}${t.self ? ` <span class="hint" style="font-size:11.5px">(сам себе)</span>` : ""}${t.due
          ? `<div style="font-size:12px" class="hint">📅 на ${t.due}</div>`
          : t.status === "review" ? `<div style="font-size:12px;color:var(--orange)">⏳ ждёт вашей проверки</div>` : ""}</div>
        ${t.status === "review" && !t.due ? `<button class="btn btn-main btn-sm" data-review="${t.id}" data-act="ok">✅</button>
        <button class="btn btn-soft btn-sm" data-review="${t.id}" data-act="back">↩️</button>` : ""}
        ${t.due ? "" : `<button class="btn btn-soft btn-sm" data-review="${t.id}" data-act="later" title="На завтра">📅</button>`}
        ${t.status === "review" ? "" : `<button class="btn btn-soft btn-sm" data-del="${t.id}">✕</button>`}</div>`).join("") : `<div class="empty">Открытых задач нет</div>`}
      ${(e.recurring || []).length ? `<div class="hint" style="font-size:12.5px;margin-top:8px">🔁 Повторяющиеся:</div>` +
        e.recurring.map(r => `<div class="task"><div class="tt">🔁 ${esc(r.title)}<div style="font-size:12px" class="hint">${esc(r.when)}</div></div>
          <button class="btn btn-soft btn-sm" data-stoprep="${r.id}" title="Остановить повтор">✕</button></div>`).join("") : ""}</div>
    <div class="card"><div class="head"><h3>🔴 Штрафы</h3></div>
      <div class="pen-stats three">
        <div><b style="color:${e.penalties_active ? "var(--red)" : "var(--green)"}">${e.penalties_active || 0}</b><span>активные</span></div>
        <div><b>${e.penalties_month}</b><span>за месяц</span></div><div><b>${e.penalties_total}</b><span>за всё время</span></div></div>
      ${(() => { const act = e.penalties.filter(x => !x.paid);
        return act.length ? act.map(x => `<div class="pen pen-act"><div class="tt"><span style="color:var(--red)">🔴 ${x.date}</span> · ${esc(x.reason)}${x.amount ? ` · <b>${esc(x.amount)}</b>` : ""}</div>
        <div class="pen-btns"><button class="btn btn-main btn-sm" data-paypen="${x.id}">💸 Оплачено</button>
        <button class="btn btn-soft btn-sm" data-delpen="${x.id}">Отменить</button></div></div>`).join("")
        : `<div class="empty">Активных штрафов нет 👍</div>`; })()}
      ${(e.penalties_history || []).length ? `<details class="pen-hist"><summary>📜 История штрафов (${e.penalties_history.length}) ›</summary>
        ${e.penalties_history.map(x => `<div class="pen ${x.paid ? "paid" : ""}"><div class="tt">${x.paid ? "✅" : "🔴"} ${x.date} · ${esc(x.reason)}${x.amount ? ` · <b>${esc(x.amount)}</b>` : ""}</div>
          ${x.paid ? `<span class="pen-tag">оплачен ${x.paid_at}</span>` : `<span class="pen-tag" style="color:var(--red)">не оплачен</span>`}</div>`).join("")}</details>` : ""}</div>`;
  $("ec-week").onclick = $("ec-week2").onclick = () => openWeek(e.id);
  bindKpiAdmin(e);
  $("ec-billz").onclick = () => billzPick(e.id, e.name);
  bindDm("staff");
  if ($("ec-out")) $("ec-out").onclick = () => {
    const now = new Date(), p2 = n => String(n).padStart(2, "0"), cur = `${p2(now.getHours())}:${p2(now.getMinutes())}`;
    invSheet(`<h3>🚪 Закончить смену — ${esc(e.name)}</h3>
      <div class="hint" style="font-size:13px;margin:-6px 0 10px">На месте с ${e.on_site_since.slice(11, 16)}. Забыл(а) нажать «Ушёл» — отметьте уход сами.
        Задачи не проверяются, штраф за ранний уход не ставится. Сотруднику придёт уведомление.</div>
      <div class="kpi-seg" id="eo-q"><button data-t="" class="on">Сейчас</button>${["18:00", "19:00", "20:00"].filter(t => t > e.on_site_since.slice(11, 16) && t <= cur)
        .map(t => `<button data-t="${t}">${t}</button>`).join("")}</div>
      <div class="f-lbl">или своё время ухода</div><input type="time" id="eo-time" max="${cur}">
      ${sheetBtns("Закончить смену")}`);
    let tm = "";
    document.querySelectorAll("#eo-q [data-t]").forEach(b => b.onclick = () => { tm = b.dataset.t; $("eo-time").value = "";
      document.querySelectorAll("#eo-q [data-t]").forEach(x => x.classList.toggle("on", x === b)); });
    $("eo-time").onchange = () => { tm = $("eo-time").value; document.querySelectorAll("#eo-q [data-t]").forEach(x => x.classList.remove("on")); };
    $("is-cancel").onclick = invSheetClose;
    $("is-ok").onclick = () => { const time = tm; invSheetClose(); action("/api/admin_checkout", {user_id: e.id, time}); };
  };
  $("ec-earn").onclick = () => earnSheet(e.id);
  $("ec-task").onclick = () => {
    invSheet(`<h3>📌 Задача — ${esc(e.name)}</h3><input id="ek-t" maxlength="500" placeholder="Что нужно сделать?">
      <div class="quick" id="ek-rep" style="margin:0 0 10px"><button data-r="once" class="on">Один раз</button><button data-r="daily">🔁 Каждый день</button><button data-r="weekly">🔁 Каждую неделю</button></div>
      <select id="ek-wd" class="hidden">${["понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям"].map((d, i) => `<option value="${i}">по ${d}</option>`).join("")}</select>
      ${sheetBtns("Поставить")}`);
    let rep = "once";
    document.querySelectorAll("#ek-rep [data-r]").forEach(b => b.onclick = () => { rep = b.dataset.r;
      document.querySelectorAll("#ek-rep [data-r]").forEach(x => x.classList.toggle("on", x === b)); $("ek-wd").classList.toggle("hidden", rep !== "weekly"); });
    $("is-cancel").onclick = invSheetClose;
    $("is-ok").onclick = () => { const title = $("ek-t").value.trim(); if (!title) return toast("Введите задачу");
      const body = {user_ids: [e.id], title, repeat: rep}; if (rep === "weekly") body.weekday = +$("ek-wd").value;
      invSheetClose(); action("/api/add_task", body); };
  };
  $("ec-fine").onclick = () => {
    invSheet(`<h3>🔴 Штраф — ${esc(e.name)}</h3><input id="efn-amount" maxlength="50" placeholder="Сумма, например 50 000 сум (можно пусто)">
      <input id="efn-reason" maxlength="300" placeholder="Причина">${sheetBtns("Выписать")}`);
    $("is-cancel").onclick = invSheetClose;
    $("is-ok").onclick = () => { const reason = $("efn-reason").value.trim(); if (!reason) return toast("Укажите причину");
      const amount = $("efn-amount").value.trim(); invSheetClose(); action("/api/add_penalty", {user_id: e.id, reason, amount}); };
  };
  document.querySelectorAll("#emp-body [data-review]").forEach(b => b.onclick = () =>
    action("/api/task_review", {id: +b.dataset.review, action: b.dataset.act}));
  document.querySelectorAll("#emp-body [data-paypen]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отметить штраф как оплаченный?", ok => ok && action("/api/pay_penalty", {id: +b.dataset.paypen})));
  document.querySelectorAll("#emp-body [data-delpen]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отменить штраф?", ok => ok && action("/api/delete_penalty", {id: +b.dataset.delpen})));
  document.querySelectorAll("#emp-body [data-del]").forEach(b => b.onclick = () =>
    tg.showConfirm("Удалить задачу?", ok => ok && action("/api/delete_task", {id: +b.dataset.del})));
  document.querySelectorAll("#emp-body [data-stoprep]").forEach(b => b.onclick = () =>
    tg.showConfirm("Остановить повтор? Новые задачи перестанут появляться (уже созданные останутся).",
      ok => ok && action("/api/stop_recurring", {id: +b.dataset.stoprep})));
}

/* ---------- инвестиции ---------- */
const INV_STATUS = {pending: "⏳ ждёт", active: "📈 работает", rating: "⏳ ждёт оценки", matured: "🎉 к выплате",
                    paid: "✔️ выплачена", withdrawn: "↩️ капитал возвращён", rejected: "отклонена"};
const sum = n => Math.round(Number(n || 0)).toLocaleString("ru-RU") + " " + ((data && data.invest_cfg && data.invest_cfg.currency) || "");
const pc = x => (Math.round(x * 100) / 100).toLocaleString("ru-RU") + "%";
const daysWord = n => n + " " + plural(n, "день", "дня", "дней");
const LVL = () => Object.fromEntries(data.invest_cfg.levels.map(l => [l.key, l]));
const lvlTag = k => { const l = LVL()[k]; return l ? `${l.emoji} ${l.title.toLowerCase()}` : "—"; };
function invTiles(items) {
  return `<div class="inv-tiles">${items.map(([v, label, color]) =>
    `<div><b style="${color ? "color:" + color : ""}">${typeof v === "string" ? v : sum(v)}</b><span>${label}</span></div>`).join("")}</div>`;
}
function invItem(r, admin) {
  let sub = "", act = "", months = "";
  if (r.status === "pending") {
    sub = `<span>подана ${r.created} · на ${r.months} мес.</span><span>ожидаемая прибыль ${sum(r.min_total)} – ${sum(r.max_total)}</span>`;
    act = admin ? `<div class="inv-act"><button class="btn btn-main btn-sm" data-inv-ok="${r.id}">✅ Деньги получил</button>
                   <button class="btn btn-soft btn-sm" data-inv-no="${r.id}">✕ Отклонить</button></div>`
                : `<div class="inv-act"><button class="btn btn-soft btn-sm" data-inv-cancel="${r.id}">Отменить заявку</button></div>`;
  } else if (["active", "rating", "matured"].includes(r.status)) {
    sub = `<span>${r.start} → ${r.end}</span><span style="color:var(--green)">прибыль +${sum(r.accrued)}</span>`;
    if (r.status === "active") sub += `<span style="width:100%">ещё ${daysWord(r.days_left)} · ожидается в конце: ${sum(r.amount + r.min_total)} – ${sum(r.amount + r.max_total)}</span>`;
    else if (r.status === "rating") sub += `<span style="width:100%;color:var(--orange)">Срок закончился — ждём оценку последнего месяца</span>`;
    else sub += `<span style="width:100%;color:var(--green)">Срок закончился — к выплате <b>${sum(r.payout)}</b></span>`;
    if (admin && r.status === "matured")
      act = `<div class="inv-act"><button class="btn btn-main btn-sm" data-inv-paid="${r.id}">💸 Выплачено ${sum(r.payout)}</button></div>`;
    if (r.early) {
      sub += `<span style="width:100%;color:var(--orange)">↩️ Заявка на досрочный выход: вернуть капитал ${sum(r.amount)}, доля прибыли +${sum(r.accrued)} не выплачивается</span>`;
      if (r.early_info) sub += `<span style="width:100%;color:var(--text);line-height:1.5">👤 ${esc(r.early_info.name)}<br>📞 ${esc(r.early_info.phone)}<br>💳 ${esc(r.early_info.requisites)}</span>`;
      act = admin ? `<div class="inv-act"><button class="btn btn-main btn-sm" data-early-ok="${r.id}" data-amount="${r.amount}" data-name="${esc(r.name)}">✅ Вернуть капитал ${sum(r.amount)}</button>
                     <button class="btn btn-soft btn-sm" data-early-no="${r.id}">✕ Отклонить</button></div>`
                  : `<div class="inv-act"><button class="btn btn-soft btn-sm" data-early-cancel="${r.id}">Передумал — оставить инвестицию</button></div>`;
    } else if (!admin && r.status === "active") {
      act = `<div class="inv-act"><button class="btn btn-soft btn-sm" data-early="${r.id}" data-amount="${r.amount}" data-accrued="${r.accrued}">↩️ Досрочный выход</button></div>`;
    }
    if (r.months_list.length) months = `<details class="inv-months"><summary>Доля прибыли по месяцам</summary><div class="mchips">${
      r.months_list.map(m => `<span>${esc(m.title.split(" ")[0].slice(0, 3).toLowerCase())}${m.share < 100 ? " (неполн.)" : ""} ${
        m.level ? LVL()[m.level].emoji + " +" + sum(m.earned) : "⏳"}</span>`).join("")}</div></details>`;
  } else if (r.status === "withdrawn") {
    sub = `<span>${r.start} → ${r.end}</span><span>досрочный выход · возвращено ${sum(r.payout)}${r.pay_method_title ? " · " + r.pay_method_title : ""}</span>`;
  } else if (r.status === "paid") {
    sub = `<span>${r.start} → ${r.end}</span><span>доля прибыли ${sum(r.accrued)}</span>`;
  } else {
    sub = `<span>${r.created}</span>`;
  }
  const bar = ["active", "rating", "matured"].includes(r.status)
    ? `<div class="bar mini-bar"><div style="width:${r.progress}%"></div></div>` : "";
  return `<div class="inv"><div class="inv-top"><b>${admin ? esc(r.name) + " · " : ""}${sum(r.amount)}</b>
    <span class="chip ${r.status}">${INV_STATUS[r.status] || r.status}</span></div>
    ${bar}<div class="inv-sub hint">${sub}</div>${months}${act}</div>`;
}
function bindInvest() {
  document.querySelectorAll("[data-inv-ok]").forEach(b => b.onclick = () =>
    tg.showConfirm("Подтвердить, что деньги получены? С сегодняшнего дня капитал работает в товаре.",
      ok => ok && action("/api/invest_decide", {id: +b.dataset.invOk, approve: true})));
  document.querySelectorAll("[data-inv-no]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отклонить заявку?", ok => ok && action("/api/invest_decide", {id: +b.dataset.invNo, approve: false})));
  document.querySelectorAll("[data-inv-paid]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отметить инвестицию как выплаченную?", ok => ok && action("/api/invest_paid", {id: +b.dataset.invPaid})));
  document.querySelectorAll("[data-inv-cancel]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отменить заявку на инвестицию?", ok => ok && action("/api/invest_cancel", {id: +b.dataset.invCancel})));
  document.querySelectorAll("[data-early]").forEach(b => b.onclick = () => earlyForm(b.dataset));
  document.querySelectorAll("[data-acc-ok]").forEach(b => b.onclick = () =>
    tg.showConfirm("Открыть сотруднику доступ к инвестициям?", ok => ok && action("/api/invest_access_decide", {user_id: +b.dataset.accOk, approve: true})));
  document.querySelectorAll("[data-acc-no]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отклонить заявку на участие?", ok => ok && action("/api/invest_access_decide", {user_id: +b.dataset.accNo, approve: false})));
  document.querySelectorAll("[data-early-cancel]").forEach(b => b.onclick = () =>
    action("/api/invest_early", {id: +b.dataset.earlyCancel, cancel: true}));
  document.querySelectorAll("[data-early-ok]").forEach(b => b.onclick = () => payForm(b.dataset));
  document.querySelectorAll("[data-early-no]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отклонить досрочный выход? Инвестиция продолжит работать.",
      ok => ok && action("/api/invest_early_decide", {id: +b.dataset.earlyNo, approve: false})));
  document.querySelectorAll("[data-lvl]").forEach(b => b.onclick = () => {
    const [month, level] = b.dataset.lvl.split("|");
    if (b.classList.contains("on")) return;
    tg.showConfirm(`${b.dataset.title}: ${lvlTag(level)} месяц?`, ok => ok && action("/api/invest_rate", {month, level}));
  });
}

/* ---------- формы досрочного выхода ---------- */
function invSheet(html) { $("inv-sheet").innerHTML = html; $("inv-sheet-bg").classList.remove("hidden"); }
function invSheetClose() { $("inv-sheet-bg").classList.add("hidden"); $("inv-sheet").innerHTML = ""; }
$("inv-sheet-bg").onclick = ev => { if (ev.target.id === "inv-sheet-bg") invSheetClose(); };
const sheetBtns = ok => `<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:6px">
  <button class="btn btn-soft" id="is-cancel">Отмена</button><button class="btn btn-main" id="is-ok">${ok}</button></div>`;

function earlyForm(d) {
  invSheet(`<h3>↩️ Досрочный выход из инвестиции</h3>
    <div class="inv-calc">Вам вернут капитал <b>${sum(d.amount)}</b>.<br>
      <span style="color:var(--orange)">Доля прибыли +${sum(d.accrued)} не выплачивается.</span></div>
    <input id="ef-name" placeholder="ФИО полностью" maxlength="100" value="${esc((data.profile && data.profile.name) || "")}">
    <input id="ef-phone" placeholder="Телефон, например +998 90 123 45 67" inputmode="tel" maxlength="30">
    <textarea id="ef-req" rows="3" maxlength="300" placeholder="Реквизиты: номер карты (Uzcard / Humo) или банк и счёт"></textarea>
    ${sheetBtns("Отправить заявку")}`);
  $("is-cancel").onclick = invSheetClose;
  $("is-ok").onclick = () => {
    const name = $("ef-name").value.trim(), phone = $("ef-phone").value.trim(), requisites = $("ef-req").value.trim();
    if (name.length < 3) return toast("Укажите ФИО полностью");
    const digits = phone.replace(/\D/g, "");
    if (digits.length < 9 || digits.length > 15) return toast("Укажите номер телефона");
    if (requisites.length < 4) return toast("Укажите реквизиты");
    invSheetClose();
    action("/api/invest_early", {id: +d.early, name, phone, requisites});
  };
}

/* Сжать фото-отчёт (чек) до 1600 px по большей стороне, JPEG */
function reportJpeg(file) {
  return new Promise((ok, fail) => {
    const img = new Image();
    img.onload = () => {
      const k = Math.min(1, 1600 / Math.max(img.width, img.height));
      const c = document.createElement("canvas"); c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(img.src);
      ok(c.toDataURL("image/jpeg", 0.85));
    };
    img.onerror = () => fail(new Error("Не удалось открыть фото"));
    img.src = URL.createObjectURL(file);
  });
}
function payForm(d) {
  let method = null, photo = null;
  invSheet(`<h3>💸 Возврат капитала (досрочный выход)</h3>
    <div class="hint" style="margin:-8px 0 12px">${d.name} · ${sum(d.amount)}</div>
    <div class="hint" style="font-size:13px;margin-bottom:6px">1. Как оплатили?</div>
    <div class="lvl-btns" style="margin:0 0 14px;grid-template-columns:1fr 1fr"><button data-m="online">💳 Онлайн<span>перевод на карту</span></button>
      <button data-m="offline">💵 Офлайн<span>наличными</span></button></div>
    <div class="hint" style="font-size:13px;margin-bottom:6px">2. Фото-отчёт <span id="pf-hint"></span></div>
    <label class="btn btn-soft" style="display:block;text-align:center;margin-bottom:10px">📷 Выбрать фото
      <input type="file" id="pf-file" accept="image/*" hidden></label>
    <img id="pf-prev" class="hidden" style="width:100%;max-height:260px;object-fit:contain;border-radius:12px;margin-bottom:10px;background:var(--card2)">
    ${sheetBtns("Подтвердить выплату")}`);
  document.querySelectorAll("#inv-sheet [data-m]").forEach(b => b.onclick = () => {
    method = b.dataset.m;
    document.querySelectorAll("#inv-sheet [data-m]").forEach(x => x.classList.toggle("on", x === b));
    $("pf-hint").textContent = method === "online" ? "— чек / скриншот перевода" : "— фото передачи денег";
  });
  $("pf-file").onchange = async ev => {
    const f = ev.target.files[0]; if (!f) return;
    try { photo = await reportJpeg(f); $("pf-prev").src = photo; $("pf-prev").classList.remove("hidden"); }
    catch (e) { toast(e.message); }
  };
  $("is-cancel").onclick = invSheetClose;
  $("is-ok").onclick = () => {
    if (!method) return toast("Выберите: онлайн или офлайн");
    if (!photo) return toast("Прикрепите фото-отчёт");
    invSheetClose();
    toast("Отправляю…");
    action("/api/invest_early_decide", {id: +d.earlyOk, approve: true, method, image: photo});
  };
}

const invAmount = () => +($("inv-amount").value.replace(/\D/g, "")) || 0;
function addMonths(d, m) {
  const r = new Date(d.getFullYear(), d.getMonth() + m, 1);
  r.setDate(Math.min(d.getDate(), new Date(r.getFullYear(), r.getMonth() + 1, 0).getDate()));
  return r;
}
function renderInvCalc() {
  const cfg = data.invest_cfg, a = invAmount();
  if (!a) { $("inv-calc").innerHTML = `Сумма от ${sum(cfg.min)} до ${sum(cfg.max)}`; return; }
  const lo = a * cfg.annual_min / 100 * cfg.term / 12, hi = a * cfg.annual_max / 100 * cfg.term / 12;
  const end = addMonths(new Date(), cfg.term).toLocaleDateString("ru-RU");
  $("inv-calc").innerHTML = `Ожидаемая доля прибыли за ${cfg.term} мес.: <b>+${sum(lo)} – ${sum(hi)}</b><br>
    Ожидается к выплате: <b>${sum(a + lo)} – ${sum(a + hi)}</b> · примерно ${end}<br>
    <span class="hint" style="font-size:12.5px">Прибыль зависит от продаж и не гарантируется</span>` +
    (a < cfg.min ? `<br><span style="color:var(--orange)">Мин. сумма — ${sum(cfg.min)}</span>` : "") +
    (a > cfg.max ? `<br><span style="color:var(--orange)">Макс. сумма — ${sum(cfg.max)}</span>` : "");
}
function setFolderSub(html) { $("inv-folder-sub").innerHTML = html; $("inv-folder-sub-own").innerHTML = html; }
function renderInvestMe() {
  const m = data.invest, cfg = data.invest_cfg;
  const has = m.access === "approved";
  $("inv-access").classList.toggle("hidden", has);
  $("inv-main").classList.toggle("hidden", !has);
  if (!has) {
    $("inv-access-body").innerHTML = `<div class="hint" style="font-size:13.5px;line-height:1.5;margin-bottom:12px">
      Инвестиции в товар компании открываются по заявке. Ваш капитал работает в товаре, а вы получаете долю прибыли от продаж
      (это не вклад и не проценты). Оставьте заявку — руководитель рассмотрит её и откроет доступ.</div>` +
      (m.access === "pending" ? `<div class="inv-calc" style="color:var(--orange)">⏳ Заявка на рассмотрении у руководителя</div>`
       : m.access === "rejected" ? `<div class="inv-calc">Прошлая заявка отклонена — можно подать снова.</div>` : "");
    $("inv-access-form").classList.toggle("hidden", m.access === "pending");
    setFolderSub(m.access === "pending" ? "⏳ Заявка на участие на рассмотрении" : "🙋 Участие по заявке — подайте заявку");
    $("inv-page-sub").textContent = `Инвестиции в товар · по заявке`;
    return;
  }
  const live = m.items.filter(r => ["active", "rating", "matured"].includes(r.status));
  const hist = cfg.history.slice(0, 6);
  $("inv-me").innerHTML = (live.length ? invTiles([[m.invested, "вложено"], ["+" + sum(m.accrued), "доля прибыли", "var(--green)"],
      [m.payout, "капитал + прибыль"]]) : "")
    + (m.items.length ? m.items.slice(0, 10).map(r => invItem(r, false)).join("")
       : `<div class="empty">Инвестируйте в товар компании на ${cfg.term} мес. и получайте долю прибыли от продаж 📈</div>`)
    + (hist.length ? `<div class="inv-h">Как прошли месяцы</div><div class="mchips">${hist.map(h =>
        `<span>${esc(h.title.toLowerCase())} ${lvlTag(h.level)}</span>`).join("")}</div>` : "")
    + (m.earned ? `<div class="hint" style="font-size:13px;margin-top:8px">Всего получено прибыли: <b style="color:var(--green)">${sum(m.earned)}</b></div>` : "");
  $("inv-rules").innerHTML = `Это не вклад и не проценты: ваш капитал работает в товаре компании, а вы получаете долю прибыли от продаж. ` +
    `Каждый месяц руководитель подводит итог продаж — ${cfg.levels.map(l => `${l.emoji} ${l.title.toLowerCase()}`).join(", ")} месяц — от него зависит ваша доля прибыли.<br>` +
    `Деньги передаются руководителю лично, капитал начинает работать со дня подтверждения. Через ${cfg.term} мес. вы получаете капитал и долю прибыли. ` +
    `Досрочный выход — по заявке: возвращается только капитал, доля прибыли не выплачивается.`;
  const pend = m.items.some(r => r.status === "pending"), ready = m.items.some(r => r.status === "matured");
  setFolderSub(live.length
    ? `Вложено ${sum(m.invested)} · <span style="color:var(--green)">+${sum(m.accrued)}</span>${ready ? " · 🎉 к выплате" : ""}`
    : pend ? "⏳ Заявка ждёт руководителя" : `Инвестиции в товар · доля прибыли от продаж`);
  $("inv-page-sub").textContent = `Инвестиция в товар на ${cfg.term} мес. · доля прибыли зависит от итогов продаж каждого месяца`;
  renderInvCalc();
  bindInvest();
}
$("inv-amount").oninput = () => {
  const a = invAmount();
  $("inv-amount").value = a ? a.toLocaleString("ru-RU") : "";
  renderInvCalc();
};
$("ia-plan").oninput = () => { const a = +$("ia-plan").value.replace(/\D/g, "") || 0; $("ia-plan").value = a ? a.toLocaleString("ru-RU") : ""; };
$("ia-send").onclick = () => {
  const plan = $("ia-plan").value.trim(), note = $("ia-note").value.trim();
  tg.showConfirm("Отправить заявку на участие в инвестициях?", ok => ok &&
    action("/api/invest_access", {plan: plan ? plan + " " + data.invest_cfg.currency : "", note})
      .then(() => { $("ia-plan").value = ""; $("ia-note").value = ""; }));
};
$("inv-send").onclick = () => {
  const a = invAmount(), cfg = data.invest_cfg;
  if (!a) return toast("Введите сумму");
  tg.showConfirm(`Инвестировать ${sum(a)} в товар на ${cfg.term} мес.?`, ok => ok &&
    action("/api/invest_request", {amount: a}).then(() => {
      if (data.invest.items.some(r => r.status === "pending")) { $("inv-amount").value = ""; $("inv-form").open = false; }
    }));
};
function lvlButtons(month, title, current) {
  return `<div class="lvl-btns">${data.invest_cfg.levels.map(l => `<button class="${l.key === current ? "on" : ""}"
    data-lvl="${month}|${l.key}" data-title="${esc(title)}">${l.emoji} ${esc(l.title)}<span>месяц</span></button>`).join("")}</div>`;
}
function renderInvestAdmin() {
  const o = data.invest_admin;
  const block = (title, rows) => rows.length ? `<div class="inv-h">${title}</div>` + rows.map(r => invItem(r, true)).join("") : "";
  const any = o.pending.length + o.matured.length + o.active.length + o.paid.length + o.access.length;
  let rate = "";
  if (o.to_rate.length) {
    rate = o.to_rate.map(t => `<div class="lvl-row"><b style="color:var(--orange)">⚠️ Оцените: ${esc(t.title.toLowerCase())}</b>
      ${lvlButtons(t.month, t.title, null)}</div>`).join("");
  } else if (o.last_rated) {
    rate = `<div class="lvl-row"><b>${esc(o.last_rated.title)}</b> <span class="hint" style="font-size:12.5px">· можно изменить</span>
      ${lvlButtons(o.last_rated.month, o.last_rated.title, o.last_rated.level)}</div>`;
  }
  if (!o.current.level && !o.to_rate.some(t => t.month === o.current.month))
    rate += `<div class="hint" style="font-size:12.5px;margin-top:6px">📅 ${esc(o.current.title)} можно будет оценить ${o.current.opens}</div>`;
  $("inv-admin").innerHTML = `<div class="inv-calc" style="margin-bottom:12px"><b style="color:var(--text)">📊 Итог месяца</b>${rate}</div>`
    + invTiles([[o.invested, "вложено"], ["+" + sum(o.accrued), "доля прибыли", "var(--green)"],
      [`${sum(o.obligations_min)} –<br>${sum(o.obligations_max)}`, "ожидается к выплате", "var(--orange)"]])
    + (o.access.length ? `<div class="inv-h">🙋 Заявки на участие (${o.access.length})</div>` + o.access.map(x => `
      <div class="inv"><div class="inv-top"><b>${esc(x.name)}</b><span class="chip pending">⏳ ждёт</span></div>
        <div class="inv-sub hint"><span>подана ${x.created}</span>${x.plan_amount ? `<span>планирует: ${esc(x.plan_amount)}</span>` : ""}
          ${x.note ? `<span style="width:100%;color:var(--text)">💬 ${esc(x.note)}</span>` : ""}</div>
        <div class="inv-act"><button class="btn btn-main btn-sm" data-acc-ok="${x.user_id}">✅ Открыть доступ</button>
          <button class="btn btn-soft btn-sm" data-acc-no="${x.user_id}">✕ Отклонить</button></div></div>`).join("") : "")
    + block(`⏳ Ждут подтверждения (${o.pending.length})`, o.pending)
    + block(`↩️ Досрочный выход (${o.early.length})`, o.early)
    + block(`🎉 Пора выплатить — ${sum(o.to_pay_now)}`, o.matured)
    + block(`📈 Работают (${o.active.filter(r => !r.early).length})`, o.active.filter(r => !r.early))
    + block("✔️ Выплачены", o.paid)
    + (any ? "" : `<div class="empty">Инвестиций пока нет</div>`);
  const todo = o.pending.length + o.matured.length + o.to_rate.length + o.early.length + o.access.length;
  $("inv-admin-badge").innerHTML = todo ? `<span class="f-badge">${todo}</span>` : "";
  $("inv-admin-folder-sub").textContent = [
    o.to_rate.length ? "⚠️ оцените месяц" : "",
    o.access.length ? `на участие: ${o.access.length}` : "",
    o.pending.length ? `заявок: ${o.pending.length}` : "",
    o.early.length ? `досрочно: ${o.early.length}` : "",
    o.matured.length ? `к выплате: ${o.matured.length}` : "",
    `вложено ${sum(o.invested)}`].filter(Boolean).join(" · ");
  bindInvest();
}

/* ---------- мои проекты (руководитель) ---------- */
let curProject = null, ntDue = "";
const P = () => data.projects;
const isoDay = add => { const d = new Date(); d.setDate(d.getDate() + add);
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); };
const projAct = body => action("/api/proj", body);

function ptaskRow(x, withProject) {
  const meta = [];
  if (withProject) meta.push(`📁 ${esc(x.project)}`);
  if (x.due_label) meta.push(`<span class="${x.overdue ? "late" : "dl"}">${x.overdue ? "⏰ просрочено · " : "📅 "}${esc(x.due_label)}</span>`);
  else if (!x.done) meta.push("без даты");
  if (x.note) meta.push("📝 заметка");
  return `<div class="ptask ${x.done ? "done" : ""}">
    <button class="pcheck" data-pdone="${x.id}" data-v="${x.done ? 0 : 1}">${x.done ? "✓" : ""}</button>
    <div class="pbody" data-pedit="${x.id}"><div class="pt">${esc(x.title)}</div>
      ${meta.length ? `<div class="pmeta">${meta.map(m => `<span>${m}</span>`).join("")}</div>` : ""}</div>
    ${!x.done && x.due_date !== P().today_iso ? `<button class="btn btn-soft btn-sm take-today" data-ptoday="${x.id}">☀️ На сегодня</button>` : ""}</div>`;
}
function bindPtasks() {
  document.querySelectorAll("[data-pdone]").forEach(b => b.onclick = () => projAct({action: "done", id: +b.dataset.pdone, done: b.dataset.v === "1"}));
  document.querySelectorAll("[data-pedit]").forEach(b => b.onclick = () => editPtask(+b.dataset.pedit));
  document.querySelectorAll("[data-ptoday]").forEach(b => b.onclick = () => projAct({action: "plan", id: +b.dataset.ptoday, when: "today"}));
}
function renderProjects() {
  const o = P(), d = o.day;
  const todo = d.today.length + d.overdue.length;
  $("proj-badge").innerHTML = todo ? `<span class="f-badge">${todo}</span>` : "";
  $("proj-folder-sub").textContent = `${o.projects.length} ${plural(o.projects.length, "проект", "проекта", "проектов")}` +
    ` · на сегодня: ${d.today.length}` + (d.overdue.length ? ` · просрочено: ${d.overdue.length}` : "");
  $("day-title").textContent = `☀️ Мой день · ${o.today}`;
  const sec = (title, rows) => rows.length ? `<div class="day-h">${title}</div>` + rows.map(x => ptaskRow(x, true)).join("") : "";
  $("proj-day").innerHTML = (todo || d.done_today.length || d.upcoming.length)
    ? sec(`<span style="color:var(--red)">🔴 Просрочено (${d.overdue.length})</span>`, d.overdue)
      + sec(`📅 Сегодня (${d.today.length})`, d.today)
      + sec(`✅ Сделано сегодня`, d.done_today)
      + sec(`🗓 Ближайшие 7 дней`, d.upcoming)
    : `<div class="empty">На сегодня задач нет. Поставьте дату задаче в проекте — она появится здесь.</div>`;
  $("proj-count").textContent = o.projects.length ? `всего: ${o.projects.length}` : "";
  $("proj-list").innerHTML = o.projects.length ? o.projects.map(p => `
    <div class="prow" data-popen="${p.id}"><div class="f-ic">📁</div>
      <div style="flex:1;min-width:0"><b style="word-break:break-word">${esc(p.title)}</b>
        <div class="pmeta"><span>задач: ${p.open}</span>${p.undated ? `<span>без даты: ${p.undated}</span>` : ""}
          ${p.overdue ? `<span class="late">просрочено: ${p.overdue}</span>` : ""}${p.done ? `<span>сделано: ${p.done}</span>` : ""}</div></div>
      <div class="f-arrow" style="font-size:24px;color:var(--hint)">›</div></div>`).join("")
    : `<div class="empty">Проектов пока нет — создайте первый</div>`;
  document.querySelectorAll("[data-popen]").forEach(r => r.onclick = () => { curProject = +r.dataset.popen; renderProject(); show("project"); });
  if (curProject && !o.projects.some(p => p.id === curProject)) { curProject = null; if (view === "project") view = "projects"; }
  if (curProject) renderProject();
  bindPtasks();
}
function renderProject() {
  const p = P().projects.find(x => x.id === curProject);
  if (!p) return;
  const all = P().tasks.filter(x => x.project_id === p.id);
  const dated = all.filter(x => !x.done && x.due_date).sort((a, b) => a.due_date < b.due_date ? -1 : 1);
  const undated = all.filter(x => !x.done && !x.due_date), done = all.filter(x => x.done);
  $("pj-title").textContent = "📁 " + p.title;
  $("pj-sub").textContent = `Задач: ${p.open} · с датой: ${dated.length} · без даты: ${undated.length}`;
  $("pj-tasks").innerHTML = (dated.length ? `<div class="day-h">📅 С датой (${dated.length})</div>` + dated.map(x => ptaskRow(x)).join("") : "")
    + (undated.length ? `<div class="day-h">📝 Без даты — в работе (${undated.length})</div>` + undated.map(x => ptaskRow(x)).join("") : "")
    + (done.length ? `<details class="inv-months"><summary>✅ Выполнено (${done.length})</summary>${done.map(x => ptaskRow(x)).join("")}</details>` : "")
    + (all.length ? "" : `<div class="empty">Задач пока нет</div>`);
  bindPtasks();
}
function setQuick(q) {
  document.querySelectorAll("#nt-quick button").forEach(b => b.classList.toggle("on", b.dataset.q === q));
  $("nt-date").classList.toggle("hidden", q !== "pick");
  ntDue = q === "" ? "" : q === "pick" ? ($("nt-date").value || "") : isoDay(+q);
}
document.querySelectorAll("#nt-quick button").forEach(b => b.onclick = () => setQuick(b.dataset.q));
$("nt-date").onchange = () => { ntDue = $("nt-date").value; };
setQuick("");
$("nt-add").onclick = () => {
  const title = $("nt-title").value.trim();
  if (!title) return toast("Введите задачу");
  projAct({action: "add_task", id: curProject, title, due: ntDue}).then(() => { $("nt-title").value = ""; });
};
$("np-add").onclick = () => {
  const title = $("np-title").value.trim();
  if (!title) return toast("Введите название проекта");
  projAct({action: "add_project", title}).then(() => { $("np-title").value = ""; });
};
$("pj-rename").onclick = () => {
  const p = P().projects.find(x => x.id === curProject); if (!p) return;
  invSheet(`<h3>✏️ Название проекта</h3><input id="pr-title" maxlength="100" value="${esc(p.title)}">${sheetBtns("Сохранить")}`);
  $("is-cancel").onclick = invSheetClose;
  $("is-ok").onclick = () => { const title = $("pr-title").value.trim(); if (!title) return toast("Введите название");
    invSheetClose(); projAct({action: "rename_project", id: p.id, title}); };
};
$("pj-delete").onclick = () => {
  const p = P().projects.find(x => x.id === curProject); if (!p) return;
  tg.showConfirm(`Удалить проект «${p.title}» со всеми задачами?`, ok => ok &&
    projAct({action: "delete_project", id: p.id}).then(() => show("projects")));
};
function editPtask(id) {
  const x = P().tasks.find(t => t.id === id); if (!x) return;
  invSheet(`<h3>📝 Задача</h3>
    <div class="hint" style="font-size:12.5px;margin:-8px 0 10px">📁 ${esc(x.project)}</div>
    <input id="et-title" maxlength="300" value="${esc(x.title)}">
    <textarea id="et-note" rows="5" maxlength="3000" placeholder="Заметки, детали, идеи…">${esc(x.note || "")}</textarea>
    <div class="hint" style="font-size:13px;margin-bottom:6px">Дедлайн</div>
    <div class="quick" id="et-quick" style="margin:0 0 10px">
      <button data-d="${P().today_iso}">☀️ Сегодня</button><button data-d="${P().tomorrow_iso}">Завтра</button>
      <button data-d="">📝 Без даты</button></div>
    <input type="date" id="et-due" value="${x.due_date || ""}">
    ${sheetBtns("Сохранить")}
    <button class="link" id="et-del" style="color:var(--red);display:block;margin:14px auto 0">Удалить задачу</button>`);
  const qd = () => document.querySelectorAll("#et-quick [data-d]").forEach(b => b.classList.toggle("on", b.dataset.d === $("et-due").value));
  document.querySelectorAll("#et-quick [data-d]").forEach(b => b.onclick = () => { $("et-due").value = b.dataset.d; qd(); });
  $("et-due").oninput = qd; qd();
  $("is-cancel").onclick = invSheetClose;
  $("is-ok").onclick = () => { const title = $("et-title").value.trim(); if (!title) return toast("Введите задачу");
    const body = {action: "update_task", id, title, note: $("et-note").value, due: $("et-due").value};
    invSheetClose(); projAct(body); };
  $("et-del").onclick = () => tg.showConfirm("Удалить задачу?", ok => { if (ok) { invSheetClose(); projAct({action: "delete_task", id}); } });
}

/* ---------- текст на трёх языках (uz / ru / en) ---------- */
const LANG_TABS = [["uz", "🇺🇿 UZ"], ["ru", "🇷🇺 RU"], ["en", "🇬🇧 EN"]];
function langEditor(boxId, rows, max, ph, onInput) {
  const box = $(boxId); let cur = "uz";
  box.innerHTML = `<div class="ltabs">${LANG_TABS.map(([k, t]) => `<button data-lt="${k}">${t}</button>`).join("")}</div>` +
    LANG_TABS.map(([k]) => `<textarea data-lta="${k}" rows="${rows}" maxlength="${max}" placeholder="${esc(ph[k])}"></textarea>`).join("");
  const ta = k => box.querySelector(`[data-lta="${k}"]`);
  const paint = () => {
    box.querySelectorAll("[data-lt]").forEach(b => {
      const k = b.dataset.lt; b.classList.toggle("on", k === cur);
      b.innerHTML = LANG_TABS.find(x => x[0] === k)[1] + (ta(k).value.trim() ? `<span class="ok">✓</span>` : "");
    });
    box.querySelectorAll("[data-lta]").forEach(t => t.classList.toggle("hidden", t.dataset.lta !== cur));
  };
  box.querySelectorAll("[data-lt]").forEach(b => b.onclick = () => { cur = b.dataset.lt; paint(); ta(cur).focus(); onInput && onInput(); });
  box.querySelectorAll("[data-lta]").forEach(t => t.oninput = () => { paint(); onInput && onInput(); });
  paint();
  return {
    texts: () => Object.fromEntries(LANG_TABS.map(([k]) => [k, ta(k).value.trim()]).filter(([, v]) => v)),
    current: () => ta(cur),
    clear: () => { box.querySelectorAll("[data-lta]").forEach(t => t.value = ""); cur = "uz"; paint(); },
  };
}

/* ---------- постинг ---------- */
let poPhoto = null, poWhen = "now";
const poAud = new Set(["clients"]);  // по умолчанию — все в боте
let poBotOpen = false;
/* «Где опубликовать»: бот — раскрывающийся список (все или группа клиентов), ниже — группы и каналы */
function renderAud() {
  const a = data.posting.audience;
  const botOpts = [["clients", "🤖 Все в боте", a.clients], ...(a.segments || []).map(s => [s.key, s.title, s.count])];
  const cur = botOpts.find(o => poAud.has(o[0]));
  $("po-aud").innerHTML = `<div class="po-bot">
      <button class="po-bot-head ${cur ? "on" : ""}" id="po-bot-head"><span>🤖 Бот XUPING Bonus</span>
        <span class="po-bot-cur">${cur ? esc(cur[1].replace("🤖 ", "")) + " · " + cur[2] : "не отправлять"}
          <span class="po-caret" style="transform:rotate(${poBotOpen ? 180 : 0}deg)">▾</span></span></button>
      <div class="po-bot-list ${poBotOpen ? "" : "hidden"}">
        ${botOpts.map(([k, t, n]) => `<button data-bot="${k}" class="${poAud.has(k) ? "on" : ""}"><span>${poAud.has(k) ? "✓ " : ""}${esc(t)}</span><span class="hint">${n}</span></button>`).join("")}
        <button data-bot="" class="${cur ? "" : "on"}"><span>✕ Не отправлять в бот</span></button></div></div>` +
    a.chats.map(c => `<button data-aud="chat:${c.id}" class="${poAud.has("chat:" + c.id) ? "on" : ""}">📢 ${esc(c.title)}</button>`).join("");
  $("po-bot-head").onclick = () => { poBotOpen = !poBotOpen; renderAud(); };
  document.querySelectorAll("[data-bot]").forEach(b => b.onclick = () => {
    botOpts.forEach(o => poAud.delete(o[0]));
    if (b.dataset.bot) poAud.add(b.dataset.bot);
    poBotOpen = false; renderAud();
  });
  document.querySelectorAll("#po-aud [data-aud]").forEach(b => b.onclick = () => {
    poAud.has(b.dataset.aud) ? poAud.delete(b.dataset.aud) : poAud.add(b.dataset.aud);
    b.classList.toggle("on", poAud.has(b.dataset.aud));
  });
}
function renderPosting() {
  const a = data.posting.audience, h = data.posting.history;
  $("po-bonus-row").classList.toggle("hidden", !a.bonus_on);
  renderAud();
  const cl = a.client_langs || {};
  $("po-lang-hint").textContent = `Клиенты по языкам: UZ ${cl.uz || 0} · RU ${cl.ru || 0} · EN ${cl.en || 0}`;
  const planned = h.filter(p => p.status === "scheduled").length;
  $("posting-folder-sub").textContent = `клиентов: ${a.clients} · групп: ${a.chats.length}` +
    (planned ? ` · запланировано: ${planned}` : h.length ? ` · постов: ${h.length}` : "");
  $("po-history").innerHTML = h.length ? h.map(p => {
    const st = p.status === "scheduled" ? `<span class="chip pending">🕒 ${p.when}</span>`
      : p.queued ? `<span class="chip active">📤 отправляется</span>`
      : p.failed && !p.delivered ? `<span class="chip pending" style="color:var(--red)">⚠️ не доставлен</span>`
      : `<span class="chip matured">✅ доставлен${p.failed ? " частично" : ""}</span>`;
    const stats = p.status === "scheduled" ? "" : `<span>доставлено: ${p.delivered} из ${p.total}</span>` +
      (p.failed ? `<span class="late">не доставлено: ${p.failed}</span>` : "") + (p.queued ? `<span>в очереди: ${p.queued}</span>` : "");
    return `<div class="po-post"><div class="inv-top"><span class="hint" style="font-size:12.5px">${p.status === "scheduled" ? "Запланирован" : p.when}${p.has_photo ? " · 📷" : ""}${p.button_text ? " · 🔗" : ""}<span class="lang-badges">${(p.langs || []).map(l => `<span>${l.toUpperCase()}</span>`).join("")}</span></span>${st}</div>
      <div class="pt">${esc(p.text || "(только фото)")}</div>
      <div class="pmeta"><span>→ ${esc(p.audience_titles.join(", "))}</span>${stats}</div>
      ${p.bonus ? `<div class="pmeta"><span style="color:var(--green)">🎁 бонус ${Math.round(p.bonus).toLocaleString("ru-RU")} сум · начислено ${p.bonus_ok} из ${p.bonus_total}</span>${p.bonus_wait ? `<span>в процессе: ${p.bonus_wait}</span>` : ""}${p.bonus_fail ? `<span class="late">не удалось: ${p.bonus_fail}</span>` : ""}</div>` : ""}
      ${p.status === "scheduled" ? `<div class="inv-act"><button class="btn btn-soft btn-sm" data-pocancel="${p.id}">Отменить</button></div>` : ""}</div>`;
  }).join("") : `<div class="empty">Постов пока нет</div>`;
  document.querySelectorAll("[data-pocancel]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отменить запланированный пост?", ok => ok && action("/api/post", {action: "cancel", id: +b.dataset.pocancel})));
}
const poBonus = () => +($("po-bonus").value || "").replace(/[^\d]/g, "") || 0;
$("po-bonus").oninput = () => { const v = poBonus(); $("po-bonus").value = v ? v.toLocaleString("ru-RU") : ""; };
const poCount = () => { const n = poEd.current().value.length, max = poPhoto ? 1024 : 4000;
  $("po-count").innerHTML = `<span style="color:${n > max ? "var(--red)" : "inherit"}">${n} / ${max}</span>`; };
const poEd = langEditor("po-langs", 6, 4000, {uz: "Post matni (o'zbekcha)…", ru: "Текст поста (по-русски)…",
  en: "Post text (English)…"}, () => poCount());
$("po-file").onchange = async ev => {
  const f = ev.target.files[0]; ev.target.value = ""; if (!f) return;
  try { poPhoto = await reportJpeg(f); $("po-prev").src = poPhoto; $("po-photo-box").classList.remove("hidden");
        $("po-photo-btn").classList.add("hidden"); poCount(); } catch (e) { toast(e.message); }
};
$("po-photo-del").onclick = () => { poPhoto = null; $("po-photo-box").classList.add("hidden"); $("po-photo-btn").classList.remove("hidden"); poCount(); };
document.querySelectorAll("#po-when button").forEach(b => b.onclick = () => {
  poWhen = b.dataset.w;
  document.querySelectorAll("#po-when button").forEach(x => x.classList.toggle("on", x === b));
  $("po-at").classList.toggle("hidden", poWhen !== "later");
});
$("po-btn-channel").onclick = () => {
  const u = data.client_bot || (data.posting && data.posting.client_bot);
  if (!u) return toast("Бот клиентов ещё не запущен — кнопка недоступна");
  $("po-btn-text").value = "📢 Kanal"; $("po-btn-url").value = `https://t.me/${u}?start=channel`;
  toast("Кнопка «Kanal» добавлена: клиент получит личную ссылку-заявку в канал");
};
function poBody() {
  return {texts: poEd.texts(), image: poPhoto || "", button_text: $("po-btn-text").value.trim(),
          button_url: $("po-btn-url").value.trim()};
}
function poReset() {
  poEd.clear(); $("po-btn-text").value = ""; $("po-btn-url").value = ""; $("po-at").value = "";
  $("po-news").checked = false; poAud.clear(); poAud.add("clients"); poBotOpen = false; renderAud(); $("po-bonus").value = ""; $("po-photo-del").onclick(); poCount();
}
$("po-send").onclick = () => {
  const b = poBody();
  if (!Object.keys(b.texts).length && !b.image) return toast("Напишите текст или добавьте фото");
  if (!poAud.size) return toast("Выберите, кому отправить");
  if (poWhen === "later" && !$("po-at").value) return toast("Выберите дату и время");
  const names = [$("po-bot-head").classList.contains("on") ? "🤖 " + $("po-bot-head").querySelector(".po-bot-cur").textContent.replace("▾", "").trim() : "",
    ...[...document.querySelectorAll("#po-aud [data-aud].on")].map(x => x.textContent)].filter(Boolean).join(", ");
  const when = poWhen === "later" ? `\nКогда: ${$("po-at").value.replace("T", " ")}` : "\nОтправить сейчас.";
  const filled = Object.keys(b.texts), missing = LANG_TABS.map(x => x[0]).filter(k => !filled.includes(k));
  const langs = `\nЯзыки: ${filled.map(k => k.toUpperCase()).join(", ") || "—"}` +
    (missing.length && filled.length ? ` (без ${missing.map(k => k.toUpperCase()).join(", ")} — получат на ${filled.includes("uz") ? "UZ" : filled[0].toUpperCase()})` : "");
  const bonus = poBonus(), aud = data.posting.audience;
  const reach = [...poAud].reduce((n, k) => n + (k === "clients" ? aud.clients : ((aud.segments || []).find(s => s.key === k) || {}).count || 0), 0);
  const bonusLine = bonus ? `\n🎁 Бонус: ${bonus.toLocaleString("ru-RU")} сум каждому клиенту (до ${reach} чел., итого до ${(bonus * reach).toLocaleString("ru-RU")} сум)` : "";
  if (bonus > aud.bonus_max) return toast(`Бонус — не больше ${aud.bonus_max.toLocaleString("ru-RU")} сум`);
  tg.showConfirm(`Опубликовать пост?\nКому: ${names}${when}${langs}${bonusLine}`, ok => {
    if (!ok) return;
    toast("Отправляю…");
    api("/api/post", Object.assign({action: "create", audience: [...poAud], to_news: $("po-news").checked, bonus: poBonus(),
        send_at: poWhen === "later" ? $("po-at").value : ""}, b))
      .then(r => { toast(r.msg); if (r.ok) poReset(); return load(); }).catch(e => toast(e.message));
  });
};
poCount();

/* ---------- заработок ---------- */
const money0 = (n, cur) => Math.round(n || 0).toLocaleString("ru-RU") + " " + cur;
function earnRows(items, cur, admin) {
  return items.length ? items.map(i => `<div class="pen"><div class="tt"><b>${esc(i.title)}</b>${i.note ? `<div class="hint" style="font-size:12.5px">${esc(i.note)}</div>` : ""}</div>
      <b style="color:var(--green);white-space:nowrap">+${money0(i.amount, cur)}</b>
      ${admin ? `<button class="btn btn-soft btn-sm" data-delearn="${i.id}">✕</button>` : ""}</div>`).join("") : "";
}
/* вид списка: по месяцам (суммы за месяц, внутри — периоды) или по периодам (каждая запись) */
let earnView = "months";
function earnSwitch(id, t) {
  return `<div class="seg" style="margin:0 0 10px" id="${id}"><button data-ev="months" class="${earnView === "months" ? "on" : ""}">${t.months}</button>
    <button data-ev="periods" class="${earnView === "periods" ? "on" : ""}">${t.periods}</button></div>`;
}
function bindEarnSwitch(id, redraw) {
  document.querySelectorAll(`#${id} [data-ev]`).forEach(b => b.onclick = () => { earnView = b.dataset.ev; redraw(); });
}
function earnMonths(months, cur, t) {
  return months.length ? months.map(m => `<details class="emonth"><summary class="pen" style="cursor:pointer;list-style:none">
      <div class="tt"><b>${esc(m.title)}</b><div class="hint" style="font-size:12.5px">${t.records(m.parts.length)}</div></div>
      <b style="color:var(--green);white-space:nowrap">${money0(m.amount, cur)}</b></summary>
      <div style="padding:0 0 6px 12px;border-left:2px solid var(--line);margin-left:4px">${m.parts.map(p => `<div class="pmeta" style="justify-content:space-between;margin:4px 0">
        <span>${esc(p.title)}${p.note ? " · " + esc(p.note) : ""}${p.split ? ` <i>(${t.part} ${money0(p.full, cur)})</i>` : ""}</span>
        <span>+${money0(p.amount, cur)}</span></div>`).join("")}</div></details>`).join("") : "";
}
const EARN_T = {
  ru: {months: "По месяцам", periods: "По периодам", part: "часть от", records: n => `${n} ${plural(n, "запись", "записи", "записей")}`},
  uz: {months: "Oylar bo'yicha", periods: "Davrlar bo'yicha", part: "qismi:", records: n => `${n} ta yozuv`},
};
function renderEarn() {
  const e = data.earnings, cur = sx("cur"), T = EARN_T[meLang()];
  $("earn-folder-sub").textContent = sx("earn_folder", {s: money0(e.total, cur)});
  $("earn-tiles").innerHTML = `<div><b style="color:var(--green)">${money0(e.total, cur)}</b><span>${sx("earn_total")}</span></div>
    <div><b>${money0(e.year, cur)}</b><span>${sx("earn_year")}</span></div>`;
  $("earn-list").innerHTML = earnSwitch("earn-sw", T) +
    ((earnView === "months" ? earnMonths(e.months, cur, T) : earnRows(e.items, cur, false)) || `<div class="empty">${sx("earn_empty")}</div>`);
  bindEarnSwitch("earn-sw", renderEarn);
}
function renderEarnAdmin() {
  const t = data.earnings_team, total = t.reduce((a, x) => a + x.total, 0);
  $("earn-admin-sub").textContent = `всего выплачено: ${money0(total, "сум")}`;
  $("earn-team").innerHTML = t.map(x => `<div class="prow" data-earnemp="${x.id}">
      <div class="f-ic" style="background:rgba(52,211,153,.14)">💵</div>
      <div style="flex:1;min-width:0"><b>${esc(x.name)}</b><div class="pmeta"><span>записей: ${x.items.length}</span>
        ${x.items.length ? `<span>посл.: ${esc(x.items[0].title)}</span>` : ""}</div></div>
      <b style="color:var(--green);white-space:nowrap">${money0(x.total, "сум")}</b></div>`).join("");
  document.querySelectorAll("[data-earnemp]").forEach(r => r.onclick = () => earnSheet(+r.dataset.earnemp));
  if (earnOpen && !$("inv-sheet-bg").classList.contains("hidden")) earnSheet(earnOpen, true);
}
let earnOpen = null;
function earnSheet(uid, keep) {
  const x = data.earnings_team.find(e => e.id === uid); if (!x) return;
  earnOpen = uid;
  const prev = keep ? {a: $("ea-amount") && $("ea-amount").value, f: $("ea-from") && $("ea-from").value,
                       t: $("ea-to") && $("ea-to").value, n: $("ea-note") && $("ea-note").value} : {};
  const d = new Date(), p2 = n => String(n).padStart(2, "0");
  const today = `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`, first = today.slice(0, 8) + "01";
  invSheet(`<h3>💵 ${esc(x.name)}</h3>
    <div class="inv-tiles" style="grid-template-columns:1fr 1fr"><div><b style="color:var(--green)">${money0(x.total, "сум")}</b><span>за всё время</span></div>
      <div><b>${money0(x.year, "сум")}</b><span>в этом году</span></div></div>
    <input id="ea-amount" inputmode="numeric" placeholder="Сумма, сум" maxlength="15">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <label class="hint" style="font-size:12.5px">с<input type="date" id="ea-from" value="${first}"></label>
      <label class="hint" style="font-size:12.5px">по<input type="date" id="ea-to" value="${today}"></label></div>
    <input id="ea-note" maxlength="200" placeholder="Комментарий: Foyda, Bonus…">
    <div class="quick" id="ea-quick" style="margin:-4px 0 10px">${["Foyda", "Bonus", "Premiya"].map(w =>
      `<button data-note="${w}">${w}</button>`).join("")}</div>
    <button class="btn btn-main" style="width:100%" id="ea-add">Добавить</button>
    <div style="margin-top:12px">${earnSwitch("earn-sw-a", EARN_T.ru)}
      ${(earnView === "months" ? earnMonths(x.months, "сум", EARN_T.ru) : earnRows(x.items, "сум", true)) || '<div class="empty">Записей пока нет</div>'}
      ${earnView === "months" && x.items.length ? '<div class="hint" style="font-size:12px;margin-top:6px">Удалить запись — во вкладке «По периодам».</div>' : ""}</div>
    <button class="btn btn-soft" style="width:100%;margin-top:10px" id="is-cancel">Закрыть</button>`);
  if (keep) { $("ea-amount").value = prev.a || ""; if (prev.f) $("ea-from").value = prev.f; if (prev.t) $("ea-to").value = prev.t;
              $("ea-note").value = prev.n || ""; }
  document.querySelectorAll("#ea-quick [data-note]").forEach(b => b.onclick = () => {
    $("ea-note").value = b.dataset.note;
    document.querySelectorAll("#ea-quick [data-note]").forEach(x => x.classList.toggle("on", x === b));
  });
  $("ea-amount").oninput = () => { const v = +$("ea-amount").value.replace(/\D/g, "") || 0; $("ea-amount").value = v ? v.toLocaleString("ru-RU") : ""; };
  $("is-cancel").onclick = () => { earnOpen = null; invSheetClose(); };
  $("ea-add").onclick = () => {
    const amount = $("ea-amount").value.replace(/\D/g, "");
    if (!amount) return toast("Введите сумму");
    const date_from = $("ea-from").value, date_to = $("ea-to").value;
    if (!date_from || !date_to) return toast("Выберите период: с … по …");
    if (date_from > date_to) return toast("Дата «с» позже даты «по»");
    action("/api/earning", {user_id: uid, amount, date_from, date_to, note: $("ea-note").value.trim()})
      .then(() => { $("ea-amount").value = ""; $("ea-note").value = ""; earnSheet(uid, true); });
  };
  bindEarnSwitch("earn-sw-a", () => earnSheet(uid, true));
  document.querySelectorAll("#inv-sheet [data-delearn]").forEach(b => b.onclick = () =>
    tg.showConfirm("Удалить запись?", ok => ok && action("/api/earning", {action: "delete", id: +b.dataset.delearn})
      .then(() => earnSheet(uid, true))));
}

let openedShop = false;
const splashT0 = Date.now();
function hideSplash(fast = false) {
  const s = document.getElementById("splash"); if (!s || s.classList.contains("out")) return;
  if (fast) { s.style.transition = "opacity .25s ease, visibility .25s"; s.classList.add("out"); return; }  // клиентам — сразу
  setTimeout(() => s.classList.add("out"), Math.max(0, 700 - (Date.now() - splashT0)));  // сотрудникам — минимум ~0.7 с
}
setTimeout(hideSplash, 6000);  // на всякий случай: не держать заставку дольше 8 секунд
/* Надписи кабинета (руководитель меняет кнопкой ✏️ на главной) */
const BRAND_DEF = {title: "RADI TEAM", sub: () => IS_APP ? "приложение" : "мини-приложение", welcome: "Добро пожаловать!",
                   welcome_sub: "Ваш личный кабинет команды RADI"};
function applyBranding() {
  const b = (data && data.branding) || {};
  $("br-title").textContent = b.title || BRAND_DEF.title;
  $("br-sub").textContent = b.sub || BRAND_DEF.sub();
  $("wl-title").textContent = b.welcome || BRAND_DEF.welcome;
  $("wl-sub").textContent = b.welcome_sub || BRAND_DEF.welcome_sub;
  $("wl-edit").classList.toggle("hidden", !(data && data.is_admin));
}
function brandSheet() {
  const b = data.branding || {};
  invSheet(`<h3>✏️ Надписи кабинета</h3><div class="hint" style="font-size:12.5px;margin:-6px 0 10px">Пустое поле — вернуть как было. Видят все сотрудники.</div>
    <input id="bs-title" type="hidden" value="${esc(b.title || "")}">
    <div class="f-lbl" style="margin-top:0">Подпись под логотипом</div><input id="bs-sub" maxlength="60" value="${esc(b.sub || "")}" placeholder="${BRAND_DEF.sub()}">
    <div class="f-lbl">Приветствие на главной</div><input id="bs-wl" maxlength="60" value="${esc(b.welcome || "")}" placeholder="${BRAND_DEF.welcome}">
    <div class="f-lbl">Текст под приветствием</div><input id="bs-wls" maxlength="120" value="${esc(b.welcome_sub || "")}" placeholder="${BRAND_DEF.welcome_sub}">
    ${sheetBtns("Сохранить")}`);
  $("is-cancel").onclick = invSheetClose;
  $("is-ok").onclick = () => { const body = {title: $("bs-title").value, sub: $("bs-sub").value, welcome: $("bs-wl").value, welcome_sub: $("bs-wls").value};
    invSheetClose(); action("/api/branding", body); };
}
async function load(fresh = false) {
  try {
    data = await api("/api/me", fresh ? {fresh: true} : {});
    $("loading").classList.add("hidden");
    $("error").classList.add("hidden");  // прошлая ошибка больше не актуальна
    if (data.is_client) { renderClient(); show("client"); hideSplash(true); return; }
    applyBranding(); hideSplash();
    if (data.is_employee) { renderMe(); renderEarn(); renderKpiMe(); renderAssign(); }
    $("go-shop-me").classList.toggle("hidden", !(data.shop_access && data.is_employee));
    $("go-shop").classList.toggle("hidden", !data.is_admin);
    SUBPAGES.shop = data.is_admin && !data.is_employee ? "team" : (view === "shop" && SUBPAGES.shop) || (data.is_employee ? "me" : "team");
    if (data.invest) renderInvestMe();
    // руководитель без кабинета сотрудника открывает свои инвестиции с вкладки «Команда»
    $("go-inv-own").classList.toggle("hidden", !(data.invest_enabled && data.is_admin && !data.is_employee));
    if (!data.invest_enabled) investSoon();
    SUBPAGES.invest = data.is_employee ? "me" : "team";
    if (data.is_admin) { renderTeam(); renderNewsAdmin(); renderClientsAdmin(); if (data.invest_admin) renderInvestAdmin();
                         if (data.projects) renderProjects();
                         if (data.earnings_team) renderEarnAdmin(); renderDash(); }
    if (data.posting) renderPosting();
    $("go-posting-me").classList.toggle("hidden", !(data.posting && !data.is_admin));
    SUBPAGES.posting = data.is_admin ? "team" : "me";
    if (!data.is_employee && view === "me") view = "team";
    if (!openedShop && new URLSearchParams(location.search).get("open") === "shop" && data.shop_access) {
      openedShop = true; view = "shop"; shopTab = data.is_admin ? "home" : "sell"; show(view); loadShop(); return;
    }
    openedShop = true;
    if (appHiddenSec(APP_SEC_OF[view] || view)) view = data.is_employee ? "me" : "team";  // раздел скрыли в приложении
    const y = window.scrollY;
    show(view, true);  // тот же экран после действия / автообновления — страница остаётся на месте
    window.scrollTo(0, y);
  } catch (e) {
    hideSplash();
    if (IS_APP && /Telegram|сотрудник|Только/.test(e.message)) { appToken.set(""); return appLogin(); }  // сессия кончилась
    $("loading").classList.add("hidden");
    $("error").classList.remove("hidden");
    $("error").textContent = e.message;
  }
}

/* Вход в приложение через бот сотрудников */
async function appLogin() {
  hideSplash();
  $("loading").classList.remove("hidden");
  $("loading").innerHTML = `<div style="text-align:center;padding:30px 10px">
    <img src="/static/radi-logo.png" alt="RADI &amp; CO." style="width:78%;max-width:320px;margin:10px auto 18px;display:block;animation:spIn 1s ease both">
    <div class="hint" style="margin:6px 0 22px">Kirish / Вход</div>
    <button class="btn btn-main" style="width:100%;padding:16px;font-size:16px" id="al-go">✈️ Telegram orqali kirish / Войти через Telegram</button>
    <div class="hint" id="al-st" style="font-size:13px;margin-top:14px;line-height:1.5">Bot ochiladi — «Start»ni bosing va ilovaga qayting.<br>Откроется бот — нажмите «Start» и вернитесь в приложение.</div></div>`;
  $("al-go").onclick = async () => {
    let r; try { r = await (await fetch("/api/app/login_start", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"})).json(); }
    catch (e) { return toast("Нет связи с сервером"); }
    if (!r.link) return toast("Бот ещё не запущен — попробуйте через минуту");
    if (window.RadiApp && window.RadiApp.openExternal) window.RadiApp.openExternal(r.link); else window.location.href = r.link;
    $("al-st").innerHTML = "⏳ Telegramda tasdiqlashni kutyapman… / Жду подтверждения в Telegram…";
    const started = Date.now();
    const poll = async () => {
      let s; try { s = await (await fetch("/api/app/login_check", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({code: r.code, platform: APP})})).json(); } catch (e) { s = {status: "wait"}; }
      if (s.status === "ok") { appToken.set(s.token); $("loading").innerHTML = "Загрузка…"; load(true); setInterval(() => load(), 30000); return; }
      if (s.status === "expired" || Date.now() - started > 9 * 60000) { $("al-st").textContent = "Kod eskirdi — qayta bosing / Код устарел — нажмите ещё раз"; return; }
      setTimeout(poll, 2000);
    };
    setTimeout(poll, 2000);
  };
}
function appLogout() {
  tg.showConfirm("Ilovadan chiqish? / Выйти из приложения?", ok => { if (!ok) return;
    fetch("/api/app/logout", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({appToken: appToken.get()})});
    appToken.set(""); location.reload(); });
}

function newsHtml(items, admin) {
  return items.length ? items.map(n => `<div class="news-item"><div class="hint" style="font-size:12px;display:flex;justify-content:space-between">
      <span>🗓 ${n.date}${admin && n.langs && n.langs.length ? `<span class="lang-badges">${n.langs.map(l => `<span>${l.toUpperCase()}</span>`).join("")}</span>` : ""}</span>${admin ? `<button class="link" data-delnews="${n.id}">удалить</button>` : ""}</div>${esc(n.text)}</div>`).join("")
    : `<div class="empty">Новостей пока нет</div>`;
}
const TIER_EMOJI = {bronza: "🥉", silver: "🥈", gold: "🥇"};
const CL_T = {
  uz: {hello: "Salom, {n}! 👋", debt: "Qarzingiz · batafsil", debt_order: "Buyurtma", debt_one: "Qarz", debt_sum: "summa", debt_paid_lbl: "to'langan",
       debt_due: "muddat", debt_no_list: "Tafsilotlar hozircha mavjud emas — umumiy qarz yuqorida ko'rsatilgan", purchases: "xaridlar summasi", bonuses: "bonuslar", soon: "tez orada", invest: "Investitsiyalar",
       invest_soon: "Bo'lim tez orada paydo bo'ladi", soon_pill: "Tez orada ⏳", news: "📰 Yangiliklar",
       no_news: "Hozircha yangiliklar yo'q", no_phone: "raqam ko'rsatilmagan", client: "Mijoz",
       finish_reg: "📱 Ro'yxatdan o'tishni bot bilan chatda yakunlang — shunda xaridlaringiz va bonuslaringizni topamiz.",
       birth: "🎂 Tug'ilgan kun", photo: "Rasm yuklanmoqda…",
       billz_not_found: "Raqamingiz hali do'kon bazasida yo'q — birinchi xariddan keyin bu yerda xaridlar va bonuslar paydo bo'ladi",
       billz_error: "Ma'lumotni hozir olib bo'lmadi, keyinroq yangilanadi", refresh: "🔄 Yangilash", refreshing: "⏳ Yangilanmoqda…",
       su_kicker: "Bronza → Silver", su_title: "Silver darajasiga ko'tariling ✨", su_p1: "🏷 Maxsus Silver narxlar",
       su_p2: "👤 Shaxsiy menejer — tanlash va buyurtmada yordam", su_p3: "🔒 Yopiq takliflar va yangi kolleksiyalar birinchi bo'lib",
       su_cta: "🥈 Silver bo'lish →",
       channel_open: "Kanal ochiladi — «Qo'shilish so'rovi»ni bosing, bot darhol tasdiqlaydi ✅",
       channel_sub: "Xaridorlar uchun yopiq kanal — shaxsiy havola"},
  ru: {hello: "Здравствуйте, {n}! 👋", debt: "Ваш долг · подробнее", debt_order: "Заказ", debt_one: "Долг", debt_sum: "сумма", debt_paid_lbl: "оплачено",
       debt_due: "до", debt_no_list: "Подробности пока недоступны — общий долг указан выше", purchases: "сумма покупок", bonuses: "бонусы", soon: "скоро", invest: "Инвестиции",
       invest_soon: "Раздел скоро появится", soon_pill: "Скоро ⏳", news: "📰 Новости",
       no_news: "Новостей пока нет", no_phone: "номер не указан", client: "Клиент",
       finish_reg: "📱 Завершите регистрацию в чате с ботом — так мы найдём ваши покупки и бонусы.",
       birth: "🎂 День рождения", photo: "Загружаю фото…",
       billz_not_found: "Вашего номера пока нет в базе магазина — после первой покупки здесь появятся покупки и бонусы",
       billz_error: "Сейчас не удалось получить данные, обновится позже", refresh: "🔄 Обновить", refreshing: "⏳ Обновляю…",
       su_kicker: "Bronza → Silver", su_title: "Повысьте уровень до Silver ✨", su_p1: "🏷 Специальные цены Silver",
       su_p2: "👤 Личный менеджер — поможет с выбором и заказом", su_p3: "🔒 Закрытые предложения и новые коллекции первыми",
       su_cta: "🥈 Стать Silver →",
       channel_open: "Откроется канал — нажмите «Подать заявку», бот сразу одобрит ✅",
       channel_sub: "Закрытый канал для покупателей — личная ссылка"},
  en: {hello: "Hello, {n}! 👋", debt: "Your debt · details", debt_order: "Order", debt_one: "Debt", debt_sum: "amount", debt_paid_lbl: "paid",
       debt_due: "due", debt_no_list: "Details are not available yet — the total debt is shown above", purchases: "total purchases", bonuses: "bonuses", soon: "soon", invest: "Investments",
       invest_soon: "This section is coming soon", soon_pill: "Soon ⏳", news: "📰 News",
       no_news: "No news yet", no_phone: "no phone number", client: "Client",
       finish_reg: "📱 Please finish registration in the bot chat so we can find your purchases and bonuses.",
       birth: "🎂 Birthday", photo: "Uploading photo…",
       billz_not_found: "Your number isn't in the store database yet — purchases and bonuses will appear after your first purchase",
       billz_error: "Couldn't get the data right now, it will update later", refresh: "🔄 Refresh", refreshing: "⏳ Refreshing…",
       su_kicker: "Bronza → Silver", su_title: "Upgrade to Silver ✨", su_p1: "🏷 Special Silver prices",
       su_p2: "👤 A personal manager to help you choose and order", su_p3: "🔒 Private offers and new collections first",
       su_cta: "🥈 Become Silver →",
       channel_open: "The channel will open — tap «Request to join», the bot approves it right away ✅",
       channel_sub: "Private channel for our customers — personal link"},
};
const ct = k => (CL_T[(data && data.client && data.client.lang) || "uz"] || CL_T.uz)[k];
function renderClient() {
  const c = data.client;
  langSwitch("cl-langsw", [["uz", "🇺🇿 UZ"], ["ru", "🇷🇺 RU"], ["en", "🇬🇧 EN"]], c.lang,
             l => action("/api/client_lang", {lang: l}));
  $("cl-ava").innerHTML = avatar(c.id, c.name || "?", c.ava_v);
  $("cl-ring").className = "ava tier-ring " + c.tier;
  $("cl-badge").textContent = TIER_EMOJI[c.tier] || "💎";
  $("cl-tier").className = "tier-pill " + c.tier;
  $("cl-tier").textContent = (TIER_EMOJI[c.tier] || "") + " " + c.tier_title;
  document.querySelectorAll("#view-client [data-t]").forEach(el => el.textContent = ct(el.dataset.t));
  $("cl-name").textContent = c.name || ct("client");
  $("cl-hello").textContent = c.first ? ct("hello").replace("{n}", c.first) : "";
  $("cl-hello").classList.toggle("hidden", !c.first);
  $("cl-phone").textContent = c.phone || ct("no_phone");
  $("cl-birth").textContent = c.birth ? `${ct("birth")}: ${c.birth}` : "";
  $("cl-birth").classList.toggle("hidden", !c.birth);
  $("cl-nophone").classList.toggle("hidden", !!(c.phone && c.registered));
  // покупки и бонусы из BILLZ
  const bz = c.billz || {}, cur = {uz: "so'm", ru: "сум", en: "UZS"}[c.lang] || "";
  const money = v => v === null || v === undefined ? "—" : Math.floor(v).toLocaleString("ru-RU") + " " + cur;
  ["cl-st-buy", "cl-st-bonus"].forEach(id => $(id).classList.toggle("soon-stat", !bz.enabled));
  document.querySelectorAll("#cl-st-buy em, #cl-st-bonus em").forEach(e => e.classList.toggle("hidden", !!bz.enabled));
  $("cl-buy").textContent = bz.enabled && bz.linked ? money(bz.purchases) : "—";
  $("cl-bonus").textContent = bz.enabled && bz.linked ? money(bz.balance) : "—";
  if (c.erp) {  // ERP — основной: баллы и замороженный бонус из своего учёта
    $("cl-st-bonus").classList.remove("soon-stat");
    $("cl-bonus").textContent = money(c.erp.points);
  }
  const fr = c.erp && c.erp.frozen;
  $("cl-frozen").textContent = fr ? `🧊 ${money(c.erp.frozen)} → ${c.erp.frozen_until.split("-").reverse().join(".")}` : "";
  $("cl-frozen").classList.toggle("hidden", !fr);
  const debt = bz.enabled && bz.linked ? +(bz.debt || 0) : 0;
  $("cl-debt").classList.toggle("hidden", !(debt > 0));
  $("cl-debt-sum").textContent = debt > 0 ? money(debt) : "";
  $("cl-debt").onclick = () => toggleDebts(money);
  if (debtOpen && debt > 0) loadDebts(money);
  const note = !bz.enabled ? "" : bz.error && !bz.linked ? ct("billz_error") : !bz.linked ? ct("billz_not_found") : "";
  $("cl-billz-note").textContent = note; $("cl-billz-note").classList.toggle("hidden", !note);
  $("cl-refresh-row").classList.toggle("hidden", !bz.enabled);
  $("cl-refresh").textContent = ct("refresh") + (bz.synced ? " · " + bz.synced.slice(11, 16) : "");
  $("cl-news").innerHTML = data.news.length ? newsHtml(data.news, false) : `<div class="empty">${ct("no_news")}</div>`;
  $("cl-silver").classList.toggle("hidden", !c.upgrade);
  $("cl-silver").dataset.url = c.upgrade_soon ? "" : (c.upgrade || "");
  $("cl-silver").classList.toggle("su-off", !!c.upgrade_soon);
  $("cl-silver").querySelector(".su-cta").textContent = c.upgrade_soon ? ct("soon_pill") : ct("su_cta");
  $("cl-channel").classList.toggle("hidden", !c.channel);
  $("cl-channel-t").textContent = (c.channel || "").replace(/^[^\p{L}]+/u, "");
  $("cl-channel-s").textContent = ct("channel_sub");
}
$("cl-silver").onclick = () => {
  const url = $("cl-silver").dataset.url;
  if (!url) return toast(ct("soon_pill"));
  if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred && tg.HapticFeedback.impactOccurred("medium");
  if (/^https:\/\/t\.me\//.test(url) && tg.openTelegramLink) tg.openTelegramLink(url);
  else if (tg.openLink) tg.openLink(url); else location.href = url;
};
$("cl-channel").onclick = async () => {
  const b = $("cl-channel"); if (b.disabled) return; b.disabled = true;
  try {
    const r = await api("/api/client_channel");
    if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(r.ok ? "success" : "warning");
    if (r.status === "link" && r.link) {
      toast(ct("channel_open"));
      setTimeout(() => { if (tg.openTelegramLink) tg.openTelegramLink(r.link); else location.href = r.link; }, 600);
    } else toast(r.msg);
  } catch (e) { toast(e.message); }
  b.disabled = false;
};
let debtOpen = false;
function toggleDebts(money) {
  debtOpen = !debtOpen;
  $("cl-debt-list").classList.toggle("hidden", !debtOpen);
  $("cl-debt-arrow").style.transform = debtOpen ? "rotate(90deg)" : "";
  if (debtOpen) loadDebts(money);
}
async function loadDebts(money) {
  const box = $("cl-debt-list");
  if (!box.innerHTML) box.innerHTML = `<div class="empty">⏳</div>`;
  let r; try { r = await api("/api/client_debts"); } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const items = r.items || [];
  const dt = d => d ? d.split("-").reverse().join(".") : "";
  box.innerHTML = items.length ? items.map(d => `
    <div class="inv" style="padding:10px 0">
      <div class="inv-top"><b>${d.order ? ct("debt_order") + " №" + esc(String(d.order)) : ct("debt_one")}</b>
        <b style="color:var(--red);white-space:nowrap">${money(d.left)}</b></div>
      <div class="inv-sub hint"><span>${ct("debt_sum")}: ${money(d.amount)}${d.paid ? " · " + ct("debt_paid_lbl") + ": " + money(d.paid) : ""}</span>
        ${d.due ? `<span style="${d.overdue ? "color:var(--red);font-weight:700" : ""}">${d.overdue ? "⏰ " : "📅 "}${ct("debt_due")} ${dt(d.due)}</span>` : ""}</div>
    </div>`).join("") : `<div class="hint" style="font-size:13px">${ct("debt_no_list")}</div>`;
}
let bzTimer = null, bzItems = [], bzPoll = null;
async function loadClients(more = false) {
  let r;
  try { r = await api("/api/billz_base", {q: $("cl-search").value, offset: more ? bzLoaded : 0}); }
  catch (e) { $("cl-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  bzItems = more ? bzItems.concat(r.items) : r.items;
  bzLoaded = r.loaded_store;
  const st = r.status || {};
  $("cl-total").textContent = `всего: ${r.total}`;
  const line = !r.enabled ? "" : st.running
    ? `⏳ Загружается база магазина… ${st.loaded || 0}${st.total ? " из " + st.total : ""}`
    : st.error ? `⚠️ База магазина не загрузилась: ${esc(st.error)}`
    : st.at ? `🤖 в боте: ${r.in_bot} · база магазина: ${st.at}` : "База магазина ещё не загружалась";
  $("bz-status").innerHTML = line ? `<span style="flex:1">${line}</span>` +
    (!st.running ? `<button class="btn btn-soft btn-sm" id="bz-refresh">🔄</button>` : "") : "";
  $("bz-status").classList.toggle("hidden", !line);
  if ($("bz-refresh")) $("bz-refresh").onclick = async () => { await action("/api/billz_base", {action: "refresh"}); loadClients(); };
  clearTimeout(bzPoll);
  if (st.running) bzPoll = setTimeout(() => view === "clients" && loadClients(), 3000);
  const money = v => Math.round(v || 0).toLocaleString("ru-RU");
  const tiers = data.tiers || {};
  $("cl-list").innerHTML = bzItems.length ? bzItems.map(c => {
    const meta = [esc(c.phone || "номер не указан")];
    if (c.birth) meta.push("🎂 " + c.birth);
    if (c.last_tx) meta.push("🛍 " + c.last_tx.split("-").reverse().join("."));
    if (c.imported) meta.push("📥 из прежнего бота");
    else if (c.bot_id) meta.push((c.lang || "—").toUpperCase() + " · с " + c.since);
    const opts = Object.entries(tiers).map(([k, t]) => `<option value="${k}" ${k === c.tier ? "selected" : ""}>${t}</option>`).join("");
    const right = c.bot_id ? `<select data-tier="${c.bot_id}">${opts}</select>`
      : `<select data-stier="${esc(c.store_id)}">${opts}</select>`;
    const bonus = c.balance ? `<span style="color:var(--green);font-weight:700">🎁 ${money(c.balance)}</span>` : "";
    return `<div class="cl-row" data-cc="${c.bot_id ? "b:" + c.bot_id : "s:" + esc(c.store_id || "")}" style="cursor:pointer"><div style="font-size:20px">${TIER_EMOJI[c.tier] || (c.bot_id ? "🤖" : "👤")}</div>
      <div style="flex:1;min-width:0"><b>${esc(c.name)}</b>${c.bot_id ? ` <span class="chip active" style="font-size:10.5px;padding:2px 7px">в боте</span>` : ""}
        <div class="hint" style="font-size:12px">${meta.join(" · ")}</div>${bonus ? `<div style="font-size:12.5px;margin-top:2px">${bonus}</div>` : ""}</div>
      ${right}</div>`;
  }).join("") + (r.more ? `<button class="btn btn-soft" style="width:100%;margin-top:10px" id="bz-more">Показать ещё (${r.more})</button>` : "")
    : `<div class="empty">${$("cl-search").value.trim() ? "Никого не нашли" : "Клиентов пока нет"}</div>`;
  if ($("bz-more")) $("bz-more").onclick = () => loadClients(true);
  document.querySelectorAll("#cl-list [data-cc]").forEach(r => r.onclick = e => {
    if (e.target.closest("select")) return;
    const [k, v] = r.dataset.cc.split(":"); ccKey = k === "b" ? {bot_id: +v} : {store_id: v}; show("ccard"); loadCard();
  });
  document.querySelectorAll("#cl-list [data-tier]").forEach(s => s.onchange = () =>
    action("/api/set_tier", {user_id: +s.dataset.tier, tier: s.value}).then(() => loadClients()));
  document.querySelectorAll("#cl-list [data-stier]").forEach(s => s.onchange = () =>
    action("/api/set_tier", {store_id: s.dataset.stier, tier: s.value}).then(() => loadClients()));
}
let bzLoaded = 0;
let impXlsx = null;  // Excel-файл (.xlsx) читает сервер
$("imp-file").onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  if (f.size > 3_000_000) return toast("Файл слишком большой");
  if (/\.xlsx$/i.test(f.name)) {
    impXlsx = await new Promise(ok => { const fr = new FileReader(); fr.onload = () => ok(fr.result); fr.readAsDataURL(f); });
    $("imp-text").value = `📊 Excel: ${f.name} — нажмите «Загрузить»`;
  } else { impXlsx = null; $("imp-text").value = await f.text(); }
  e.target.value = "";
  toast(`Файл «${f.name}» выбран — нажмите «Загрузить»`);
};
$("imp-text").oninput = () => { impXlsx = null; };
$("imp-go").onclick = async () => {
  const text = $("imp-text").value.trim(); if (!text && !impXlsx) return toast("Выберите файл или вставьте список ID");
  try { const r = await api("/api/import_clients", impXlsx ? {xlsx: impXlsx} : {text}); toast(r.msg);
    if (r.ok) { $("imp-text").value = ""; impXlsx = null; loadClients(); load(); } }
  catch (e) { toast(e.message); }
};
/* ---------- карточка клиента ---------- */
let ccKey = null, ccData = null;
async function loadCard() {
  $("cc-body").innerHTML = `<div class="card empty">⏳ Загружаю данные клиента…</div>`;
  try { ccData = await api("/api/client_card", ccKey); } catch (e) { $("cc-body").innerHTML = `<div class="card empty">${esc(e.message)}</div>`; return; }
  renderCard();
}
function renderCard() {
  const c = ccData, k = c.card || {}, m = v => v === null || v === undefined ? "—" : Math.round(v).toLocaleString("ru-RU");
  const dt = v => v ? String(v).slice(0, 10).split("-").reverse().join(".") : "—";
  const tiers = data.tiers || {};
  const hist = c.history || {}, h = c.purchases || [];
  const sales = h.filter(x => x.type === "SALE"), ret = h.filter(x => x.type === "RETURN");
  const initials = (c.name || "?").split(" ").map(x => x[0] || "").join("").slice(0, 2).toUpperCase();
  $("cc-body").innerHTML = `
    <div class="card"><div class="member"><div class="ava tier-ring ${c.tier}" style="width:72px;height:72px;padding:3px"><div style="font-size:24px">${esc(initials)}</div></div>
      <div style="flex:1;min-width:0"><b style="font-size:20px">${esc(c.name)}</b>
        <div class="hint" style="font-size:13.5px">${c.phone ? `<a href="tel:${esc(c.phone)}" style="color:#b9a2ff">${esc(c.phone)}</a>` : "номер не указан"}${c.birth ? " · 🎂 " + dt(c.birth) : ""}</div>
        <div style="margin-top:6px">${c.in_bot ? '<span class="badge">🤖 в боте</span>' : ""}${c.lang ? `<span class="badge">${c.lang.toUpperCase()}</span>` : ""}${c.since ? `<span class="badge">с ${dt(c.since)}</span>` : ""}
          ${(k.group_names || []).map(g => `<span class="badge">👥 ${esc(g)}</span>`).join("")}${(k.tag_names || []).map(g => `<span class="badge">🏷 ${esc(g)}</span>`).join("")}</div></div></div>
      <select id="cc-tier" style="margin:12px 0 0">${Object.entries(tiers).map(([t, n]) => `<option value="${t}" ${t === c.tier ? "selected" : ""}>${n}</option>`).join("")}</select>
      <div class="hint" style="font-size:12.5px;margin:10px 0 4px">👤 Личный менеджер — клиент видит его в боте (Silver и Gold)</div>
      <select id="cc-manager"><option value="0">— не назначен —</option>${(c.managers || []).map(e => `<option value="${e.id}" ${e.id === c.manager ? "selected" : ""}>${esc(e.name)}</option>`).join("")}</select></div>
    ${c.card_error ? `<div class="card hint" style="font-size:13px">⚠️ BILLZ: ${esc(c.card_error)}</div>` : ""}
    ${c.card ? `<div class="card"><b style="font-size:18px">📊 Статистика (BILLZ)</b>
      <div class="inv-tiles" style="grid-template-columns:1fr 1fr;margin-top:10px">
        <div><b>${m(k.purchase_amount)}</b><span>сумма покупок</span></div><div><b style="color:var(--green)">${m(k.balance)}</b><span>бонусы</span></div>
        <div><b>${m(k.visits_count)}</b><span>визитов</span></div><div><b>${m(k.average_purchase_amount)}</b><span>средний чек</span></div>
        <div><b>${m(k.top_transaction)}</b><span>крупнейшая покупка</span></div><div><b style="${k.debt_amount ? "color:var(--red)" : ""}">${m(k.debt_amount)}</b><span>долг</span></div></div>
      <div class="kv"><span>Первая покупка</span><b>${dt(k.first_purchase_date)}</b></div>
      <div class="kv"><span>Последняя покупка</span><b>${dt(k.last_purchase_date)}</b></div>
      ${k.average_discount ? `<div class="kv"><span>Средняя скидка</span><b>${Math.round(k.average_discount * 10) / 10}%</b></div>` : ""}
      ${k.registered_shop_name ? `<div class="kv"><span>Магазин регистрации</span><b>${esc(k.registered_shop_name)}</b></div>` : ""}
      ${(k.cards || []).length ? `<div class="kv"><span>Карты</span><b>${k.cards.map(esc).join(", ")}</b></div>` : ""}
      ${c.erp_points ? `<div class="kv"><span>🧾 Баллы ERP</span><b>${m(c.erp_points)}</b></div>` : ""}</div>` : ""}
    ${dmCard(c.bot_id ? "client" : "", c.bot_id, "Клиент ещё не в боте — написать ему нельзя (нужно, чтобы он хоть раз нажал /start).")}
    <div class="card"><b style="font-size:18px">📝 Заметки</b>
      ${(k.notes || []).map(n => `<div class="pen"><div class="tt">${esc(n.text)}<div class="hint" style="font-size:11.5px">BILLZ · ${esc(n.at)}</div></div></div>`).join("")}
      <textarea id="cc-note" rows="3" maxlength="1000" placeholder="Ваша заметка о клиенте…" style="margin-top:10px">${esc(c.note)}</textarea>
      <button class="btn btn-soft" style="width:100%" id="cc-note-save">Сохранить заметку</button></div>
    ${c.debts.length ? `<div class="card"><b style="font-size:18px">💳 Долги</b>${c.debts.map(d => `<div class="inv" style="padding:10px 0">
      <div class="inv-top"><b>${d.order ? "Заказ №" + esc(String(d.order)) : "Долг"}</b><b style="color:var(--red)">${m(d.left)}</b></div>
      <div class="inv-sub hint"><span>сумма ${m(d.amount)}${d.paid ? " · оплачено " + m(d.paid) : ""}</span>${d.due ? `<span style="${d.overdue ? "color:var(--red);font-weight:700" : ""}">${d.overdue ? "⏰" : "📅"} до ${dt(d.due)}</span>` : ""}</div></div>`).join("")}</div>` : ""}
    <div class="card"><div class="head"><b style="font-size:18px">🛍 История покупок</b><span class="hint" style="font-size:12.5px">${sales.length} покупок${ret.length ? ` · ${ret.length} возвр.` : ""}</span></div>
      <div class="hint" style="font-size:12px;margin-bottom:6px">${hist.from ? `Из BILLZ с ${dt(hist.from)}${hist.running ? " · загружается…" : ""}` : "История из BILLZ ещё не загружена"}
        <button class="link" id="cc-hist" style="font-size:12px">🔄 ${hist.from ? "Обновить за год" : "Загрузить за год"}</button></div>
      ${h.length ? h.map(x => `<div class="sale"><div class="inv-top"><b style="${x.type === "RETURN" ? "color:var(--red)" : ""}">${x.type === "RETURN" ? "↩️ Возврат" : x.type === "EXCHANGE" ? "🔁 Обмен" : "🛍 Покупка"} · ${dt(x.date)} ${x.date.slice(11, 16)}</b>
        <b style="${x.type === "RETURN" ? "color:var(--red)" : ""}">${m(x.total)}</b></div>
        <div class="pmeta"><span>📍 ${esc(x.shop)}</span>${x.seller ? `<span>👤 ${esc(x.seller)}</span>` : ""}${x.units ? `<span>${x.units} ед.</span>` : ""}${x.receipt ? `<span>#${esc(x.receipt)}</span>` : ""}</div>
        <div class="pmeta">${x.pays.map(p => `<span>${p.icon} ${esc(p.name)} ${m(p.sum)}</span>`).join("")}</div></div>`).join("")
      : `<div class="empty">Покупок не найдено</div>`}
      ${c.erp_sales.length ? `<div class="hint" style="font-size:12.5px;margin-top:8px">🧾 В нашем учёте: ${c.erp_sales.map(x => `№${x.id} ${dt(x.date)} — ${m(x.amount)}`).join(" · ")}</div>` : ""}</div>`;
  $("cc-tier").onchange = e => action("/api/set_tier", c.bot_id ? {user_id: c.bot_id, tier: e.target.value} : {store_id: c.store_id, tier: e.target.value});
  $("cc-manager").onchange = e => action("/api/client_manager", {user_id: c.bot_id || 0, store_id: c.store_id || "", manager: +e.target.value});
  $("cc-note-save").onclick = () => api("/api/client_card", {action: "note", phone: c.phone, name: c.name, note: $("cc-note").value})
    .then(r => toast(r.msg)).catch(e => toast(e.message));
  bindDm("client");
  $("cc-hist").onclick = () => api("/api/client_card", {action: "history"}).then(r => { toast(r.msg); setTimeout(loadCard, 8000); }).catch(e => toast(e.message));
}
/* Личное сообщение в бот (карточка клиента / сотрудника) */
const dmDraft = {};  // черновик не теряется при автообновлении кабинета
function dmCard(kind, to, noText) {
  if (!kind) return noText ? `<div class="card hint" style="font-size:13px">✉️ ${esc(noText)}</div>` : "";
  return `<div class="card" id="dm-card-${kind}" data-kind="${kind}" data-to="${to}"><b style="font-size:18px">✉️ Сообщение в бот</b>
    <div class="hint" style="font-size:12.5px;margin:2px 0 8px">Придёт ${kind === "client" ? "клиенту в бот клиентов" : "сотруднику в бот RADI TEAM"} от имени бота. {name} — имя.</div>
    <textarea id="dm-text-${kind}" rows="3" maxlength="4000" placeholder="Например: {name}, ваш заказ готов — ждём вас в магазине 💎">${esc(dmDraft[kind + to] || "")}</textarea>
    <button class="btn btn-main" style="width:100%" id="dm-send-${kind}">Отправить</button></div>`;
}
function bindDm(kind) {
  const card = $("dm-card-" + kind); if (!card) return;
  const key = kind + card.dataset.to, ta = $("dm-text-" + kind), btn = $("dm-send-" + kind);
  ta.oninput = () => { dmDraft[key] = ta.value; };
  btn.onclick = async () => {
    const text = ta.value.trim(); if (!text) return toast("Напишите сообщение");
    btn.disabled = true;
    try { const r = await api("/api/direct_message", {to: +card.dataset.to, kind, text});
      toast(r.msg); if (r.ok) { ta.value = ""; delete dmDraft[key]; } } catch (e) { toast(e.message); }
    btn.disabled = false;
  };
}
function renderClientsAdmin(user = false) {
  if (!user && bzItems.length) return;  // автообновление кабинета не сбрасывает открытый список
  clearTimeout(bzTimer); bzTimer = setTimeout(() => loadClients(), user ? 250 : 0);
}
$("cl-search").oninput = () => renderClientsAdmin(true);
$("exp-go").onclick = () => action("/api/export_clients");

function renderNewsAdmin() {
  $("cl-count").textContent = `клиентов: ${data.clients.total} (с номером: ${data.clients.with_phone})`;
  const all = data.clients.all != null ? data.clients.all : data.clients.total;
  $("clients-folder-sub").textContent = `${all} ${plural(all, "клиент", "клиента", "клиентов")} · в боте: ${data.clients.total}` +
    (data.news.length ? ` · новостей: ${data.news.length}` : "");
  $("n-list").innerHTML = newsHtml(data.news, true);
  document.querySelectorAll("[data-delnews]").forEach(b => b.onclick = () =>
    tg.showConfirm("Удалить новость?", ok => ok && action("/api/delete_news", {id: +b.dataset.delnews})));
}
const newsEd = langEditor("n-langs", 3, 2000, {uz: "Yangilik matni (o'zbekcha)…", ru: "Текст новости (по-русски)…",
  en: "News text (English)…"});
$("n-add").onclick = () => {
  const texts = newsEd.texts();
  if (!Object.keys(texts).length) return toast("Напишите текст новости хотя бы на одном языке");
  action("/api/add_news", {texts}).then(() => newsEd.clear());
};

document.querySelectorAll("#seg button").forEach(b => b.onclick = () => show(b.dataset.view));
$("all-btn").onclick = () => show("history");
/* Переключатель языка: кнопки, текущий язык подсвечен */
function langSwitch(id, langs, cur, onPick) {
  const box = $(id);
  box.innerHTML = langs.map(([k, t]) => `<button data-lng="${k}" class="${k === cur ? "on" : ""}">${t}</button>`).join("");
  box.querySelectorAll("[data-lng]").forEach(b => b.onclick = () => { if (b.dataset.lng !== cur) onPick(b.dataset.lng); });
}
/* ================= ERP: продажи ================= */
let shopS = null, shopTab = "home", shopSub = null, shopDay = null, shopQ = "", shopPoint = null, shopOpenTx = null, shopSource = "erp";
const sm = v => Math.round(v || 0).toLocaleString("ru-RU");
const snum = v => +String(v || "").replace(/[^\d.,]/g, "").replace(",", ".") || 0;
const todayISO = () => { const t = new Date(); return new Date(t.getTime() - t.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };
const POINT_COLORS = ["#60a5fa", "#a855f7", "#f59e0b", "#f97316", "#34d399", "#f472b6", "#22d3ee"];
const newCart = () => ({amount: "", parts: [], step: "amount", client: null, name: "", seller_id: "", discount_mode: "sum", discount_value: 0,
  loyalty_on: true, loyalty_mode: "pct", loyalty_value: null, spend: 0, bonus_mode: "sum", bonus_value: 0, exchange: false, note: "",
  mixed: false, single: null, pays: {}, draft_id: null, point_id: null});
let cart = newCart();
const cartTotal = () => cart.parts.reduce((a, x) => a + x, 0) + snum(cart.amount);
const cartDiscount = () => cart.discount_mode === "pct" ? cartTotal() * snum(cart.discount_value) / 100 : snum(cart.discount_value);
const cartToPay = () => Math.max(0, cartTotal() - cartDiscount());
async function loadShop() {
  try { shopS = await api("/api/shop/state", Object.assign(shopDay ? {from: shopDay.from, to: shopDay.to} : {}, {q: shopQ, point_id: shopPoint, source: shopSource})); }
  catch (e) { $("shop-body").innerHTML = `<div class="card empty">${esc(e.message)}</div>`; return; }
  const p = shopS.me.perms;
  if (shopTab === "sell" && !p.includes("sell")) shopTab = "home";
  document.querySelectorAll("#shop-nav [data-snav]").forEach(b => {
    b.classList.toggle("on", b.dataset.snav === shopTab);
    b.style.display = b.dataset.snav === "sell" && !p.includes("sell") ? "none" : "";
  });
  $("shop-nav").style.gridTemplateColumns = `repeat(${p.includes("sell") ? 4 : 3},1fr)`;
  if (cart.loyalty_value === null) cart.loyalty_value = shopS.settings.loyalty_pct;
  if (!cart.seller_id && shopS.me.role !== "owner" && shopS.sellers.find(x => x.id === shopS.me.id)) cart.seller_id = shopS.me.id;
  renderShop();
}
function renderShop() {
  const head = $("sh-head-btn"); head.style.visibility = "hidden"; head.onclick = null;
  $("shop-sub").textContent = `${shopS.me.role_title} · ${((shopS.points.find(x => x.id === shopS.me.point_id) || {}).title) || "все точки"}`;
  if (shopTab === "home") return shopHome();
  if (shopTab === "sell") return shopSell();
  if (shopTab === "list") return shopList();
  return shopMenu();
}
document.querySelectorAll("#shop-nav [data-snav]").forEach(b => b.onclick = () => {
  shopTab = b.dataset.snav; shopSub = null; window.scrollTo(0, 0); loadShop();
});
function sourceSwitch(id) {
  if (!shopS.owner_view || !shopS.billz_on) return "";
  return `<div class="seg" id="${id}" style="margin-bottom:10px"><button data-src="erp" class="${shopS.source === "erp" ? "on" : ""}">🧾 Наш учёт</button>
    <button data-src="billz" class="${shopS.source === "billz" ? "on" : ""}">🅱️ BILLZ</button></div>`;
}
function bindSourceSwitch(id) {
  document.querySelectorAll(`#${id} [data-src]`).forEach(b => b.onclick = () => { shopSource = b.dataset.src; loadShop(); });
}
function reconCard() {
  const r = shopS.recon; if (!r) return "";
  const diff = r.billz_total - r.erp_total, dc = r.billz_count - r.erp_count;
  const d = shopDay || {from: todayISO(), to: todayISO()};
  return `<div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><b style="font-size:19px">⚖️ Сверка с BILLZ</b>
      <button class="btn btn-soft btn-sm" id="rc-sync">🔄 Обновить</button></div>
    <div class="kv"><span>🅱️ BILLZ</span><b>${r.billz_count} · ${sm(r.billz_total)}</b></div>
    <div class="kv"><span>🧾 Наш учёт</span><b>${r.erp_count} · ${sm(r.erp_total)}</b></div>
    <div class="kv"><span>Разница</span><b style="color:${Math.abs(diff) < 1 && !dc ? "var(--green)" : "var(--red)"}">${Math.abs(diff) < 1 && !dc ? "✅ совпадает" : `${dc > 0 ? "+" : ""}${dc} продаж · ${diff > 0 ? "+" : ""}${sm(diff)}`}</b></div>
    <div class="hint" style="font-size:11.5px">BILLZ копируется каждые ~2 минуты за сегодня${r.mirror.at ? ` · последний раз ${r.mirror.at}` : ""}. За прошлые дни — кнопкой «Обновить» (период: ${d.from.split("-").reverse().join(".")} — ${d.to.split("-").reverse().join(".")}).</div></div>`;
}
function bindRecon() {
  if (!$("rc-sync")) return;
  $("rc-sync").onclick = () => { const d = shopDay || {from: todayISO(), to: todayISO()}; toast("Загружаю продажи из BILLZ…");
    api("/api/shop/settings", {action: "mirror_billz", from: d.from, to: d.to}).then(r => { toast(r.msg); loadShop(); }).catch(e => toast(e.message)); };
}
function pointChips(id) {
  const pts = shopS.points.filter(p => p.active);
  return `<div class="chips" id="${id}"><button data-pt="" class="${!shopPoint ? "on" : ""}">Все точки</button>${pts.map(p =>
    `<button data-pt="${p.id}" class="${+shopPoint === p.id ? "on" : ""}">${esc(p.title)}</button>`).join("")}</div>`;
}
function bindPointChips(id) {
  document.querySelectorAll(`#${id} [data-pt]`).forEach(b => b.onclick = () => { shopPoint = b.dataset.pt ? +b.dataset.pt : null; loadShop(); });
}
function dateBar() {
  const d = shopDay || {from: todayISO(), to: todayISO()};
  return `<div class="card" style="display:flex;gap:8px;align-items:center;padding:12px">📅
    <input type="date" id="sd-from" value="${d.from}" style="margin:0;flex:1;min-width:0;padding:10px 8px"><span class="hint">—</span><input type="date" id="sd-to" value="${d.to}" style="margin:0;flex:1;min-width:0;padding:10px 8px"></div>`;
}
function bindDateBar() {
  const upd = () => { const f = $("sd-from").value || todayISO(); shopDay = {from: f, to: $("sd-to").value && $("sd-to").value >= f ? $("sd-to").value : f}; loadShop(); };
  $("sd-from").onchange = upd; $("sd-to").onchange = upd;
}
/* --- главная --- */
function shopHome() {
  $("sh-title").textContent = "Главная";
  const S = shopS.summary, max = Math.max(1, ...S.by_hour.slice(8, 24));
  const pts = shopS.points.filter(p => p.active);
  const byPt = Object.fromEntries(S.by_point);
  $("shop-body").innerHTML = sourceSwitch("hs") + pointChips("hp") + `
    <div class="card"><div style="display:flex;justify-content:space-between;align-items:flex-start">
      <div><b style="font-size:19px">Продажи</b><div class="big-sum">${sm(S.total)} <small>UZS</small></div>
      <div class="hint" style="font-size:12.5px">${S.count} продаж · средний чек ${sm(S.avg)}${S.discount ? " · скидки " + sm(S.discount) : ""}</div></div>
      <div class="f-ic" style="width:52px;height:52px;display:flex;align-items:center;justify-content:center;font-size:22px">📊</div></div>
      <div class="bars">${S.by_hour.slice(8, 24).map(v => `<div style="height:${Math.round(v / max * 100)}%"></div>`).join("")}</div>
      <div class="bars-x"><span>8:00</span><span>12:00</span><span>16:00</span><span>20:00</span><span>23:00</span></div>
      <div style="margin-top:10px">${pts.map((p, i) => `<div class="kv"><span><span class="dot-c" style="background:${POINT_COLORS[i % POINT_COLORS.length]}"></span>${esc(p.title)}</span><b>${sm(byPt[p.title] || 0)} UZS</b></div>`).join("")}</div></div>
    <div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><div><b style="font-size:19px">Платежи</b>
      <div class="big-sum" style="font-size:28px">${sm(Object.values(S.by_pay).reduce((a, v) => a + v, 0) + S.points_spent)} <small>UZS</small></div></div>
      <div class="f-ic" style="width:52px;height:52px;display:flex;align-items:center;justify-content:center;font-size:22px">💵</div></div>
      ${Object.entries(S.by_pay).map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><b>${sm(v)}</b></div>`).join("")}
      ${S.points_spent ? `<div class="kv"><span>🎁 Баллами</span><b>${sm(S.points_spent)}</b></div>` : ""}</div>
    <div class="card"><b style="font-size:19px">Топ продавцов</b><div class="hint" style="font-size:12.5px;margin-bottom:4px">по сумме продаж</div>
      ${S.by_seller.length ? S.by_seller.slice(0, 10).map(([n, v], i) => `<div class="kv"><span><span class="pill" style="padding:3px 9px;margin-right:6px">${i + 1}</span>${esc(n)}</span><b>${sm(v)}</b></div>`).join("") : `<div class="empty">Продаж пока нет</div>`}</div>
    <div class="card"><b style="font-size:19px">Баллы и бонусы</b>
      <div class="kv"><span>➕ Начислено баллов</span><b>${sm(S.loyalty)}</b></div>
      <div class="kv"><span>➖ Оплачено баллами</span><b>${sm(S.points_spent)}</b></div>
      <div class="kv"><span>🎁 Бонусы</span><b>${sm(S.bonus)}</b></div></div>` + reconCard() + dateBar();
  bindPointChips("hp"); bindDateBar(); bindSourceSwitch("hs"); bindRecon();
}
/* --- продажа: 1) сумма --- */
function shopSell() {
  if (cart.step === "details") return sellDetails();
  if (cart.step === "pay") return sellPay();
  $("sh-title").textContent = "Корзина";
  const head = $("sh-head-btn"); head.style.visibility = shopS.drafts.length ? "visible" : "hidden";
  head.textContent = "⏸"; head.title = "Отложенные"; head.onclick = () => { shopTab = "menu"; shopSub = "drafts"; loadShop(); };
  const total = cartTotal();
  $("shop-body").innerHTML = `<div class="card">
      <div class="pad-sum">${sm(snum(cart.amount))} <small>UZS</small></div>
      <div class="pad-parts">${cart.parts.length ? cart.parts.map(sm).join(" + ") + " + … = " + sm(total) : (shopS.drafts.length ? `⏸ отложенных: ${shopS.drafts.length}` : "&nbsp;")}</div>
      <div class="numpad">${["1", "2", "3", "4", "5", "6", "7", "8", "9", "+", "0", "⌫"].map(k =>
        `<button data-k="${k}" class="${k === "+" || k === "⌫" ? "fn" : ""}">${k}</button>`).join("")}</div></div>
    <button class="btn btn-main" style="width:100%;padding:18px;border-radius:20px;font-size:17px" id="pad-next" ${total ? "" : "disabled"}>
      ${total ? "Далее · " + sm(total) + " UZS" : "Введите сумму"}</button>`;
  document.querySelectorAll("[data-k]").forEach(b => b.onclick = () => {
    const k = b.dataset.k;
    if (k === "⌫") { if (cart.amount) cart.amount = cart.amount.slice(0, -1); else if (cart.parts.length) cart.amount = String(cart.parts.pop()); }
    else if (k === "+") { if (snum(cart.amount)) { cart.parts.push(snum(cart.amount)); cart.amount = ""; } }
    else if (cart.amount.length < 11) cart.amount = (cart.amount === "0" ? "" : cart.amount) + k;
    if (tg.HapticFeedback && tg.HapticFeedback.selectionChanged) tg.HapticFeedback.selectionChanged();
    shopSell();
  });
  $("pad-next").onclick = () => { cart.step = "details"; shopSell(); window.scrollTo(0, 0); };
}
/* --- продажа: 2) детали --- */
function sellDetails() {
  $("sh-title").textContent = "Продажа";
  const head = $("sh-head-btn"); head.style.visibility = "visible"; head.textContent = "‹"; head.onclick = () => { cart.step = "amount"; shopSell(); };
  const S = shopS, c = cart.client, seller = S.sellers.find(x => x.id === +cart.seller_id);
  const disc = cartDiscount(), toPay = cartToPay();
  const loy = !cart.loyalty_on ? 0 : cart.loyalty_mode === "pct" ? (toPay - snum(cart.spend)) * snum(cart.loyalty_value) / 100 : snum(cart.loyalty_value);
  const bon = cart.bonus_mode === "pct" ? toPay * snum(cart.bonus_value) / 100 : snum(cart.bonus_value);
  const row = (id, icon, title, val, act) => `<button class="card row-btn" id="${id}"><span class="ri">${icon}</span>
    <span class="rt"><b>${title}</b>${val ? `<span>${val}</span>` : ""}</span><span class="ra">${act || "Добавить ›"}</span></button>`;
  $("shop-body").innerHTML = `
    <div class="card" style="text-align:center;padding:14px"><div class="hint" style="font-size:13px">Сумма покупки</div>
      <div style="font-size:30px;font-weight:800">${sm(cartTotal())} <small class="hint" style="font-size:16px">UZS</small></div>
      ${disc ? `<div style="color:var(--green);font-weight:700">скидка −${sm(disc)} → к оплате ${sm(toPay)}</div>` : ""}</div>
    ${row("sd-client", "👤", "Клиент", c ? `${esc(c.name || "Новый клиент")} · ${esc(c.phone)}${c.points ? " · 🎁 " + sm(c.points) : ""}` : "", c ? "Изменить ›" : "")}
    ${S.me.role === "owner" ? row("sd-point", "📍", "Точка", esc(((S.points.find(p => p.id === +cart.point_id) || {}).title) || ""), cart.point_id ? "Изменить ›" : "Выбрать ›") : ""}
    ${row("sd-seller", "🧑‍💼", "Продавец", seller ? esc(seller.name) : "", seller ? "Изменить ›" : "Выбрать ›")}
    ${row("sd-disc", "％", "Скидка", disc ? `−${sm(disc)}${cart.discount_mode === "pct" ? ` (${cart.discount_value}%)` : ""}` : "")}
    ${c && c.points ? row("sd-spend", "🎁", "Оплатить баллами", snum(cart.spend) ? `−${sm(cart.spend)} из ${sm(c.points)}` : `доступно ${sm(c.points)}`, snum(cart.spend) ? "Изменить ›" : "Списать ›") : ""}
    <div class="card row-btn" style="cursor:default"><span class="ri">⭐</span><span class="rt"><b>Баллы лояльности</b>
      <span id="sd-loy-v">${cart.loyalty_on ? `+${sm(loy)}${cart.loyalty_mode === "pct" ? ` (${cart.loyalty_value}%)` : ""} — изменить` : "не начислять"}</span></span>
      <label class="sw"><input type="checkbox" id="sd-loy" ${cart.loyalty_on ? "checked" : ""}><span></span></label></div>
    ${row("sd-bonus", "🎀", "Бонус", bon ? `+${sm(bon)}` : "")}
    <div class="card row-btn" style="cursor:default"><span class="ri">🔁</span><span class="rt"><b>Обмен разрешён</b><span style="color:var(--hint);font-weight:500">1 раз в течение месяца</span></span>
      <label class="sw"><input type="checkbox" id="sd-exch" ${cart.exchange ? "checked" : ""}><span></span></label></div>
    ${row("sd-note", "📝", "Заметка", cart.note ? esc(cart.note.slice(0, 40)) : "")}
    <div class="bottom-bar"><button class="btn btn-soft" id="sd-later">Отложить</button><button class="btn btn-main" id="sd-pay">Перейти к оплате</button></div>`;
  $("sd-client").onclick = clientSheet;
  if ($("sd-point")) $("sd-point").onclick = () => pickSheet("📍 Точка", S.points.filter(p => p.active).map(p => [p.id, p.title]), v => { cart.point_id = +v; });
  $("sd-seller").onclick = () => pickSheet("🧑‍💼 Продавец", S.sellers.filter(x => !cart.point_id && !S.me.point_id || !x.point_id || x.point_id === (+cart.point_id || S.me.point_id)).map(x => [x.id, x.name]), v => { cart.seller_id = +v; });
  $("sd-disc").onclick = () => valueSheet("％ Скидка", "discount_mode", "discount_value", true);
  if ($("sd-spend")) $("sd-spend").onclick = () => {
    const max = Math.min(c.points, Math.floor(toPay * S.settings.spend_limit_pct / 100));
    invSheet(`<h3>🎁 Оплатить баллами</h3><div class="hint" style="font-size:13px;margin:-6px 0 10px">Доступно ${sm(c.points)} · максимум ${sm(max)} (${S.settings.spend_limit_pct}% покупки)</div>
      <input id="sv-v" inputmode="numeric" value="${snum(cart.spend) || ""}" placeholder="0"><div class="quick" style="margin:0 0 10px"><button id="sv-max">Максимум ${sm(max)}</button><button id="sv-0">Не списывать</button></div>${sheetBtns("Готово")}`);
    $("sv-max").onclick = () => { $("sv-v").value = max; }; $("sv-0").onclick = () => { $("sv-v").value = ""; };
    $("is-cancel").onclick = invSheetClose;
    $("is-ok").onclick = () => { const v = snum($("sv-v").value); if (v > max) return toast("Не больше " + sm(max)); cart.spend = v; invSheetClose(); sellDetails(); };
  };
  $("sd-loy").onchange = e => { cart.loyalty_on = e.target.checked; sellDetails(); };
  $("sd-loy-v").onclick = () => cart.loyalty_on && valueSheet("⭐ Баллы лояльности", "loyalty_mode", "loyalty_value", true);
  $("sd-bonus").onclick = () => valueSheet("🎀 Бонус", "bonus_mode", "bonus_value", true,
    "При оплате Uzum Nasiya / рассрочкой бонус замораживается на 6 месяцев, иначе сразу становится баллами.");
  $("sd-exch").onchange = e => { cart.exchange = e.target.checked; };
  $("sd-note").onclick = () => { invSheet(`<h3>📝 Заметка</h3><textarea id="sv-note" rows="4" maxlength="300">${esc(cart.note)}</textarea>${sheetBtns("Готово")}`);
    $("is-cancel").onclick = invSheetClose; $("is-ok").onclick = () => { cart.note = $("sv-note").value.trim(); invSheetClose(); sellDetails(); }; };
  $("sd-later").onclick = postpone;
  $("sd-pay").onclick = () => {
    if (!cart.client) return clientSheet();
    if (!cart.seller_id) return toast("Выберите продавца");
    if (S.me.role === "owner" && !cart.point_id) return toast("Выберите точку");
    cart.step = "pay"; shopSell(); window.scrollTo(0, 0);
  };
}
function pickSheet(title, list, onPick) {
  invSheet(`<h3>${title}</h3>${list.map(([v, t]) => `<button class="card row-btn" data-pk="${v}" style="padding:14px 16px !important"><span class="rt"><b style="font-size:15px">${esc(t)}</b></span><span class="ra">›</span></button>`).join("") || '<div class="empty">Пусто</div>'}
    <button class="btn btn-soft" style="width:100%" id="is-cancel">Отмена</button>`);
  $("is-cancel").onclick = invSheetClose;
  document.querySelectorAll("[data-pk]").forEach(b => b.onclick = () => { onPick(b.dataset.pk); invSheetClose(); sellDetails(); });
}
function valueSheet(title, modeKey, valKey, allowPct, hint) {
  invSheet(`<h3>${title}</h3>${hint ? `<div class="hint" style="font-size:12.5px;margin:-6px 0 10px">${hint}</div>` : ""}
    <div class="seg" style="margin-bottom:10px"><button data-vm="sum" class="${cart[modeKey] !== "pct" ? "on" : ""}">Сумма</button>${allowPct ? `<button data-vm="pct" class="${cart[modeKey] === "pct" ? "on" : ""}">% от покупки</button>` : ""}</div>
    <input id="sv-v" inputmode="decimal" value="${snum(cart[valKey]) || ""}" placeholder="0">${sheetBtns("Готово")}`);
  let mode = cart[modeKey] === "pct" ? "pct" : "sum";
  document.querySelectorAll("[data-vm]").forEach(b => b.onclick = () => { mode = b.dataset.vm; document.querySelectorAll("[data-vm]").forEach(x => x.classList.toggle("on", x === b)); });
  $("is-cancel").onclick = invSheetClose;
  $("is-ok").onclick = () => { const v = snum($("sv-v").value); if (mode === "pct" && v > 100) return toast("Не больше 100%");
    cart[modeKey] = mode; cart[valKey] = v; invSheetClose(); sellDetails(); };
}
function clientSheet() {
  invSheet(`<h3>👤 Клиент</h3>
    <input id="cs-q" placeholder="Номер (часть) или имя / фамилия" autocomplete="off" value="" style="margin-bottom:6px">
    <div class="hint" style="font-size:12px;margin-bottom:8px">Например: <b>4567</b>, <b>90 123</b>, <b>Malika</b>, <b>Karimova</b></div>
    <div id="cs-res" style="max-height:52vh;overflow-y:auto"></div>
    <button class="btn btn-soft" style="width:100%;margin-top:8px" id="is-cancel">Отмена</button>`);
  $("is-cancel").onclick = invSheetClose;
  const SRC = {erp: "🧾", bot: "🤖", store: "🏪"};
  const pick = async phone => {
    let c; try { c = await api("/api/shop/client", {phone}); } catch (e) { return toast(e.message); }
    if (c.error) return toast(c.error);
    $("cs-res").innerHTML = `<div class="cl-card"><b>${esc(c.name || "Новый клиент")}</b> ${c.in_bot ? '<span class="badge">в боте</span>' : ""}${c.tier ? `<span class="badge">${esc(c.tier)}</span>` : ""}
      <div class="hint" style="font-size:12.5px">${esc(c.phone)} · 🎁 баллы: <b style="color:var(--green)">${sm(c.points)}</b>${c.frozen ? ` · 🧊 ${sm(c.frozen)}` : ""}</div>
      ${!c.found ? `<input id="cs-name" maxlength="80" placeholder="Имя и фамилия" value="${esc(c.name || "")}" style="margin:8px 0 0">` : ""}
      ${c.note ? `<div class="hint" style="font-size:12px;margin-top:4px">📝 ${esc(c.note)}</div>` : ""}</div>
      <button class="btn btn-main" style="width:100%" id="cs-ok">Выбрать клиента</button>`;
    $("cs-ok").onclick = () => { if (!c.found) c.name = ($("cs-name").value || "").trim() || c.name;
      cart.client = c; if (snum(cart.spend) > c.points) cart.spend = 0; invSheetClose(); sellDetails(); };
  };
  let t = null, seq = 0;
  const search = async () => {
    const q = $("cs-q").value.trim(), digits = q.replace(/\D/g, ""), my = ++seq;
    if (digits.length < 3 && q.replace(/[\d\s+()-]/g, "").length < 2) { $("cs-res").innerHTML = ""; return; }
    let r; try { r = await api("/api/shop/client", {q}); } catch (e) { return toast(e.message); }
    if (my !== seq) return;
    const items = r.items || [], full = digits.length >= 9;
    const exact = full && items.some(x => x.tail === digits.slice(-9));
    $("cs-res").innerHTML = items.map(x => `<button class="card row-btn" data-cpick="${esc(x.phone)}" style="padding:12px 14px !important;margin-bottom:8px">
        <span class="rt"><b style="font-size:15px">${esc(x.name || "Без имени")}</b><span style="color:var(--hint);font-weight:500">${esc(x.phone)} ${x.src.map(k => SRC[k]).join("")}</span></span>
        ${x.points ? `<span class="ra" style="color:var(--green)">🎁 ${sm(x.points)}</span>` : '<span class="ra">›</span>'}</button>`).join("")
      + (full && !exact ? `<button class="card row-btn" id="cs-new" style="padding:12px 14px !important"><span class="ri">➕</span><span class="rt"><b style="font-size:15px">Новый клиент</b><span>${esc(digits.length === 9 ? "+998" + digits : "+" + digits)}</span></span></button>` : "")
      + (!items.length && !full ? `<div class="empty">Никого не нашли. Введите номер полностью, чтобы добавить нового клиента.</div>` : "");
    document.querySelectorAll("[data-cpick]").forEach(b => b.onclick = () => pick(b.dataset.cpick));
    if ($("cs-new")) $("cs-new").onclick = () => pick(digits);
  };
  $("cs-q").oninput = () => { clearTimeout(t); t = setTimeout(search, 300); };
  $("cs-q").onkeydown = e => { if (e.key === "Enter") { clearTimeout(t); search(); } };
  if (cart.client) { $("cs-q").value = cart.client.phone; search(); }
  setTimeout(() => $("cs-q").focus(), 100);
}
/* --- продажа: 3) оплата --- */
function sellPay() {
  $("sh-title").textContent = "Оплата";
  const head = $("sh-head-btn"); head.style.visibility = "visible"; head.textContent = "‹"; head.onclick = () => { cart.step = "details"; shopSell(); };
  const types = shopS.pay_types.filter(t => t.active), need = Math.max(0, cartToPay() - snum(cart.spend));
  if (!cart.mixed) { cart.pays = cart.single ? {[cart.single]: need} : {}; }
  const paid = Object.values(cart.pays).reduce((a, v) => a + snum(v), 0), left = need - paid;
  $("shop-body").innerHTML = `<div class="card" style="text-align:center;padding:18px 14px">
      <div style="font-size:40px;font-weight:800">${sm(need)} <small class="hint" style="font-size:18px">UZS</small></div>
      ${snum(cart.spend) ? `<div class="hint" style="font-size:13px">ещё ${sm(cart.spend)} баллами</div>` : ""}</div>
    <div class="card row-btn" style="cursor:default"><span class="rt"><b>Смешанная оплата</b></span>
      <label class="sw"><input type="checkbox" id="sp-mixed" ${cart.mixed ? "checked" : ""}><span></span></label></div>
    <div class="pay-tiles">${types.map(t => { const on = t.id in cart.pays; return `<button class="card pay-tile ${on ? "on" : ""}" data-tile="${t.id}">
      <span class="pi">${esc(t.icon)}</span><b>${esc(t.title)}</b>${t.kind === "nasiya" ? '<div class="hint" style="font-size:11px">🧊 бонус заморозится</div>' : t.kind === "debt" ? '<div class="hint" style="font-size:11px">📝 в долг</div>' : ""}
      ${cart.mixed && on ? `<input data-tamt="${t.id}" inputmode="numeric" value="${cart.pays[t.id] || ""}" placeholder="сумма">` : ""}</button>`; }).join("")}</div>
    <div class="to-pay" style="color:${Math.abs(left) < 0.5 && need ? "var(--green)" : "var(--red)"}">${Math.abs(left) < 0.5 ? (need ? "✅ Всё оплачено" : "Оплата баллами") : left > 0 ? "К оплате: " + sm(left) + " UZS" : "Лишние: " + sm(-left) + " UZS"}</div>
    <div class="bottom-bar"><button class="btn btn-soft" id="sp-later">Отложить</button><button class="btn btn-main" id="sp-pay" ${Math.abs(left) < 0.5 ? "" : "disabled"}>Оплатить</button></div>`;
  $("sp-mixed").onchange = e => { cart.mixed = e.target.checked; if (cart.mixed && cart.single) cart.pays = {[cart.single]: need}; if (!cart.mixed) cart.pays = {}; sellPay(); };
  document.querySelectorAll("[data-tile]").forEach(b => b.onclick = e => {
    if (e.target.tagName === "INPUT") return;
    const id = +b.dataset.tile;
    if (!cart.mixed) { cart.single = cart.single === id ? null : id; }
    else if (id in cart.pays) delete cart.pays[id];
    else { const rest = need - Object.values(cart.pays).reduce((a, v) => a + snum(v), 0); cart.pays[id] = rest > 0 ? rest : ""; }
    sellPay();
  });
  document.querySelectorAll("[data-tamt]").forEach(i => { i.onclick = e => e.stopPropagation(); i.onchange = () => { cart.pays[+i.dataset.tamt] = snum(i.value); sellPay(); }; });
  $("sp-later").onclick = postpone;
  $("sp-pay").onclick = () => tg.showConfirm(`Провести продажу ${sm(cartToPay())} UZS — ${cart.client.name || cart.client.phone}?`, ok => ok && submitSale());
}
function cartBody() {
  return {phone: cart.client ? cart.client.phone : "", name: cart.client ? cart.client.name || "" : "", amount: cartTotal(),
    discount_mode: cart.discount_mode, discount_value: snum(cart.discount_value), seller_id: +cart.seller_id || null,
    point_id: +cart.point_id || null, points_spent: snum(cart.spend),
    payments: Object.entries(cart.pays).filter(([, v]) => snum(v)).map(([k, v]) => ({type_id: +k, amount: snum(v)})),
    loyalty_on: cart.loyalty_on, loyalty_mode: cart.loyalty_mode, loyalty_value: snum(cart.loyalty_value),
    bonus_mode: cart.bonus_mode, bonus_value: snum(cart.bonus_value), exchange: cart.exchange, note: cart.note, draft_id: cart.draft_id};
}
async function submitSale() {
  try { const r = await api("/api/shop/sale", cartBody()); toast(r.msg);
    if (r.ok) { if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success"); cart = newCart(); loadShop(); }
  } catch (e) { toast(e.message); }
}
async function postpone() {
  if (!cartTotal()) return toast("Введите сумму");
  try { const r = await api("/api/shop/draft", {id: cart.draft_id, data: Object.assign(cartBody(), {_cart: cart})}); toast(r.msg);
    if (r.ok) { cart = newCart(); loadShop(); } } catch (e) { toast(e.message); }
}
/* --- транзакции --- */
function shopList() {
  $("sh-title").textContent = "Транзакции";
  const head = $("sh-head-btn"); head.style.visibility = "visible"; head.textContent = shopS.summary.count; head.onclick = null;
  const groups = {};
  shopS.sales.forEach(x => { (groups[x.date] = groups[x.date] || []).push(x); });
  $("shop-body").innerHTML = sourceSwitch("ts") + `<div class="card" style="padding:10px 12px"><input id="tx-q" placeholder="🔍 ID, клиент, продавец" value="${esc(shopQ)}" style="margin:0"></div>`
    + pointChips("tp") + (Object.keys(groups).length ? Object.entries(groups).map(([d, list]) => `<div class="tx-date"><span>${d}</span><span class="hint" style="font-size:13px">${list.length} · ${sm(list.reduce((a, x) => a + (x.status === "return" ? -1 : x.status === "exchange" ? 0 : 1) * (x.amount - (x.discount || 0)), 0))}</span></div>
    <div class="card" style="padding:4px 16px">${list.map(x => `<div class="tx" data-tx="${esc(String(x.id))}">
      <div class="inv-top"><span class="tx-a" style="${x.status === "return" ? "color:var(--red)" : ""}">${x.status === "return" ? "Возврат" : x.status === "exchange" ? "Обмен" : "Продажа"} #${esc(String(x.id))}</span><span class="tx-a" style="${x.status === "return" ? "color:var(--red)" : ""}">${sm(x.amount - (x.discount || 0))} UZS</span></div>
      <div class="inv-top" style="margin-top:6px"><b>${x.time}</b><span class="hint" style="font-size:13px"><span class="dot-c" style="background:${POINT_COLORS[(shopS.points.findIndex(p => p.id === x.point_id) + POINT_COLORS.length) % POINT_COLORS.length]}"></span>${esc(x.point || "—")}</span></div>
      <div class="inv-top" style="margin-top:8px"><span class="pill">👤 ${esc(x.client_name || x.client_phone || "—")}</span>${x.source === "billz" && x.units ? `<span class="pill">${x.units} ед.</span>` : ""}
        ${x.loyalty_amount ? `<span class="pill">+${sm(x.loyalty_amount)}</span>` : ""}</div>
      ${String(shopOpenTx) === String(x.id) ? `<div class="tot">${x.pays.map(p => `${p.icon} ${esc(p.title)}: <b>${sm(p.amount)}</b>`).join("<br>")}
        ${x.points_spent ? `<br>🎁 Баллами: <b>${sm(x.points_spent)}</b>` : ""}${x.discount ? `<br>％ Скидка: <b>${sm(x.discount)}</b> (сумма ${sm(x.amount)})` : ""}
        <br>🧑‍💼 Продавец: <b>${esc(x.seller)}</b> · касса: ${esc(x.cashier)}
        ${x.bonus_amount ? `<br>${x.bonus_frozen ? "🧊" : "🎀"} Бонус: <b>${sm(x.bonus_amount)}</b>` : ""}${x.exchange ? "<br>🔁 Обмен разрешён" : ""}${x.debt_amount ? `<br>📝 В долг: <b>${sm(x.debt_amount)}</b>` : ""}
        ${x.note ? `<br>📝 ${esc(x.note)}` : ""}</div>` : ""}</div>`).join("")}</div>`).join("")
    : `<div class="card empty">${shopS.source === "billz" ? "В копии BILLZ за этот период продаж нет — нажмите «🔄 Обновить» в сверке на Главной" : "Продаж за этот период нет"}</div>`) + dateBar();
  bindPointChips("tp"); bindDateBar(); bindSourceSwitch("ts");
  let t = null; $("tx-q").oninput = e => { clearTimeout(t); t = setTimeout(() => { shopQ = e.target.value; loadShop().then(() => { const q = $("tx-q"); if (q) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); } }); }, 350); };
  document.querySelectorAll("[data-tx]").forEach(r => r.onclick = () => { shopOpenTx = String(shopOpenTx) === r.dataset.tx ? null : r.dataset.tx; shopList(); });
}
/* --- меню --- */
function shopMenu() {
  const p = shopS.me.perms;
  if (shopSub === "bonus") { $("sh-title").textContent = "Бонусы"; backToMenu(); return shopBonus(); }
  if (shopSub === "set") { $("sh-title").textContent = "Настройки"; backToMenu(); return shopSettings(); }
  if (shopSub === "drafts") { $("sh-title").textContent = "Отложенные"; backToMenu(); return shopDrafts(); }
  $("sh-title").textContent = "Меню";
  const items = [];
  if (p.includes("sell")) items.push(["drafts", "⏸", `Отложенные${shopS.drafts.length ? " (" + shopS.drafts.length + ")" : ""}`]);
  if (p.includes("unfreeze")) items.push(["bonus", "🧊", `Бонусы${(shopS.bonuses || []).length ? " (" + shopS.bonuses.length + ")" : ""}`]);
  if (p.includes("settings")) items.push(["set", "⚙️", "Настройки"]);
  $("shop-body").innerHTML = `<div class="card"><b style="font-size:19px">🧾 Продажи</b><div class="menu-grid" style="margin-top:12px">
      ${items.map(([k, i, t]) => `<button class="card" data-mi="${k}"><span>${i}</span>${t}</button>`).join("")}</div></div>
    ${p.includes("settings") && shopS.billz_on ? `<div class="card"><b>🔄 Из BILLZ</b><div class="hint" style="font-size:12.5px;margin:4px 0 10px">Загрузить точки (магазины) и типы оплаты из BILLZ — чтобы всё совпадало.</div>
      <button class="btn btn-soft" style="width:100%" id="mi-import">Загрузить точки и типы оплаты</button></div>` : ""}
    <div class="card"><b>Скоро</b><div class="hint" style="font-size:13px;line-height:1.7;margin-top:4px">📝 Долги и «Ожидаемая прибыль» · 📈 KPI продавцов и старших · ↩️ Возвраты · 🎯 Акции</div></div>`;
  document.querySelectorAll("[data-mi]").forEach(b => b.onclick = () => { shopSub = b.dataset.mi; renderShop(); window.scrollTo(0, 0); });
  if ($("mi-import")) $("mi-import").onclick = () => { toast("Загружаю из BILLZ…");
    api("/api/shop/settings", {action: "import_billz"}).then(r => { toast(r.msg); loadShop(); }).catch(e => toast(e.message)); };
}
function backToMenu() { const h = $("sh-head-btn"); h.style.visibility = "visible"; h.textContent = "‹"; h.onclick = () => { shopSub = null; renderShop(); }; }
function shopDrafts() {
  const list = shopS.drafts;
  $("shop-body").innerHTML = `<div class="card">${list.length ? list.map(d => `<div class="sale"><div class="inv-top"><b>${esc(d.title)}</b><span class="hint" style="font-size:12.5px">${d.when}</span></div>
      <div class="pmeta"><span>касса: ${esc(d.cashier)}</span></div>
      <div class="inv-act"><button class="btn btn-main btn-sm" data-dopen="${d.id}">Открыть</button><button class="btn btn-soft btn-sm" data-ddel="${d.id}">Удалить</button></div></div>`).join("")
    : `<div class="empty">Отложенных продаж нет</div>`}</div>`;
  document.querySelectorAll("[data-dopen]").forEach(b => b.onclick = () => { const d = list.find(x => x.id === +b.dataset.dopen);
    cart = Object.assign(newCart(), d.data._cart || {}, {draft_id: d.id, step: "details"}); shopTab = "sell"; shopSub = null; loadShop(); });
  document.querySelectorAll("[data-ddel]").forEach(b => b.onclick = () => tg.showConfirm("Удалить отложенную продажу?", ok => ok &&
    api("/api/shop/draft", {action: "delete", id: +b.dataset.ddel}).then(r => { toast(r.msg); loadShop(); })));
}
/* --- замороженные бонусы --- */
function shopBonus() {
  const list = shopS.bonuses || [], now = new Date().toISOString().slice(0, 10);
  $("shop-body").innerHTML = `<div class="card"><div class="hint" style="font-size:12.5px;margin-bottom:8px">Бонусы за Uzum Nasiya / рассрочку. Когда клиент закроет рассрочку — разморозьте: бонус перейдёт в баллы.</div>
    ${list.length ? list.map(b => { const due = b.unfreeze_at.slice(0, 10) <= now; return `<div class="sale"><div class="inv-top"><b>${esc(b.name || b.phone)}</b><b>${sm(b.amount)}</b></div>
      <div class="pmeta"><span>продажа №${b.sale_id}</span><span style="${due ? "color:var(--green);font-weight:700" : ""}">${due ? "✅ срок наступил" : "🧊 до"} ${b.unfreeze_at.slice(0, 10).split("-").reverse().join(".")}</span></div>
      <button class="btn ${due ? "btn-main" : "btn-soft"} btn-sm" style="margin-top:6px" data-unf="${b.id}">🔓 Разморозить</button></div>`; }).join("")
    : `<div class="empty">Замороженных бонусов нет</div>`}</div>`;
  document.querySelectorAll("[data-unf]").forEach(b => b.onclick = () => tg.showConfirm("Клиент закрыл рассрочку? Разморозить бонус и перевести в баллы?", ok => ok &&
    api("/api/shop/unfreeze", {id: +b.dataset.unf}).then(r => { toast(r.msg); loadShop(); }).catch(e => toast(e.message))));
}
/* --- настройки --- */
function shopSettings() {
  const S = shopS, st = S.settings;
  const opt = (list, cur, empty) => (empty ? `<option value="">${empty}</option>` : "") + list.map(([v, t]) => `<option value="${v}" ${String(v) === String(cur) ? "selected" : ""}>${esc(t)}</option>`).join("");
  const staffName = id => (S.staff.find(x => x.id === id) || {}).name || "";
  $("shop-body").innerHTML = `
    <div class="card"><h3>🎁 Баллы лояльности</h3>
      <div class="two"><div><div class="f-lbl" style="margin-top:0">% по умолчанию</div><input id="ss-pct" inputmode="decimal" value="${st.loyalty_pct}"></div>
        <div><div class="f-lbl" style="margin-top:0">Оплата баллами до, %</div><input id="ss-lim" inputmode="decimal" value="${st.spend_limit_pct}"></div></div>
      <div class="f-lbl">Сгорание: через сколько дней без покупок (0 — не сгорают)</div><input id="ss-exp" inputmode="numeric" value="${st.loyalty_expire_days}">
      <div class="sw-row" style="border-top:1px solid var(--line)"><div><b>ERP — основной для клиентов</b><div class="hint" style="font-size:12px">«Спасибо за покупку» и баллы в кабинете клиента — из ERP. На тестовый месяц оставьте выключенным.</div></div>
        <label class="sw"><input type="checkbox" id="ss-live" ${st.client_live ? "checked" : ""}><span></span></label></div>
      <button class="btn btn-main" style="width:100%;margin-top:10px" id="ss-save">Сохранить</button></div>
    <div class="card"><h3>📍 Точки продаж</h3>
      ${S.points.map(p => `<div class="st-row"><div class="two"><input data-ptitle="${p.id}" value="${esc(p.title)}" style="margin:0"><input data-paddr="${p.id}" value="${esc(p.address || "")}" placeholder="адрес" style="margin:0"></div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px"><label class="chk" style="margin:0"><input type="checkbox" data-pact="${p.id}" ${p.active ? "checked" : ""}>работает</label>
        <button class="btn btn-soft btn-sm" data-psave="${p.id}">Сохранить</button></div></div>`).join("")}
      <div class="add-row" style="margin-top:8px"><input id="np-point" placeholder="Новая точка" style="margin:0"><button class="btn btn-main" id="np-point-add">＋</button></div></div>
    <div class="card"><h3>💳 Типы оплаты</h3>
      ${S.pay_types.map(t => `<div class="st-row"><div style="display:grid;grid-template-columns:56px 1fr;gap:6px"><input data-ticon="${t.id}" value="${esc(t.icon)}" style="margin:0;text-align:center"><input data-ttitle="${t.id}" value="${esc(t.title)}" style="margin:0"></div>
        <select data-tkind="${t.id}" style="margin:6px 0 0">${opt(Object.entries(S.pay_kinds), t.kind)}</select>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px"><label class="chk" style="margin:0"><input type="checkbox" data-tact="${t.id}" ${t.active ? "checked" : ""}>включён</label>
        <button class="btn btn-soft btn-sm" data-tsave="${t.id}">Сохранить</button></div></div>`).join("")}
      <div style="display:grid;grid-template-columns:56px 1fr auto;gap:6px;margin-top:8px"><input id="nt-icon" value="💳" style="margin:0;text-align:center"><input id="nt-title" placeholder="Новый тип оплаты" style="margin:0"><button class="btn btn-main" id="nt-add">＋</button></div></div>
    <div class="card"><h3>👥 Сотрудники, роли и KPI</h3>
      <div class="hint" style="font-size:12px;margin-bottom:6px">Кассир вводит продажи. Продавец видит только свои. Старший — свои и прямых помощников (KPI старшего — % от их продаж).</div>
      ${S.staff.map(x => `<div class="st-row"><b>${esc(x.name)}</b>
        <div class="two" style="margin-top:6px"><select data-srole="${x.id}" style="margin:0">${opt([["none", "— нет доступа —"], ...Object.entries(S.roles).filter(([k]) => k !== "owner")], x.role)}</select>
          <select data-spoint="${x.id}" style="margin:0">${opt(S.points.map(p => [p.id, p.title]), x.point_id, "— точка —")}</select></div>
        <div class="two" style="margin-top:6px"><input data-skpi="${x.id}" inputmode="decimal" value="${x.kpi_pct}" placeholder="KPI %" style="margin:0">
          <select data-ssen="${x.id}" style="margin:0">${opt(S.staff.filter(y => y.id !== x.id).map(y => [y.id, "старший: " + y.name]), x.senior_id, "— без старшего —")}</select></div>
        <div class="two" style="margin-top:6px"><div class="hint" style="font-size:12px;align-self:center">KPI старшего с продаж этого сотрудника, %</div><input data-sspct="${x.id}" inputmode="decimal" value="${x.senior_pct}" style="margin:0"></div>
        <div class="perm-chk">${Object.entries(S.perm_titles).map(([k, t]) => `<label><input type="checkbox" data-sperm="${x.id}" value="${k}" ${x.perms.includes(k) ? "checked" : ""}>${esc(t)}</label>`).join("")}</div>
        <button class="btn btn-soft btn-sm" style="margin-top:8px" data-ssave="${x.id}">Сохранить</button></div>`).join("")}</div>`;
  const post = body => api("/api/shop/settings", body).then(r => { toast(r.msg); if (r.ok) loadShop(); }).catch(e => toast(e.message));
  $("ss-save").onclick = () => post({action: "settings", loyalty_pct: $("ss-pct").value, spend_limit_pct: $("ss-lim").value,
    loyalty_expire_days: $("ss-exp").value, client_live: $("ss-live").checked});
  document.querySelectorAll("[data-psave]").forEach(b => b.onclick = () => { const i = b.dataset.psave;
    post({action: "point", id: +i, title: document.querySelector(`[data-ptitle="${i}"]`).value, address: document.querySelector(`[data-paddr="${i}"]`).value,
      active: document.querySelector(`[data-pact="${i}"]`).checked}); });
  $("np-point-add").onclick = () => post({action: "point", title: $("np-point").value});
  document.querySelectorAll("[data-tsave]").forEach(b => b.onclick = () => { const i = b.dataset.tsave;
    post({action: "pay_type", id: +i, title: document.querySelector(`[data-ttitle="${i}"]`).value, icon: document.querySelector(`[data-ticon="${i}"]`).value,
      kind: document.querySelector(`[data-tkind="${i}"]`).value, active: document.querySelector(`[data-tact="${i}"]`).checked}); });
  $("nt-add").onclick = () => post({action: "pay_type", title: $("nt-title").value, icon: $("nt-icon").value, kind: "normal"});
  document.querySelectorAll("[data-ssave]").forEach(b => b.onclick = () => { const i = b.dataset.ssave, q = k => document.querySelector(`[data-${k}="${i}"]`);
    post({action: "staff", user_id: +i, role: q("srole").value, point_id: q("spoint").value, kpi_pct: q("skpi").value, senior_id: q("ssen").value,
      senior_pct: q("sspct").value, perms: [...document.querySelectorAll(`[data-sperm="${i}"]:checked`)].map(x => x.value)}); });
}
const SOON = "💰 Раздел «Инвестиции» скоро появится ⏳";
$("go-shop").onclick = () => { show("shop"); loadShop(); };
$("go-shop-me").onclick = () => { shopTab = "sell"; show("shop"); loadShop(); };
/* Своя задача сотрудника: на сегодня или завтра */
let mtDay = "today";
document.querySelectorAll("#mt-day [data-day]").forEach(b => b.onclick = () => {
  mtDay = b.dataset.day;
  document.querySelectorAll("#mt-day [data-day]").forEach(x => x.classList.toggle("on", x === b));
  $("mt-date").classList.toggle("hidden", mtDay !== "pick");
  if (mtDay === "pick") { const t = new Date(); $("mt-date").min = t.toISOString().slice(0, 10); if ($("mt-date").showPicker) try { $("mt-date").showPicker(); } catch (e) {} }
});
$("mt-add").onclick = () => {
  const title = $("mt-title").value.trim(); if (!title) return toast(sx("mt_ph"));
  const day = mtDay === "pick" ? $("mt-date").value : mtDay;
  if (!day) return toast(sx("mt_pick"));
  action("/api/my_task", {action: "add", title, day}).then(() => { $("mt-title").value = ""; });
};
$("mt-title").onkeydown = e => { if (e.key === "Enter") $("mt-add").onclick(); };
$("go-inv").onclick = () => data.invest_enabled ? show("invest") : toast(data.profile ? sx("invest_soon") : SOON);
$("go-inv-own").onclick = () => data.invest_enabled ? show("invest") : toast(SOON);
$("go-clients").onclick = () => show("clients");
document.querySelectorAll("[data-dgo], #tabbar [data-tab]").forEach(b => b.onclick = () => show(b.dataset.dgo || b.dataset.tab));
/* Главная руководителя: плитки с цифрами и «Мой день» */
/* Рабочий день руководителя: Пришёл / Ушёл и часы (цель — 8–10 часов в день) */
const BD_GOAL = 8, BD_MAX = 10, WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const hm = h => { const m = Math.round(h * 60); return `${Math.floor(m / 60)} ч ${String(m % 60).padStart(2, "0")} м`; };
let bdTimer = null;
function renderBossDay() {
  const b = data.boss_day; $("bday").classList.toggle("hidden", !b); if (!b) return;
  $("bd-btn").textContent = b.on_site ? "🚪 Ушёл" : "✅ Пришёл";
  $("bd-btn").className = "btn " + (b.on_site ? "btn-soft" : "btn-main");
  const t0 = Date.now();
  const tick = () => {
    const today = b.today + (b.on_site ? (Date.now() - t0) / 3600000 : 0);
    $("bd-lbl").textContent = b.on_site ? `🟢 На работе с ${b.since}` : (b.came_today ? "Сегодня отработано" : "Мой рабочий день");
    $("bd-state").textContent = b.on_site || b.came_today ? hm(today) : "Ещё не пришёл";
    $("bd-bar").style.width = Math.min(100, today / BD_MAX * 100) + "%";
    $("bd-goal-t").textContent = (today >= BD_GOAL
      ? (today >= BD_MAX ? "🔥 10+ часов — отличный день" : "✅ 8 часов есть, цель 8–10 ч") : `до 8 часов: ${hm(BD_GOAL - today)}`);
  };
  tick(); clearInterval(bdTimer); if (b.on_site) bdTimer = setInterval(tick, 30000);
  $("bd-week").textContent = hm(b.week).replace(" м", "м").replace(" ч", "ч");
  $("bd-month").textContent = hm(b.month).replace(" м", "м").replace(" ч", "ч");
  $("bd-avg").textContent = hm(b.avg).replace(" м", "м").replace(" ч", "ч");
  $("bd-list").innerHTML = b.history.length ? b.history.map(r => `<div class="bd-row"><span>${WD[r.wd]} ${r.day}</span>
    <span style="color:var(--muted)">${r.in} – ${r.out || "сейчас"}${r.auto ? " (авто)" : ""}</span><b>${hm(r.h)}</b></div>`).join("")
    : `<div class="empty">Смен пока нет</div>`;
}
$("bd-btn").onclick = () => action(data.boss_day.on_site ? "/api/checkout" : "/api/checkin");
/* Микрофон «как есть»: без шумо- и эхоподавления и автоусиления браузера — они глушат начало и рвут голос */
const RAW_AUDIO = {echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1};
/* Видео-подтверждение прихода прямо в кабинете: камера на экране, человек говорит вслух число */
const REC_MAX = 8;
let recStream = null, recRec = null, recTimer = null, recMax = REC_MAX, recOnSend = null;
function recStop() {
  clearInterval(recTimer);
  if (recRec && recRec.state !== "inactive") { recRec.onstop = null; recRec.stop(); }
  if (recStream) recStream.getTracks().forEach(t => t.stop());
  recStream = recRec = null;
}
function recClose() { recStop(); $("rec").classList.add("hidden"); $("rec-v").srcObject = null; }
async function recSend(blob, dur) {
  $("rec-hint").textContent = sx("rec_send"); $("rec-btn").disabled = true;
  const url = await new Promise(ok => { const fr = new FileReader(); fr.onload = () => ok(fr.result); fr.readAsDataURL(blob); });
  if (recOnSend) return recOnSend(url, dur);  // другой режим (кружок клиенту из CRM)
  try {
    const r = await api("/api/proof_video", {video: url, duration: dur});
    if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(r.ok ? "success" : "warning");
    if (!r.ok) { $("rec-hint").textContent = r.msg; $("rec-btn").disabled = false; $("rec-btn").textContent = sx("rec_again");
                 return recCamera(); }
    recClose(); toast(r.msg); load();
  } catch (e) { $("rec-hint").textContent = e.message; $("rec-btn").disabled = false; }
}
async function recCamera() {
  if (recStream) return true;
  try {
    recStream = await navigator.mediaDevices.getUserMedia({video: {facingMode: "user", width: {ideal: 480}, height: {ideal: 480}},
                                                           audio: RAW_AUDIO});
  } catch (e) { return false; }
  $("rec-v").srcObject = recStream; $("rec-ph").classList.add("hidden");
  return true;
}
async function openRecorder(r) {
  const minSec = r.min_sec || 3;
  recOnSend = r.onSend || null; recMax = r.max || REC_MAX;
  $("rec-code").textContent = r.code || ""; $("rec-code").classList.toggle("hidden", !r.code); $("rec").classList.remove("hidden");
  $("rec-prog").style.transition = "none"; $("rec-prog").style.strokeDashoffset = 301.6;
  $("rec-ph").classList.remove("hidden"); $("rec-btn").textContent = sx("rec_btn");
  $("rec-say").textContent = r.title || sx("rec_say"); $("rec-hint").textContent = sx("rec_cam");
  $("rec-file-t").textContent = sx("rec_open"); $("rec-x").textContent = sx("rec_cancel");
  $("rec-x").onclick = recClose;
  const canRec = window.MediaRecorder && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && await recCamera();
  $("rec-btn").classList.toggle("hidden", !canRec); $("rec-file").classList.toggle("hidden", !!canRec);
  if (!canRec) {  // камера в странице недоступна — системная камера телефона
    $("rec-hint").textContent = r.onSend ? "Запишите видео на камеру телефона" : sx("rec_sys", {a: minSec, b: recMax, m: r.minutes});
    $("rec-input").value = "";
    $("rec-input").onchange = () => {
      const f = $("rec-input").files[0]; if (!f) return;
      const v = document.createElement("video"); v.preload = "metadata";
      v.onloadedmetadata = () => { URL.revokeObjectURL(v.src); recSend(f, v.duration || 0); };
      v.onerror = () => recSend(f, 0);
      v.src = URL.createObjectURL(f);
    };
    return;
  }
  $("rec-hint").textContent = r.onSend ? `Нажмите «Записать». До ${recMax} сек.` : sx("rec_go", {a: minSec, b: recMax});
  $("rec-btn").disabled = false;
  $("rec-btn").onclick = () => {
    if (recRec && recRec.state === "recording") {
      if ((Date.now() - recRec.t0) / 1000 < minSec) return toast(sx("rec_short", {s: minSec}));
      return recRec.stop();
    }
    const type = ["video/mp4", "video/webm;codecs=vp8,opus", "video/webm"].find(t => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t));
    const chunks = [];
    recRec = new MediaRecorder(recStream, Object.assign({videoBitsPerSecond: 700000, audioBitsPerSecond: 128000}, type ? {mimeType: type} : {}));
    recRec.ondataavailable = e => e.data && e.data.size && chunks.push(e.data);
    recRec.onstop = () => {
      clearInterval(recTimer);
      const dur = (Date.now() - recRec.t0) / 1000, mime = (recRec.mimeType || type || "video/webm").split(";")[0];
      if (recStream) recStream.getTracks().forEach(t => t.stop()); recStream = null;
      recSend(new Blob(chunks, {type: mime}), dur);
    };
    recRec.t0 = Date.now(); recRec.start(250);
    $("rec-prog").style.transition = `stroke-dashoffset ${recMax}s linear`; $("rec-prog").style.strokeDashoffset = 0;
    $("rec-btn").textContent = sx("rec_done");
    recTimer = setInterval(() => {
      const s = (Date.now() - recRec.t0) / 1000;
      $("rec-hint").textContent = s < minSec ? sx("rec_rec", {s: Math.ceil(minSec - s)}) : sx("rec_can");
      if (s >= recMax) recRec.stop();
    }, 200);
  };
}
/* ---------- CRM: переписки рабочих аккаунтов (этап 1 — просмотр) ---------- */
let crmOnly = "", crmChat = null, crmTimer = null, crmLastSig = "";
const crmInit = n => (n || "?").trim().split(/\s+/).slice(0, 2).map(w => w[0]).join("").toUpperCase();
const crmTime = at => { if (!at) return ""; const d = at.slice(0, 10), t = new Date(); const today = t.toISOString().slice(0, 10);
  return d === today ? at.slice(11, 16) : at.slice(8, 10) + "." + at.slice(5, 7); };
const crmWin = w => w === null || w === undefined ? "" : w >= 1 ? `можно ответить ещё ${Math.floor(w)} ч` : `ещё ${Math.round(w * 60)} мин`;
/* Аватар: фото профиля из Telegram, если его нет или оно скрыто — буквы */
const crmAva = c => `<div class="crm-ava">${esc(crmInit(c.name))}${c.ava ? `<img src="${c.ava}" alt="" loading="lazy" onerror="this.remove()">` : ""}</div>`;
/* ---------- Разделы в приложении ---------- */
async function appSecLoad(body) {
  try {
    const r = await api("/api/app_sections", body || {});
    $("as-list").innerHTML = Object.entries(r.titles).map(([k, t]) => `<div style="display:flex;align-items:center;gap:10px;padding:9px 0">
        <span style="flex:1">${esc(t)}</span><label class="sw"><input type="checkbox" data-as="${k}" ${r.hidden.includes(k) ? "" : "checked"}><span></span></label></div>`).join("");
    $("as-admin").checked = !!r.admin_too;
    if (r.msg) toast(r.msg);
  } catch (e) { toast(e.message); }
}
$("go-appsec").onclick = () => show("appsec");
$("as-save").onclick = () => {
  const hidden = []; document.querySelectorAll("[data-as]").forEach(i => { if (!i.checked) hidden.push(i.dataset.as); });
  appSecLoad({hidden, admin_too: $("as-admin").checked});
};

/* ---------- RADI Chat: телефоны, доступы, белый список ---------- */
const MS_KIND = {delete: "удаление", contact: "контакт/ссылка", edit: "редактирование", ttl: "исчезающее", other: "другое"};
let msNum = null;  // подключение номера: {id, step}
function msgrRender(r) {
  window._msNumbers = r.numbers || [];
  if (r.numbers) {
    $("ms-nums").innerHTML = r.numbers.length ? r.numbers.map(n => `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)">
        <span style="font-size:20px">${n.ready ? "📞" : "⏳"}</span>
        <span style="flex:1;min-width:0"><b>${esc(n.title)}</b><div class="hint" style="font-size:12px">${esc(n.phone)} · ${n.ready ? "подключён" : "не завершено"} · телефонов: ${n.devices}</div></span>
        <button class="btn btn-soft" data-num-ren="${n.id}" style="padding:8px 10px" title="Название воронки">✏️</button>
        <button class="btn btn-soft" data-num-rm="${n.id}" style="padding:8px 12px">Удалить</button></div>`).join("")
      : `<div class="hint">${r.numbers_enabled ? "Пока ни одного номера" : "⚠️ На сервере нет TG_API_ID / TG_API_HASH — добавьте их в Railway → Variables"}</div>`;
    document.querySelectorAll("[data-num-rm]").forEach(b => b.onclick = async () => {
      if (!await new Promise(ok => tg.showConfirm("Удалить рабочий номер с сервера? Новые ключи на него выдать будет нельзя.", ok))) return;
      msgrApi({action: "num_remove", number_id: Number(b.dataset.numRm)});
    });
    document.querySelectorAll("[data-num-ren]").forEach(b => b.onclick = () => {
      const id = Number(b.dataset.numRen);
      const cur = (window._msNumbers || []).find(x => x.id === id) || {};
      const row = b.parentElement;
      row.innerHTML = `<input class="ms-ren-in" style="flex:1;margin:0" placeholder="Напр. Воронка 1 — Новые" maxlength="60">
        <button class="btn btn-main ms-ren-ok" style="padding:8px 14px">✓</button>
        <button class="btn btn-soft ms-ren-no" style="padding:8px 12px">✕</button>`;
      const inp = row.querySelector(".ms-ren-in");
      inp.value = cur.title || ""; inp.focus();
      const save = () => { const t = inp.value.trim(); if (!t) { toast("Введите название воронки"); return; } msgrApi({action: "num_rename", number_id: id, title: t}); };
      row.querySelector(".ms-ren-ok").onclick = save;
      inp.onkeydown = e => { if (e.key === "Enter") save(); };
      row.querySelector(".ms-ren-no").onclick = () => msgrApi({action: "reload"});
    });
    const ready = r.numbers.filter(n => n.ready), ns = $("ms-number"), curN = ns.value;
    ns.innerHTML = ready.map(n => `<option value="${n.id}">📞 ${esc(n.title)}</option>`).join("");
    ns.classList.toggle("hidden", ready.length < 2);  // один номер — выбирать нечего
    if (curN) ns.value = curN;
  }
  const sel = $("ms-staff"), cur = sel.value;
  sel.innerHTML = `<option value="">— Чей телефон? Выберите сотрудника —</option>` +
    (r.staff || []).map(x => `<option value="${x.uid}">${esc(x.name)}</option>`).join("");
  sel.value = cur;

  // --- Менеджер: вход по логину + названия воронок ---
  if ($("ml-staff")) {
    window._msStaff = r.staff || []; window._msLogins = r.logins || [];
    const mlc = $("ml-staff").value;
    $("ml-staff").innerHTML = `<option value="">— Выберите сотрудника —</option>` +
      (r.staff || []).map(x => `<option value="${x.uid}">${esc(x.name)}</option>`).join("");
    $("ml-staff").value = mlc;
    const readyN = (r.numbers || []).filter(n => n.ready), mln = $("ml-number").value;
    $("ml-number").innerHTML = readyN.length
      ? readyN.map(n => `<option value="${n.id}">📞 ${esc(n.title)}</option>`).join("")
      : `<option value="">— сначала подключите рабочий номер —</option>`;
    if (mln) $("ml-number").value = mln;
    $("ml-list").innerHTML = (r.logins || []).length
      ? `<div class="hint" style="font-size:12px;margin-bottom:2px">Созданные логины:</div>` + r.logins.map(l => `<div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-top:1px solid var(--line)">
          <span style="flex:1;min-width:0"><b>${esc(l.name)}</b><div class="hint" style="font-size:12px">логин: ${esc(l.login)} · ${esc(l.number)}</div></span>
          <button class="btn btn-soft" data-login-del="${l.staff_uid}" style="padding:7px 11px">Удалить</button></div>`).join("")
      : "";
    $("ml-staff").onchange = mlFill;
    document.querySelectorAll("[data-login-del]").forEach(b => b.onclick = async () => {
      if (!await new Promise(ok => tg.showConfirm("Удалить логин менеджера? Он больше не сможет войти по нему.", ok))) return;
      msgrApi({action: "login_del", staff_uid: Number(b.dataset.loginDel)});
    });
  }
  $("ms-devs").innerHTML = r.devices.length ? r.devices.map(d => `<div class="row-btn" style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)">
      <span style="font-size:20px">${d.disabled ? "🚫" : "📱"}</span>
      <span style="flex:1;min-width:0"><b>${esc(d.name)}</b><div class="hint" style="font-size:12px">был онлайн ${esc(d.seen)}${d.version ? " · " + esc(d.version) : ""}</div></span>
      <button class="btn btn-soft" data-ms-dev="${esc(d.id)}" data-ms-act="${d.disabled ? "enable" : "disable"}" style="padding:8px 12px">${d.disabled ? "Включить" : "Отключить"}</button></div>`).join("")
    : `<div class="hint">Пока ни одного телефона</div>`;
  document.querySelectorAll("[data-ms-dev]").forEach(b => b.onclick = async () => {
    if (b.dataset.msAct === "disable" && !await new Promise(ok => tg.showConfirm("Отключить этот телефон? RADI Chat на нём перестанет работать.", ok))) return;
    msgrApi({action: b.dataset.msAct, id: b.dataset.msDev});
  });
  $("ms-perms").innerHTML = Object.entries(r.perm_titles).map(([k, t]) => `<div style="display:flex;align-items:center;gap:10px;padding:8px 0">
      <span style="flex:1">${esc(t)}</span><label class="sw"><input type="checkbox" data-ms-perm="${k}" ${r.perms[k] ? "checked" : ""}><span></span></label></div>`).join("");
  if ("owner_code_set" in r) {
    $("ms-owner-state").textContent = r.owner_code_set ? "🔒 Код установлен — вход по номеру защищён" : "⚠️ Код не задан — вход по номеру открыт";
    if (r.owner_code_set) $("ms-owner-code").placeholder = "Изменить код (или пусто — снять)";
  }
  if (r.sessions) {
    $("ms-sessions").innerHTML = r.sessions.length ? r.sessions.map(g => `<details style="padding:8px 0;border-bottom:1px solid var(--line)">
        <summary><b>${esc(g.name)}</b> <span class="hint" style="font-size:12px">воронок: ${g.count}</span></summary>
        <div style="padding:6px 0 2px">${g.funnels.map(f => `<div style="display:flex;align-items:center;gap:8px;padding:6px 0">
          <span style="flex:1;min-width:0"><b style="font-weight:600">${esc(f.number)}</b>
            <div class="hint" style="font-size:12px">${f.key_login ? "вход по ключу · " : ""}онлайн ${esc(f.seen)}${f.version ? " · " + esc(f.version) : ""}</div></span>
          <button class="btn btn-soft" data-kick="${esc(f.id)}" style="padding:8px 12px">🚪 Выкинуть</button></div>`).join("")}</div></details>`).join("")
      : `<div class="hint">Активных сессий нет</div>`;
    document.querySelectorAll("[data-kick]").forEach(b => b.onclick = async () => {
      if (!await new Promise(ok => tg.showConfirm("Выкинуть эту сессию? Сотрудник выйдет из аккаунта на этом телефоне.", ok))) return;
      msgrApi({action: "kick", id: b.dataset.kick});
    });
  }
  $("ms-keep").checked = !!r.keepalive;
  $("ms-mode").innerHTML = Object.entries(r.assign_modes).map(([k, t]) => `<option value="${k}">${esc(t)}</option>`).join("");
  $("ms-mode").value = r.assign.assign_mode;
  $("ms-unass").checked = !!r.assign.unassigned_visible;
  $("ms-tmin").value = r.assign.timeout_min || 0;
  $("ms-tact").innerHTML = Object.entries(r.timeout_actions).map(([k, t]) => `<option value="${k}">затем: ${esc(t)}</option>`).join("");
  $("ms-tact").value = r.assign.timeout_action;
  const SEE = ["не видит чужие чаты", "видит чужие чаты (только чтение)"];
  $("ms-staff-list").innerHTML = r.staff.map(x => `<details style="padding:8px 0;border-bottom:1px solid var(--line)">
      <summary><b>${esc(x.name)}</b> <span class="hint" style="font-size:12px">🔑 воронок: ${x.funnels || 0}${x.custom ? " · свои доступы" : ""}${x.see_others ? " · видит чужие" : ""}</span></summary>
      <div style="padding:6px 0">${Object.entries(r.perm_titles).map(([k, t]) => `<div style="display:flex;align-items:center;gap:10px;padding:6px 0">
        <span style="flex:1">${esc(t)}</span><label class="sw"><input type="checkbox" data-sp="${x.uid}" data-k="${k}" ${x.perms[k] ? "checked" : ""}><span></span></label></div>`).join("")}
        <select data-see="${x.uid}" style="width:100%;margin:6px 0">${SEE.map((t, i) => `<option value="${i}" ${x.see_others === i ? "selected" : ""}>${t}</option>`).join("")}</select>
        <div style="display:flex;gap:8px"><button class="btn btn-main" data-sp-save="${x.uid}" style="flex:1">💾 Сохранить</button>
        <button class="btn btn-soft" data-sp-reset="${x.uid}" style="flex:1">↩️ Как у всех</button></div></div></details>`).join("");
  document.querySelectorAll("[data-sp-save]").forEach(b => b.onclick = () => {
    const uid = b.dataset.spSave, perms = {};
    document.querySelectorAll(`[data-sp="${uid}"]`).forEach(i => perms[i.dataset.k] = i.checked);
    msgrApi({action: "staff", staff_uid: Number(uid), perms, see_others: Number(document.querySelector(`[data-see="${uid}"]`).value)});
  });
  document.querySelectorAll("[data-sp-reset]").forEach(b => b.onclick = () =>
    msgrApi({action: "staff", staff_uid: Number(b.dataset.spReset), reset: true,
             see_others: Number(document.querySelector(`[data-see="${b.dataset.spReset}"]`).value)}));
  const opts = `<option value="0">— свободный —</option>` + r.staff.map(x => `<option value="${x.uid}">${esc(x.name)}</option>`).join("");
  $("ms-chats").innerHTML = r.chats.length ? r.chats.map(c => `<div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--line)">
      <span style="flex:1;min-width:0"><b style="display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.name)}</b>
        <span class="hint" style="font-size:12px">${c.username ? "@" + esc(c.username) + " · " : ""}${esc(c.last)}</span></span>
      <select data-as-peer="${c.peer}" style="width:46%">${opts}</select></div>`).join("") : `<div class="hint">Чатов пока нет</div>`;
  document.querySelectorAll("[data-as-peer]").forEach(sel => {
    const c = r.chats.find(x => String(x.peer) === sel.dataset.asPeer); sel.value = String(c ? c.staff_uid : 0);
    sel.onchange = () => msgrApi({action: "assign", peer: Number(sel.dataset.asPeer), staff_uid: Number(sel.value), q: $("ms-q").value});
  });
  $("ms-wl").value = r.whitelist || "";
  $("ms-viol").innerHTML = r.violations.length ? r.violations.map(x => `<div style="padding:6px 0;border-bottom:1px solid var(--line)">
      <b>${esc(x.device)}</b> · ${esc(MS_KIND[x.kind] || x.kind)}${x.peer ? " · " + esc(x.peer) : ""}
      <div class="hint" style="font-size:12px">${esc(x.at)}${x.detail ? " · «" + esc(x.detail) + "»" : ""}</div></div>`).join("")
    : `<div class="hint">Нарушений нет 👍</div>`;
}
async function msgrApi(body) {
  try { const r = await api("/api/msgr/admin", body); msgrRender(r); if (r.msg) toast(r.msg); return r; } catch (e) { toast(e.message); }
}
function msgrLoad() { $("ms-code").classList.add("hidden"); msgrApi({}); }
function mlFill() {
  const uid = Number($("ml-staff").value || 0);
  const st = (window._msStaff || []).find(x => x.uid === uid);
  const lg = (window._msLogins || []).find(x => x.staff_uid === uid);
  const fn = (st && st.funnel_names) || ["Новые", "Повторные", "Постоянные"];
  $("ml-f1").value = fn[0] || ""; $("ml-f2").value = fn[1] || ""; $("ml-f3").value = fn[2] || "";
  $("ml-login").value = lg ? lg.login : ""; $("ml-pass").value = "";
  if (lg && lg.number_id) $("ml-number").value = String(lg.number_id);
}
if ($("ml-save")) $("ml-save").onclick = async () => {
  const staff_uid = Number($("ml-staff").value || 0);
  if (!staff_uid) return toast("Выберите сотрудника");
  const number_id = Number($("ml-number").value || 0);
  if (!number_id) return toast("Сначала подключите рабочий номер");
  const login = $("ml-login").value.trim();
  if (!login) return toast("Введите логин");
  try {
    await api("/api/msgr/admin", {action: "login_set", staff_uid, number_id, login, password: $("ml-pass").value});
    const r = await api("/api/msgr/admin", {action: "funnel_names", staff_uid, names: [$("ml-f1").value, $("ml-f2").value, $("ml-f3").value]});
    msgrRender(r); toast("✅ Менеджер сохранён: " + login);
  } catch (e) { toast(e.message); }
};
$("ms-code-btn").onclick = async () => {
  try {
    if (!$("ms-staff").value) return toast("Выберите сотрудника");
    const ready = (window._msNumbers || []).filter(n => n.ready);
    const number_id = ready.length ? Number($("ms-number").value || ready[0].id) : 0;
    const r = await api("/api/msgr/admin", {action: "code", staff_uid: Number($("ms-staff").value), number_id});
    const numTitle = number_id ? (ready.find(n => n.id === number_id) || {}).title : "";
    window._msLastKey = {staff_uid: Number($("ms-staff").value), number_title: numTitle || ""};
    $("ms-code-v").textContent = r.code;
    $("ms-code-h").textContent = `Для: ${r.name}${numTitle ? " · " + numTitle : ""} · действует ${r.minutes} минут, один раз`;
    $("ms-code-send").classList.toggle("hidden", !$("ms-staff").value);
    $("ms-code").classList.remove("hidden");
  } catch (e) { toast(e.message); }
};
/* подключение рабочего номера: номер → код из Telegram → облачный пароль */
function msNumStep(step, hint, type) {
  $("ms-num-step").classList.toggle("hidden", !step);
  $("ms-num-add").classList.toggle("hidden", !!step);
  if (!step) return;
  $("ms-num-hint").textContent = hint; $("ms-num-val").value = ""; $("ms-num-val").type = type;
  $("ms-num-val").focus();
}
async function msNumApi(body) {
  try {
    const r = await api("/api/msgr/admin", body);
    msgrRender(Object.assign(await api("/api/msgr/admin", {}), {}));
    if (r.step === "code") { msNum = {id: r.number_id}; msNumStep("code", "Введите код, который пришёл в Telegram этого номера (или SMS)", "text"); }
    else if (r.step === "password") { msNumStep("password", "Введите облачный пароль этого аккаунта. Он хранится на сервере зашифрованным и не передаётся на телефоны", "password"); }
    else if (r.step === "ready") { msNum = null; msNumStep(null); toast(`✅ Номер подключён: ${r.title}`); }
  } catch (e) { toast(e.message); }
}
$("ms-num-go").onclick = () => msNumApi({action: "num_add", phone: $("ms-num-phone").value});
$("ms-num-next").onclick = () => {
  if (!msNum) return msNumStep(null);
  const v = $("ms-num-val").value;
  msNumApi($("ms-num-val").type === "password" ? {action: "num_password", number_id: msNum.id, password: v}
                                               : {action: "num_code", number_id: msNum.id, code: v});
};
$("ms-owner-save").onclick = () => {
  const v = $("ms-owner-code").value;
  if ($("ms-owner-state").textContent.includes("установлен") || v.trim())
    tg.showConfirm(v.trim() ? "Сохранить код входа по номеру?" : "Снять код? Вход по номеру станет без кода.",
      ok => { if (ok) { msgrApi({action: "owner_code", code: v}); $("ms-owner-code").value = ""; } });
  else msgrApi({action: "owner_code", code: v});
};
$("ms-code-send").onclick = async () => {
  const k = window._msLastKey; if (!k || !k.staff_uid) return;
  try {
    const r = await api("/api/msgr/admin", {action: "code", staff_uid: k.staff_uid,
      number_id: (window._msNumbers || []).filter(n => n.ready).find(n => n.title === k.number_title)?.id || 0,
      send: true, number_title: k.number_title});
    $("ms-code-v").textContent = r.code;
    $("ms-code-h").textContent = `Новый ключ отправлен в бот сотруднику. Старый больше не действует.`;
    window._msLastKey = {staff_uid: k.staff_uid, number_title: k.number_title};
    toast(r.sent ? "📩 Ключ отправлен сотруднику в бот" : "Ключ обновлён, но отправить не удалось");
  } catch (e) { toast(e.message); }
};
$("ms-save").onclick = () => {
  const perms = {}; document.querySelectorAll("[data-ms-perm]").forEach(i => perms[i.dataset.msPerm] = i.checked);
  msgrApi({action: "save", perms, whitelist: $("ms-wl").value, keepalive: $("ms-keep").checked,
           assign: {assign_mode: $("ms-mode").value, unassigned_visible: $("ms-unass").checked,
                    timeout_min: Number($("ms-tmin").value) || 0, timeout_action: $("ms-tact").value}});
};
let msQTimer = null;
$("ms-q").oninput = () => { clearTimeout(msQTimer); msQTimer = setTimeout(() => msgrApi({q: $("ms-q").value}), 400); };

function crmOnShow(v) {
  clearInterval(crmTimer);
  if (v !== "crmchat") { crmVoiceStop(false); if (vpAudio) vpAudio.pause(); }
  $("crm-foot").classList.toggle("hidden", v !== "crmchat");
  if (v === "crm") { loadCrm(); crmTimer = setInterval(() => loadCrm(true), 7000); }
  if (v === "crmchat") { crmLastSig = ""; $("crm-pin").classList.add("hidden"); loadCrmChat(); crmInfo();
    crmTimer = setInterval(() => loadCrmChat(true), 4000); }
}
async function loadCrm(silent) {
  if (!silent && !$("crm-list").innerHTML) $("crm-list").innerHTML = `<div class="empty">⏳</div>`;
  let r; try { r = await api("/api/crm/chats", {q: $("crm-q").value, only: crmOnly, account: $("crm-acc").value}); }
  catch (e) { if (!silent) $("crm-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const acc = $("crm-acc"), keep = acc.value;
  acc.classList.toggle("hidden", r.accounts.length < 2);
  acc.innerHTML = `<option value="">Все аккаунты</option>` + r.accounts.map(a => `<option value="${a.id}">${a.enabled ? "🟢" : "🔴"} ${esc(a.name)}${a.username ? " @" + esc(a.username) : ""}</option>`).join("");
  acc.value = keep;
  $("d-crm").textContent = `чатов: ${r.chats.length}`;
  $("crm-list").innerHTML = r.chats.length ? r.chats.map(c => `<div class="crm-item" data-crm="${c.conn}|${c.chat}">
      ${crmAva(c)}
      <div class="mid"><div class="nm"><b>${esc(c.name)}</b><span>${crmTime(c.at)}</span></div>
        <div class="pv">${c.last_out ? "Вы: " : ""}${esc(c.last)}</div>
        <div class="crm-tags">${c.waiting ? `<span class="crm-tag wait">⏳ ждёт ответа</span>` : ""}
          ${c.window !== null ? `<span class="crm-tag win">⏱ ${crmWin(c.window)}</span>` : ""}
          ${c.deleted ? `<span class="crm-tag del">🗑 ${c.deleted}</span>` : ""}${c.edited ? `<span class="crm-tag">✏️ ${c.edited}</span>` : ""}
          ${r.accounts.length > 1 ? `<span class="crm-tag">📱 ${esc(c.account)}</span>` : ""}</div></div></div>`).join("")
    : `<div class="empty">${r.accounts.length ? "Переписок пока нет — они появятся, как только клиенты напишут на рабочий аккаунт." :
        "Рабочие аккаунты не подключены: Настройки → Telegram для бизнеса → Чат-боты → бот RADI TEAM."}</div>`;
  document.querySelectorAll("[data-crm]").forEach(el => el.onclick = () => {
    const [conn, chat] = el.dataset.crm.split("|"); crmChat = {conn, chat: +chat}; show("crmchat"); });
}
let crmQT = null;
$("crm-q").oninput = () => { clearTimeout(crmQT); crmQT = setTimeout(() => loadCrm(), 300); };
$("crm-acc").onchange = () => loadCrm();
document.querySelectorAll("#crm-only [data-o]").forEach(b => b.onclick = () => {
  crmOnly = b.dataset.o; document.querySelectorAll("#crm-only [data-o]").forEach(x => x.classList.toggle("on", x === b)); loadCrm(); });
function crmBody(m) {
  const md = m.media;
  let media = "";
  if (md) media = m.kind === "photo" || m.kind === "sticker" ? `<img loading="lazy" src="${md}">`
    : m.kind === "video_note" ? `<video class="circle" controls playsinline preload="metadata" src="${md}"></video>`
    : ["video", "animation"].includes(m.kind) ? `<video controls playsinline preload="metadata" src="${md}"></video>`
    : ["voice", "audio"].includes(m.kind) ? `<div class="vp" data-src="${md}" data-dur="${m.dur || 0}">
        <button class="vp-btn" aria-label="play">▶</button>
        <div class="vp-mid"><input type="range" class="vp-bar" min="0" max="${m.dur || 1}" step="0.1" value="0">
          <div class="vp-t"><span class="vp-cur">0:00</span><span class="vp-tot">${vpFmt(m.dur || 0)}</span></div></div>
        <button class="vp-sp">1x</button></div>`
    : `<a href="${md}" target="_blank">📎 Открыть файл</a>`;
  const label = !md && m.kind !== "text" ? `<i>${{contact: "👤 Контакт", location: "📍 Геолокация", other: "Сообщение"}[m.kind] || m.kind}</i> ` : "";
  return media + (label || m.text ? `<div class="crm-tx">${label}${esc(m.text)}</div>` : "");
}
/* Плеер голосовых: ▶/⏸, перемотка ползунком, время, скорость 1x / 1.5x / 2x */
const vpFmt = t => { t = Math.max(0, Math.floor(t || 0)); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`; };
let vpAudio = null, vpSrc = "", vpRate = 1;
function vpEl() { return [...document.querySelectorAll("#crm-feed .vp")].find(v => v.dataset.src === vpSrc); }
function vpPaint() {
  document.querySelectorAll("#crm-feed .vp").forEach(v => {
    const me = vpAudio && v.dataset.src === vpSrc, a = vpAudio;
    const dur = me && isFinite(a.duration) && a.duration > 0 ? a.duration : +v.dataset.dur || 0;
    const cur = me ? a.currentTime : 0, bar = v.querySelector(".vp-bar");
    bar.max = dur || 1; if (!v.dragging) bar.value = cur;
    bar.style.setProperty("--p", (dur ? Math.min(100, cur / dur * 100) : 0) + "%");
    v.querySelector(".vp-cur").textContent = vpFmt(cur); v.querySelector(".vp-tot").textContent = vpFmt(dur);
    const playing = me && !a.paused; v.querySelector(".vp-btn").textContent = playing ? "⏸" : "▶";
    v.querySelector(".vp-btn").classList.toggle("on", playing); v.querySelector(".vp-sp").textContent = (me ? vpRate : 1) + "x";
  });
}
function vpOpen(src) {
  if (vpAudio && vpSrc === src) return vpAudio;
  if (vpAudio) { vpAudio.pause(); vpAudio.src = ""; }
  vpSrc = src; vpRate = 1; vpAudio = new Audio(src); vpAudio.preload = "auto";
  ["timeupdate", "loadedmetadata", "play", "pause", "ended"].forEach(ev => vpAudio.addEventListener(ev, vpPaint));
  vpAudio.addEventListener("ended", () => { vpAudio.currentTime = 0; vpPaint(); });
  vpAudio.addEventListener("error", () => toast("Не удалось воспроизвести запись"));
  return vpAudio;
}
$("crm-feed").addEventListener("click", e => {
  const v = e.target.closest(".vp"); if (!v) return;
  if (e.target.closest(".vp-btn")) { const a = vpOpen(v.dataset.src); a.paused ? a.play().catch(() => toast("Нажмите ещё раз")) : a.pause(); vpPaint(); }
  if (e.target.closest(".vp-sp")) { const a = vpOpen(v.dataset.src); vpRate = vpRate === 1 ? 1.5 : vpRate === 1.5 ? 2 : 1;
    a.playbackRate = vpRate; vpPaint(); }
});
$("crm-feed").addEventListener("input", e => {
  if (!e.target.classList.contains("vp-bar")) return;
  const v = e.target.closest(".vp"), a = vpOpen(v.dataset.src); v.dragging = true;
  const t = +e.target.value; e.target.style.setProperty("--p", (t / (+e.target.max || 1) * 100) + "%");
  v.querySelector(".vp-cur").textContent = vpFmt(t);
  if (a.readyState >= 1) a.currentTime = t; else a.addEventListener("loadedmetadata", () => { a.currentTime = t; }, {once: true});
});
$("crm-feed").addEventListener("change", e => { const v = e.target.closest(".vp"); if (v) { v.dragging = false; vpPaint(); } });
async function loadCrmChat(silent) {
  if (!crmChat) return;
  let r; try { r = await api("/api/crm/messages", crmChat); } catch (e) { if (!silent) toast(e.message); return; }
  const sig = r.items.map(m => m.id + (m.deleted ? "d" : "") + m.edits.length).join(",") + "|" + r.window;
  if (sig === crmLastSig) return; crmLastSig = sig;
  const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
  $("crm-head").innerHTML = `${crmAva(r)}<div style="min-width:0"><b>${esc(r.name)}</b>
    <div class="st ${r.window !== null ? "on" : ""}">${r.window !== null ? crmWin(r.window) : "окно ответа закрыто"}${r.username ? " · @" + esc(r.username) : ""}</div></div>`;
  let day = "";
  $("crm-feed").innerHTML = r.items.map(m => {
    const d = m.at.slice(0, 10), sep = d !== day ? `<div class="crm-day">${d.split("-").reverse().join(".")}</div>` : ""; day = d;
    return sep + `<div class="crm-msg ${m.out ? "out" : ""} ${m.deleted ? "del" : ""}">
      <div class="who">${m.out ? "🧑‍💼 " + esc(m.from || "Менеджер") + (m.via ? ` <span style="color:var(--muted);font-weight:500">· из CRM: ${esc(m.via)}</span>` : "") : "👤 Клиент"}</div>${crmBody(m)}
      ${m.edits.map(e => `<div class="old">✏️ было: ${esc(e.old || "—")}</div>`).join("")}
      <div class="meta">${m.edited ? "✏️ изменено · " : ""}${m.at.slice(11, 16)}${m.deleted ? ` · <b style="color:var(--red)">🗑 удалено ${m.deleted.slice(8, 10)}.${m.deleted.slice(5, 7)} ${m.deleted.slice(11, 16)}</b>` : ""}</div></div>`;
  }).join("") || `<div class="empty">Сообщений нет</div>`;
  vpPaint();
  const open = r.window !== null;
  $("crm-comp").classList.toggle("off", !open);
  $("crm-note").classList.toggle("hidden", open);
  $("crm-note").textContent = open ? "" : "⏳ Окно 24 ч закрыто: клиент давно не писал. Ответить можно, когда клиент напишет снова; написать первым — из Telegram.";
  if (!silent || atBottom) window.scrollTo(0, document.body.scrollHeight);
}
/* Ответ из CRM: текст, фото, быстрые ответы (⚡ и «/») */
let crmTpls = null, crmPhoto = null, crmSending = false;
async function crmLoadTpls(body = {}) {
  try { const r = await api("/api/crm/templates", body); crmTpls = r.items; if (r.msg) toast(r.msg); } catch (e) { toast(e.message); }
  return crmTpls || [];
}
function crmFit() { const t = $("crm-input"); t.style.height = "auto"; t.style.height = Math.min(140, t.scrollHeight) + "px";
  $("crm-send").textContent = t.value.trim() || crmPhoto ? "➤" : "🎤"; }
/* Голосовое: как в Telegram — пустое поле, нажали 🎤 → запись → ➤ отправить / ✕ отмена */
let crmRec = null, crmRecT = null;
async function crmVoiceStart() {
  if (!navigator.mediaDevices || !window.MediaRecorder) return toast("Запись голоса не поддерживается на этом устройстве");
  let stream; try { stream = await navigator.mediaDevices.getUserMedia({audio: RAW_AUDIO}); }
  catch (e) { return toast("Нет доступа к микрофону — разрешите его в настройках"); }
  const type = ["audio/mp4", "audio/ogg;codecs=opus", "audio/webm;codecs=opus", "audio/webm"].find(t => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t));
  const chunks = [];
  crmRec = new MediaRecorder(stream, Object.assign({audioBitsPerSecond: 128000}, type ? {mimeType: type} : {}));
  crmRec.ondataavailable = e => e.data && e.data.size && chunks.push(e.data);
  crmRec.chunks = chunks; crmRec.stream = stream;
  await new Promise(ok => setTimeout(ok, 250));  // микрофон «прогревается» — иначе теряется начало
  crmRec.t0 = Date.now(); crmRec.start(1000);
  $("crm-comp").classList.add("hidden"); $("crm-recbar").classList.remove("hidden");
  crmRecT = setInterval(() => { const s = Math.floor((Date.now() - crmRec.t0) / 1000);
    $("crm-rect").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; if (s >= 300) $("crm-recok").onclick(); }, 250);
}
function crmVoiceStop(send) {
  if (!crmRec) return; const rec = crmRec; crmRec = null; clearInterval(crmRecT);
  $("crm-recbar").classList.add("hidden"); $("crm-comp").classList.remove("hidden"); $("crm-rect").textContent = "0:00";
  rec.onstop = async () => {
    rec.stream.getTracks().forEach(t => t.stop());
    if (!send) return;
    const dur = (Date.now() - rec.t0) / 1000; if (dur < 1) return toast("Слишком коротко");
    const blob = new Blob(rec.chunks, {type: (rec.mimeType || "audio/webm").split(";")[0]});
    const url = await new Promise(ok => { const fr = new FileReader(); fr.onload = () => ok(fr.result); fr.readAsDataURL(blob); });
    toast("⏳ Отправляю голосовое…");
    try { const r = await api("/api/crm/voice", Object.assign({}, crmChat, {audio: url, duration: dur}));
      toast(r.msg); if (r.ok) { crmLastSig = ""; loadCrmChat(); } } catch (e) { toast(e.message); }
  };
  rec.stop();
}
$("crm-recx").onclick = () => crmVoiceStop(false);
$("crm-circle").onclick = () => openRecorder({title: "⭕ Кружок для клиента", min_sec: 1, max: 60, onSend: async (url, dur) => {
  try {
    const r = await api("/api/crm/circle", Object.assign({}, crmChat, {video: url, duration: dur}));
    if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(r.ok ? "success" : "warning");
    if (!r.ok) { $("rec-hint").textContent = r.msg; $("rec-btn").disabled = false; $("rec-btn").textContent = sx("rec_again"); return recCamera(); }
    recClose(); toast(r.msg); crmLastSig = ""; loadCrmChat();
  } catch (e) { $("rec-hint").textContent = e.message; $("rec-btn").disabled = false; }
}});
$("crm-recok").onclick = () => crmVoiceStop(true);
function crmPut(text) { $("crm-input").value = text; crmFit(); $("crm-sug").classList.add("hidden"); $("crm-input").focus(); }
$("crm-input").oninput = async () => {
  crmFit();
  const v = $("crm-input").value, box = $("crm-sug");
  if (!v.startsWith("/")) { box.classList.add("hidden"); return; }
  const list = (crmTpls || await crmLoadTpls()).filter(t => (t.title + " " + t.text).toLowerCase().includes(v.slice(1).toLowerCase()));
  box.innerHTML = list.length ? list.map(t => `<div data-tp="${t.id}"><b>${esc(t.title)}</b><span>${esc(t.text)}</span></div>`).join("")
    : `<div style="color:var(--muted)">Нет быстрых ответов — добавьте кнопкой ⚡</div>`;
  box.classList.remove("hidden");
  box.querySelectorAll("[data-tp]").forEach(el => el.onclick = () => crmPut(crmTpls.find(t => t.id === +el.dataset.tp).text));
};
$("crm-input").onkeydown = e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("crm-send").onclick(); };
$("crm-file").onchange = async () => {
  const f = $("crm-file").files[0]; $("crm-file").value = ""; if (!f) return;
  try { crmPhoto = await reportJpeg(f); } catch (e) { return toast(e.message); }
  $("crm-photo-img").src = crmPhoto; $("crm-photo").classList.remove("hidden"); crmFit();
};
$("crm-photo-x").onclick = () => { crmPhoto = null; $("crm-photo").classList.add("hidden"); crmFit(); };
$("crm-send").onclick = async () => {
  const text = $("crm-input").value.trim();
  if (!text && !crmPhoto) return crmVoiceStart();
  if (crmSending || !crmChat || (!text && !crmPhoto)) return;
  crmSending = true; $("crm-send").disabled = true;
  try {
    const r = await api("/api/crm/send", Object.assign({}, crmChat, {text}, crmPhoto ? {photo: crmPhoto} : {}));
    if (r.ok) { $("crm-input").value = ""; crmFit(); crmPhoto = null; $("crm-photo").classList.add("hidden");
                if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success"); crmLastSig = ""; await loadCrmChat(); }
    else toast(r.msg);
  } catch (e) { toast(e.message); }
  crmSending = false; $("crm-send").disabled = false;
};
async function crmTplSheet(manage) {
  const list = await crmLoadTpls();
  invSheet(`<h3>⚡ Быстрые ответы</h3>
    <div class="hint" style="font-size:12.5px;margin:-6px 0 10px">${manage ? "Нажмите, чтобы изменить. {имя} — подставится имя клиента."
      : "Нажмите — текст появится в поле ввода, можно поправить и отправить."}</div>
    ${list.length ? list.map(t => `<div class="kpi-row" style="cursor:pointer" data-tq="${t.id}"><div class="tt"><b>${esc(t.title)}</b>
      <div class="hint" style="font-size:12.5px;white-space:pre-wrap">${esc(t.text.slice(0, 160))}</div></div>${manage ? "<span>✏️</span>" : ""}</div>`).join("")
      : `<div class="empty">Быстрых ответов пока нет</div>`}
    ${manage ? `<button class="btn btn-main" style="width:100%;margin-top:12px" id="tq-new">＋ Быстрый ответ</button>`
      : `<button class="btn btn-soft" style="width:100%;margin-top:12px" id="tq-manage">⚙️ Добавить / изменить</button>`}
    <button class="btn btn-soft" style="width:100%;margin-top:8px" id="tq-close">Закрыть</button>`);
  $("tq-close").onclick = invSheetClose;
  if (manage) $("tq-new").onclick = () => crmTplEdit(null); else $("tq-manage").onclick = () => crmTplSheet(true);
  document.querySelectorAll("[data-tq]").forEach(el => el.onclick = () => {
    const t = list.find(x => x.id === +el.dataset.tq);
    if (manage) crmTplEdit(t); else { invSheetClose(); crmPut(t.text); }
  });
}
function crmTplEdit(t) {
  invSheet(`<h3>${t ? "✏️ Быстрый ответ" : "＋ Быстрый ответ"}</h3>
    <input id="tq-title" maxlength="60" placeholder="Название, например «Прайс» или «Адрес»" value="${t ? esc(t.title) : ""}">
    <textarea id="tq-text" rows="6" maxlength="4000" placeholder="Текст. {имя} — имя клиента">${t ? esc(t.text) : ""}</textarea>
    ${sheetBtns("Сохранить")}
    ${t ? `<button class="btn btn-soft" style="width:100%;margin-top:8px" id="tq-del">Удалить</button>` : ""}`);
  $("is-cancel").onclick = () => crmTplSheet(true);
  $("is-ok").onclick = async () => { await crmLoadTpls({action: "save", id: t ? t.id : 0, title: $("tq-title").value, text: $("tq-text").value});
    crmTplSheet(true); };
  if (t) $("tq-del").onclick = () => tg.showConfirm("Удалить быстрый ответ?", async ok => {
    if (ok) { await crmLoadTpls({action: "delete", id: t.id}); crmTplSheet(true); } });
}
$("crm-tpl").onclick = () => crmTplSheet(false);
$("crm-back").onclick = () => show("crm");
/* Заметки и задачи по клиенту */
let crmInf = {notes: [], tasks: []};
async function crmInfo(body) {
  if (!crmChat) return;
  try { const r = await api("/api/crm/info", Object.assign({}, crmChat, body || {})); crmInf = r; if (r.msg) toast(r.msg); }
  catch (e) { return toast(e.message); }
  const open = crmInf.tasks.filter(t => !t.done), n = crmInf.notes[0];
  $("crm-pin").classList.toggle("hidden", !open.length && !n);
  if (!open.length && !n) return;
  $("crm-pin").innerHTML = `<i></i><div>${open.length ? `<b>📌 Задачи: ${open.length}</b><span>${esc(open[0].title)} · ${open[0].due.slice(8, 10)}.${open[0].due.slice(5, 7)} · ${esc(open[0].who)}</span>`
    : `<b>📝 Заметка</b><span>${esc(n.text)}</span>`}</div>`;
  $("crm-pin").onclick = () => open.length ? crmTaskSheet() : crmNotesSheet();
}
function crmNotesSheet() {
  invSheet(`<h3>📝 Заметки о клиенте</h3>
    <textarea id="cn-text" rows="3" maxlength="2000" placeholder="Например: хочет серьги под золото, бюджет 300 тыс."></textarea>
    <button class="btn btn-main" style="width:100%" id="cn-add">Сохранить заметку</button>
    <div style="margin-top:12px">${crmInf.notes.length ? crmInf.notes.map(x => `<div class="kpi-row"><div class="tt" style="white-space:pre-wrap">${esc(x.text)}
      <div class="hint" style="font-size:12px">${esc(x.who)} · ${x.at.slice(8, 10)}.${x.at.slice(5, 7)} ${x.at.slice(11, 16)}</div></div>
      <button class="btn btn-soft btn-sm" data-cnd="${x.id}">✕</button></div>`).join("") : `<div class="empty">Заметок пока нет</div>`}</div>
    <button class="btn btn-soft" style="width:100%;margin-top:10px" id="cn-close">Закрыть</button>`);
  $("cn-close").onclick = invSheetClose;
  $("cn-add").onclick = async () => { const text = $("cn-text").value.trim(); if (!text) return toast("Напишите заметку");
    await crmInfo({action: "note", text}); crmNotesSheet(); };
  document.querySelectorAll("[data-cnd]").forEach(b => b.onclick = () => tg.showConfirm("Удалить заметку?", async ok => {
    if (ok) { await crmInfo({action: "del_note", id: +b.dataset.cnd}); crmNotesSheet(); } }));
}
function crmTaskSheet() {
  const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const people = [[data.me_id || 0, "Мне"]].concat((data.team || []).map(e => [e.id, e.name]));
  invSheet(`<h3>📌 Задача по клиенту</h3>
    <input id="ct-title" maxlength="250" placeholder="Например: перезвонить, отправить каталог">
    <div class="f-lbl" style="margin-top:0">Когда</div>
    <div class="kpi-seg" id="ct-day"><button data-d="today" class="on">Сегодня</button><button data-d="tomorrow">Завтра</button>
      <label class="kpi-other"><span id="ct-other">Дата…</span><input type="date" id="ct-pick" min="${today}"></label></div>
    <div class="f-lbl">Кому</div>
    <select id="ct-who">${people.map(([id, n]) => `<option value="${id}">${esc(n)}</option>`).join("")}</select>
    ${sheetBtns("Поставить задачу")}
    ${crmInf.tasks.length ? `<div class="f-lbl" style="margin-top:14px">Задачи по этому клиенту</div>` + crmInf.tasks.map(t => `<div class="kpi-row ${t.done ? "paid" : ""}">
      <div class="tt">${t.done ? "✅" : "📌"} ${esc(t.title)}<div class="hint" style="font-size:12px">${t.due.slice(8, 10)}.${t.due.slice(5, 7)} · ${esc(t.who)}</div></div></div>`).join("") : ""}`);
  let day = "today";
  const pick = el => document.querySelectorAll("#ct-day > *").forEach(x => x.classList.toggle("on", x === el));
  document.querySelectorAll("#ct-day [data-d]").forEach(b => b.onclick = () => { day = b.dataset.d; pick(b); $("ct-other").textContent = "Дата…"; });
  $("ct-pick").onchange = () => { if (!$("ct-pick").value) return; day = $("ct-pick").value; pick($("ct-pick").parentElement);
    $("ct-other").textContent = day.slice(8, 10) + "." + day.slice(5, 7); };
  $("is-cancel").onclick = invSheetClose;
  $("is-ok").onclick = async () => { const title = $("ct-title").value.trim(); if (!title) return toast("Напишите задачу");
    const assignee = +$("ct-who").value; invSheetClose(); await crmInfo({action: "task", title, day, assignee}); };
}
$("crm-notes").onclick = crmNotesSheet;
$("crm-task").onclick = crmTaskSheet;
function renderDash() {
  renderBossDay();
  const o = data.projects, cl = data.clients || {}, et = data.earnings_team || [];
  $("d-proj").textContent = o ? o.projects.length : "—";
  $("d-team").textContent = (data.team || []).length;
  $("d-earn").textContent = money0(et.reduce((a, x) => a + x.total, 0), "").trim();
  $("d-cl").textContent = cl.all != null ? cl.all : cl.total != null ? cl.total : "—";
  const d = o && o.day;
  $("d-date").textContent = o ? o.today : "";
  const pa = data.posting && data.posting.history;
  $("d-post").textContent = pa ? `постов: ${pa.length}` : "посты";
  // мои задачи на сегодня: просроченные, на сегодня, сделанные сегодня
  if (!d) { $("d-tasks").innerHTML = ""; return; }
  const sec = (title, rows) => rows.length ? `<div class="day-h">${title}</div>` + rows.map(x => ptaskRow(x, true)).join("") : "";
  const todo = d.today.length + d.overdue.length;
  $("d-tasks").innerHTML = (todo || d.done_today.length)
    ? sec(`<span style="color:var(--red)">🔴 Просрочено (${d.overdue.length})</span>`, d.overdue)
      + sec(`📅 Сегодня (${d.today.length})`, d.today) + sec("✅ Сделано сегодня", d.done_today)
    : `<div class="empty">На сегодня задач нет. Отличная работа! ✨</div>`;
  bindPtasks();
  const projs = o.projects || [];
  $("d-add-row").style.display = $("d-add-proj").style.display = projs.length ? "" : "none";
  const keep = $("d-add-proj").value;
  $("d-add-proj").innerHTML = projs.map(p => `<option value="${p.id}">📁 ${esc(p.title)}</option>`).join("");
  if (keep && projs.some(p => String(p.id) === keep)) $("d-add-proj").value = keep;
}
$("d-shop-tile").onclick = () => $("go-shop").onclick();
$("wl-edit").onclick = () => brandSheet();
$("d-add-btn").onclick = () => {
  const title = $("d-add").value.trim(); if (!title) return toast("Введите задачу");
  projAct({action: "add_task", id: +$("d-add-proj").value, title, due: P().today_iso}).then(() => { $("d-add").value = ""; });
};
$("d-add").onkeydown = e => { if (e.key === "Enter") $("d-add-btn").onclick(); };
/* Заголовки страниц — в виде карточки с логотипом */
document.querySelectorAll(".page-title").forEach(t => {
  const sub = t.nextElementSibling;
  if (!sub || !sub.classList.contains("page-sub")) return;
  const hero = document.createElement("div");
  hero.className = "hero";
  hero.innerHTML = `<div style="min-width:0"></div>`;
  if (!t.dataset.sx) t.textContent = t.textContent.replace(/^[^\p{L}\p{N}]+/u, "");
  t.before(hero);
  hero.lastElementChild.append(t, sub);
});
$("go-staff").onclick = () => show("staff");
/* Связь продавцов BILLZ с сотрудниками */
/* Кнопка «Связать с продавцом BILLZ»: список реальных продавцов из BILLZ, выбор касанием */
async function billzPick(uid, name, onDone) {
  invSheet(`<h3>🔗 ${esc(name)} — продавец BILLZ</h3><div class="empty">⏳ Загружаю продавцов из BILLZ…</div>`);
  let r; try { r = await api("/api/sellers", {view: "billz"}); } catch (e) { return invSheet(`<h3>🔗 Продавец BILLZ</h3><div class="empty">${esc(e.message)}</div>`); }
  const me = (r.emps.find(e => e.id === uid) || {}).billz || [];
  const taken = {}; r.emps.forEach(e => e.billz.forEach(n => { if (e.id !== uid) taken[n] = e.name; }));
  const sel = new Set(me);
  const draw = () => {
    const q = ($("bp-q") && $("bp-q").value || "").toLowerCase();
    $("bp-list").innerHTML = r.names.filter(n => !q || n.toLowerCase().includes(q)).map(n =>
      `<button data-bp="${esc(n)}" class="${sel.has(n) ? "on" : ""}">${sel.has(n) ? "✅ " : ""}${esc(n)}${taken[n] ? ` <span class="hint" style="font-size:11px">· ${esc(taken[n])}</span>` : ""}</button>`).join("")
      || `<div class="hint" style="font-size:13px">${r.names.length ? "Никого не нашлось" : "BILLZ не отдал список продавцов — впишите имя вручную ниже."}</div>`;
    document.querySelectorAll("[data-bp]").forEach(b => b.onclick = () => { const n = b.dataset.bp; sel.has(n) ? sel.delete(n) : sel.add(n); draw(); });
  };
  invSheet(`<h3>🔗 ${esc(name)} — продавец BILLZ</h3>
    <div class="hint" style="font-size:12.5px;margin:-6px 0 8px">Нажмите на продавца из BILLZ (можно несколько).${r.source ? " Источник: " + (r.source.startsWith("/") ? "сотрудники в BILLZ" : "продажи BILLZ") + "." : ""}</div>
    ${r.names.length > 8 ? `<input id="bp-q" placeholder="🔎 Поиск">` : ""}
    <div class="quick" id="bp-list" style="margin:0 0 10px;max-height:45vh;overflow:auto"></div>
    <input id="bp-manual" placeholder="или вписать вручную, как в BILLZ" maxlength="100">
    ${r.error && !r.names.length ? `<div class="hint" style="font-size:11.5px;margin:-4px 0 8px">BILLZ: ${esc(r.error)}</div>` : ""}
    ${sheetBtns("Сохранить")}`);
  draw(); if ($("bp-q")) $("bp-q").oninput = draw;
  $("is-cancel").onclick = invSheetClose;
  $("is-ok").onclick = async () => {
    const extra = $("bp-manual").value.trim(); if (extra) sel.add(extra);
    try { const x = await api("/api/sellers", {action: "save_emp", view: "emp", links: {[uid]: [...sel].join(", ")}}); toast(x.msg); invSheetClose();
      if (onDone) onDone(); else load(); } catch (e) { toast(e.message); }
  };
}
/* Связь сотрудников с BILLZ без продаж: у каждого сотрудника — как он записан продавцом в BILLZ */
async function sellersEmpSheet() {
  invSheet(`<h3>🔗 Сотрудники ↔ BILLZ</h3><div class="empty">⏳</div>`);
  let r; try { r = await api("/api/sellers", {view: "emp"}); } catch (e) { return toast(e.message); }
  invSheet(`<h3>🔗 Сотрудники ↔ BILLZ</h3>
    <div class="hint" style="font-size:12.5px;margin:-6px 0 10px">Впишите, как каждый сотрудник записан продавцом в BILLZ — точно как в BILLZ
      (например «Махира»). Несколько вариантов — через запятую. Пусто — не связан.</div>
    ${r.emps.map(e => `<div class="kpi-row" style="flex-wrap:wrap"><div class="tt" style="flex:1 1 100%"><b>${esc(e.name)}</b></div>
      <input data-se="${e.id}" maxlength="200" placeholder="Имя в BILLZ" value="${esc(e.billz.join(", "))}" style="margin:6px 0 0">
      <button class="btn btn-soft btn-sm" style="width:100%;margin-top:6px" data-sep="${e.id}" data-sen="${esc(e.name)}">🔗 Выбрать продавца из BILLZ</button></div>`).join("")}
    ${sheetBtns("Сохранить")}
    <button class="link" id="sl-from-sales" style="display:block;margin:12px auto 0;font-size:13px">📥 Подобрать по продажам BILLZ</button>`);
  $("is-cancel").onclick = invSheetClose;
  $("sl-from-sales").onclick = sellersSheet;
  document.querySelectorAll("[data-sep]").forEach(b => b.onclick = () => billzPick(+b.dataset.sep, b.dataset.sen, sellersEmpSheet));
  $("is-ok").onclick = async () => {
    const links = {}; document.querySelectorAll("[data-se]").forEach(i => { links[i.dataset.se] = i.value; });
    try { const x = await api("/api/sellers", {action: "save_emp", view: "emp", links}); toast(x.msg); invSheetClose(); load(); }
    catch (e) { toast(e.message); }
  };
}
async function sellersSheet() {
  invSheet(`<h3>🔗 Продавцы BILLZ</h3><div class="empty">⏳</div>`);
  let r; try { r = await api("/api/sellers"); } catch (e) { return invSheet(`<h3>🔗 Продавцы BILLZ</h3><div class="empty">${esc(e.message)}</div>`); }
  const opt = (sel) => `<option value="0">— не сотрудник / не связывать —</option>` +
    r.team.map(t => `<option value="${t.id}" ${t.id === sel ? "selected" : ""}>${esc(t.name)}</option>`).join("");
  const sug = r.items.filter(x => !x.saved && x.suggested).length, d = r.diag || {};
  const diag = `<div class="hint" style="font-size:12px;margin:0 0 10px;padding:8px 10px;border:1px solid var(--line);border-radius:12px">
    📊 Продаж в копии BILLZ: ${d.total || 0} · за неделю: ${d.week || 0} (с продавцом: ${d.week_sellers || 0})
    ${d.last_sale ? " · последняя: " + esc(d.last_sale) : ""}${d.mirror_at ? "<br>🔄 Обновлено: " + esc(d.mirror_at) + " (" + esc(d.mirror_last || "") + ")" : ""}
    ${d.billz_on === false ? "<br>⚠️ BILLZ не подключён" : d.report_on === false ? "<br>⚠️ BILLZ не даёт отчёт продаж — копия не обновляется сама" : ""}
    ${d.keys && d.keys.seller_fields ? "<br>🔎 Поля продавца в BILLZ: " + esc(JSON.stringify(d.keys.seller_fields).slice(0, 200)) : ""}
    <button class="btn btn-soft btn-sm" style="width:100%;margin-top:8px" id="sl-refresh">🔄 Загрузить продажи за неделю из BILLZ</button></div>`;
  invSheet(`<h3>🔗 Продавцы BILLZ ↔ сотрудники</h3>
    <div class="hint" style="font-size:12.5px;margin:-6px 0 10px">Продавцы из продаж BILLZ за полгода. Система предложила совпадения по имени —
      проверьте и нажмите «Сохранить».</div>${diag}
    ${r.items.length ? r.items.map((x, i) => `<div class="kpi-row" style="flex-wrap:wrap">
      <div class="tt" style="flex:1 1 100%"><b>${esc(x.name)}</b> <span class="hint" style="font-size:12px">· продаж: ${x.sales} · посл. ${x.last.slice(8, 10)}.${x.last.slice(5, 7)}</span>
        ${x.saved ? (x.uid ? ' <span class="pen-tag">✅ связан</span>' : ' <span class="hint" style="font-size:12px">не связан</span>')
          : x.suggested ? ' <span class="pen-tag" style="color:#fbbf24">💡 предложение</span>' : ""}</div>
      <select data-sl="${i}" style="margin:6px 0 0">${opt(x.saved ? x.uid : x.suggested)}</select></div>`).join("")
      : `<div class="empty">Продавцов пока нет — нажмите «Загрузить продажи за неделю».</div>`}
    ${r.items.length ? sheetBtns(sug ? `Сохранить (предложений: ${sug})` : "Сохранить") : `<button class="btn btn-soft" style="width:100%" id="is-cancel">Закрыть</button>`}`);
  $("is-cancel").onclick = invSheetClose;
  $("sl-refresh").onclick = async () => {
    $("sl-refresh").disabled = true; $("sl-refresh").textContent = "⏳ Загружаю из BILLZ…";
    try { const x = await api("/api/sellers", {action: "refresh"}); toast(x.msg); } catch (e) { toast(e.message); }
    sellersSheet();
  };
  if ($("is-ok")) $("is-ok").onclick = async () => {
    const links = {}; document.querySelectorAll("[data-sl]").forEach(s => { links[r.items[+s.dataset.sl].name] = +s.value; });
    try { const x = await api("/api/sellers", {action: "save", links}); toast(x.msg); invSheetClose(); load(); } catch (e) { toast(e.message); }
  };
}
$("go-sellers").onclick = sellersEmpSheet;
/* Обратная связь после покупки: тест / включено, предпросмотр */
async function followupSheet(body) {
  let r; try { r = await api("/api/followup", body || {}); } catch (e) { return toast(e.message); }
  if (r.msg) toast(r.msg);
  const live = r.mode === "live";
  invSheet(`<h3>📞 Обратная связь после покупки</h3>
    <div class="hint" style="font-size:12.5px;margin:-6px 0 10px">По каждой новой продаже BILLZ (с ${esc(r.since)}) на следующий день в ${r.hour}:00
      продавцу — задача связаться с клиентом. Продавец определяется по связи «Продавцы BILLZ ↔ сотрудники». Задача не мешает отметить уход.</div>
    <div class="kpi-res" style="margin:0 0 10px">${live ? "✅ <b style='font-size:15px'>Включено</b> — задачи ставятся менеджерам"
      : "🧪 <b style='font-size:15px;color:#fbbf24'>Тест</b> — менеджерам ничего не отправляется, предпросмотр утром только вам"}</div>
    <div class="pen-stats three" style="margin-bottom:10px"><div><b>${r.pending}</b><span>ждут</span></div>
      <div><b>${live ? r.tasks : r.test}</b><span>${live ? "задач поставлено" : "в тесте"}</span></div><div><b>${r.unlinked + r.noclient + (r.noname || 0) + (r.skiptest || 0)}</b><span>пропущено</span></div></div>
    ${r.text ? `<div class="hint" style="font-size:13px;white-space:pre-wrap;border:1px solid var(--line);border-radius:12px;padding:10px;margin-bottom:10px;max-height:40vh;overflow:auto">${esc(r.text)}</div>` : ""}
    <details style="margin-bottom:10px"><summary style="cursor:pointer;font-size:14px;padding:6px 0">🧪 Тестовые клиенты — без обратной связи
      (${(r.clients || []).filter(c => c.test).length})</summary>
      <div class="hint" style="font-size:12px;margin:4px 0 6px">Отметьте свои тестовые / розничные карточки — по ним задачи не ставятся. Остальные клиенты не трогаются.</div>
      ${(r.clients || []).map((c, i) => `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-top:1px solid var(--line)">
        <div style="flex:1;min-width:0;font-size:13px">${c.test ? "🧪 " : ""}${esc(c.name)}<div class="hint" style="font-size:11.5px">${esc(c.phone)} · покупок ${c.n}</div></div>
        <button class="btn ${c.test ? "btn-main" : "btn-soft"}" style="padding:6px 10px;font-size:12.5px" data-fu-test="${i}">${c.test ? "Снять" : "🧪 Тест"}</button></div>`).join("")
        || '<div class="hint" style="font-size:12.5px">Пока нет продаж с клиентами</div>'}</details>
    <button class="btn btn-soft" style="width:100%" id="fu-prev">👁 Проверить сейчас</button>
    <button class="btn btn-soft" style="width:100%;margin-top:8px" id="fu-now">📌 Поставить задачи сейчас (не ждать утра)</button>
    <button class="btn ${live ? "btn-soft" : "btn-main"}" style="width:100%;margin-top:8px" id="fu-mode">${live ? "🧪 Вернуть тестовый режим" : "✅ Включить для менеджеров"}</button>
    <button class="btn btn-soft" style="width:100%;margin-top:8px" id="fu-close">Закрыть</button>`);
  $("fu-close").onclick = invSheetClose;
  document.querySelectorAll("[data-fu-test]").forEach(b => b.onclick = () => {
    const c = r.clients[+b.dataset.fuTest];
    followupSheet({action: "test", key: c.key, name: c.name, on: !c.test}).then(() => {
      const d = document.querySelector("#inv-sheet details, .sheet details"); if (d) d.open = true; });
  });
  $("fu-prev").onclick = () => followupSheet({action: "preview"});
  $("fu-now").onclick = () => tg.showConfirm("Поставить задачи «обратная связь» прямо сейчас по всем ожидающим продажам?",
    ok => ok && followupSheet({action: "now"}));
  $("fu-mode").onclick = () => tg.showConfirm(live ? "Вернуть тестовый режим? Задачи перестанут ставиться." :
    "Включить? Менеджеры начнут получать задачи «обратная связь» по новым продажам.", ok => ok && followupSheet({action: "mode", mode: live ? "test" : "live"}));
}
$("go-followup").onclick = () => followupSheet();
$("cancel-late").onclick = () => tg.showConfirm("Отменить все штрафы за опоздание за сегодня и не выписывать новые до конца дня?",
  ok => ok && action("/api/cancel_late_fines"));
$("go-projects").onclick = () => show("projects");
$("go-posting").onclick = () => show("posting");
$("go-posting-me").onclick = () => show("posting");
$("go-earn").onclick = () => show("earn");
$("go-done").onclick = () => $("my-tasks-card").scrollIntoView({behavior: "smooth", block: "start"});
$("go-earn-admin").onclick = () => show("earn-admin");
$("go-inv-admin").onclick = () => data.invest_enabled ? show("invest-admin") : toast(SOON);
/* Раздел выключен: карточки «Инвестиции» — в виде «Скоро» */
function investSoon() {
  // вызывается при каждом автообновлении кабинета — повторный вызов ничего не меняет
  ["go-inv", "go-inv-admin"].forEach(id => {
    const el = $(id);
    if (!el) return;
    el.classList.add("soon");
    const mine = id === "go-inv" && data.profile;  // карточка сотрудника — на его языке
    const sub = el.querySelector(".f-sub"); if (sub) sub.textContent = mine ? sx("soon") : "Раздел скоро появится";
    const badge = el.querySelector("#inv-admin-badge"); if (badge) badge.innerHTML = "";
    const arrow = el.querySelector(".f-arrow");
    if (arrow) arrow.outerHTML = `<span class="soon-pill" style="margin-left:auto"></span>`;
    const pill = el.querySelector(".soon-pill"); if (pill) pill.textContent = mine ? sx("soon_pill") : "Скоро ⏳";
  });
  if (view === "invest" || view === "invest-admin") view = data.is_employee ? "me" : "team";
}
$("ava-file").onchange = async (ev) => {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  try {
    toast(data.profile ? sx("photo") : "Загружаю фото…");
    await action("/api/avatar", {image: await squareJpeg(file)});
  } catch (e) { toast(e.message); }
};
$("cl-refresh").onclick = async () => {
  $("cl-refresh").textContent = ct("refreshing");
  await load(true);
};
$("cl-file").onchange = async (ev) => {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  try {
    toast(ct("photo"));
    await action("/api/avatar", {image: await squareJpeg(file)});
  } catch (e) { toast(e.message); }
};
$("btn-mark").onclick = () => action(data.profile.on_site ? "/api/checkout" : "/api/checkin", {video: 1});  // кружок — прямо в кабинете
let tRepeat = "once";
const tUsers = new Set();  // кому ставим задачу (выбор сохраняется при автообновлении)
function renderTaskUsers() {
  const all = data.team.length && data.team.every(e => tUsers.has(e.id));
  $("t-users").innerHTML = `<button data-tu="all" class="${all ? "on" : ""}">✅ Все</button>` +
    data.team.map(e => `<button data-tu="${e.id}" class="${tUsers.has(e.id) ? "on" : ""}">${esc(e.name)}</button>`).join("");
  document.querySelectorAll("#t-users [data-tu]").forEach(b => b.onclick = () => {
    if (b.dataset.tu === "all") {
      const allOn = data.team.every(e => tUsers.has(e.id));
      data.team.forEach(e => allOn ? tUsers.delete(e.id) : tUsers.add(e.id));
    } else {
      const id = +b.dataset.tu; tUsers.has(id) ? tUsers.delete(id) : tUsers.add(id);
    }
    renderTaskUsers();
  });
  $("t-add").textContent = tUsers.size > 1 ? `Поставить задачу (${tUsers.size})` : "Поставить задачу";
}
document.querySelectorAll("#t-repeat [data-rep]").forEach(b => b.onclick = () => {
  tRepeat = b.dataset.rep;
  document.querySelectorAll("#t-repeat [data-rep]").forEach(x => x.classList.toggle("on", x === b));
  $("t-wd").classList.toggle("hidden", tRepeat !== "weekly");
  $("t-rep-hint").classList.toggle("hidden", tRepeat === "once");
  $("t-rep-hint").textContent = tRepeat === "daily"
    ? "Задача будет появляться каждый рабочий день сотрудника (по графику) в 07:00."
    : "Задача будет появляться раз в неделю в выбранный день в 07:00.";
});
$("t-add").onclick = () => {
  const title = $("t-title").value.trim();
  if (!title) return toast("Введите задачу");
  if (!tUsers.size) return toast("Выберите, кому поставить задачу");
  const body = {user_ids: [...tUsers], title, repeat: tRepeat};
  if (tRepeat === "weekly") body.weekday = +$("t-wd").value;
  action("/api/add_task", body).then(() => $("t-title").value = "");
};

$("f-add").onclick = () => {
  const reason = $("f-reason").value.trim();
  if (!reason) return toast("Укажите причину");
  tg.showConfirm("Выписать штраф?", ok => ok && action("/api/add_penalty",
    {user_id: +$("f-user").value, reason, amount: $("f-amount").value.trim()})
    .then(() => { $("f-reason").value = ""; $("f-amount").value = ""; }));
};

if (IS_APP) { if (appToken.get()) { load(true); setInterval(() => load(), 30000); } else appLogin(); }
else if (!tg.initData) hideSplash(), $("loading").innerHTML = "Kabinetni bot ichidagi tugma orqali oching: pastdagi «🏠» tugmasini bosing va " +
  "kelgan xabardagi «🏠 Kabinetni ochish» tugmasini bosing yoki maydon chap tomonidagi «Kabinet» tugmasini bosing.<br><br>" +
  "Откройте кабинет через бота: нажмите «🏠» внизу, затем «🏠 Открыть кабинет» в сообщении бота — или кнопку «Kabinet» слева от поля ввода.<br><br>" +
  "Open your account from the bot: tap «🏠» below, then «🏠 Open account» in the bot's message — or the «Kabinet» button left of the input field.";
else { load(true); setInterval(() => load(), 30000); }  // при открытии — свежие данные из BILLZ
</script>
</body>
</html>
"""
