"""Мини-апп: личный кабинет сотрудника и панель руководителя."""
import base64
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
import invest
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
    Запоминает в g.via, из какого бота открыт кабинет: "staff" (BOT_TOKEN) или "client" (CLIENT_BOT_TOKEN)."""
    init_data = (request.get_json(silent=True) or {}).get("initData", "")
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


@app.post("/api/me")
@need_user()
def api_me(uid, data):
    staff_view = g.via == "staff"  # из бота клиентов — всегда кабинет клиента, даже у сотрудника
    res = {"is_admin": staff_view and core.is_admin(uid), "is_employee": staff_view and bool(core.employee(uid)),
           "goal": config.GOAL_HOURS,
           "ranks": _ranks(uid),
           "today_wd": core.now().weekday()}
    if res["is_employee"]:
        res["profile"] = core.profile(uid)
        res["profile"]["ava_v"] = ava_version(uid)
        res["tasks"] = core.tasks(uid)
        res["history"] = core.history(uid)
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
        res["client"] = {"id": uid, "name": core.client_full_name(cl), "phone": cl.get("phone"),
                         "tier": tier, "tier_title": core.CLIENT_TIERS[tier]["title"],
                         "ava_v": ava_version(uid), "lang": i18n.lang_of(cl.get("lang")),
                         "registered": cl.get("step") == "done",
                         "birth": f"{birth[8:10]}.{birth[5:7]}" if birth else None,
                         "billz": dict(billz, enabled=bool(billz) or erp.enabled())}
        res["news"] = core.news_list(10, i18n.lang_of(cl.get("lang")))  # каждый клиент — на своём языке
    if invest.ENABLED and (res["is_employee"] or res["is_admin"]):
        res["invest_cfg"] = {"levels": invest.levels_info(), "term": invest.TERM, "min": invest.MIN_AMOUNT,
                             "max": invest.MAX_AMOUNT, "currency": invest.CURRENCY,
                             "annual_min": invest.ANNUAL_MIN, "annual_max": invest.ANNUAL_MAX,
                             "history": invest.history_public()}
    if res["is_admin"]:
        res["projects"] = projects.overview(uid)
        res["posting"] = {"audience": posts.audience_info(), "history": posts.history()}
        if invest.ENABLED:
            res["invest_admin"] = invest.overview()
        res["news"] = core.news_list(10)
        res["clients"] = core.clients_stats()
        res["client_list"] = core.clients_list()
        res["tiers"] = {k: v["title"] for k, v in core.CLIENT_TIERS.items()}
        res["shifts"] = {k: v["title"] for k, v in core.SHIFTS.items()}
        res["shifts"][core.OFF] = "Выходной"
        res["team"] = core.team()
        for e in res["team"]:
            e["ava_v"] = ava_version(e["id"])
    return jsonify(res)


@app.post("/api/checkin")
@need_user(staff=True)
def api_checkin(uid, data):
    if core.PROOF_REQUIRED:
        ok, msg = core.start_proof(uid)
        if not ok:
            return jsonify(ok=False, msg=msg)
        with core.db() as c:
            core.notify(c, uid, "HTML:" + msg)  # бот пришлёт просьбу о кружке в чат
        return jsonify(ok=True, close=True, msg=st.s(core.staff_lang(uid), "proof_closed"))
    ok, msg = core.check_in(uid)
    return jsonify(ok=ok, msg=msg)


@app.post("/api/checkout")
@need_user(staff=True)
def api_checkout(uid, data):
    ok, msg = core.check_out(uid)
    return jsonify(ok=ok, msg=msg)


@app.post("/api/task_done")
@need_user(staff=True)
def api_task_done(uid, data):
    ok, msg = core.complete_task(int(data.get("id", 0)), uid)
    return jsonify(ok=ok, msg=msg)


@app.post("/api/add_task")
@need_user(admin=True)
def api_add_task(uid, data):
    target = int(data.get("user_id", 0))
    title = str(data.get("title", "")).strip()
    if not core.employee(target) or not title:
        return jsonify(ok=False, msg="Выберите сотрудника и введите задачу.")
    core.add_task(target, title)
    return jsonify(ok=True, msg=f"Задача поставлена: {core.name(target)}")


@app.post("/api/add_penalty")
@need_user(admin=True)
def api_add_penalty(uid, data):
    target = int(data.get("user_id", 0))
    reason = str(data.get("reason", "")).strip()
    if not core.employee(target) or not reason:
        return jsonify(ok=False, msg="Выберите сотрудника и укажите причину.")
    core.add_penalty(target, reason, str(data.get("amount", "")))
    return jsonify(ok=True, msg=f"Штраф выписан: {core.name(target)}")


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


@app.post("/api/set_tier")
@need_user(admin=True)
def api_set_tier(uid, data):
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

@app.post("/api/post")
@need_user(admin=True)
def api_post(uid, data):
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
        ok, msg = posts.test(uid, *args, text=data.get("text", ""))
    elif act == "create":
        ok, msg = posts.create(uid, *args, data.get("audience") or [], bool(data.get("to_news")),
                               data.get("send_at") or "", text=data.get("text", ""))
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
  .staff-act { padding: 14px 16px; }
  /* проекты руководителя */
  .ptask { display: flex; gap: 12px; align-items: flex-start; padding: 11px 0; border-top: 1px solid var(--line); }
  .ptask:first-child { border-top: none; }
  .pcheck { width: 26px; height: 26px; flex: none; border-radius: 50%; border: 2px solid var(--hint); background: none;
            padding: 0; display: flex; align-items: center; justify-content: center; font-size: 14px; margin-top: 1px; }
  .ptask.done .pcheck { background: var(--green); border-color: var(--green); color: #06281c; }
  .ptask .pbody { flex: 1; min-width: 0; cursor: pointer; }
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
</style>
</head>
<body>
<div class="wrap">
  <div id="loading" class="card empty">Загрузка…</div>
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
    <div class="card" id="pen-card"></div>

    <div class="card">
      <h3 data-sx="ranks"></h3>
      <div class="levels" id="levels"></div>
    </div>

    <div class="card">
      <div class="head"><h3 data-sx="my_tasks"></h3></div>
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
    <div class="card"><div class="head"><h3 id="day-title">☀️ Мой день</h3></div><div id="proj-day"></div></div>
    <div class="card">
      <div class="head"><h3>Проекты</h3><span class="hint" style="font-size:13px" id="proj-count"></span></div>
      <div class="add-row"><input id="np-title" placeholder="Название нового проекта" maxlength="100">
        <button class="btn btn-main" id="np-add">＋</button></div>
      <div id="proj-list" style="margin-top:6px"></div>
    </div>
  </div>

  <!-- ПОСТИНГ -->
  <div id="view-posting" class="hidden">
    <div class="page-title">📣 Постинг</div>
    <div class="page-sub">Посты от имени бота — клиентам, сотрудникам, в группы и каналы</div>
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
        </div>
      </details>
      <div class="po-lbl">Кому</div>
      <div class="quick" id="po-aud" style="margin:0"></div>
      <label class="chk"><input type="checkbox" id="po-news">📰 Показать и в «Новостях» в панели клиентов</label>
      <div class="po-lbl">Когда</div>
      <div class="quick" id="po-when" style="margin:0"><button data-w="now" class="on">Сейчас</button><button data-w="later">🕒 Запланировать</button></div>
      <input type="datetime-local" id="po-at" class="hidden" style="margin-top:10px">
      <div style="display:grid;grid-template-columns:1fr 1.6fr;gap:10px;margin-top:14px">
        <button class="btn btn-soft" id="po-test">👁 Себе</button>
        <button class="btn btn-main" id="po-send">📣 Опубликовать</button>
      </div>
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
        <div class="pname" id="cl-name"></div>
        <div class="prole" id="cl-phone"></div>
        <div class="hint hidden" style="font-size:13px;margin:-4px 0 8px" id="cl-birth"></div>
        <div class="tier-pill" id="cl-tier"></div>
        <div><div class="lang-switch" id="cl-langsw"></div></div>
      </div>
    </div>
    <div class="card hidden" id="cl-nophone" style="border-color:rgba(251,191,36,.4)" data-t="finish_reg"></div>
    <div class="stats" style="grid-template-columns:1fr 1fr">
      <div class="stat s1 soon-stat" id="cl-st-buy"><div class="ic">🛍</div><b id="cl-buy">—</b><span data-t="purchases"></span><em data-t="soon"></em></div>
      <div class="stat s3 soon-stat" id="cl-st-bonus"><div class="ic">🎁</div><b id="cl-bonus">—</b><span data-t="bonuses"></span><em data-t="soon"></em></div>
    </div>
    <div class="hint hidden" style="font-size:12.5px;text-align:center;margin:-6px 0 12px" id="cl-billz-note"></div>
    <div style="text-align:center;margin:-6px 0 12px" class="hidden" id="cl-refresh-row">
      <button class="link" id="cl-refresh" style="font-size:13px"></button></div>
    <div style="display:none">
    </div>
    <div class="card soon"><div class="soon-ic">💰</div><div style="flex:1"><b data-t="invest"></b>
      <div class="hint" style="font-size:13px" data-t="invest_soon"></div></div><span class="soon-pill" data-t="soon_pill"></span></div>
    <div class="card"><h3 data-t="news"></h3><div id="cl-news"></div></div>
  </div>

  <!-- ВСЯ ИСТОРИЯ -->
  <div id="view-history" class="hidden">
    <div class="card"><h3 data-sx="all_visits">Все посещения</h3><div id="history-all"></div></div>
  </div>

  <!-- КОМАНДА -->
  <div id="view-team" class="hidden">
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
  </div>

  <!-- КОМАНДА: сотрудники, задачи и штрафы (отдельная страница руководителя) -->
  <div id="view-staff" class="hidden">
    <div class="page-title">👥 Команда</div>
    <div class="page-sub" id="staff-page-sub"></div>
    <div class="staff-actions">
      <details class="card staff-act">
        <summary><b>📌 Новая задача</b><span class="hint">›</span></summary>
        <div style="margin-top:12px">
          <select id="t-user"></select>
          <input id="t-title" placeholder="Что нужно сделать?" maxlength="500">
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
const tg = window.Telegram.WebApp;
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
    body: JSON.stringify(Object.assign({initData: tg.initData}, body))});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || "Ошибка");
  return j;
}
async function action(path, body) {
  try { const r = await api(path, body); toast(r.msg);
    if (r.close) { setTimeout(() => tg.close(), 1800); }
    if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(r.ok ? "success" : "warning");
    await load();
  } catch (e) { toast(e.message); }
}

/* ---------- экраны ---------- */
const SUBPAGES = {history: "me", invest: "me", "invest-admin": "team", clients: "team", staff: "team",
                  projects: "team", project: "projects", posting: "team"};  // страница → куда ведёт «назад»
function show(v) {
  view = v;
  ["me", "history", "team", "client", "invest", "invest-admin", "clients", "staff", "projects", "project", "posting"].forEach(n => $("view-" + n).classList.toggle("hidden", n !== v));
  $("seg").classList.toggle("hidden", !(data && data.is_admin && data.is_employee) || v in SUBPAGES);
  document.querySelectorAll("#seg button").forEach(b => b.classList.toggle("on", b.dataset.view === v));
  if (v in SUBPAGES) tg.BackButton.show(); else tg.BackButton.hide();
  window.scrollTo(0, 0);
}
tg.BackButton.onClick(() => show(SUBPAGES[view] || "me"));

/* ---------- тексты кабинета сотрудника (uz / ru) ---------- */
const SX = {
  ru: {h: "ч", per_month: "за месяц", in_row: "подряд", cur_rank: "текущее звание", invest: "Инвестиции",
       my_week: "🗓 Мой график на неделю", ranks: "Звания", my_tasks: "📋 Мои задачи", last_visits: "Последние посещения",
       all: "Все →", all_visits: "Все посещения", now: "сейчас", late: "опоздание", early: "ранний уход",
       auto: "уход закрыт автоматически", here_since: "На месте с {t}", not_here: "Не на месте",
       close_tasks: "⛔ Сначала закройте задачи ({n})", free_here: "Свободный график · отметьте уход, когда закончите",
       free_any: "Свободный график · отмечайтесь в любое время", day_off: "Сегодня выходной", out_at: "Уход: {w}",
       shift_done: "Смена завершена · график {s}", in_at: "Приход: {w}", btn_out: "🚪 Ушёл", btn_in: "✅ Пришёл",
       fines: "🔴 Штрафы", total: "всего", this_month: "за этот месяц", no_fines_month: "В этом месяце штрафов нет 👍",
       no_fines: "Штрафов нет — так держать! 👍", due: "📅 на {d}", review: "⏳ на проверке", done: "Готово",
       no_tasks: "Задач нет 🎉", no_visits: "Посещений пока нет", to_rank: "до звания «{r}»",
       keep_going: "Продолжайте в том же духе!", you_are: "Вы — {r}! 🏆", left_to: "До звания «{r}» осталось {h} ч 💪",
       goal_done: "Цель {h} часов достигнута", soon: "Раздел скоро появится", soon_pill: "Скоро ⏳",
       invest_soon: "💰 Раздел «Инвестиции» скоро появится ⏳", lang_btn: "🌐 O'zbekchaga o'tish", photo: "Загружаю фото…"},
  uz: {h: "soat", per_month: "shu oyda", in_row: "ketma-ket", cur_rank: "joriy unvon", invest: "Investitsiyalar",
       my_week: "🗓 Haftalik grafigim", ranks: "Unvonlar", my_tasks: "📋 Vazifalarim", last_visits: "Oxirgi tashriflar",
       all: "Hammasi →", all_visits: "Barcha tashriflar", now: "hozir", late: "kechikish", early: "erta ketish",
       auto: "ketish avtomatik yopildi", here_since: "{t} dan beri ishda", not_here: "Ishda emas",
       close_tasks: "⛔ Avval vazifalarni yoping ({n})", free_here: "Erkin grafik · tugatganingizda ketishni belgilang",
       free_any: "Erkin grafik · istalgan vaqtda belgilang", day_off: "Bugun dam olish kuni", out_at: "Ketish: {w}",
       shift_done: "Smena tugadi · grafik {s}", in_at: "Kelish: {w}", btn_out: "🚪 Ketdim", btn_in: "✅ Keldim",
       fines: "🔴 Jarimalar", total: "jami", this_month: "shu oyda", no_fines_month: "Bu oyda jarima yo'q 👍",
       no_fines: "Jarima yo'q — shunday davom eting! 👍", due: "📅 {d} ga", review: "⏳ tekshiruvda", done: "Bajarildi",
       no_tasks: "Vazifa yo'q 🎉", no_visits: "Hozircha tashriflar yo'q", to_rank: "«{r}» unvonigacha",
       keep_going: "Shunday davom eting!", you_are: "Siz — {r}! 🏆", left_to: "«{r}» unvonigacha {h} soat qoldi 💪",
       goal_done: "{h} soatlik maqsadga erishildi", soon: "Bo'lim tez orada ochiladi", soon_pill: "Tez orada ⏳",
       invest_soon: "💰 «Investitsiyalar» bo'limi tez orada ochiladi ⏳", lang_btn: "🌐 Перейти на русский", photo: "Rasm yuklanmoqda…"},
};
const meLang = () => (data && data.profile && data.profile.lang === "uz") ? "uz" : "ru";
const sx = (k, v = {}) => (SX[meLang()][k] || SX.ru[k] || k).replace(/\{(\w+)\}/g, (_, n) => v[n] ?? "");
const daysSx = n => meLang() === "uz" ? `${n} kun` : n + " " + plural(n, "день", "дня", "дней");

function visitRow(h, full) {
  return `<div class="visit ${full ? "" : "short"}">
    <div class="t">
      <span>${icCal}${h.date.slice(0, 5)}<span class="yr">${h.date.slice(5)}</span>${full ? ` <span class="hint">${h.wd}</span>` : ""}</span>
      <span>${icClock}${h.in} – ${h.out || sx("now")}</span>
      ${full && (h.late || h.early || h.auto) ? `<div class="tag" style="width:100%">
        ${h.late ? `<span style="color:var(--orange)">${sx("late")} ${h.late}</span> ` : ""}
        ${h.early ? `<span style="color:var(--orange)">${sx("early")} ${h.early}</span> ` : ""}
        ${h.auto ? `<span class="hint">${sx("auto")}</span>` : ""}</div>` : ""}
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
    : sx("in_at", {w: p.in_window});
  $("btn-mark").textContent = p.on_site ? sx("btn_out") : sx("btn_in");
  $("btn-mark").className = "btn " + (p.on_site ? "btn-soft" : "btn-main");
  $("btn-mark").disabled = !can;
  $("pen-card").style.borderColor = p.penalties_total ? "rgba(248,113,113,.35)" : "";
  $("pen-card").innerHTML = `
    <div class="head"><h3>${sx("fines")}</h3></div>
    <div class="pen-stats">
      <div><b style="color:${p.penalties_total ? "var(--red)" : "var(--green)"}">${p.penalties_total}</b><span>${sx("total")}</span></div>
      <div><b style="color:${p.penalties_month ? "var(--red)" : "var(--green)"}">${p.penalties_month}</b><span>${sx("this_month")}</span></div>
    </div>` + (p.penalties.length ? p.penalties.map(x => `
    <div class="pen"><div class="tt">${x.date} · ${esc(x.reason)}</div>
    ${x.amount ? `<b style="color:var(--red)">${esc(x.amount)}</b>` : ""}</div>`).join("")
    : `<div class="empty">${p.penalties_total ? sx("no_fines_month") : sx("no_fines")}</div>`);

  $("levels").innerHTML = L.map((l, k) => (k ? `<div class="arrow">›</div>` : "") + `
    <div class="lv ${k > idx ? "locked" : ""} ${k === idx ? "cur" : ""}">${l.icon()}<b>${l.name}</b>
    <span>${l.to === null ? num(l.from) + "+" : num(l.from) + " – " + num(l.to)}</span></div>`).join("");

  const open = data.tasks.filter(t => !t.done), done = data.tasks.filter(t => t.done).slice(0, 3);
  $("tasks").innerHTML = (open.length || done.length) ? [...open, ...done].map(t => `
    <div class="task ${t.done ? "done" : ""}"><div class="tt">${esc(t.title)}<div class="hint" style="font-size:12px">${t.created}</div></div>
    ${t.done ? "✔️" : t.due ? `<span class="hint" style="font-size:13px;white-space:nowrap">${sx("due", {d: t.due})}</span>`
      : t.status === "review" ? `<span class="hint" style="font-size:13px;white-space:nowrap">${sx("review")}</span>`
      : `<button class="btn btn-main btn-sm" data-done="${t.id}">${sx("done")}</button>`}</div>`).join("")
    : `<div class="empty">${sx("no_tasks")}</div>`;
  document.querySelectorAll("[data-done]").forEach(b => b.onclick = () => action("/api/task_done", {id: +b.dataset.done}));

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
function renderTeam() {
  renderStaffFolder();
  $("t-user").innerHTML = $("f-user").innerHTML = data.team.map(e => `<option value="${e.id}">${esc(e.name)}</option>`).join("");
  $("team").innerHTML = data.team.map(e => {
    const [lvl] = levelOf(e.total_hours, data.goal);
    const pct = Math.min(100, e.total_hours / data.goal * 100);
    return `<div class="card">
      <div class="member"><div class="ava">${avatar(e.id, e.name, e.ava_v)}</div>
        <div style="flex:1;min-width:0">
          <div style="display:flex;justify-content:space-between;gap:8px"><button class="name-btn" data-week="${e.id}">${esc(e.name)} ✏️</button><span>${e.icon}</span></div>
          <div class="hint" style="font-size:13px">${esc(e.role)} · сегодня: ${esc(e.schedule)}</div>
          <div style="font-size:14px;margin-top:2px">${esc(e.status)}${e.late ? ` <span style="color:var(--red)">· опоздание ${e.late}</span>` : ""}</div>

        </div></div>
      ${weekChips(e.week, data.today_wd)}
      <div class="bar mini-bar"><div style="width:${pct}%"></div></div>
      <div class="hint" style="font-size:13px;margin-top:6px"><span class="rank-sm">${lvl.icon()}</span> ${esc(lvl.name)} · ${num(e.total_hours)} / ${num(data.goal)} ч</div>
      <div style="font-size:13px;margin-top:4px;color:${e.penalties_total ? "var(--red)" : "var(--hint)"}">🔴 Штрафов: всего ${e.penalties_total} · за месяц ${e.penalties_month}</div>
      ${e.penalties.map(x => `<div class="pen"><div class="tt"><span style="color:var(--red)">🔴 ${x.date}</span> · ${esc(x.reason)}${x.amount ? ` · <b>${esc(x.amount)}</b>` : ""}</div>
        <button class="btn btn-soft btn-sm" data-delpen="${x.id}">Отменить</button></div>`).join("")}
      ${e.tasks.map(t => `<div class="task"><div class="tt">📌 ${esc(t.title)}${t.due
          ? `<div style="font-size:12px" class="hint">📅 на ${t.due}</div>`
          : t.status === "review" ? `<div style="font-size:12px;color:var(--orange)">⏳ ждёт вашей проверки</div>` : ""}</div>
        ${t.status === "review" && !t.due ? `<button class="btn btn-main btn-sm" data-review="${t.id}" data-act="ok">✅</button>
        <button class="btn btn-soft btn-sm" data-review="${t.id}" data-act="back">↩️</button>` : ""}
        ${t.due ? "" : `<button class="btn btn-soft btn-sm" data-review="${t.id}" data-act="later" title="На завтра">📅</button>`}
        ${t.status === "review" ? "" : `<button class="btn btn-soft btn-sm" data-del="${t.id}">✕</button>`}</div>`).join("")}
    </div>`; }).join("");
  document.querySelectorAll("[data-week]").forEach(b => b.onclick = () => openWeek(+b.dataset.week));
  document.querySelectorAll("[data-review]").forEach(b => b.onclick = () =>
    action("/api/task_review", {id: +b.dataset.review, action: b.dataset.act}));
  document.querySelectorAll("[data-delpen]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отменить штраф?", ok => ok && action("/api/delete_penalty", {id: +b.dataset.delpen})));
  document.querySelectorAll("[data-del]").forEach(b => b.onclick = () =>
    tg.showConfirm("Удалить задачу?", ok => ok && action("/api/delete_task", {id: +b.dataset.del})));
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
      ${meta.length ? `<div class="pmeta">${meta.map(m => `<span>${m}</span>`).join("")}</div>` : ""}</div></div>`;
}
function bindPtasks() {
  document.querySelectorAll("[data-pdone]").forEach(b => b.onclick = () => projAct({action: "done", id: +b.dataset.pdone, done: b.dataset.v === "1"}));
  document.querySelectorAll("[data-pedit]").forEach(b => b.onclick = () => editPtask(+b.dataset.pedit));
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
    <div class="add-row" style="margin-bottom:10px"><input type="date" id="et-due" value="${x.due_date || ""}">
      <button class="btn btn-soft" id="et-nodue">Без даты</button></div>
    ${sheetBtns("Сохранить")}
    <button class="link" id="et-del" style="color:var(--red);display:block;margin:14px auto 0">Удалить задачу</button>`);
  $("et-nodue").onclick = () => { $("et-due").value = ""; };
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
const poAud = new Set();
function renderPosting() {
  const a = data.posting.audience, h = data.posting.history;
  const opts = [["clients", `👥 Клиенты (${a.clients})`], ["staff", `🧑‍💼 Сотрудники (${a.staff})`],
                ...a.chats.map(c => ["chat:" + c.id, "📢 " + c.title])];
  $("po-aud").innerHTML = opts.map(([k, t]) => `<button data-aud="${k}" class="${poAud.has(k) ? "on" : ""}">${esc(t)}</button>`).join("");
  document.querySelectorAll("[data-aud]").forEach(b => b.onclick = () => {
    poAud.has(b.dataset.aud) ? poAud.delete(b.dataset.aud) : poAud.add(b.dataset.aud);
    b.classList.toggle("on", poAud.has(b.dataset.aud));
  });
  const cl = a.client_langs || {};
  $("po-lang-hint").textContent = `Клиенты по языкам: UZ ${cl.uz || 0} · RU ${cl.ru || 0} · EN ${cl.en || 0}`;
  const planned = h.filter(p => p.status === "scheduled").length;
  $("posting-folder-sub").textContent = `клиентов: ${a.clients} · групп: ${a.chats.length}` +
    (planned ? ` · запланировано: ${planned}` : h.length ? ` · постов: ${h.length}` : "");
  $("po-history").innerHTML = h.length ? h.map(p => {
    const st = p.status === "scheduled" ? `<span class="chip pending">🕒 ${p.when}</span>`
      : `<span class="chip ${p.queued ? "active" : "matured"}">${p.queued ? "📤 отправляется" : "✅ отправлен"}</span>`;
    const stats = p.status === "scheduled" ? "" : `<span>доставлено: ${p.delivered} из ${p.total}</span>` +
      (p.failed ? `<span class="late">не доставлено: ${p.failed}</span>` : "") + (p.queued ? `<span>в очереди: ${p.queued}</span>` : "");
    return `<div class="po-post"><div class="inv-top"><span class="hint" style="font-size:12.5px">${p.status === "scheduled" ? "Запланирован" : p.when}${p.has_photo ? " · 📷" : ""}${p.button_text ? " · 🔗" : ""}<span class="lang-badges">${(p.langs || []).map(l => `<span>${l.toUpperCase()}</span>`).join("")}</span></span>${st}</div>
      <div class="pt">${esc(p.text || "(только фото)")}</div>
      <div class="pmeta"><span>→ ${esc(p.audience_titles.join(", "))}</span>${stats}</div>
      ${p.status === "scheduled" ? `<div class="inv-act"><button class="btn btn-soft btn-sm" data-pocancel="${p.id}">Отменить</button></div>` : ""}</div>`;
  }).join("") : `<div class="empty">Постов пока нет</div>`;
  document.querySelectorAll("[data-pocancel]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отменить запланированный пост?", ok => ok && action("/api/post", {action: "cancel", id: +b.dataset.pocancel})));
}
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
function poBody() {
  return {texts: poEd.texts(), image: poPhoto || "", button_text: $("po-btn-text").value.trim(),
          button_url: $("po-btn-url").value.trim()};
}
function poReset() {
  poEd.clear(); $("po-btn-text").value = ""; $("po-btn-url").value = ""; $("po-at").value = "";
  $("po-news").checked = false; poAud.clear(); $("po-photo-del").onclick(); poCount();
}
$("po-test").onclick = () => {
  const b = poBody(); if (!Object.keys(b.texts).length && !b.image) return toast("Напишите текст или добавьте фото");
  toast("Отправляю…"); api("/api/post", Object.assign({action: "test"}, b)).then(r => toast(r.msg)).catch(e => toast(e.message));
};
$("po-send").onclick = () => {
  const b = poBody();
  if (!Object.keys(b.texts).length && !b.image) return toast("Напишите текст или добавьте фото");
  if (!poAud.size) return toast("Выберите, кому отправить");
  if (poWhen === "later" && !$("po-at").value) return toast("Выберите дату и время");
  const names = [...document.querySelectorAll("[data-aud].on")].map(x => x.textContent).join(", ");
  const when = poWhen === "later" ? `\nКогда: ${$("po-at").value.replace("T", " ")}` : "\nОтправить сейчас.";
  const filled = Object.keys(b.texts), missing = LANG_TABS.map(x => x[0]).filter(k => !filled.includes(k));
  const langs = `\nЯзыки: ${filled.map(k => k.toUpperCase()).join(", ") || "—"}` +
    (missing.length && filled.length ? ` (без ${missing.map(k => k.toUpperCase()).join(", ")} — получат на ${filled.includes("uz") ? "UZ" : filled[0].toUpperCase()})` : "");
  tg.showConfirm(`Опубликовать пост?\nКому: ${names}${when}${langs}`, ok => {
    if (!ok) return;
    toast("Отправляю…");
    api("/api/post", Object.assign({action: "create", audience: [...poAud], to_news: $("po-news").checked,
        send_at: poWhen === "later" ? $("po-at").value : ""}, b))
      .then(r => { toast(r.msg); if (r.ok) poReset(); return load(); }).catch(e => toast(e.message));
  });
};
poCount();

async function load(fresh = false) {
  try {
    data = await api("/api/me", fresh ? {fresh: true} : {});
    $("loading").classList.add("hidden");
    $("error").classList.add("hidden");  // прошлая ошибка больше не актуальна
    if (data.is_client) { renderClient(); show("client"); return; }
    if (data.is_employee) renderMe();
    if (data.invest) renderInvestMe();
    // руководитель без кабинета сотрудника открывает свои инвестиции с вкладки «Команда»
    $("go-inv-own").classList.toggle("hidden", !(data.invest_enabled && data.is_admin && !data.is_employee));
    if (!data.invest_enabled) investSoon();
    SUBPAGES.invest = data.is_employee ? "me" : "team";
    if (data.is_admin) { renderTeam(); renderNewsAdmin(); renderClientsAdmin(); if (data.invest_admin) renderInvestAdmin();
                         if (data.projects) renderProjects(); if (data.posting) renderPosting(); }
    if (!data.is_employee && view === "me") view = "team";
    show(view);
  } catch (e) {
    $("loading").classList.add("hidden");
    $("error").classList.remove("hidden");
    $("error").textContent = e.message;
  }
}

function newsHtml(items, admin) {
  return items.length ? items.map(n => `<div class="news-item"><div class="hint" style="font-size:12px;display:flex;justify-content:space-between">
      <span>🗓 ${n.date}${admin && n.langs && n.langs.length ? `<span class="lang-badges">${n.langs.map(l => `<span>${l.toUpperCase()}</span>`).join("")}</span>` : ""}</span>${admin ? `<button class="link" data-delnews="${n.id}">удалить</button>` : ""}</div>${esc(n.text)}</div>`).join("")
    : `<div class="empty">Новостей пока нет</div>`;
}
const TIER_EMOJI = {bronza: "🥉", silver: "🥈", gold: "🥇"};
const CL_T = {
  uz: {purchases: "xaridlar summasi", bonuses: "bonuslar", soon: "tez orada", invest: "Investitsiyalar",
       invest_soon: "Bo'lim tez orada paydo bo'ladi", soon_pill: "Tez orada ⏳", news: "📰 Yangiliklar",
       no_news: "Hozircha yangiliklar yo'q", no_phone: "raqam ko'rsatilmagan", client: "Mijoz",
       finish_reg: "📱 Ro'yxatdan o'tishni bot bilan chatda yakunlang — shunda xaridlaringiz va bonuslaringizni topamiz.",
       birth: "🎂 Tug'ilgan kun", photo: "Rasm yuklanmoqda…",
       billz_not_found: "Raqamingiz hali do'kon bazasida yo'q — birinchi xariddan keyin bu yerda xaridlar va bonuslar paydo bo'ladi",
       billz_error: "Ma'lumotni hozir olib bo'lmadi, keyinroq yangilanadi", refresh: "🔄 Yangilash", refreshing: "⏳ Yangilanmoqda…"},
  ru: {purchases: "сумма покупок", bonuses: "бонусы", soon: "скоро", invest: "Инвестиции",
       invest_soon: "Раздел скоро появится", soon_pill: "Скоро ⏳", news: "📰 Новости",
       no_news: "Новостей пока нет", no_phone: "номер не указан", client: "Клиент",
       finish_reg: "📱 Завершите регистрацию в чате с ботом — так мы найдём ваши покупки и бонусы.",
       birth: "🎂 День рождения", photo: "Загружаю фото…",
       billz_not_found: "Вашего номера пока нет в базе магазина — после первой покупки здесь появятся покупки и бонусы",
       billz_error: "Сейчас не удалось получить данные, обновится позже", refresh: "🔄 Обновить", refreshing: "⏳ Обновляю…"},
  en: {purchases: "total purchases", bonuses: "bonuses", soon: "soon", invest: "Investments",
       invest_soon: "This section is coming soon", soon_pill: "Soon ⏳", news: "📰 News",
       no_news: "No news yet", no_phone: "no phone number", client: "Client",
       finish_reg: "📱 Please finish registration in the bot chat so we can find your purchases and bonuses.",
       birth: "🎂 Birthday", photo: "Uploading photo…",
       billz_not_found: "Your number isn't in the store database yet — purchases and bonuses will appear after your first purchase",
       billz_error: "Couldn't get the data right now, it will update later", refresh: "🔄 Refresh", refreshing: "⏳ Refreshing…"},
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
  const note = !bz.enabled ? "" : bz.error && !bz.linked ? ct("billz_error") : !bz.linked ? ct("billz_not_found") : "";
  $("cl-billz-note").textContent = note; $("cl-billz-note").classList.toggle("hidden", !note);
  $("cl-refresh-row").classList.toggle("hidden", !bz.enabled);
  $("cl-refresh").textContent = ct("refresh") + (bz.synced ? " · " + bz.synced.slice(11, 16) : "");
  $("cl-news").innerHTML = data.news.length ? newsHtml(data.news, false) : `<div class="empty">${ct("no_news")}</div>`;
}
function renderClientsAdmin() {
  const q = ($("cl-search").value || "").trim().toLowerCase();
  const list = (data.client_list || []).filter(c =>
    !q || (c.name + " " + (c.phone || "") + " " + c.username).toLowerCase().includes(q));
  $("cl-total").textContent = `всего: ${data.client_list.length}`;
  $("cl-list").innerHTML = list.length ? list.slice(0, 100).map(c => `
    <div class="cl-row"><div style="font-size:20px">${TIER_EMOJI[c.tier] || ""}</div>
      <div style="flex:1;min-width:0"><b>${esc(c.name || "Без имени")}</b>
        <div class="hint" style="font-size:12px">${esc(c.phone || "номер не указан")}${c.birth ? " · 🎂 " + c.birth : ""}
          · ${(c.lang || "—").toUpperCase()} · с ${c.since}</div></div>
      <select data-tier="${c.id}">${Object.entries(data.tiers).map(([k, t]) =>
        `<option value="${k}" ${k === c.tier ? "selected" : ""}>${t}</option>`).join("")}</select></div>`).join("")
    : `<div class="empty">${q ? "Никого не нашли" : "Клиентов пока нет"}</div>`;
  document.querySelectorAll("[data-tier]").forEach(s => s.onchange = () =>
    action("/api/set_tier", {user_id: +s.dataset.tier, tier: s.value}));
}
$("cl-search").oninput = () => renderClientsAdmin();

function renderNewsAdmin() {
  $("cl-count").textContent = `клиентов: ${data.clients.total} (с номером: ${data.clients.with_phone})`;
  $("clients-folder-sub").textContent = `${data.clients.total} ${plural(data.clients.total, "клиент", "клиента", "клиентов")}` +
    ` · с номером: ${data.clients.with_phone} · новостей: ${data.news.length}`;
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
const SOON = "💰 Раздел «Инвестиции» скоро появится ⏳";
$("go-inv").onclick = () => data.invest_enabled ? show("invest") : toast(data.profile ? sx("invest_soon") : SOON);
$("go-inv-own").onclick = () => data.invest_enabled ? show("invest") : toast(SOON);
$("go-clients").onclick = () => show("clients");
$("go-staff").onclick = () => show("staff");
$("go-projects").onclick = () => show("projects");
$("go-posting").onclick = () => show("posting");
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
$("btn-mark").onclick = () => action(data.profile.on_site ? "/api/checkout" : "/api/checkin");
$("t-add").onclick = () => {
  const title = $("t-title").value.trim();
  if (!title) return toast("Введите задачу");
  action("/api/add_task", {user_id: +$("t-user").value, title}).then(() => $("t-title").value = "");
};

$("f-add").onclick = () => {
  const reason = $("f-reason").value.trim();
  if (!reason) return toast("Укажите причину");
  tg.showConfirm("Выписать штраф?", ok => ok && action("/api/add_penalty",
    {user_id: +$("f-user").value, reason, amount: $("f-amount").value.trim()})
    .then(() => { $("f-reason").value = ""; $("f-amount").value = ""; }));
};

if (!tg.initData) $("loading").innerHTML = "Kabinetni bot ichidagi tugma orqali oching: pastdagi «🏠» tugmasini bosing va " +
  "kelgan xabardagi «🏠 Kabinetni ochish» tugmasini bosing yoki maydon chap tomonidagi «Kabinet» tugmasini bosing.<br><br>" +
  "Откройте кабинет через бота: нажмите «🏠» внизу, затем «🏠 Открыть кабинет» в сообщении бота — или кнопку «Kabinet» слева от поля ввода.<br><br>" +
  "Open your account from the bot: tap «🏠» below, then «🏠 Open account» in the bot's message — or the «Kabinet» button left of the input field.";
else { load(true); setInterval(() => load(), 30000); }  // при открытии — свежие данные из BILLZ
</script>
</body>
</html>
"""
