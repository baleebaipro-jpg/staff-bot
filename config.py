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

# Сотрудники.
#   name         — имя
#   role         — должность
#   start / end  — начало и конец рабочего дня
#   days         — рабочие дни: 0=Пн 1=Вт 2=Ср 3=Чт 4=Пт 5=Сб 6=Вс
#   hours_before — сколько часов уже наработано до начала учёта
EMPLOYEES = {
    9211646422: {"name": "Silver manager", "role": "Менеджер", "start": "09:00", "end": "18:00", "days": [0, 1, 2, 3, 4], "hours_before": 0},
}
