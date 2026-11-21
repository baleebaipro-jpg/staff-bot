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
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
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
import posts
import projects
import relay
import staff_text as st

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
    """Кнопки сотрудника — на его языке (uz / ru); кнопки руководителя — по-русски."""
    rows = []
    if core.employee(uid):
        L = core.staff_lang(uid)
        rows.append([st.s(L, "btn_in"), st.s(L, "btn_out")])
        rows.append([st.s(L, "btn_tasks"), st.s(L, "btn_invest")])
        rows.append([KeyboardButton(st.s(L, "btn_cabinet"), web_app=WebAppInfo(url=config.WEBAPP_URL)),
                     st.s(L, "btn_lang")])
    if core.is_admin(uid):
        rows.append([BTN_TEAM] + ([] if core.employee(uid) else [BTN_INVEST]))
        if not core.employee(uid):
            rows.append([KeyboardButton(BTN_CABINET, web_app=WebAppInfo(url=config.WEBAPP_URL))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def staff_lang_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(title, callback_data=f"slang:{code}")]
                                 for code, title in st.LANGS.items()])


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
        return await reply(update, st.s(core.staff_lang(u.id), "phone_saved"), reply_markup=keyboard(u.id))
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
        found = await asyncio.to_thread(erp.find_client, cl["phone"])  # поиск в BILLZ по номеру
    except Exception:
        logging.exception("Ошибка поиска клиента в ERP")
    if found and found.get("first_name"):
        core.update_client(u.id, first_name=found.get("first_name"), last_name=found.get("last_name"),
                           birth_date=found.get("birth_date"), billz_id=found.get("billz_id"),
                           billz_balance=found.get("balance"), step="done")
        try:
            await asyncio.to_thread(erp.sync_client, u.id, True)  # сумма покупок и статус
        except Exception:
            logging.exception("Ошибка BILLZ")
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
        if erp.CREATE_CLIENTS:  # завести клиента в BILLZ (включается в config.py)
            try:
                await asyncio.to_thread(erp.create_client, u.id)
            except Exception:
                logging.exception("BILLZ: не удалось создать клиента")
        await reply(update, i18n.t(lang, "registered"))
        return await client_menu(update)
    if text in i18n.all_variants("btn_lang"):
        return await reply(update, i18n.t(None, "choose_lang"), reply_markup=lang_buttons())
    if not lang or not cl.get("phone"):
        return await client_menu(update)
    if text in i18n.all_variants("btn_purchases") or text in i18n.all_variants("btn_bonus"):
        purchases = text in i18n.all_variants("btn_purchases")
        if not erp.enabled():
            key = "soon_purchases" if purchases else "soon_bonus"
            return await reply(update, i18n.t(lang, key), reply_markup=client_keyboard(u.id))
        stats = await asyncio.to_thread(erp.sync_client, u.id)
        value = stats.get("purchases" if purchases else "balance")
        if not stats.get("linked"):
            msg = i18n.t(lang, "billz_error") if stats.get("error") else i18n.t(lang, "billz_not_found")
        elif value is None:
            msg = i18n.t(lang, "billz_error")
        else:
            money = f"{int(value):,}".replace(",", " ") + " " + i18n.t(lang, "currency")  # вниз: не обещать лишнего
            msg = i18n.t(lang, "purchases_info" if purchases else "bonus_info", sum=money)
        return await reply(update, msg, reply_markup=client_keyboard(u.id))
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
    if core.employee(uid) and not core.staff_lang_chosen(uid):
        # сотрудник впервые — сначала выбор языка
        return await reply(update, st.s(None, "choose_lang"), reply_markup=staff_lang_buttons())
    L = core.staff_lang(uid) if core.employee(uid) else "ru"
    text = st.s(L, "hello", name=core.name(uid) if core.employee(uid) else update.effective_user.first_name) + "\n\n"
    if core.employee(uid):
        text += st.s(L, "help_staff", btn_in=st.s(L, "btn_in"), btn_out=st.s(L, "btn_out"), btn_tasks=st.s(L, "btn_tasks"))
    if core.is_admin(uid):
        text += (
            f"{BTN_TEAM} — кто на месте\n"
            "/task ID текст — поставить задачу\n"
            "/fine ID сумма причина — выписать штраф\n"
            "/news текст — новость для клиентов\n"
        )
    text += st.s(L, "help_cabinet", btn=st.s(L, "btn_cabinet"))
    await reply(update, text, reply_markup=keyboard(uid))


async def choose_staff_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сотрудник выбрал язык (uz / ru)."""
    q = update.callback_query
    uid = q.from_user.id
    if not core.employee(uid):
        return await q.answer()
    L = core.set_staff_lang(uid, q.data.split(":", 1)[1])
    await q.answer(st.s(L, "lang_set"))
    try:
        await q.edit_message_text(st.s(L, "lang_set"))
    except TelegramError:
        pass
    await start(update, context)


async def staff_lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «🌐 Til / Язык» у сотрудника."""
    if not core.employee(update.effective_user.id):
        return await other(update, context)
    await reply(update, st.s(None, "choose_lang"), reply_markup=staff_lang_buttons())


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await reply(update, st.s(core.staff_lang(uid) if core.employee(uid) else "ru", "your_id", id=uid))


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
    cin, late = core.last_checkin(uid)  # для руководителя — по-русски, независимо от языка сотрудника
    note = f"📹 {core.name(uid)}: приход отмечен в {cin}\n🔢 Код: {code} — проверьте, что назвал(а) его"
    if late:
        note += f"\n⚠️ Опоздание: {core.fmt_min(late)}"
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
        await reply(update, st.s(core.staff_lang(uid), "need_circle"))
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
    L = core.staff_lang(uid)
    open_tasks = [t for t in core.tasks(uid) if not t["done"]]
    if not open_tasks:
        return await reply(update, st.s(L, "no_tasks"))
    lines, buttons = [], []
    for i, t in enumerate(open_tasks, 1):
        if t["due"]:
            lines.append(f"{i}. {t['title']} — {st.s(L, 'task_due', d=t['due'])}")
        elif t["status"] == "review":
            lines.append(f"{i}. {t['title']} — {st.s(L, 'task_review')}")
        else:
            lines.append(f"{i}. {t['title']}")
            buttons.append([InlineKeyboardButton(st.s(L, "btn_done", t=t["title"][:40]), callback_data=f"done:{t['id']}")])
    await reply(update, st.s(L, "tasks_title") + "\n\n" + "\n".join(lines),
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
        ok, msg = False, st.s(core.staff_lang(uid) if core.employee(uid) else "ru", "no_access")
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
    if not invest_mod.ENABLED:
        return await reply(update, st.s(core.staff_lang(uid) if core.employee(uid) else "ru", "invest_soon"),
                           reply_markup=keyboard(uid))
    M = invest_mod.money
    lines = []
    if invest_mod.can_invest(uid):
        m = invest_mod.mine(uid)
        lines.append("💰 Мои инвестиции")
        if m["access"] != "approved":
            lines.append({"pending": "⏳ Ваша заявка на участие на рассмотрении у руководителя.",
                          "rejected": "Заявка на участие отклонена. Можно подать снова в «🏠 Личный кабинет» → «💰 Инвестиции»."}
                         .get(m["access"], "Инвестиции в товар компании — по заявке.\n"
                                           "Подайте заявку на участие в «🏠 Личный кабинет» → «💰 Инвестиции»."))
            if not core.is_admin(uid):
                return await reply(update, "\n".join(lines), reply_markup=keyboard(uid))
        else:
            if m["invested"]:
                lines.append(f"Капитал в товаре: {M(m['invested'])}\n"
                             f"Доля прибыли: +{M(m['accrued'])}")
            for r in m["items"]:
                if r["status"] == "pending":
                    lines.append(f"⏳ Заявка {M(r['amount'])} — ждёт руководителя")
                elif r["status"] == "active":
                    lines.append(f"📈 {M(r['amount'])} · +{M(r['accrued'])} · до {r['end']} (ещё {r['days_left']} дн.)"
                                 + (" · ⏳ заявка на досрочный выход" if r["early"] else ""))
                elif r["status"] == "rating":
                    lines.append(f"📈 {M(r['amount'])} · срок закончился, ждём оценку последнего месяца")
                elif r["status"] == "matured":
                    lines.append(f"🎉 {M(r['amount'])} · к выплате {M(r['payout'])}")
            if not m["items"]:
                lines.append("Инвестиций пока нет.")
            hist = invest_mod.history_public(3)
            if hist:
                lv = invest_mod.LEVELS
                lines.append("\nПоследние месяцы: " + ", ".join(
                    f"{h['title'].split()[0].lower()} {lv[h['level']]['emoji']}" for h in hist))
            lines.append(f"\nИнвестиция в товар на {invest_mod.TERM} мес. Доля прибыли зависит от итогов продаж "
                         f"каждого месяца.\nИнвестировать — в «{BTN_CABINET}».")
    if core.is_admin(uid):
        o = invest_mod.overview()
        lines.append(f"{chr(10) if lines else ''}📊 Инвестиции команды\n"
                     f"Капитал в товаре: {M(o['invested'])}\n"
                     f"Доля прибыли инвесторов: {M(o['accrued'])}\n"
                     f"Ожидается к выплате: {M(o['obligations_min'])} – {M(o['obligations_max'])}\n"
                     f"Заявок ждут: {len(o['pending'])} · досрочный выход: {len(o['early'])} · "
                     f"к выплате сейчас: {len(o['matured'])}")
        for t in o["to_rate"]:
            lines.append(f"⚠️ Не оценён месяц: {t['title'].lower()}")
        if o["access"]:
            lines.append(f"🙋 Заявок на участие: {len(o['access'])}")
    buttons = None
    if core.is_admin(uid):
        if o["to_rate"]:
            ym = o["to_rate"][0]["month"]
            buttons = InlineKeyboardMarkup([[InlineKeyboardButton(f"{v['emoji']} {v['title']}", callback_data=f"inv_lvl:{ym}:{k}")
                                             for k, v in invest_mod.LEVELS.items()]])
    await reply(update, "\n".join(lines), reply_markup=buttons or keyboard(uid))


async def invest_chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/investchat в группе — сообщения по инвестициям идут в эту группу
    (по темам из INVEST_TOPICS в config.py). /investchat off — в личку руководителям."""
    uid = update.effective_user.id
    if not core.is_admin(uid):
        return await reply(update, "Подключить группу может только руководитель.")
    chat = update.effective_chat
    if context.args and context.args[0].lower() in ("off", "выкл"):
        invest_mod.set_group(None)
        return await reply(update, "✅ Группа для инвестиций отключена — заявки снова придут руководителям в личку.")
    if chat.type not in ("group", "supergroup"):
        cur = invest_mod.group()
        return await reply(update, "Напишите /investchat в группе, куда должны приходить заявки по инвестициям."
                                   + (f"\nСейчас подключена группа {cur}." if cur else ""))
    invest_mod.set_group(chat.id)
    t = invest_mod.TOPICS
    await reply(update, "✅ Группа подключена. Темы:\n"
                        f"• заявки на участие — {t.get('access', '—')}\n"
                        f"• новые заявки — {t.get('new', '—')}\n• выплаты по сроку — {t.get('payout', '—')}\n"
                        f"• досрочный выход — {t.get('early', '—')}\n• оценка месяца — {t.get('rating', '—')}\n"
                        "Нажимать кнопки может только руководитель.")


class _WaitingReport(filters.MessageFilter):
    """Сообщение-фото от руководителя, от которого ждём фото-отчёт о выплате."""
    def filter(self, message) -> bool:
        return bool(invest_mod.ENABLED and message.from_user and core.is_admin(message.from_user.id)
                    and invest_mod.waiting_report(message.from_user.id))


async def invest_report_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    uid = update.effective_user.id
    wait = invest_mod.waiting_report(uid)
    if not wait:
        return
    inv_id, method = wait
    file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
    in_group = update.effective_chat.id == invest_mod.group()
    ok, text = invest_mod.early_pay(inv_id, method, "tg:" + file_id, post_to_group=not in_group)
    invest_mod.stop_waiting(uid)
    await msg.reply_text(("✅ " if ok else "⚠️ ") + text)


async def invest_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки под сообщениями: «Деньги получил / Отклонить» и оценка месяца."""
    q = update.callback_query
    if not invest_mod.ENABLED:
        return await q.answer(invest_mod.SOON_TEXT, show_alert=True)
    if not core.is_admin(q.from_user.id):
        return await q.answer("Нет доступа.", show_alert=True)
    kind, a, b = q.data.split(":")
    if kind == "inv_early" and a == "ok":
        # шаг 1: как платили?
        await q.answer()
        try:
            await q.edit_message_reply_markup(InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Онлайн", callback_data=f"inv_pay:{b}:online"),
                 InlineKeyboardButton("💵 Офлайн", callback_data=f"inv_pay:{b}:offline")],
                [InlineKeyboardButton("✕ Отклонить", callback_data=f"inv_early:no:{b}")]]))
        except TelegramError:
            pass
        return
    if kind == "inv_pay":
        # шаг 2: ждём фото-отчёт
        inv_id, method = int(a), b
        invest_mod.wait_report(q.from_user.id, inv_id, method)
        await q.answer()
        await q.message.reply_text(
            f"📸 {q.from_user.first_name}, пришлите фото-отчёт ({invest_mod.REPORT_HINT[method]}) "
            f"по инвестиции №{inv_id} — ответом на это сообщение или боту в личку. Жду 30 минут.")
        return
    if kind == "inv_acc":
        ok, msg = invest_mod.access_decide(int(b), a == "ok")
    elif kind == "inv":
        ok, msg = invest_mod.decide(int(b), a == "ok")
    elif kind == "inv_early":
        ok, msg = invest_mod.early_reject(int(b))
    else:
        ok, msg = invest_mod.set_level(a, b)
    await q.answer(msg[:190], show_alert=not ok)
    if ok or kind in ("inv", "inv_early", "inv_acc"):
        if q.message.chat.type != "private":
            msg += f" — {q.from_user.first_name}"
        try:
            await q.edit_message_text(q.message.text + "\n\n" + msg)
        except TelegramError:
            pass


async def other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not known(uid):
        return await client_text(update, context)
    await reply(update, st.s(core.staff_lang(uid) if core.employee(uid) else "ru", "use_buttons"), reply_markup=keyboard(uid))


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
                L = core.staff_lang(uid)
                await app.bot.send_message(uid, st.s(L, "proof_expired_bot", btn=st.s(L, "btn_in")))
        except Exception:
            logging.exception("Ошибка при проверке кружков")
        try:
            posts.dispatch_due()
        except Exception:
            logging.exception("Ошибка запланированных постов")
        try:
            projects.morning_digest()
        except Exception:
            logging.exception("Ошибка утреннего списка задач")
        try:
            if invest_mod.ENABLED:
                invest_mod.check_rating_day()
                invest_mod.check_matured()
        except Exception:
            logging.exception("Ошибка при проверке сроков инвестиций")
        try:
            await relay.daily_check(app.bot)
        except Exception:
            logging.exception("Ошибка при проверке альбомов фотографа")
        try:
            for m in core.pending_outbox():
                try:
                    thread = m.get("thread_id") or (PENALTY_THREAD_ID if m["chat_id"] == core.PENALTY_CHAT_ID else None)
                    text, mode = m["text"], None
                    if text.startswith("HTML:"):
                        text, mode = text[5:], "HTML"
                    markup = None
                    if m.get("markup") == "staff_kb":      # язык сменили в кабинете — новые кнопки
                        markup = keyboard(m["chat_id"])
                    elif m.get("markup") == "client_kb":
                        markup = client_keyboard(m["chat_id"])
                    elif m.get("markup"):
                        markup = InlineKeyboardMarkup([[
                            InlineKeyboardButton(t, url=d[4:]) if d.startswith("url:") else InlineKeyboardButton(t, callback_data=d)
                            for t, d in row] for row in json.loads(m["markup"])])
                    if m.get("photo"):
                        p = m["photo"]
                        src = p[3:] if p.startswith("tg:") else open(p, "rb")
                        try:
                            await app.bot.send_photo(m["chat_id"], src, caption=text[:1024], message_thread_id=thread,
                                                     parse_mode=mode, reply_markup=markup)
                        finally:
                            if not isinstance(src, str):
                                src.close()
                    else:
                        await app.bot.send_message(m["chat_id"], text, message_thread_id=thread, parse_mode=mode,
                                                   reply_markup=markup)
                    core.mark_sent(m["id"])
                except RetryAfter as e:
                    await asyncio.sleep(float(getattr(e, "retry_after", 5)) + 1)  # Telegram просит подождать
                    break
                except BadRequest as e:
                    logging.warning("Сообщение отклонено %s: %s", m["chat_id"], e)
                    core.mark_sent(m["id"], failed=True)
                except NetworkError:
                    break  # сеть сбоит — попробуем позже
                except TelegramError as e:
                    # например, человек ни разу не нажимал /start у бота или заблокировал его
                    logging.warning("Не доставлено %s: %s", m["chat_id"], e)
                    core.mark_sent(m["id"], failed=True)
        except Exception:
            logging.exception("Ошибка в очереди уведомлений")
        # пока идёт рассылка — разбираем очередь быстрее
        await asyncio.sleep(1 if len(core.pending_outbox(1)) else 10)


async def post_init(app: Application) -> None:
    app.bot_data["outbox"] = asyncio.create_task(outbox_loop(app))


def main() -> None:
    core.init_db()
    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & _WaitingReport(), invest_report_photo))
    app.add_handler(MessageHandler(
        (filters.ChatType.GROUPS | filters.ChatType.CHANNEL) & ~filters.StatusUpdate.ALL & ~filters.COMMAND,
        relay.on_group_message,
    ))
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(ChatMemberHandler(bot_added, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(CommandHandler("investchat", invest_chat_cmd))
    app.add_handler(CommandHandler("task", task_cmd))
    app.add_handler(CommandHandler("fine", fine_cmd))
    app.add_handler(CallbackQueryHandler(task_button, pattern=r"^(done|task):"))
    app.add_handler(CallbackQueryHandler(choose_lang, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(choose_staff_lang, pattern=r"^slang:(uz|ru)$"))
    app.add_handler(CallbackQueryHandler(invest_button, pattern=r"^(inv(_early)?:(ok|no):\d+|inv_lvl:\d{4}-\d{2}:\w+|inv_pay:\d+:(online|offline)|inv_acc:(ok|no):\d+)$"))
    # кнопки сотрудника — на любом из языков (uz / ru)
    app.add_handler(MessageHandler(filters.Text(st.variants("btn_in")), came))
    app.add_handler(MessageHandler(filters.Text(st.variants("btn_out")), left))
    app.add_handler(MessageHandler(filters.Text(st.variants("btn_tasks")), my_tasks))
    app.add_handler(MessageHandler(filters.Text([BTN_TEAM]), team))
    app.add_handler(MessageHandler(filters.Text(list(dict.fromkeys([BTN_INVEST] + st.variants("btn_invest")))), invest))
    app.add_handler(MessageHandler(filters.Text(st.variants("btn_lang")), staff_lang_cmd))
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
