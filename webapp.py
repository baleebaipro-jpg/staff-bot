"""Мини-апп: личный кабинет сотрудника и панель руководителя."""
import base64
import hashlib
import hmac
import json
import time
from functools import wraps
from urllib.parse import parse_qsl

import os

from flask import Flask, abort, jsonify, request, send_file

import config
import core
import i18n
import invest

AVATAR_DIR = getattr(config, "AVATAR_DIR", "/home/abdullohbe/avatars")

app = Flask(__name__)
core.init_db()


def telegram_user_id():
    """Проверить подпись Telegram и вернуть ID пользователя (или None)."""
    init_data = (request.get_json(silent=True) or {}).get("initData", "")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    if time.time() - int(pairs.get("auth_date", 0)) > 86400:
        return None
    return json.loads(pairs.get("user", "{}")).get("id")


def need_user(admin=False, staff=False):
    """admin — только руководитель; staff — сотрудник или руководитель; иначе — любой (в т.ч. клиент)."""
    def deco(fn):
        @wraps(fn)
        def wrapper():
            uid = telegram_user_id()
            if uid is None:
                return jsonify(error="Откройте кабинет через Telegram-бота."), 401
            if admin and not core.is_admin(uid):
                return jsonify(error="Только для руководителя."), 403
            if staff and not (core.employee(uid) or core.is_admin(uid)):
                return jsonify(error="Только для сотрудников."), 403
            return fn(uid, request.get_json(silent=True) or {})
        return wrapper
    return deco


@app.route("/")
def index():
    return PAGE


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
    raw = str(data.get("image", ""))
    if "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        img = base64.b64decode(raw, validate=True)
    except Exception:
        return jsonify(ok=False, msg="Не удалось прочитать фото.")
    if len(img) > 2_000_000:
        return jsonify(ok=False, msg="Фото слишком большое.")
    if not img.startswith(b"\xff\xd8\xff"):
        return jsonify(ok=False, msg="Нужна картинка в формате JPEG.")
    os.makedirs(AVATAR_DIR, exist_ok=True)
    tmp = os.path.join(AVATAR_DIR, f"{uid}.tmp")
    with open(tmp, "wb") as f:
        f.write(img)
    os.replace(tmp, os.path.join(AVATAR_DIR, f"{uid}.jpg"))
    # отметка, что фото своё — бот не будет заменять его фото из Telegram
    open(os.path.join(AVATAR_DIR, f"{uid}.custom"), "w").close()
    lang = i18n.lang_of((core.get_client(uid) or {}).get("lang")) if core.is_client(uid) else "ru"
    return jsonify(ok=True, msg={"uz": "Rasm yangilandi 📸", "ru": "Фото обновлено 📸", "en": "Photo updated 📸"}[lang])


@app.post("/api/me")
@need_user()
def api_me(uid, data):
    res = {"is_admin": core.is_admin(uid), "is_employee": bool(core.employee(uid)), "goal": config.GOAL_HOURS,
           "ranks": getattr(config, "RANKS", None),
           "today_wd": core.now().weekday()}
    if res["is_employee"]:
        res["profile"] = core.profile(uid)
        res["profile"]["ava_v"] = ava_version(uid)
        res["tasks"] = core.tasks(uid)
        res["history"] = core.history(uid)
        res["invest"] = invest.mine(uid)
    res["is_client"] = not res["is_admin"] and not res["is_employee"]
    if res["is_client"]:
        cl = core.get_client(uid) or {}
        tier = core.tier_of(cl)
        birth = cl.get("birth_date")
        res["client"] = {"id": uid, "name": core.client_full_name(cl), "phone": cl.get("phone"),
                         "tier": tier, "tier_title": core.CLIENT_TIERS[tier]["title"],
                         "ava_v": ava_version(uid), "lang": i18n.lang_of(cl.get("lang")),
                         "registered": cl.get("step") == "done",
                         "birth": f"{birth[8:10]}.{birth[5:7]}" if birth else None}
        res["news"] = core.news_list(10)
    if res["is_employee"] or res["is_admin"]:
        res["invest_cfg"] = {"levels": invest.levels_info(), "term": invest.TERM, "min": invest.MIN_AMOUNT,
                             "max": invest.MAX_AMOUNT, "currency": invest.CURRENCY,
                             "annual_min": invest.ANNUAL_MIN, "annual_max": invest.ANNUAL_MAX,
                             "history": invest.history_public()}
    if res["is_admin"]:
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
        return jsonify(ok=True, close=True, msg="📹 Запишите кружок в чате с ботом — это подтвердит приход")
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
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify(ok=False, msg="Напишите текст новости.")
    core.add_news(text)
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
      <label class="ava" title="Изменить фото">
        <span id="me-ava" style="display:contents"></span>
        <span class="ava-edit">📷</span>
        <input type="file" id="ava-file" accept="image/*" hidden>
      </label>
      <div style="min-width:0">
        <div class="pname" id="p-name"></div>
        <div class="prole" id="p-role"></div>
        <div class="lvl-pill" id="p-lvl"></div>
      </div>
    </div>

    <div class="goal"><span id="g-have"></span> <small>/ <span id="g-goal"></span> ч</small></div>
    <div class="hint" id="g-to">до высшего звания</div>
    <div class="bar-row"><div class="bar"><div id="g-bar" style="width:0"></div></div><div class="pct" id="g-pct"></div></div>

    <div class="stats">
      <div class="stat s1"><div class="ic">📅</div><b id="s-month"></b><span>за месяц</span></div>
      <div class="stat s2"><div class="ic">🔥</div><b id="s-streak"></b><span>подряд</span></div>
      <div class="stat s3"><div class="ic">📊</div><b id="s-level"></b><span>текущее звание</span></div>
    </div>

    <button class="card folder" id="go-inv">
      <div class="f-ic">💰</div>
      <div style="min-width:0"><b>Инвестиции</b><div class="f-sub" id="inv-folder-sub"></div></div>
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
      <div class="hint" style="font-size:13px">🗓 Мой график на неделю</div>
      <div class="week" id="my-week"></div>
    </div>
    <div class="card" id="pen-card"></div>

    <div class="card">
      <h3>Звания</h3>
      <div class="levels" id="levels"></div>
    </div>

    <div class="card">
      <div class="head"><h3>📋 Мои задачи</h3></div>
      <div id="tasks"></div>
    </div>

    <div class="card">
      <div class="head">
        <h3><svg class="ico" style="width:22px;height:22px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Последние посещения</h3>
        <button class="link" id="all-btn">Все →</button>
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
    <div class="card">
      <div id="inv-me"></div>
      <details class="inv-form" id="inv-form">
        <summary class="btn btn-main" style="display:block;text-align:center;margin-top:10px">➕ Открыть вклад</summary>
        <div style="margin-top:12px">
          <input id="inv-amount" inputmode="numeric" autocomplete="off" placeholder="Сумма">
          <div class="inv-calc" id="inv-calc"></div>
          <button class="btn btn-main" style="width:100%" id="inv-send">Подать заявку</button>
          <div class="hint" style="font-size:12.5px;margin-top:10px;line-height:1.45" id="inv-rules"></div>
        </div>
      </details>
    </div>
  </div>

  <!-- ИНВЕСТИЦИИ (отдельная страница руководителя) -->
  <div id="view-invest-admin" class="hidden">
    <div class="page-title">💰 Инвестиции сотрудников</div>
    <div class="page-sub">Заявки, оценка месяца и выплаты</div>
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
      </div>
    </div>
    <div class="card hidden" id="cl-nophone" style="border-color:rgba(251,191,36,.4)" data-t="finish_reg"></div>
    <div class="stats" style="grid-template-columns:1fr 1fr">
      <div class="stat s1 soon-stat"><div class="ic">🛍</div><b>—</b><span data-t="purchases"></span><em data-t="soon"></em></div>
      <div class="stat s3 soon-stat"><div class="ic">🎁</div><b>—</b><span data-t="bonuses"></span><em data-t="soon"></em></div>
    </div>
    <div class="card soon"><div class="soon-ic">💰</div><div style="flex:1"><b data-t="invest"></b>
      <div class="hint" style="font-size:13px" data-t="invest_soon"></div></div><span class="soon-pill" data-t="soon_pill"></span></div>
    <div class="card"><h3 data-t="news"></h3><div id="cl-news"></div></div>
  </div>

  <!-- ВСЯ ИСТОРИЯ -->
  <div id="view-history" class="hidden">
    <div class="card"><h3>Все посещения</h3><div id="history-all"></div></div>
  </div>

  <!-- КОМАНДА -->
  <div id="view-team" class="hidden">
    <div class="card">
      <div class="head"><h3>💎 Клиенты</h3><span class="hint" style="font-size:13px" id="cl-total"></span></div>
      <input id="cl-search" placeholder="Поиск: имя или номер">
      <div id="cl-list"></div>
    </div>
    <div class="card">
      <div class="head"><h3>📰 Новости для клиентов</h3><span class="hint" style="font-size:13px" id="cl-count"></span></div>
      <textarea id="n-text" rows="3" placeholder="Текст новости…" maxlength="2000"></textarea>
      <button class="btn btn-main" style="width:100%" id="n-add">Опубликовать</button>
      <div id="n-list" style="margin-top:8px"></div>
    </div>
    <button class="card folder" id="go-inv-admin">
      <div class="f-ic">💰</div>
      <div style="min-width:0"><b>Инвестиции сотрудников<span id="inv-admin-badge"></span></b>
        <div class="f-sub" id="inv-admin-folder-sub"></div></div>
      <div class="f-arrow">›</div>
    </button>
    <div class="card">
      <h3>Новая задача</h3>
      <select id="t-user"></select>
      <input id="t-title" placeholder="Что нужно сделать?" maxlength="500">
      <button class="btn btn-main" style="width:100%" id="t-add">Поставить задачу</button>
    </div>
    <div class="card">
      <h3>🔴 Выписать штраф</h3>
      <select id="f-user"></select>
      <input id="f-amount" placeholder="Сумма, например 50 000 сум (можно пусто)" maxlength="50">
      <input id="f-reason" placeholder="Причина" maxlength="300">
      <button class="btn btn-danger" style="width:100%" id="f-add">Выписать штраф</button>
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
const SUBPAGES = {history: "me", invest: "me", "invest-admin": "team"};  // страница → куда ведёт «назад»
function show(v) {
  view = v;
  ["me", "history", "team", "client", "invest", "invest-admin"].forEach(n => $("view-" + n).classList.toggle("hidden", n !== v));
  $("seg").classList.toggle("hidden", !(data && data.is_admin && data.is_employee) || v in SUBPAGES);
  document.querySelectorAll("#seg button").forEach(b => b.classList.toggle("on", b.dataset.view === v));
  if (v in SUBPAGES) tg.BackButton.show(); else tg.BackButton.hide();
  window.scrollTo(0, 0);
}
tg.BackButton.onClick(() => show(SUBPAGES[view] || "me"));

function visitRow(h, full) {
  return `<div class="visit ${full ? "" : "short"}">
    <div class="t">
      <span>${icCal}${h.date.slice(0, 5)}<span class="yr">${h.date.slice(5)}</span>${full ? ` <span class="hint">${h.wd}</span>` : ""}</span>
      <span>${icClock}${h.in} – ${h.out || "сейчас"}</span>
      ${full && (h.late || h.early || h.auto) ? `<div class="tag" style="width:100%">
        ${h.late ? `<span style="color:var(--orange)">опоздание ${h.late}</span> ` : ""}
        ${h.early ? `<span style="color:var(--orange)">ранний уход ${h.early}</span> ` : ""}
        ${h.auto ? `<span class="hint">уход закрыт автоматически</span>` : ""}</div>` : ""}
    </div>
    <div class="dur">${h.worked}</div></div>`;
}

function renderMe() {
  const p = data.profile;
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
  $("s-month").textContent = num(p.month_hours) + " ч";
  $("s-streak").textContent = p.streak + " " + plural(p.streak, "день", "дня", "дней");
  $("s-level").textContent = lvl.name;

  $("my-week").innerHTML = weekCells(p.week, p.today_wd);
  $("st-text").innerHTML = p.on_site
    ? `<span class="dot" style="background:var(--green)"></span>На месте с ${p.since}`
    : `<span class="dot" style="background:var(--hint)"></span>Не на месте`;
  const can = p.on_site ? p.can_out : p.can_in;
  $("st-sub").textContent = p.on_site && p.unfinished ? `⛔ Сначала закройте задачи (${p.unfinished})`
    : !p.has_schedule ? (p.on_site ? "Свободный график · отметьте уход, когда закончите" : "Свободный график · отмечайтесь в любое время")
    : !p.workday ? `Сегодня выходной`
    : p.on_site ? `Уход: ${p.out_window}`
    : p.came_today ? `Смена завершена · график ${p.schedule}`
    : `Приход: ${p.in_window}`;
  $("btn-mark").textContent = p.on_site ? "🚪 Ушёл" : "✅ Пришёл";
  $("btn-mark").className = "btn " + (p.on_site ? "btn-soft" : "btn-main");
  $("btn-mark").disabled = !can;
  $("pen-card").style.borderColor = p.penalties_total ? "rgba(248,113,113,.35)" : "";
  $("pen-card").innerHTML = `
    <div class="head"><h3>🔴 Штрафы</h3></div>
    <div class="pen-stats">
      <div><b style="color:${p.penalties_total ? "var(--red)" : "var(--green)"}">${p.penalties_total}</b><span>всего</span></div>
      <div><b style="color:${p.penalties_month ? "var(--red)" : "var(--green)"}">${p.penalties_month}</b><span>за этот месяц</span></div>
    </div>` + (p.penalties.length ? p.penalties.map(x => `
    <div class="pen"><div class="tt">${x.date} · ${esc(x.reason)}</div>
    ${x.amount ? `<b style="color:var(--red)">${esc(x.amount)}</b>` : ""}</div>`).join("")
    : `<div class="empty">${p.penalties_total ? "В этом месяце штрафов нет 👍" : "Штрафов нет — так держать! 👍"}</div>`);

  $("levels").innerHTML = L.map((l, k) => (k ? `<div class="arrow">›</div>` : "") + `
    <div class="lv ${k > idx ? "locked" : ""} ${k === idx ? "cur" : ""}">${l.icon()}<b>${l.name}</b>
    <span>${l.to === null ? num(l.from) + "+" : num(l.from) + " – " + num(l.to)}</span></div>`).join("");

  const open = data.tasks.filter(t => !t.done), done = data.tasks.filter(t => t.done).slice(0, 3);
  $("tasks").innerHTML = (open.length || done.length) ? [...open, ...done].map(t => `
    <div class="task ${t.done ? "done" : ""}"><div class="tt">${esc(t.title)}<div class="hint" style="font-size:12px">${t.created}</div></div>
    ${t.done ? "✔️" : t.due ? `<span class="hint" style="font-size:13px;white-space:nowrap">📅 на ${t.due}</span>`
      : t.status === "review" ? `<span class="hint" style="font-size:13px;white-space:nowrap">⏳ на проверке</span>`
      : `<button class="btn btn-main btn-sm" data-done="${t.id}">Готово</button>`}</div>`).join("")
    : `<div class="empty">Задач нет 🎉</div>`;
  document.querySelectorAll("[data-done]").forEach(b => b.onclick = () => action("/api/task_done", {id: +b.dataset.done}));

  $("visits").innerHTML = data.history.length ? data.history.slice(0, 4).map(h => visitRow(h, false)).join("")
    : `<div class="empty">Посещений пока нет</div>`;
  $("history-all").innerHTML = data.history.length ? data.history.map(h => visitRow(h, true)).join("")
    : `<div class="empty">Посещений пока нет</div>`;

  const next = L[idx + 1];
  const top = L[L.length - 1];
  $("g-to").textContent = `до звания «${top.name}»`;
  $("m-title").textContent = next ? "Продолжайте в том же духе!" : `Вы — ${top.name}! 🏆`;
  $("m-sub").textContent = next ? `До звания «${next.name}» осталось ${num(Math.max(0, next.from - p.total_hours))} ч 💪`
                                : `Цель ${num(p.goal)} часов достигнута`;
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

function renderTeam() {
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
                    paid: "✔️ выплачен", withdrawn: "↩️ выведен", rejected: "отклонён"};
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
    sub = `<span>подана ${r.created} · на ${r.months} мес.</span><span>доход ${sum(r.min_total)} – ${sum(r.max_total)}</span>`;
    act = admin ? `<div class="inv-act"><button class="btn btn-main btn-sm" data-inv-ok="${r.id}">✅ Деньги получил</button>
                   <button class="btn btn-soft btn-sm" data-inv-no="${r.id}">✕ Отклонить</button></div>`
                : `<div class="inv-act"><button class="btn btn-soft btn-sm" data-inv-cancel="${r.id}">Отменить заявку</button></div>`;
  } else if (["active", "rating", "matured"].includes(r.status)) {
    sub = `<span>${r.start} → ${r.end}</span><span style="color:var(--green)">+${sum(r.accrued)} (${pc(r.percent)})</span>`;
    if (r.status === "active") sub += `<span style="width:100%">ещё ${daysWord(r.days_left)} · в конце: ${sum(r.amount + r.min_total)} – ${sum(r.amount + r.max_total)}</span>`;
    else if (r.status === "rating") sub += `<span style="width:100%;color:var(--orange)">Срок закончился — ждём оценку последнего месяца</span>`;
    else sub += `<span style="width:100%;color:var(--green)">Срок закончился — к выплате <b>${sum(r.payout)}</b></span>`;
    if (admin && r.status === "matured")
      act = `<div class="inv-act"><button class="btn btn-main btn-sm" data-inv-paid="${r.id}">💸 Выплачено ${sum(r.payout)}</button></div>`;
    if (r.early) {
      sub += `<span style="width:100%;color:var(--orange)">↩️ Заявка на досрочный вывод: к выплате ${sum(r.amount)}, доход +${sum(r.accrued)} сгорает</span>`;
      if (r.early_info) sub += `<span style="width:100%;color:var(--text);line-height:1.5">👤 ${esc(r.early_info.name)}<br>📞 ${esc(r.early_info.phone)}<br>💳 ${esc(r.early_info.requisites)}</span>`;
      act = admin ? `<div class="inv-act"><button class="btn btn-main btn-sm" data-early-ok="${r.id}" data-amount="${r.amount}" data-name="${esc(r.name)}">✅ Выплатить ${sum(r.amount)}</button>
                     <button class="btn btn-soft btn-sm" data-early-no="${r.id}">✕ Отклонить</button></div>`
                  : `<div class="inv-act"><button class="btn btn-soft btn-sm" data-early-cancel="${r.id}">Передумал — оставить вклад</button></div>`;
    } else if (!admin && r.status === "active") {
      act = `<div class="inv-act"><button class="btn btn-soft btn-sm" data-early="${r.id}" data-amount="${r.amount}" data-accrued="${r.accrued}">↩️ Забрать досрочно</button></div>`;
    }
    if (r.months_list.length) months = `<details class="inv-months"><summary>Начисления по месяцам</summary><div class="mchips">${
      r.months_list.map(m => `<span>${esc(m.title.split(" ")[0].slice(0, 3).toLowerCase())}${m.share < 100 ? ` (${m.share}%)` : ""} ${
        m.level ? LVL()[m.level].emoji + " +" + sum(m.earned) : "⏳"}</span>`).join("")}</div></details>`;
  } else if (r.status === "withdrawn") {
    sub = `<span>${r.start} → ${r.end}</span><span>выведен досрочно · выплачено ${sum(r.payout)}${r.pay_method_title ? " · " + r.pay_method_title : ""}</span>`;
  } else if (r.status === "paid") {
    sub = `<span>${r.start} → ${r.end}</span><span>доход ${sum(r.accrued)} (${pc(r.percent)})</span>`;
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
    tg.showConfirm("Подтвердить, что деньги получены? С сегодняшнего дня начнутся начисления.",
      ok => ok && action("/api/invest_decide", {id: +b.dataset.invOk, approve: true})));
  document.querySelectorAll("[data-inv-no]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отклонить заявку?", ok => ok && action("/api/invest_decide", {id: +b.dataset.invNo, approve: false})));
  document.querySelectorAll("[data-inv-paid]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отметить вклад как выплаченный?", ok => ok && action("/api/invest_paid", {id: +b.dataset.invPaid})));
  document.querySelectorAll("[data-inv-cancel]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отменить заявку на вклад?", ok => ok && action("/api/invest_cancel", {id: +b.dataset.invCancel})));
  document.querySelectorAll("[data-early]").forEach(b => b.onclick = () => earlyForm(b.dataset));
  document.querySelectorAll("[data-early-cancel]").forEach(b => b.onclick = () =>
    action("/api/invest_early", {id: +b.dataset.earlyCancel, cancel: true}));
  document.querySelectorAll("[data-early-ok]").forEach(b => b.onclick = () => payForm(b.dataset));
  document.querySelectorAll("[data-early-no]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отклонить досрочный вывод? Вклад продолжит работать.",
      ok => ok && action("/api/invest_early_decide", {id: +b.dataset.earlyNo, approve: false})));
  document.querySelectorAll("[data-lvl]").forEach(b => b.onclick = () => {
    const [month, level] = b.dataset.lvl.split("|");
    if (b.classList.contains("on")) return;
    tg.showConfirm(`${b.dataset.title}: ${lvlTag(level)} месяц?`, ok => ok && action("/api/invest_rate", {month, level}));
  });
}

/* ---------- формы досрочного вывода ---------- */
function invSheet(html) { $("inv-sheet").innerHTML = html; $("inv-sheet-bg").classList.remove("hidden"); }
function invSheetClose() { $("inv-sheet-bg").classList.add("hidden"); $("inv-sheet").innerHTML = ""; }
$("inv-sheet-bg").onclick = ev => { if (ev.target.id === "inv-sheet-bg") invSheetClose(); };
const sheetBtns = ok => `<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:6px">
  <button class="btn btn-soft" id="is-cancel">Отмена</button><button class="btn btn-main" id="is-ok">${ok}</button></div>`;

function earlyForm(d) {
  invSheet(`<h3>↩️ Забрать вклад досрочно</h3>
    <div class="inv-calc">Вы получите <b>${sum(d.amount)}</b>.<br>
      <span style="color:var(--orange)">Начисленный доход +${sum(d.accrued)} сгорит.</span></div>
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
  invSheet(`<h3>💸 Досрочная выплата</h3>
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
  $("inv-calc").innerHTML = `Доход за ${cfg.term} мес.: <b>+${sum(lo)} – ${sum(hi)}</b><br>
    К выплате: <b>${sum(a + lo)} – ${sum(a + hi)}</b> · примерно ${end}` +
    (a < cfg.min ? `<br><span style="color:var(--orange)">Мин. сумма — ${sum(cfg.min)}</span>` : "") +
    (a > cfg.max ? `<br><span style="color:var(--orange)">Макс. сумма — ${sum(cfg.max)}</span>` : "");
}
function renderInvestMe() {
  const m = data.invest, cfg = data.invest_cfg;
  const live = m.items.filter(r => ["active", "rating", "matured"].includes(r.status));
  const hist = cfg.history.slice(0, 6);
  $("inv-me").innerHTML = (live.length ? invTiles([[m.invested, "вложено"], ["+" + sum(m.accrued), "начислено", "var(--green)"],
      [m.payout, "сейчас стоит"]]) : "")
    + (m.items.length ? m.items.slice(0, 10).map(r => invItem(r, false)).join("")
       : `<div class="empty">Вкладывайте в компанию на ${cfg.term} мес. и получайте ${pc(cfg.annual_min)}–${pc(cfg.annual_max)} 📈</div>`)
    + (hist.length ? `<div class="inv-h">Как прошли месяцы</div><div class="mchips">${hist.map(h =>
        `<span>${esc(h.title.toLowerCase())} ${lvlTag(h.level)}</span>`).join("")}</div>` : "")
    + (m.earned ? `<div class="hint" style="font-size:13px;margin-top:8px">Всего заработано на вкладах: <b style="color:var(--green)">${sum(m.earned)}</b></div>` : "");
  $("inv-rules").innerHTML = `Вклад на ${cfg.term} мес. Каждый месяц руководитель подводит итог — от него зависит ваша доля:<br>` +
    cfg.levels.map(l => `${l.emoji} ${l.title.toLowerCase()} — ${pc(l.monthly)} за месяц (${pc(l.annual)} годовых)`).join("<br>") +
    `<br>Деньги передаются руководителю лично, начисления идут со дня подтверждения. После окончания срока вы получаете вклад со всем доходом. Досрочно — по заявке, только вложенную сумму (доход сгорает).`;
  const pend = m.items.some(r => r.status === "pending"), ready = m.items.some(r => r.status === "matured");
  $("inv-folder-sub").innerHTML = live.length
    ? `Вложено ${sum(m.invested)} · <span style="color:var(--green)">+${sum(m.accrued)}</span>${ready ? " · 🎉 к выплате" : ""}`
    : pend ? "⏳ Заявка ждёт руководителя" : `Доход ${pc(cfg.annual_min)}–${pc(cfg.annual_max)} за ${cfg.term} мес.`;
  $("inv-page-sub").textContent = `Вклад на ${cfg.term} мес. · доход ${pc(cfg.annual_min)}–${pc(cfg.annual_max)} — зависит от итогов каждого месяца`;
  renderInvCalc();
  bindInvest();
}
$("inv-amount").oninput = () => {
  const a = invAmount();
  $("inv-amount").value = a ? a.toLocaleString("ru-RU") : "";
  renderInvCalc();
};
$("inv-send").onclick = () => {
  const a = invAmount(), cfg = data.invest_cfg;
  if (!a) return toast("Введите сумму");
  tg.showConfirm(`Открыть вклад ${sum(a)} на ${cfg.term} мес.?`, ok => ok &&
    action("/api/invest_request", {amount: a}).then(() => {
      if (data.invest.items.some(r => r.status === "pending")) { $("inv-amount").value = ""; $("inv-form").open = false; }
    }));
};
function lvlButtons(month, title, current) {
  return `<div class="lvl-btns">${data.invest_cfg.levels.map(l => `<button class="${l.key === current ? "on" : ""}"
    data-lvl="${month}|${l.key}" data-title="${esc(title)}">${l.emoji} ${esc(l.title)}<span>${pc(l.monthly)}</span></button>`).join("")}</div>`;
}
function renderInvestAdmin() {
  const o = data.invest_admin;
  const block = (title, rows) => rows.length ? `<div class="inv-h">${title}</div>` + rows.map(r => invItem(r, true)).join("") : "";
  const any = o.pending.length + o.matured.length + o.active.length + o.paid.length;
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
    + invTiles([[o.invested, "вложено"], ["+" + sum(o.accrued), "начислено", "var(--green)"],
      [`${sum(o.obligations_min)} –<br>${sum(o.obligations_max)}`, "вернуть всего", "var(--orange)"]])
    + block(`⏳ Ждут подтверждения (${o.pending.length})`, o.pending)
    + block(`↩️ Досрочный вывод (${o.early.length})`, o.early)
    + block(`🎉 Пора выплатить — ${sum(o.to_pay_now)}`, o.matured)
    + block(`📈 Работают (${o.active.filter(r => !r.early).length})`, o.active.filter(r => !r.early))
    + block("✔️ Выплачены", o.paid)
    + (any ? "" : `<div class="empty">Вкладов пока нет</div>`);
  const todo = o.pending.length + o.matured.length + o.to_rate.length + o.early.length;
  $("inv-admin-badge").innerHTML = todo ? `<span class="f-badge">${todo}</span>` : "";
  $("inv-admin-folder-sub").textContent = [
    o.to_rate.length ? "⚠️ оцените месяц" : "",
    o.pending.length ? `заявок: ${o.pending.length}` : "",
    o.early.length ? `досрочно: ${o.early.length}` : "",
    o.matured.length ? `к выплате: ${o.matured.length}` : "",
    `вложено ${sum(o.invested)}`].filter(Boolean).join(" · ");
  bindInvest();
}

async function load() {
  try {
    data = await api("/api/me");
    $("loading").classList.add("hidden");
    if (data.is_client) { renderClient(); show("client"); return; }
    if (data.is_employee) { renderMe(); renderInvestMe(); }
    if (data.is_admin) { renderTeam(); renderNewsAdmin(); renderClientsAdmin(); renderInvestAdmin(); }
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
      <span>🗓 ${n.date}</span>${admin ? `<button class="link" data-delnews="${n.id}">удалить</button>` : ""}</div>${esc(n.text)}</div>`).join("")
    : `<div class="empty">Новостей пока нет</div>`;
}
const TIER_EMOJI = {bronza: "🥉", silver: "🥈", gold: "🥇"};
const CL_T = {
  uz: {purchases: "xaridlar summasi", bonuses: "bonuslar", soon: "tez orada", invest: "Investitsiyalar",
       invest_soon: "Bo'lim tez orada paydo bo'ladi", soon_pill: "Tez orada ⏳", news: "📰 Yangiliklar",
       no_news: "Hozircha yangiliklar yo'q", no_phone: "raqam ko'rsatilmagan", client: "Mijoz",
       finish_reg: "📱 Ro'yxatdan o'tishni bot bilan chatda yakunlang — shunda xaridlaringiz va bonuslaringizni topamiz.",
       birth: "🎂 Tug'ilgan kun", photo: "Rasm yuklanmoqda…"},
  ru: {purchases: "сумма покупок", bonuses: "бонусы", soon: "скоро", invest: "Инвестиции",
       invest_soon: "Раздел скоро появится", soon_pill: "Скоро ⏳", news: "📰 Новости",
       no_news: "Новостей пока нет", no_phone: "номер не указан", client: "Клиент",
       finish_reg: "📱 Завершите регистрацию в чате с ботом — так мы найдём ваши покупки и бонусы.",
       birth: "🎂 День рождения", photo: "Загружаю фото…"},
  en: {purchases: "total purchases", bonuses: "bonuses", soon: "soon", invest: "Investments",
       invest_soon: "This section is coming soon", soon_pill: "Soon ⏳", news: "📰 News",
       no_news: "No news yet", no_phone: "no phone number", client: "Client",
       finish_reg: "📱 Please finish registration in the bot chat so we can find your purchases and bonuses.",
       birth: "🎂 Birthday", photo: "Uploading photo…"},
};
const ct = k => (CL_T[(data && data.client && data.client.lang) || "uz"] || CL_T.uz)[k];
function renderClient() {
  const c = data.client;
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
  $("n-list").innerHTML = newsHtml(data.news, true);
  document.querySelectorAll("[data-delnews]").forEach(b => b.onclick = () =>
    tg.showConfirm("Удалить новость?", ok => ok && action("/api/delete_news", {id: +b.dataset.delnews})));
}
$("n-add").onclick = () => {
  const text = $("n-text").value.trim();
  if (!text) return toast("Напишите текст новости");
  action("/api/add_news", {text}).then(() => $("n-text").value = "");
};

document.querySelectorAll("#seg button").forEach(b => b.onclick = () => show(b.dataset.view));
$("all-btn").onclick = () => show("history");
$("go-inv").onclick = () => show("invest");
$("go-inv-admin").onclick = () => show("invest-admin");
$("ava-file").onchange = async (ev) => {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  try {
    toast("Загружаю фото…");
    await action("/api/avatar", {image: await squareJpeg(file)});
  } catch (e) { toast(e.message); }
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

if (!tg.initData) $("loading").textContent = "Откройте кабинет через кнопку в Telegram-боте.";
else { load(); setInterval(load, 30000); }
</script>
</body>
</html>
"""
