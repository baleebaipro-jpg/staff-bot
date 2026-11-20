import asyncio
import logging
import os

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import config
import core
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


def keyboard(uid: int) -> ReplyKeyboardMarkup:
    rows = []
    if core.employee(uid):
        rows.append([BTN_IN, BTN_OUT])
        rows.append([BTN_TASKS])
    if core.is_admin(uid):
        rows.append([BTN_TEAM])
    rows.append([KeyboardButton(BTN_CABINET, web_app=WebAppInfo(url=config.WEBAPP_URL))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def reply(update: Update, text: str, **kwargs) -> None:
    """Ответить; при сбое сети повторить до 5 раз."""
    for attempt in range(5):
        try:
            await update.message.reply_text(text, **kwargs)
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


def known(uid: int) -> bool:
    return bool(core.employee(uid)) or core.is_admin(uid)


async def deny(update: Update) -> None:
    uid = update.effective_user.id
    await reply(update, f"⛔ У вас нет доступа.\nВаш ID: {uid}\nПередайте его руководителю.")


# ---------- команды ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not known(uid):
        return await deny(update)
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
    _, msg = core.check_in(uid)
    await reply(update, msg)
    await save_avatar(context.bot, uid)


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
    lines = [f"{i}. {t['title']}" for i, t in enumerate(open_tasks, 1)]
    await reply(update, "📋 Ваши задачи:\n\n" + "\n".join(lines) + "\n\nОтметить выполнение — в личном кабинете.")


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


async def other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not known(uid):
        return await deny(update)
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
            await relay.daily_check(app.bot)
        except Exception:
            logging.exception("Ошибка при проверке альбомов фотографа")
        try:
            for m in core.pending_outbox():
                try:
                    thread = PENALTY_THREAD_ID if m["chat_id"] == core.PENALTY_CHAT_ID else None
                    await app.bot.send_message(m["chat_id"], m["text"], message_thread_id=thread)
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
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(CommandHandler("task", task_cmd))
    app.add_handler(CommandHandler("fine", fine_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_IN]), came))
    app.add_handler(MessageHandler(filters.Text([BTN_OUT]), left))
    app.add_handler(MessageHandler(filters.Text([BTN_TASKS]), my_tasks))
    app.add_handler(MessageHandler(filters.Text([BTN_TEAM]), team))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, other))
    app.add_error_handler(on_error)
    print("Бот запущен. Остановить: Ctrl+C")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
