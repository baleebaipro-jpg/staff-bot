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


def need_user(admin=False):
    def deco(fn):
        @wraps(fn)
        def wrapper():
            uid = telegram_user_id()
            if uid is None:
                return jsonify(error="Откройте кабинет через Telegram-бота."), 401
            if admin and not core.is_admin(uid):
                return jsonify(error="Только для руководителя."), 403
            if not admin and not (core.employee(uid) or core.is_admin(uid)):
                return jsonify(error=f"Нет доступа. Ваш ID: {uid} — передайте его руководителю."), 403
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
    return jsonify(ok=True, msg="Фото обновлено 📸")


@app.post("/api/me")
@need_user()
def api_me(uid, data):
    res = {"is_admin": core.is_admin(uid), "is_employee": bool(core.employee(uid)), "goal": config.GOAL_HOURS,
           "today_wd": core.now().weekday()}
    if res["is_employee"]:
        res["profile"] = core.profile(uid)
        res["profile"]["ava_v"] = ava_version(uid)
        res["tasks"] = core.tasks(uid)
        res["history"] = core.history(uid)
    if res["is_admin"]:
        res["shifts"] = {k: v["title"] for k, v in core.SHIFTS.items()}
        res["shifts"][core.OFF] = "Выходной"
        res["team"] = core.team()
        for e in res["team"]:
            e["ava_v"] = ava_version(e["id"])
    return jsonify(res)


@app.post("/api/checkin")
@need_user()
def api_checkin(uid, data):
    ok, msg = core.check_in(uid)
    return jsonify(ok=ok, msg=msg)


@app.post("/api/checkout")
@need_user()
def api_checkout(uid, data):
    ok, msg = core.check_out(uid)
    return jsonify(ok=ok, msg=msg)


@app.post("/api/task_done")
@need_user()
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


@app.post("/api/delete_task")
@need_user(admin=True)
def api_delete_task(uid, data):
    core.delete_task(int(data.get("id", 0)))
    return jsonify(ok=True, msg="Задача удалена.")




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
    <div class="hint">до профессионала</div>
    <div class="bar-row"><div class="bar"><div id="g-bar" style="width:0"></div></div><div class="pct" id="g-pct"></div></div>

    <div class="stats">
      <div class="stat s1"><div class="ic">📅</div><b id="s-month"></b><span>за месяц</span></div>
      <div class="stat s2"><div class="ic">🔥</div><b id="s-streak"></b><span>подряд</span></div>
      <div class="stat s3"><div class="ic">📊</div><b id="s-level"></b><span>текущий уровень</span></div>
    </div>

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
      <h3>Уровни и бейджи</h3>
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

  <!-- ВСЯ ИСТОРИЯ -->
  <div id="view-history" class="hidden">
    <div class="card"><h3>Все посещения</h3><div id="history-all"></div></div>
  </div>

  <!-- КОМАНДА -->
  <div id="view-team" class="hidden">
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

/* ---------- бейджи ---------- */
function hexBadge(id, c1, c2, c3) {
  return `<svg viewBox="0 0 64 64"><defs>
    <linearGradient id="g${id}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${c1}"/><stop offset="1" stop-color="${c2}"/></linearGradient></defs>
    <polygon points="32,3 57,17.5 57,46.5 32,61 7,46.5 7,17.5" fill="url(#g${id})" stroke="${c3}" stroke-width="2"/>
    <polygon points="32,10 51,21 51,43 32,54 13,43 13,21" fill="none" stroke="rgba(255,255,255,.35)" stroke-width="1.5"/>
    <polygon points="32,18 35.8,27.6 46,28.2 38.1,34.7 40.7,44.6 32,39 23.3,44.6 25.9,34.7 18,28.2 28.2,27.6" fill="${c3}" stroke="rgba(255,255,255,.6)" stroke-width="1"/></svg>`;
}
function diamond() {
  return `<svg viewBox="0 0 64 64"><defs>
    <linearGradient id="gd" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#9fe2ff"/><stop offset="1" stop-color="#2d6bff"/></linearGradient></defs>
    <polygon points="14,10 50,10 60,24 32,58 4,24" fill="url(#gd)" stroke="#cdeeff" stroke-width="1.5"/>
    <polyline points="4,24 60,24" stroke="#e6f7ff" stroke-width="1.2" fill="none"/>
    <polyline points="14,10 22,24 32,58 42,24 50,10" stroke="#e6f7ff" stroke-width="1.2" fill="none" opacity=".8"/>
    <polyline points="22,24 32,10 42,24" stroke="#e6f7ff" stroke-width="1.2" fill="none" opacity=".8"/></svg>`;
}
function levels(goal) {
  const q = goal / 4;
  return [
    {name: "Новичок",      from: 0,     to: q - 1,     icon: () => hexBadge("b", "#e0955c", "#8a4a22", "#f4b183")},
    {name: "Опытный",      from: q,     to: 2*q - 1,   icon: () => hexBadge("s", "#e4e8f0", "#7d8597", "#f6f8fc")},
    {name: "Мастер",       from: 2*q,   to: 3*q - 1,   icon: () => hexBadge("g", "#ffd35c", "#c9851a", "#fff1b8")},
    {name: "Эксперт",      from: 3*q,   to: goal - 1,  icon: () => hexBadge("e", "#6cc4ff", "#1f5fb8", "#c9ecff")},
    {name: "Профессионал", from: goal,  to: null,      icon: diamond},
  ];
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
    if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(r.ok ? "success" : "warning");
    await load();
  } catch (e) { toast(e.message); }
}

/* ---------- экраны ---------- */
function show(v) {
  view = v;
  ["me", "history", "team"].forEach(n => $("view-" + n).classList.toggle("hidden", n !== v));
  document.querySelectorAll("#seg button").forEach(b => b.classList.toggle("on", b.dataset.view === v));
  if (v === "history") tg.BackButton.show(); else tg.BackButton.hide();
  window.scrollTo(0, 0);
}
tg.BackButton.onClick(() => show("me"));

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
  $("st-sub").textContent = !p.has_schedule ? (p.on_site ? "Свободный график · отметьте уход, когда закончите" : "Свободный график · отмечайтесь в любое время")
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
    ${t.done ? "✔️" : `<button class="btn btn-main btn-sm" data-done="${t.id}">Готово</button>`}</div>`).join("")
    : `<div class="empty">Задач нет 🎉</div>`;
  document.querySelectorAll("[data-done]").forEach(b => b.onclick = () => action("/api/task_done", {id: +b.dataset.done}));

  $("visits").innerHTML = data.history.length ? data.history.slice(0, 4).map(h => visitRow(h, false)).join("")
    : `<div class="empty">Посещений пока нет</div>`;
  $("history-all").innerHTML = data.history.length ? data.history.map(h => visitRow(h, true)).join("")
    : `<div class="empty">Посещений пока нет</div>`;

  const next = L[idx + 1];
  $("m-title").textContent = next ? "Продолжайте в том же духе!" : "Вы — профессионал! 🏆";
  $("m-sub").textContent = next ? `До уровня «${next.name}» осталось ${num(Math.max(0, next.from - p.total_hours))} ч 💪`
                                : "Цель 10 000 часов достигнута";
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
      <div class="hint" style="font-size:13px;margin-top:6px">${num(e.total_hours)} / ${num(data.goal)} ч · ${lvl.name}</div>
      <div style="font-size:13px;margin-top:4px;color:${e.penalties_total ? "var(--red)" : "var(--hint)"}">🔴 Штрафов: всего ${e.penalties_total} · за месяц ${e.penalties_month}</div>
      ${e.penalties.map(x => `<div class="pen"><div class="tt"><span style="color:var(--red)">🔴 ${x.date}</span> · ${esc(x.reason)}${x.amount ? ` · <b>${esc(x.amount)}</b>` : ""}</div>
        <button class="btn btn-soft btn-sm" data-delpen="${x.id}">Отменить</button></div>`).join("")}
      ${e.tasks.map(t => `<div class="task"><div class="tt">📌 ${esc(t.title)}</div>
        <button class="btn btn-soft btn-sm" data-del="${t.id}">✕</button></div>`).join("")}
    </div>`; }).join("");
  document.querySelectorAll("[data-week]").forEach(b => b.onclick = () => openWeek(+b.dataset.week));
  document.querySelectorAll("[data-delpen]").forEach(b => b.onclick = () =>
    tg.showConfirm("Отменить штраф?", ok => ok && action("/api/delete_penalty", {id: +b.dataset.delpen})));
  document.querySelectorAll("[data-del]").forEach(b => b.onclick = () =>
    tg.showConfirm("Удалить задачу?", ok => ok && action("/api/delete_task", {id: +b.dataset.del})));
}

async function load() {
  try {
    data = await api("/api/me");
    $("loading").classList.add("hidden");
    if (data.is_employee) renderMe();
    if (data.is_admin) renderTeam();
    $("seg").classList.toggle("hidden", !(data.is_admin && data.is_employee));
    if (!data.is_employee) view = "team";
    show(view);
  } catch (e) {
    $("loading").classList.add("hidden");
    $("error").classList.remove("hidden");
    $("error").textContent = e.message;
  }
}

document.querySelectorAll("#seg button").forEach(b => b.onclick = () => show(b.dataset.view));
$("all-btn").onclick = () => show("history");
$("ava-file").onchange = async (ev) => {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  try {
    toast("Загружаю фото…");
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
