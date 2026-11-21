import asyncio
import logging
import os

import json

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import config
import core
import erp
import i18n
import invest as invest_mod
import relay

AVATAR_DIR = getattr(config, "AVATAR_DIR", "/home/abdullohbe/avatars")
PENALTY_THREAD_ID = getattr(config, "PENALTY_THREAD_ID", None)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

BTN_IN = "✅ Пришёл"
BTN_OUT = "🚪 Ушёл"
BTN_TASKS = "📋 Мои задачи"
BTN_TEAM = "👥 Команда"
BTN_CABINET = "🏠 Личный кабинет"
BTN_INVEST = "💰 Инвестиции"


def keyboard(uid: int) -> ReplyKeyboardMarkup:
    rows = []
    if core.employee(uid):
        rows.append([BTN_IN, BTN_OUT])
        rows.append([BTN_TASKS, BTN_INVEST])
    if core.is_admin(uid):
        rows.append([BTN_TEAM] + ([] if core.employee(uid) else [BTN_INVEST]))
    rows.append([KeyboardButton(BTN_CABINET, web_app=WebAppInfo(url=config.WEBAPP_URL))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def reply(update: Update, text: str, **kwargs) -> None:
    """Ответить; при сбое сети повторить до 5 раз."""
    for attempt in range(5):
        try:
            await update.effective_message.reply_text(text, **kwargs)
            return
        except BadRequest as e:
            # ошибка в самом сообщении — повтор не поможет
            logging.error("Telegram отклонил сообщение: %s", e)
            return
        except NetworkError as e:
            logging.warning("Сбой сети (%s), пробую ещё раз...", e)
            await asyncio.sleep(1 + attempt)
    logging.error("Не удалось отправить ответ")


async def save_avatar(bot, uid: int) -> None:
    """Скачать аватарку пользователя из Telegram для личного кабинета."""
    if os.path.exists(os.path.join(AVATAR_DIR, f"{uid}.custom")):
        return  # сотрудник сам загрузил фото в кабинете — не трогаем
    try:
        photos = await bot.get_user_profile_photos(uid, limit=1)
        if not photos.total_count:
            return
        sizes = photos.photos[0]
        file = await bot.get_file(sizes[min(1, len(sizes) - 1)].file_id)
        os.makedirs(AVATAR_DIR, exist_ok=True)
        await file.download_to_drive(os.path.join(AVATAR_DIR, f"{uid}.jpg"))
    except TelegramError as e:
        logging.warning("Аватар %s не скачан: %s", uid, e)


ALLOWED_USERS = set(getattr(config, "BOT_ALLOWED_USERS", []) or [])


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """В личке бот отвечает только тем, кто в BOT_ALLOWED_USERS. Остальным — тишина."""
    chat = update.effective_chat
    if not ALLOWED_USERS or not chat or chat.type != "private":
        return
    user = update.effective_user
    if not user or user.id not in ALLOWED_USERS:
        raise ApplicationHandlerStop


def known(uid: int) -> bool:
    return bool(core.employee(uid)) or core.is_admin(uid)


async def deny(update: Update) -> None:
    """Клиент нажал что-то для сотрудников — показываем меню клиента."""
    await client_menu(update)


# ---------- панель клиента (3 языка) ----------

def client_keyboard(uid: int):
    cl = core.get_client(uid) or {}
    lang = cl.get("lang")
    step = cl.get("step")
    if not cl.get("phone"):
        return ReplyKeyboardMarkup([[KeyboardButton(i18n.t(lang, "btn_phone"), request_contact=True)]],
                                   resize_keyboard=True, one_time_keyboard=True)
    if step in ("name", "birth"):
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup([
        [i18n.t(lang, "btn_purchases"), i18n.t(lang, "btn_bonus")],
        [i18n.t(lang, "btn_news"), i18n.t(lang, "btn_invest")],
        [KeyboardButton(i18n.t(lang, "btn_cabinet"), web_app=WebAppInfo(url=config.WEBAPP_URL)),
         i18n.t(lang, "btn_lang")],
    ], resize_keyboard=True)


def lang_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(title, callback_data=f"lang:{code}")]
                                 for code, title in i18n.LANGS.items()])


async def client_menu(update: Update, greet: bool = False) -> None:
    """Главная точка для клиента: ведёт по шагам регистрации или показывает меню."""
    u = update.effective_user
    core.save_client(u.id, first_name=u.first_name or "", username=u.username or "")
    cl = core.get_client(u.id)
    lang = cl.get("lang")
    if not lang:  # шаг 1 — язык
        return await reply(update, i18n.t(None, "choose_lang"), reply_markup=lang_buttons())
    if not cl.get("phone"):  # шаг 2 — номер
        return await reply(update, i18n.t(lang, "greet", name=u.first_name) + "\n\n" + i18n.t(lang, "ask_phone"),
                           reply_markup=client_keyboard(u.id))
    if not cl.get("step"):  # номер есть, но анкета не заполнена (старые клиенты)
        return await after_phone(update)
    if cl.get("step") == "name":  # шаг 3 — имя и фамилия
        return await reply(update, i18n.t(lang, "ask_name"), reply_markup=ReplyKeyboardRemove())
    if cl.get("step") == "birth":  # шаг 4 — дата рождения
        return await reply(update, i18n.t(lang, "ask_birth"), reply_markup=ReplyKeyboardRemove())
    name = cl.get("first_name") or u.first_name
    text = (i18n.t(lang, "greet", name=name) + "\n" if greet else "")
    text += i18n.t(lang, "status_line", tier=core.tier_title(core.tier_of(cl))) + "\n\n" + i18n.t(lang, "menu")
    await reply(update, text, reply_markup=client_keyboard(u.id))


async def choose_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    code = i18n.lang_of(q.data.split(":", 1)[1])
    core.update_client(q.from_user.id, lang=code)
    await q.answer(i18n.t(code, "lang_set"))
    try:
        await q.edit_message_text(i18n.t(code, "lang_set"))
    except TelegramError:
        pass
    await client_menu(update, greet=True)


async def got_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    contact = update.message.contact
    cl = core.get_client(u.id) or {}
    lang = cl.get("lang")
    if contact.user_id != u.id:
        return await reply(update, i18n.t(lang, "wrong_contact"), reply_markup=client_keyboard(u.id))
    core.save_client(u.id, phone=contact.phone_number, first_name=u.first_name or "", username=u.username or "")
    if known(u.id):
        return await reply(update, "Номер сохранён ✅", reply_markup=keyboard(u.id))
    if cl.get("step") == "done":
        return await client_menu(update)
    await after_phone(update)


async def after_phone(update: Update) -> None:
    """Номер есть — ищем клиента в ERP; не нашли — спрашиваем имя и дату рождения."""
    u = update.effective_user
    cl = core.get_client(u.id)
    lang = cl.get("lang")
    found = None
    try:
        found = erp.find_client(cl["phone"])
    except Exception:
        logging.exception("Ошибка поиска клиента в ERP")
    if found:
        core.update_client(u.id, first_name=found.get("first_name"), last_name=found.get("last_name"),
                           birth_date=found.get("birth_date"), step="done")
        await reply(update, i18n.t(lang, "found"))
        return await client_menu(update, greet=True)
    core.update_client(u.id, step="name")
    await reply(update, i18n.t(lang, "ask_name"), reply_markup=ReplyKeyboardRemove())


async def client_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Любой текст от клиента: шаги регистрации или кнопки меню на любом языке."""
    u = update.effective_user
    text = (update.message.text or "").strip()
    cl = core.get_client(u.id) or {}
    lang = cl.get("lang")
    step = cl.get("step")
    if lang and cl.get("phone") and step == "name":
        parts = text.split()
        if len(parts) < 2 or len(text) > 80 or any(ch.isdigit() for ch in text):
            return await reply(update, i18n.t(lang, "bad_name"))
        core.update_client(u.id, first_name=parts[0].capitalize(),
                           last_name=" ".join(p.capitalize() for p in parts[1:]), step="birth")
        return await reply(update, i18n.t(lang, "ask_birth"))
    if lang and cl.get("phone") and step == "birth":
        birth = core.parse_birth(text)
        if not birth:
            return await reply(update, i18n.t(lang, "bad_birth"))
        core.update_client(u.id, birth_date=birth, step="done")
        await reply(update, i18n.t(lang, "registered"))
        return await client_menu(update)
    if text in i18n.all_variants("btn_lang"):
        return await reply(update, i18n.t(None, "choose_lang"), reply_markup=lang_buttons())
    if not lang or not cl.get("phone"):
        return await client_menu(update)
    if text in i18n.all_variants("btn_purchases"):
        return await reply(update, i18n.t(lang, "soon_purchases"), reply_markup=client_keyboard(u.id))
    if text in i18n.all_variants("btn_bonus"):
        return await reply(update, i18n.t(lang, "soon_bonus"), reply_markup=client_keyboard(u.id))
    if text in i18n.all_variants("btn_invest"):
        return await reply(update, i18n.t(lang, "soon_invest"), reply_markup=client_keyboard(u.id))
    if text in i18n.all_variants("btn_news"):
        items = core.news_list(5)
        if not items:
            return await reply(update, i18n.t(lang, "no_news"))
        return await reply(update, i18n.t(lang, "news_title") + "\n\n" +
                           "\n\n— — —\n\n".join(f"🗓 {n['date']}\n{n['text']}" for n in items))
    await client_menu(update)


async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/news текст — руководитель добавляет новость для клиентов."""
    if not core.is_admin(update.effective_user.id):
        return await client_menu(update)
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        return await reply(update, "Формат: /news текст новости\nНовость увидят клиенты в «📰 Новости» и в кабинете.")
    core.add_news(text)
    await reply(update, "📰 Новость опубликована для клиентов ✅")


# ---------- команды ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not known(uid):
        return await client_menu(update, greet=True)
    await save_avatar(context.bot, uid)
    text = f"Здравствуйте, {core.name(uid) if core.employee(uid) else update.effective_user.first_name}! 👋\n\n"
    if core.employee(uid):
        text += (
            f"{BTN_IN} / {BTN_OUT} — отметить приход и уход\n"
            f"{BTN_TASKS} — ваши задачи\n"
        )
    if core.is_admin(uid):
        text += (
            f"{BTN_TEAM} — кто на месте\n"
            "/task ID текст — поставить задачу\n"
            "/fine ID сумма причина — выписать штраф\n"
            "/news текст — новость для клиентов\n"
        )
    text += f"{BTN_CABINET} — часы, история, задачи"
    await reply(update, text, reply_markup=keyboard(uid))


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, f"Ваш ID: {update.effective_user.id}")


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Напишите /chatid в группе — бот покажет ID группы для config.py"""
    text = f"ID этого чата: {update.effective_chat.id}"
    if update.message and update.message.is_topic_message:
        text += f"\nID темы: {update.message.message_thread_id}"
    await reply(update, text)


async def came(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not core.employee(uid):
        return await deny(update)
    if core.PROOF_REQUIRED:
        ok, msg = core.start_proof(uid)  # ждём кружок
        await reply(update, msg, parse_mode="HTML" if ok else None)
        return
    _, msg = core.check_in(uid)
    await reply(update, msg)
    await save_avatar(context.bot, uid)


PROOF_CHAT_ID = getattr(config, "PROOF_CHAT_ID", None)
PROOF_THREAD_ID = getattr(config, "PROOF_THREAD_ID", None)


async def got_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кружок от сотрудника — подтверждение прихода."""
    uid = update.effective_user.id
    if not core.employee(uid):
        return
    m = update.message
    ok, msg, code = core.take_proof(
        uid,
        file_unique_id=m.video_note.file_unique_id,
        duration=m.video_note.duration or 0,
        forwarded=bool(m.forward_origin),
    )
    await reply(update, msg)
    if not ok:
        return
    await save_avatar(context.bot, uid)
    note = f"📹 {core.name(uid)}: {msg.splitlines()[0].replace('✅ ', '')}\n🔢 Код: {code} — проверьте, что назвал(а) его"
    if len(msg.splitlines()) > 1 and "Опоздание" in msg:
        note += "\n" + msg.splitlines()[1]
    targets = [(PROOF_CHAT_ID, PROOF_THREAD_ID)] if PROOF_CHAT_ID else [(a, None) for a in config.ADMINS]
    for chat_id, thread in targets:
        try:
            copied = await context.bot.copy_message(chat_id, uid, update.message.message_id, message_thread_id=thread)
            await context.bot.send_message(chat_id, note, message_thread_id=thread,
                                           reply_to_message_id=copied.message_id)
        except TelegramError as e:
            logging.warning("Кружок не переслан в %s: %s", chat_id, e)


async def other_media_while_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if core.proof_pending(uid):
        await reply(update, "Нужен именно кружок 📹 (круглое видео), не фото и не обычное видео.")
    else:
        await other(update, context)


async def left(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not core.employee(uid):
        return await deny(update)
    _, msg = core.check_out(uid)
    await reply(update, msg)


async def my_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not core.employee(uid):
        return await deny(update)
    open_tasks = [t for t in core.tasks(uid) if not t["done"]]
    if not open_tasks:
        return await reply(update, "У вас нет открытых задач 🎉")
    lines, buttons = [], []
    for i, t in enumerate(open_tasks, 1):
        if t["due"]:
            lines.append(f"{i}. {t['title']} — 📅 на {t['due']}")
        elif t["status"] == "review":
            lines.append(f"{i}. {t['title']} — ⏳ на проверке")
        else:
            lines.append(f"{i}. {t['title']}")
            buttons.append([InlineKeyboardButton(f"✅ Готово: {t['title'][:40]}", callback_data=f"done:{t['id']}")])
    await reply(update, "📋 Ваши задачи:\n\n" + "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def task_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки под сообщениями: «Готово» (сотрудник), «Принять/Вернуть» (руководитель)."""
    q = update.callback_query
    uid = q.from_user.id
    parts = q.data.split(":")
    if parts[0] == "done":
        ok, msg = core.complete_task(int(parts[1]), uid)
    elif parts[0] == "task" and core.is_admin(uid):
        ok, msg = core.review_task(int(parts[2]), parts[1])
    else:
        ok, msg = False, "Нет доступа."
    await q.answer(msg[:190], show_alert=not ok)
    try:
        await q.edit_message_text(q.message.text + "\n\n" + msg)
    except TelegramError:
        pass


async def team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not core.is_admin(uid):
        return await deny(update)
    lines = []
    for e in core.team():
        line = f"{e['icon']} {e['name']} ({e['schedule']}) — {e['status']}"
        if e["late"]:
            line += f", опоздание {e['late']}"
        line += f"\n    ID {e['id']} · задач: {len(e['tasks'])} · {e['total_hours']} ч"
        lines.append(line)
    await reply(update, "👥 Команда сегодня:\n\n" + "\n\n".join(lines))


async def task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not core.is_admin(uid):
        return await deny(update)
    if len(context.args) < 2 or not context.args[0].isdigit():
        return await reply(update, "Формат: /task ID текст задачи\nНапример: /task 222222222 Позвонить клиенту")
    target = int(context.args[0])
    if not core.employee(target):
        return await reply(update, "Сотрудник с таким ID не найден в config.py")
    core.add_task(target, " ".join(context.args[1:]))
    await reply(update, f"📌 Задача поставлена: {core.name(target)}")


async def fine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fine ID сумма причина   (сумму можно не писать)"""
    uid = update.effective_user.id
    if not core.is_admin(uid):
        return await deny(update)
    args = context.args
    if len(args) < 2 or not args[0].isdigit() or not core.employee(int(args[0])):
        people = "\n".join(f"{e['name']} — {i}" for i, e in config.EMPLOYEES.items())
        return await reply(
            update,
            "Формат:\n/fine ID сумма причина\n\n"
            "Например:\n/fine 9211646422 50000 Опоздал на планёрку\n"
            "/fine 9211646422 Не сдал отчёт   (без суммы)\n\n"
            f"Сотрудники:\n{people}",
        )
    target = int(args[0])
    rest = args[1:]
    amount = ""
    if rest[0].isdigit() and len(rest) > 1:
        amount = f"{int(rest[0]):,}".replace(",", " ")
        rest = rest[1:]
    core.add_penalty(target, " ".join(rest), amount)
    await reply(update, f"🔴 Штраф выписан: {core.name(target)}")


async def bot_added(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Когда бота добавляют в группу или канал — сообщить руководителям ID чата."""
    m = update.my_chat_member
    if not m or m.new_chat_member.status not in ("administrator", "member"):
        return
    if m.new_chat_member.status == m.old_chat_member.status:
        return
    chat = m.chat
    kind = {"channel": "канал", "group": "группа", "supergroup": "группа"}.get(chat.type, chat.type)
    role = "администратор ✅" if m.new_chat_member.status == "administrator" else "участник (сделайте админом!)"
    text = f"🔔 Меня добавили: {chat.title}\nТип: {kind}\nРоль: {role}\nID: {chat.id}"
    for a in config.ADMINS:
        try:
            await context.bot.send_message(a, text)
        except TelegramError as e:
            logging.warning("Не удалось сообщить %s: %s", a, e)


async def invest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not known(uid):
        return await client_text(update, context)
    lines = []
    if core.employee(uid):
        m = invest_mod.mine(uid)
        lines.append("💰 Мои инвестиции")
        if m["invested"]:
            lines.append(f"Вложено: {invest_mod.money(m['invested'])}\n"
                         f"Начислено: {invest_mod.money(m['accrued'])}\n"
                         f"К выплате в конце сроков: {invest_mod.money(m['payout'])}")
        for r in m["items"]:
            if r["status"] == "pending":
                lines.append(f"⏳ Заявка {invest_mod.money(r['amount'])} — ждёт руководителя")
            elif r["status"] in ("active", "matured"):
                tail = "готов к выплате 🎉" if r["status"] == "matured" else f"до {r['end']} (ещё {r['days_left']} дн.)"
                lines.append(f"📈 {invest_mod.money(r['amount'])} под {invest_mod.pct(r['rate'])}/мес · {tail}")
        if len(lines) == 1:
            lines.append("Вкладов пока нет.")
        plans = ", ".join(f"{p['months']} мес. — {invest_mod.pct(p['rate'])}/мес" for p in invest_mod.plans())
        lines.append(f"\nТарифы: {plans}\nОформить вклад — в «{BTN_CABINET}».")
    if core.is_admin(uid):
        o = invest_mod.overview()
        lines.append(f"{chr(10) if lines else ''}📊 Инвестиции команды\n"
                     f"Вложено: {invest_mod.money(o['invested'])}\n"
                     f"Начислено: {invest_mod.money(o['accrued'])}\n"
                     f"Обязательства (тело + доход): {invest_mod.money(o['obligations'])}\n"
                     f"Заявок ждут: {len(o['pending'])} · к выплате сейчас: {len(o['matured'])}")
    await reply(update, "\n".join(lines), reply_markup=keyboard(uid))


async def invest_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки «Деньги получил / Отклонить» под заявкой на вклад."""
    q = update.callback_query
    _, act, inv_id = q.data.split(":")
    if not core.is_admin(q.from_user.id):
        return await q.answer("Нет доступа.", show_alert=True)
    ok, msg = invest_mod.decide(int(inv_id), act == "ok")
    await q.answer(msg[:190], show_alert=not ok)
    try:
        await q.edit_message_text(q.message.text + "\n\n" + msg)
    except TelegramError:
        pass


async def other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not known(uid):
        return await client_text(update, context)
    await reply(update, "Пользуйтесь кнопками внизу 👇", reply_markup=keyboard(uid))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.warning("Ошибка: %s", context.error)


# ---------- отправка уведомлений из очереди ----------

async def outbox_loop(app: Application) -> None:
    while True:
        try:
            core.check_penalties()
        except Exception:
            logging.exception("Ошибка при проверке штрафов")
        try:
            for uid in core.expired_proofs():
                await app.bot.send_message(uid, "⌛ Время на кружок вышло, приход не засчитан.\nНажмите «✅ Пришёл» ещё раз.")
        except Exception:
            logging.exception("Ошибка при проверке кружков")
        try:
            invest_mod.check_matured()
        except Exception:
            logging.exception("Ошибка при проверке сроков вкладов")
        try:
            await relay.daily_check(app.bot)
        except Exception:
            logging.exception("Ошибка при проверке альбомов фотографа")
        try:
            for m in core.pending_outbox():
                try:
                    thread = PENALTY_THREAD_ID if m["chat_id"] == core.PENALTY_CHAT_ID else None
                    text, mode = m["text"], None
                    if text.startswith("HTML:"):
                        text, mode = text[5:], "HTML"
                    markup = None
                    if m.get("markup"):
                        markup = InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row]
                                                       for row in json.loads(m["markup"])])
                    await app.bot.send_message(m["chat_id"], text, message_thread_id=thread, parse_mode=mode,
                                               reply_markup=markup)
                    core.mark_sent(m["id"])
                except BadRequest as e:
                    logging.warning("Сообщение отклонено %s: %s", m["chat_id"], e)
                    core.mark_sent(m["id"])
                except NetworkError:
                    break  # сеть сбоит — попробуем позже
                except TelegramError as e:
                    # например, человек ни разу не нажимал /start у бота
                    logging.warning("Не доставлено %s: %s", m["chat_id"], e)
                    core.mark_sent(m["id"])
        except Exception:
            logging.exception("Ошибка в очереди уведомлений")
        await asyncio.sleep(10)


async def post_init(app: Application) -> None:
    app.bot_data["outbox"] = asyncio.create_task(outbox_loop(app))


def main() -> None:
    core.init_db()
    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(
        (filters.ChatType.GROUPS | filters.ChatType.CHANNEL) & ~filters.StatusUpdate.ALL & ~filters.COMMAND,
        relay.on_group_message,
    ))
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(ChatMemberHandler(bot_added, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(CommandHandler("task", task_cmd))
    app.add_handler(CommandHandler("fine", fine_cmd))
    app.add_handler(CallbackQueryHandler(task_button, pattern=r"^(done|task):"))
    app.add_handler(CallbackQueryHandler(choose_lang, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(invest_button, pattern=r"^inv:(ok|no):\d+$"))
    app.add_handler(MessageHandler(filters.Text([BTN_IN]), came))
    app.add_handler(MessageHandler(filters.Text([BTN_OUT]), left))
    app.add_handler(MessageHandler(filters.Text([BTN_TASKS]), my_tasks))
    app.add_handler(MessageHandler(filters.Text([BTN_TEAM]), team))
    app.add_handler(MessageHandler(filters.Text([BTN_INVEST]), invest))
    app.add_handler(CommandHandler("news", news_cmd))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.CONTACT, got_contact))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.VIDEO_NOTE, got_video_note))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO), other_media_while_proof))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, other))
    app.add_error_handler(on_error)
    print("Бот запущен. Остановить: Ctrl+C")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
