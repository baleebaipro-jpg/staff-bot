import asyncio
import logging
import os

import json
from datetime import datetime, timedelta, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    MenuButtonWebApp,
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
    ChatJoinRequestHandler,
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
import shop
import appauth
import bizwatch
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
        # у сотрудника — только кабинет и язык: приход/уход, задачи, продажи и всё остальное — в кабинете.
        # Кабинет — обычной кнопкой: мини-приложение с клавиатуры Telegram открывает без данных о пользователе,
        # поэтому по нажатию бот присылает кнопку «Открыть кабинет» под сообщением (она данные передаёт).
        # Старые кнопки (Keldim, Ketdim, Vazifalarim…) продолжают работать, если у кого-то остались.
        rows.append([st.s(L, "btn_cabinet"), st.s(L, "btn_lang")])
    elif shop.has_access(uid):  # руководитель: кнопка ERP
        rows.append([st.s("ru", "btn_shop")])
    if core.is_admin(uid):
        rows.append([BTN_TEAM] + ([] if core.employee(uid) else [BTN_INVEST]))
        if not core.employee(uid):
            rows.append([BTN_CABINET])
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


async def remember_client(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Бот клиентов: запоминаем ID каждого, кто хоть что-то сделал в боте (нажал, написал, подал заявку),
    чтобы посты из «Постинга» доходили до всех."""
    u = update.effective_user
    if not u or u.is_bot:
        return
    chat = update.effective_chat
    if chat and chat.type != "private" and not update.chat_join_request:
        return
    try:
        core.save_client(u.id, first_name=u.first_name or "", username=u.username or "")
    except Exception:  # noqa: BLE001 — запись не должна мешать ответу
        logging.exception("Не удалось запомнить клиента %s", u.id)


def known(uid: int) -> bool:
    return bool(core.employee(uid)) or core.is_admin(uid)


# ---------- два бота: сотрудники (BOT_TOKEN) и клиенты (CLIENT_BOT_TOKEN, необязательно) ----------

CLIENT_BOT = {"username": None, "ok": None}  # ok=False — токен задан, но бот клиентов не запустился


def client_bot_on() -> bool:
    return bool(getattr(config, "CLIENT_BOT_TOKEN", "")) and CLIENT_BOT["ok"] is not False


def in_client_bot(context) -> bool:
    """Обновление пришло в бота клиентов (там все — клиенты, даже сотрудники)."""
    return bool(context and context.bot_data.get("role") == "client")


async def to_client_bot(update: Update) -> None:
    """Клиент написал боту сотрудников — направляем в бота клиентов."""
    u = CLIENT_BOT["username"]
    text = ("👋 Bu — xodimlar uchun bot.\nMijozlar uchun bot: @{u}\n\n"
            "👋 Это бот для сотрудников.\nДля клиентов — @{u}").format(u=u or "…")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"➡️ @{u}", url=f"https://t.me/{u}")]]) if u else None
    await reply(update, text, reply_markup=markup)


async def deny(update: Update) -> None:
    """Клиент нажал что-то для сотрудников — показываем меню клиента (или ведём в бота клиентов)."""
    if client_bot_on():
        return await to_client_bot(update)
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
    # покупки, новости и остальное клиент смотрит в мини-приложении (кнопка «Кабинет»)
    return ReplyKeyboardMarkup([
        [i18n.t(lang, "btn_cabinet")],
        [i18n.t(lang, "btn_bonus")] + ([i18n.t(lang, "btn_channel")] if getattr(config, "CLIENT_CHANNEL_ID", None) else []),
    ] + ([[i18n.t(lang, "btn_debt")]] if float(cl.get("billz_debt") or 0) > 0 else []) + [
        [i18n.t(lang, "btn_lang"), i18n.t(lang, "btn_logout")],
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
    name = core.client_first_name(cl) or u.first_name
    text = (i18n.t(lang, "greet", name=name) + "\n" if greet else "")
    text += i18n.t(lang, "status_line", tier=core.tier_title(core.tier_of(cl))) + "\n\n" + i18n.t(lang, "menu")
    if erp.enabled() and cl.get("phone"):
        try:  # свежие данные (с кэшем) — чтобы кнопка «💳 Долг» появилась/исчезла вовремя
            await asyncio.to_thread(erp.sync_client, u.id)
        except Exception:  # noqa: BLE001
            logging.exception("BILLZ: обновление при показе меню")
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


async def client_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подтверждение «Выйти»: сбросить данные клиента и начать заново."""
    q = update.callback_query
    uid = q.from_user.id
    lang = (core.get_client(uid) or {}).get("lang")
    if known(uid) and not in_client_bot(context):
        return await q.answer()
    if q.data.endswith(":no"):
        await q.answer()
        try:
            await q.edit_message_text(i18n.t(lang, "logout_cancel"))
        except TelegramError:
            pass
        return await client_menu(update)
    core.reset_client(uid)
    await q.answer(i18n.t(lang, "logout_done"))
    try:
        await q.edit_message_reply_markup(None)  # убрать кнопки «Да / Нет» под вопросом
    except TelegramError:
        pass
    await reply(update, i18n.t(lang, "logout_done"), reply_markup=ReplyKeyboardRemove())
    await client_menu(update, greet=True)  # заново: выбор языка → номер → данные


async def got_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    contact = update.message.contact
    cl = core.get_client(u.id) or {}
    lang = cl.get("lang")
    if contact.user_id != u.id:
        return await reply(update, i18n.t(lang, "wrong_contact"), reply_markup=client_keyboard(u.id))
    core.save_client(u.id, phone=contact.phone_number, first_name=u.first_name or "", username=u.username or "")
    if known(u.id) and not in_client_bot(context):
        return await reply(update, st.s(core.staff_lang(u.id), "phone_saved"), reply_markup=keyboard(u.id))
    if not known(u.id) and client_bot_on() and not in_client_bot(context):
        return await to_client_bot(update)
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
    if text in i18n.all_variants("btn_cabinet"):
        return await reply(update, i18n.t(lang, "open_cabinet"), reply_markup=cabinet_button(i18n.t(lang, "open_cabinet_btn")))
    if text in i18n.all_variants("btn_logout"):
        return await reply(update, i18n.t(lang, "logout_confirm"), reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(i18n.t(lang, "logout_yes"), callback_data="clogout:yes"),
            InlineKeyboardButton(i18n.t(lang, "logout_no"), callback_data="clogout:no")]]))
    if text in i18n.all_variants("btn_lang"):
        return await reply(update, i18n.t(None, "choose_lang"), reply_markup=lang_buttons())
    if not lang or not cl.get("phone"):
        return await client_menu(update)
    if text in i18n.all_variants("btn_purchases") or text in i18n.all_variants("btn_bonus"):
        purchases = text in i18n.all_variants("btn_purchases")
        if not erp.enabled():
            key = "soon_purchases" if purchases else "soon_bonus"
            return await reply(update, i18n.t(lang, key), reply_markup=client_keyboard(u.id))
        stats = await asyncio.to_thread(erp.sync_client, u.id, True)  # по кнопке — всегда свежие данные
        value = stats.get("purchases" if purchases else "balance")
        if not stats.get("linked"):
            msg = i18n.t(lang, "billz_error") if stats.get("error") else i18n.t(lang, "billz_not_found")
        elif value is None:
            msg = i18n.t(lang, "billz_error")
        else:
            money = f"{int(value):,}".replace(",", " ") + " " + i18n.t(lang, "currency")  # вниз: не обещать лишнего
            msg = i18n.t(lang, "purchases_info" if purchases else "bonus_info", sum=money)
        return await reply(update, msg, reply_markup=client_keyboard(u.id))
    if text in i18n.all_variants("btn_debt"):
        return await debt_info(update, lang)
    if text in i18n.all_variants("btn_channel") and getattr(config, "CLIENT_CHANNEL_ID", None):
        return await channel_link(update, context, lang)
    if text in i18n.all_variants("btn_invest"):  # раздел скрыт от клиентов — старая кнопка просто открывает меню
        return await client_menu(update)
    if text in i18n.all_variants("btn_news"):
        items = core.news_list(5)
        if not items:
            return await reply(update, i18n.t(lang, "no_news"))
        return await reply(update, i18n.t(lang, "news_title") + "\n\n" +
                           "\n\n— — —\n\n".join(f"🗓 {n['date']}\n{n['text']}" for n in items))
    await client_menu(update)


# ---------- долг клиента ----------

def _money(v, lang) -> str:
    return f"{int(round(v or 0)):,}".replace(",", " ") + " " + i18n.t(lang, "currency")


async def debt_info(update: Update, lang: str) -> None:
    """Кнопка «💳 Долг» (видна, только когда долг есть): свежая сумма и список с датами погашения."""
    uid = update.effective_user.id
    stats = await asyncio.to_thread(erp.sync_client, uid, True) if erp.enabled() else {}
    total = float(stats.get("debt") or 0)
    if stats.get("error") and not total:
        return await reply(update, i18n.t(lang, "billz_error"), reply_markup=client_keyboard(uid))
    if total <= 0:
        return await reply(update, i18n.t(lang, "debt_none"), reply_markup=client_keyboard(uid))
    lines = [i18n.t(lang, "debt_total", sum=_money(total, lang))]
    items = await asyncio.to_thread(erp.debts, uid)
    if items:
        lines.append("")
        for d in items[:10]:
            line = i18n.t(lang, "debt_item", order=("№" + str(d["order"])) if d["order"] else "", left=_money(d["left"], lang))
            if d["due"]:
                line += i18n.t(lang, "debt_due", date=datetime.strptime(d["due"], "%Y-%m-%d").strftime("%d.%m.%Y"))
            if d["overdue"]:
                line += i18n.t(lang, "debt_overdue")
            lines.append(line)
    lines += ["", i18n.t(lang, "debt_footer")]
    await reply(update, "\n".join(lines), reply_markup=client_keyboard(uid))


# ---------- закрытый канал для покупателей ----------

async def channel_link(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    """Кнопка «📢 Канал»: покупателю — личная ссылка-заявка (одна на человека, 24 часа)."""
    uid, chat = update.effective_user.id, config.CLIENT_CHANNEL_ID
    try:
        member = await context.bot.get_chat_member(chat, uid)
        if member.status in ("member", "administrator", "creator"):
            return await reply(update, i18n.t(lang, "channel_already"), reply_markup=client_keyboard(uid))
    except TelegramError:
        pass  # не смогли проверить — просто продолжаем
    stats = await asyncio.to_thread(erp.sync_client, uid, True) if erp.enabled() else {}
    bought = float(stats.get("purchases") or 0)
    if bought < getattr(config, "CLIENT_CHANNEL_MIN_PURCHASES", 1):
        return await reply(update, i18n.t(lang, "channel_no_purchase"), reply_markup=client_keyboard(uid))
    link = core.channel_link_get(uid, chat)
    if not link:
        hours = getattr(config, "CLIENT_CHANNEL_LINK_HOURS", 24)
        expires = core.now() + timedelta(hours=hours)
        try:
            inv = await context.bot.create_chat_invite_link(
                chat, name=f"client {uid}"[:32], creates_join_request=True,
                expire_date=datetime.now(timezone.utc) + timedelta(hours=hours))
        except TelegramError as e:
            logging.warning("Канал: не удалось создать ссылку для %s: %s", uid, e)
            return await reply(update, i18n.t(lang, "channel_error"), reply_markup=client_keyboard(uid))
        link = inv.invite_link
        core.channel_link_add(link, uid, chat, expires)
    await reply(update, i18n.t(lang, "channel_link"),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(i18n.t(lang, "channel_btn"), url=link)]]))


async def channel_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Заявка в канал: одобряем, только если ссылку получил именно этот человек; ссылку после этого отзываем."""
    req = update.chat_join_request
    if not req or req.chat.id != getattr(config, "CLIENT_CHANNEL_ID", None):
        return
    url = req.invite_link.invite_link if req.invite_link else None
    owner = core.channel_link_owner(url) if url else None
    if owner != req.from_user.id:
        logging.info("Канал: заявка %s по чужой/неизвестной ссылке отклонена", req.from_user.id)
        try:
            await req.decline()
        except TelegramError:
            pass
        return
    try:
        await req.approve()
    except TelegramError as e:
        logging.warning("Канал: не удалось одобрить заявку %s: %s", req.from_user.id, e)
        return
    core.channel_link_used(url)
    try:
        await context.bot.revoke_chat_invite_link(req.chat.id, url)  # одноразовая — больше никто не войдёт
    except TelegramError:
        pass
    lang = (core.get_client(req.from_user.id) or {}).get("lang")
    try:
        await context.bot.send_message(req.from_user.id, i18n.t(lang, "channel_welcome"))
    except TelegramError:
        pass


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
    if context.args and context.args[0].startswith("applogin_"):  # вход в мобильное приложение
        L = core.staff_lang(uid) if core.employee(uid) else "ru"
        ok = appauth.confirm(context.args[0][9:], uid)
        return await reply(update, st.s(L, "app_login_ok" if ok else ("app_login_bad" if known(uid) else "app_login_denied")))
    if not known(uid):
        if client_bot_on():
            return await to_client_bot(update)
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


async def billz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/billz [номер] — руководитель проверяет подключение к BILLZ."""
    if not core.is_admin(update.effective_user.id):
        return await other(update, context)
    phone = " ".join(context.args) if context.args else None
    await reply(update, "⏳ Проверяю BILLZ…")
    try:
        text = await asyncio.to_thread(erp.diagnose, phone)
        text += "\n\n" + erp.watch_status()
    except Exception as e:
        logging.exception("BILLZ диагностика")
        text = f"❌ Ошибка проверки: {e}"
    # без превью ссылки: иначе Telegram открывает адрес API и показывает его ответ как «.json»
    await reply(update, text, link_preview_options=LinkPreviewOptions(is_disabled=True))


async def biz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/biz — руководителю: подключённые рабочие аккаунты (контроль удалённых сообщений)."""
    if not core.is_admin(update.effective_user.id):
        return
    rows = bizwatch.connections()
    if not rows:
        return await reply(update, "Рабочие аккаунты пока не подключены.\n\nTelegram → Настройки → Telegram для бизнеса → "
                                   f"Чат-боты → @{context.bot.username}")
    kinds = {"connected": "🔗 подключён", "disconnected": "⚠️ отключён", "edited": "✏️ изменено", "deleted": "🗑 удалено"}
    ev = bizwatch.recent_events()
    await reply(update, "🔗 Контроль рабочих аккаунтов:\n\n" + "\n\n".join(
        f"{'🟢' if r['enabled'] else '🔴 отключён'} {r['name']}{' @' + r['username'] if r['username'] else ''}\n"
        f"с {r['since'][:16]} · записано сообщений: {r['messages']} в {r['chats']} чатах · удалено: {r['deleted']}\n"
        f"последнее сообщение: {r['last'][:16] or 'пока ни одного'}" for r in rows)
        + ("\n\nПоследние события:\n" + "\n".join(f"{e['at'][5:16]} {kinds.get(e['kind'], e['kind'])} {e['chat_name'] or ''}"
                                                    for e in ev) if ev else ""))


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
        if t.get("repeat"):
            t = dict(t, title="🔁 " + t["title"])  # повторяющаяся задача
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
        return await (to_client_bot(update) if client_bot_on() else client_text(update, context))
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
        return await (to_client_bot(update) if client_bot_on() else client_text(update, context))
    await reply(update, st.s(core.staff_lang(uid) if core.employee(uid) else "ru", "use_buttons"), reply_markup=keyboard(uid))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.warning("Ошибка: %s", context.error)


# ---------- отправка уведомлений из очереди ----------

def _bot_for(m, app, client_app):
    """Через какого бота отправить сообщение из очереди: клиентам — через бота клиентов
    (если он подключён), сотрудникам, руководителю и в группы — через бота сотрудников."""
    if client_app is None:
        return app.bot
    chat = m["chat_id"]
    if m.get("via") == "client":
        return client_app.bot
    if m.get("via") == "staff":
        return app.bot
    if m.get("markup") == "client_kb" or (chat > 0 and not known(chat) and m.get("markup") != "staff_kb"):
        return client_app.bot
    return app.bot


async def outbox_loop(app: Application, client_app: Application = None) -> None:
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
            erp.import_due()  # раз в сутки — копия клиентской базы BILLZ
            import followup
            followup.tick()  # обратная связь с клиентом на следующий день после покупки
            shop.daily()  # ERP: напоминания о разморозке бонусов, сгорание баллов
            erp.start_purchase_watch()  # «спасибо за покупку» — проверка продаж каждые 30 с (запускается один раз)
        except Exception:
            logging.exception("Ошибка запланированных постов")
        try:
            core.spawn_recurring()  # утром — повторяющиеся задачи сотрудникам
            core.shift_reminders()  # за 15 мин до начала и конца смены — напомнить отметиться
        except Exception:
            logging.exception("Ошибка повторяющихся задач")
        try:
            projects.morning_digest()
        except Exception:
            logging.exception("Ошибка утреннего списка задач")
        try:
            import msgr
            msgr.check_timeouts()  # RADI Chat: клиент долго ждёт ответа
        except Exception:
            logging.exception("Ошибка проверки ответов RADI Chat")
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
                    sender = _bot_for(m, app, client_app)
                    if (m.get("photo") or "").startswith("doc:"):  # файл (выгрузка): путь на диске
                        with open(m["photo"][4:], "rb") as df:
                            await sender.send_document(m["chat_id"], df, caption=text[:1024], message_thread_id=thread,
                                                       filename=os.path.basename(m["photo"][4:]))
                    elif (m.get("photo") or "").startswith("video:"):  # видео-подтверждение прихода из приложения
                        with open(m["photo"][6:], "rb") as vf:
                            await sender.send_video(m["chat_id"], vf, caption=text[:1024], message_thread_id=thread,
                                                    parse_mode=mode, reply_markup=markup)
                    elif m.get("photo"):
                        p = m["photo"]
                        # file_id из бота сотрудников действует только в нём — такие фото отправляет он
                        if p.startswith("tg:"):
                            sender = app.bot
                        src = p[3:] if p.startswith("tg:") else open(p, "rb")
                        try:
                            await sender.send_photo(m["chat_id"], src, caption=text[:1024], message_thread_id=thread,
                                                     parse_mode=mode, reply_markup=markup)
                        finally:
                            if not isinstance(src, str):
                                src.close()
                    else:
                        await sender.send_message(m["chat_id"], text, message_thread_id=thread, parse_mode=mode,
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


def cabinet_button(title: str) -> InlineKeyboardMarkup:
    """Кнопка под сообщением, открывающая кабинет (передаёт данные пользователя, в отличие от кнопки клавиатуры)."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(title, web_app=WebAppInfo(url=config.WEBAPP_URL))]])


async def open_cabinet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажата кнопка «🏠 Личный кабинет» на клавиатуре сотрудника/руководителя."""
    uid = update.effective_user.id
    if not known(uid):
        return await (to_client_bot(update) if client_bot_on() else client_text(update, context))
    L = core.staff_lang(uid) if core.employee(uid) else "ru"
    await reply(update, st.s(L, "open_cabinet"), reply_markup=cabinet_button(st.s(L, "open_cabinet_btn")))


async def send_app(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/app — прислать Android-приложение RADI Team (APK) сотруднику или руководителю."""
    uid = update.effective_user.id
    if not known(uid):
        return await other(update, context)
    L = core.staff_lang(uid) if core.employee(uid) else "ru"
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "radi-team.apk")
    if not os.path.exists(path):
        return await reply(update, "APK ещё не готов.")
    with open(path, "rb") as f:
        await update.effective_message.reply_document(f, filename="RADI-Team.apk", caption=st.s(L, "app_install"))


def shop_url() -> str:
    return config.WEBAPP_URL + ("&" if "?" in config.WEBAPP_URL else "?") + "open=shop"


async def open_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «🧾 Продажи»: мини-приложение сразу на кассе."""
    uid = update.effective_user.id
    if not known(uid) or not shop.has_access(uid):
        return await open_cabinet(update, context)
    L = core.staff_lang(uid) if core.employee(uid) else "ru"
    await reply(update, st.s(L, "open_shop"), reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton(st.s(L, "open_shop_btn"), web_app=WebAppInfo(url=shop_url()))]]))


async def set_menu_button(app: Application, title: str = "Kabinet") -> None:
    """Кнопка меню слева от поля ввода — сразу открывает кабинет (с данными пользователя)."""
    try:
        await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(title, WebAppInfo(url=config.WEBAPP_URL)))
    except TelegramError as e:
        logging.warning("Не удалось поставить кнопку меню: %s", e)


BIZ_APP = {}


async def rename_penalty_chats(bot) -> None:
    """Темы штрафов сотрудников (заданные в PENALTY_CHATS явно) — название PENALTY_CHAT_TITLE.
    Саму группу НЕ переименовываем: штрафы — это тема внутри группы сотрудника."""
    wrong = getattr(config, "PENALTY_CHAT_TITLE", "") or "Shtraf/Bonus"
    for chat, old in getattr(config, "PENALTY_GROUP_RESTORE", {}).items():  # вернуть имена групп
        try:
            ch = await bot.get_chat(chat)
            if ch.title == wrong:
                await bot.set_chat_title(chat, old)
                logging.info("Группе %s возвращено название %s", chat, old)
        except TelegramError as e:
            logging.warning("Не удалось вернуть название группы %s: %s", chat, e)
    # темы, заданные в PENALTY_CHATS явно, не переименовываем: руководитель уже назвал их сам


async def _rename_topic(bot, chat: int, thread: int) -> None:
    title = getattr(config, "PENALTY_CHAT_TITLE", "")
    if not title:
        return
    key = f"pen_topic:{chat}:{thread}:{title}"
    if not core.flag_once(key):
        return
    try:
        await bot.edit_forum_topic(chat, thread, name=title)
        core.save_topic(chat, thread, title)
    except TelegramError as e:
        logging.warning("Не удалось переименовать тему штрафов %s/%s: %s", chat, thread, e)
        with core.db() as c:
            c.execute("DELETE FROM flags WHERE name=?", (key,))
            core.notify_admins(c, f"⚠️ Не смог переименовать тему штрафов в группе {chat}: {e}\n"
                                  "Проверьте право бота «Управление темами» и отправьте /shtraf внутри темы.")


async def shtraf_topic_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/shtraf — написать руководителю ВНУТРИ темы группы: эта тема — для штрафов/бонусов сотрудника
    (бот переименует её в PENALTY_CHAT_TITLE и будет слать штрафы сюда)."""
    m = update.effective_message
    if not m or not core.is_admin(update.effective_user.id):
        return
    if not m.is_topic_message or not m.message_thread_id:
        return await m.reply_text("Напишите /shtraf внутри темы «Штраф» (откройте тему и отправьте там).")
    chat, thread = m.chat.id, m.message_thread_id
    whose = [core.name(u) for u, (ch, _) in getattr(config, "PENALTY_CHATS", {}).items() if ch == chat]
    title = getattr(config, "PENALTY_CHAT_TITLE", "") or "Shtraf/Bonus"
    core.save_topic(chat, thread, title)  # теперь эта тема — тема штрафов этой группы
    try:
        await context.bot.edit_forum_topic(chat, thread, name=title)
        res = f"✅ Тема переименована в «{title}»."
    except BadRequest as e:
        res = "✅ Тема назначена для штрафов." if "not modified" in str(e).lower() else (
            f"⚠️ Не смог переименовать тему: {e}\nДайте боту право «Управление темами» и отправьте /shtraf ещё раз.")
    except TelegramError as e:
        res = (f"⚠️ Не смог переименовать тему: {e}\nДайте боту право «Управление темами» "
               "(Администраторы → RADI TEAM) и отправьте /shtraf ещё раз.")
    if whose:
        res += f"\nШтрафы {', '.join(whose)} теперь приходят сюда."
    else:
        res += f"\n❗ Эта группа ещё не привязана к сотруднику. ID группы: {chat} — пришлите его разработчику с именем."
    await m.reply_text(res)


async def learn_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запоминаем темы групп по названию (бот — админ, видит сообщения в темах). Тему «Штраф» в группе
    сотрудника из PENALTY_CHATS — переименовываем в PENALTY_CHAT_TITLE, штрафы идут в неё."""
    m = update.effective_message
    if not m or not m.chat or m.chat.type != "supergroup" or not m.is_topic_message or not m.message_thread_id:
        return
    name = None
    if m.forum_topic_created:
        name = m.forum_topic_created.name
    elif m.forum_topic_edited and m.forum_topic_edited.name:
        name = m.forum_topic_edited.name
    elif m.reply_to_message and m.reply_to_message.forum_topic_created:
        name = m.reply_to_message.forum_topic_created.name
    if not name:
        return
    core.save_topic(m.chat.id, m.message_thread_id, name)
    chats = {ch for ch, _ in getattr(config, "PENALTY_CHATS", {}).values()}
    # переименовываем только «Штраф» (кириллицей); названия «Shtraf…», которые дал руководитель, не трогаем
    if m.chat.id in chats and core.is_penalty_topic(name) and "shtraf" not in name.lower():
        await _rename_topic(context.bot, m.chat.id, m.message_thread_id)


async def biz_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if core.is_admin(uid):
        await update.message.reply_text("🔐 RADI Control: сюда приходят удалённые и изменённые сообщения рабочих аккаунтов.\n"
                                        "/biz — подключённые аккаунты.")
    else:
        await update.message.reply_text("🔐 RADI Control")


async def start_biz_bot() -> None:
    """Отдельный бот контроля рабочих аккаунтов (если задан BIZ_BOT_TOKEN). Сбой не мешает остальным ботам."""
    if not config.BIZ_BOT_TOKEN or BIZ_APP:
        return
    try:
        bapp = Application.builder().token(config.BIZ_BOT_TOKEN).build()
        bapp.add_handler(TypeHandler(Update, bizwatch.on_update), group=-100)
        bapp.add_handler(CommandHandler("start", biz_start))
        bapp.add_handler(CommandHandler("biz", biz_cmd))
        await bapp.initialize()
        await bapp.start()
        await bapp.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        BIZ_APP["app"] = bapp
        print(f"Бот контроля рабочих аккаунтов: @{bapp.bot.username}")
    except Exception as e:  # noqa: BLE001
        logging.error("❌ Бот контроля (BIZ_BOT_TOKEN) не запустился: %s", e)


async def post_init(app: Application) -> None:
    await start_biz_bot()
    await rename_penalty_chats(app.bot)
    core.flag_json("staff_bot", {"username": app.bot.username})
    await set_menu_button(app)
    app.bot_data["outbox"] = asyncio.create_task(outbox_loop(app))


async def client_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ссылка t.me/<бот>?start=channel (кнопка «📢 Kanal» под постом) — сразу личная ссылка-заявка в канал
    if context.args and context.args[0] == "channel" and getattr(config, "CLIENT_CHANNEL_ID", None):
        cl = core.get_client(update.effective_user.id) or {}
        if cl.get("lang"):
            return await channel_link(update, context, i18n.lang_of(cl.get("lang")))
    await client_menu(update, greet=True)


async def client_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await client_menu(update)


def build_client_app() -> Application:
    """Бот клиентов: только клиентская часть (язык, номер, BILLZ, новости, кабинет, «Выйти»)."""
    capp = Application.builder().token(config.CLIENT_BOT_TOKEN).build()
    capp.bot_data["role"] = "client"
    capp.add_handler(TypeHandler(Update, bizwatch.on_update), group=-100)  # бизнес-чаты — бот молчит
    capp.add_handler(TypeHandler(Update, remember_client), group=-2)
    capp.add_handler(TypeHandler(Update, gate), group=-1)
    capp.add_handler(CommandHandler("start", client_start))
    capp.add_handler(CallbackQueryHandler(choose_lang, pattern=r"^lang:"))
    capp.add_handler(ChatJoinRequestHandler(channel_join_request))
    capp.add_handler(CallbackQueryHandler(client_logout, pattern=r"^clogout:(yes|no)$"))
    capp.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.CONTACT, got_contact))
    capp.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, client_text))
    capp.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.TEXT & ~filters.CONTACT & ~filters.COMMAND,
                                    client_other))
    capp.add_error_handler(on_error)
    return capp


async def _run_both(app: Application, capp: Application) -> None:
    """Запустить оба бота в одной программе (общая база, кабинет и очередь сообщений).
    Если бот клиентов не запускается (неверный или отозванный CLIENT_BOT_TOKEN) — бот сотрудников
    и кабинет продолжают работать, а клиентам сообщения идут через бота сотрудников."""
    async with app:
        try:
            await capp.initialize()
            CLIENT_BOT.update(username=capp.bot.username, ok=True)  # до старта бота сотрудников — для ссылок
            core.flag_json("client_bot", {"username": capp.bot.username})  # для кнопки «📢 Kanal» в Постинге
        except Exception as e:  # noqa: BLE001 — любой сбой бота клиентов не должен ронять бота сотрудников
            logging.error("❌ Бот клиентов не запустился (проверьте CLIENT_BOT_TOKEN): %s — работаю одним ботом", e)
            CLIENT_BOT["ok"] = False  # клиентов обслуживает бот сотрудников, как раньше
            capp = None
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await set_menu_button(app)
        if capp:
            await set_menu_button(capp)
            await capp.start()
            await capp.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await start_biz_bot()
        await rename_penalty_chats(app.bot)
        outbox = asyncio.create_task(outbox_loop(app, capp))
        core.flag_json("staff_bot", {"username": app.bot.username})  # для ссылки входа в приложение
        print(f"Боты запущены: сотрудники @{app.bot.username}, клиенты @{CLIENT_BOT['username'] or '— (не запущен)'}")
        try:
            await asyncio.Event().wait()
        finally:
            outbox.cancel()
            if capp:
                await capp.updater.stop()
                await capp.stop()
                await capp.shutdown()
            await app.updater.stop()
            await app.stop()


def main() -> None:
    core.init_db()
    if core.flag_once("kb_cabinet_only_v1"):  # один раз: сотрудникам — новая клавиатура (только кабинет и язык)
        with core.db() as c:
            for uid in config.EMPLOYEES:
                c.execute("INSERT INTO outbox(chat_id, text, markup, via) VALUES(?, ?, 'staff_kb', 'staff')",
                          (uid, st.s(core.staff_lang(uid), "kb_cabinet_only")))
    two_bots = client_bot_on()
    builder = Application.builder().token(config.BOT_TOKEN)
    app = (builder if two_bots else builder.post_init(post_init)).build()
    app.bot_data["role"] = "staff"
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & _WaitingReport(), invest_report_photo))
    app.add_handler(MessageHandler(
        (filters.ChatType.GROUPS | filters.ChatType.CHANNEL) & ~filters.StatusUpdate.ALL & ~filters.COMMAND,
        relay.on_group_message,
    ))
    app.add_handler(TypeHandler(Update, bizwatch.on_update), group=-100)  # бизнес-чаты — только запись, бот молчит
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(TypeHandler(Update, learn_topics), group=-50)  # темы групп (штрафы — в тему «Штраф»)
    app.add_handler(ChatMemberHandler(bot_added, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("biz", biz_cmd))
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(CommandHandler("shtraf", shtraf_topic_cmd))
    app.add_handler(CommandHandler("billz", billz_cmd))
    app.add_handler(CommandHandler("investchat", invest_chat_cmd))
    app.add_handler(CommandHandler("task", task_cmd))
    app.add_handler(CommandHandler("fine", fine_cmd))
    app.add_handler(CallbackQueryHandler(task_button, pattern=r"^(done|task):"))
    app.add_handler(CallbackQueryHandler(choose_lang, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(client_logout, pattern=r"^clogout:(yes|no)$"))
    app.add_handler(CallbackQueryHandler(choose_staff_lang, pattern=r"^slang:(uz|ru)$"))
    app.add_handler(CallbackQueryHandler(invest_button, pattern=r"^(inv(_early)?:(ok|no):\d+|inv_lvl:\d{4}-\d{2}:\w+|inv_pay:\d+:(online|offline)|inv_acc:(ok|no):\d+)$"))
    # кнопки сотрудника — на любом из языков (uz / ru)
    app.add_handler(MessageHandler(filters.Text(st.variants("btn_in")), came))
    app.add_handler(MessageHandler(filters.Text(st.variants("btn_out")), left))
    app.add_handler(MessageHandler(filters.Text(st.variants("btn_tasks")), my_tasks))
    app.add_handler(MessageHandler(filters.Text([BTN_TEAM]), team))
    app.add_handler(MessageHandler(filters.Text(list(dict.fromkeys([BTN_INVEST] + st.variants("btn_invest")))), invest))
    app.add_handler(MessageHandler(filters.Text(st.variants("btn_lang")), staff_lang_cmd))
    app.add_handler(MessageHandler(filters.Text(list(dict.fromkeys([BTN_CABINET] + st.variants("btn_cabinet")))), open_cabinet))
    app.add_handler(MessageHandler(filters.Text(st.variants("btn_shop")), open_shop))
    app.add_handler(CommandHandler("app", send_app))
    app.add_handler(CommandHandler("news", news_cmd))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.CONTACT, got_contact))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.VIDEO_NOTE, got_video_note))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO), other_media_while_proof))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, other))
    app.add_error_handler(on_error)
    if two_bots:
        asyncio.run(_run_both(app, build_client_app()))
        return
    print("Бот запущен. Остановить: Ctrl+C")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
