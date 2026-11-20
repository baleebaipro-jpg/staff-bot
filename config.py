# ============ НАСТРОЙКИ ============
import os

# Токен бота и адрес мини-аппа задаются в Railway → Variables:
#   BOT_TOKEN   — токен от @BotFather
#   WEBAPP_URL  — адрес сервиса, например https://staff-bot-production.up.railway.app
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://abdullohbe.pythonanywhere.com").strip().rstrip("/")
if not WEBAPP_URL.startswith("https://"):
    WEBAPP_URL = "https://" + WEBAPP_URL.removeprefix("http://")

# Где хранить базу и аватарки (на Railway — подключённый Volume /data)
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

# Часовой пояс (Ташкент = UTC+5). Москва: "Europe/Moscow"
TIMEZONE = "Asia/Tashkent"

# Цель в часах до профессионала
GOAL_HOURS = 10000

# Опоздание меньше стольких минут не считается
GRACE_MINUTES = 5

# Кнопки «Пришёл» и «Ушёл» работают только в окне ± стольких минут
# от начала и конца графика (например 08:45–09:15 и 17:45–18:15)
WINDOW_MINUTES = 15

# Группа для штрафов: добавьте бота в группу, напишите там /chatid
# и вставьте сюда число (оно начинается с минуса). None — штрафы придут вам.
PENALTY_CHAT_ID = -1004495946180

# Если штрафы нужно писать в определённую тему группы — ID темы, иначе None
PENALTY_THREAD_ID = 3

# Сумма штрафа (текст). Оставьте "" если сумма не нужна.
PENALTY_AMOUNT = ""

# Папка, куда бот сохраняет аватарки сотрудников для кабинета
AVATAR_DIR = os.path.join(DATA_DIR, "avatars")

# Руководители (ID в Telegram): видят всю команду, ставят задачи,
# получают уведомления о приходе/уходе
ADMINS = [
    2034048111,  # Abdullohbe
]

# Кому бот отвечает в личных сообщениях. Остальным — полная тишина.
# Чтобы бот снова отвечал сотрудникам, добавьте сюда их ID.
# Пустой список [] — отвечать всем руководителям и сотрудникам из списков.
BOT_ALLOWED_USERS = [
    2034048111,  # Abdullohbe
    7711346965,  # Admin Silver
]

# Сотрудники.
#   name         — имя
#   role         — должность
#   start / end  — начало и конец рабочего дня
#   days         — рабочие дни: 0=Пн 1=Вт 2=Ср 3=Чт 4=Пт 5=Сб 6=Вс
#   hours_before — сколько часов уже наработано до начала учёта
#   Если графика пока нет — "start": None, "end": None (свободный график:
#   отметки в любое время, без опозданий и без автоштрафов)
EMPLOYEES = {
    9211646422: {"name": "Silver manager", "role": "Менеджер", "start": "09:00", "end": "18:00", "days": [0, 1, 2, 3, 4], "hours_before": 0},
    7711346965: {"name": "Admin Silver", "role": "Админ", "start": None, "end": None, "days": [0, 1, 2, 3, 4, 5], "hours_before": 0},
}


# ============ ПЕРЕСЫЛКА ФОТО С ЦЕНАМИ ============
# Фотограф скидывает в тему фото/видео с подписью, например:
#     Probalik yangi Toplam
#     silver 30.000 bronza 45.000
# Бот рассылает альбом в группу Silver и группу Bronza.
# Текст под фото: заголовок (всё кроме цен) + строка цены + footer.
# В Silver, если бронза дороже: "45.000 so'm❌ 30.000 so'm✅ (15.000 so'm foyda)"
# Бот должен быть администратором во всех этих группах.

RELAY_CURRENCY = "so'm"

# Откуда брать: группа фотографа (и тема, если нужна)
RELAY_SOURCE_CHAT_ID = -1001867137670  # Фотограф (тест)
RELAY_SOURCE_THREAD_ID = None

# Простая переотправка: всё из этой темы копируется в группы Silver и Bronza
# без изменений и без «Переслано от …»
RELAY_COPY_CHAT_ID = -1001867137670
RELAY_COPY_THREAD_ID = 35740

# Куда идёт ТОЛЬКО простая переотправка из темы выше
# (фото с ценами туда НЕ отправляются)
RELAY_COPY_ONLY_CHATS = [
    {"title": "Xuping Uzbekistan", "chat_id": -1002645398648, "thread_id": None},
]

# Похвала: после каждого альбома с ценами бот отвечает отправителю.
# {mention} — отметка того, кто отправил. None — выключить.
RELAY_PRAISE = "{mention} Siz to'g'ri yo'ldasiz, bosing jigarr💫❤️"

# Подколка фотографа: если к указанному времени альбомов с ценами
# за день меньше min_albums — бот пишет text в группу фотографа.
# В тексте можно использовать {count} — сколько альбомов было сегодня.
RELAY_TEASE = {
    "min_albums": 3,
    "time": "18:00",
    "days": [0, 1, 2, 3, 4, 5],  # Пн–Сб
    "text": "Hmm Nilufaropa Bo'ldimi? Kuchiz tugadimi?😆 @nilufarradico",
    "chat_id": None,    # None — группа фотографа (RELAY_SOURCE_CHAT_ID)
    "thread_id": None,  # None — тема, где были последние альбомы с ценами
}

# Куда отправлять. В "chats" можно указать несколько групп/каналов.
# footer — текст в самом конце. Бот должен быть админом везде.
# delay_seconds — через сколько секунд отправлять (0 — сразу).
RELAY_TARGETS = {
    "silver": {
        "delay_seconds": 0,  # сразу
        "chats": [
            {"title": "Xuping silver group", "chat_id": -1003935684670, "thread_id": None},
            {"title": "XUPING (Silver Tarif)", "chat_id": -1004354596254, "thread_id": None},
        ],
        "footer": (
            "Zakaz faqat: @xupingSilver\n"
            "Srochniy zakaz busa tel murojat 77-443-77-88☎️\n"
            "\n"
            "Talab/Taklif murojat: @xupingtaklif"
        ),
    },
    "bronza": {
        "delay_seconds": 60,  # Bronza — через 1 минуту после Silver
        "chats": [
            {"title": "Xuping (Bronza) OPTOM", "chat_id": -1001214388881, "thread_id": None},
        ],
        "footer": (
            "Zakaz uchun: @xupinguzadmim\n"
            "Tel: 93-537-20-00☎️"
        ),
    },
}
