# ============ НАСТРОЙКИ ============
import os

# Токен бота и адрес мини-аппа задаются в Railway → Variables:
#   BOT_TOKEN   — токен от @BotFather
#   WEBAPP_URL  — адрес кабинета: https://staff-bot-production-7a47ab13.up.railway.app
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Отдельный бот для клиентов (необязательно). Если задан CLIENT_BOT_TOKEN в Railway:
#   BOT_TOKEN        — бот сотрудников и руководителя (+ группы: цены, штрафы, инвестиции)
#   CLIENT_BOT_TOKEN — бот клиентов (язык, номер, покупки и бонусы BILLZ, новости, кабинет клиента)
# Без него всё работает в одном боте, как раньше.
CLIENT_BOT_TOKEN = os.getenv("CLIENT_BOT_TOKEN", "").strip()
# Отдельный бот для контроля рабочих Telegram-аккаунтов (Telegram Business, необязательно).
# Задан BIZ_BOT_TOKEN — рабочие аккаунты подключают ЕГО («RADI Control»), иначе — бота сотрудников.
BIZ_BOT_TOKEN = os.getenv("BIZ_BOT_TOKEN", "").strip()
# Куда слать уведомления контроля рабочих аккаунтов (удалённые / изменённые сообщения, подключение бота):
# ID группы (бот должен быть в ней) или None — в личку руководителям. BIZ_NOTIFY_THREAD — тема группы или None.
BIZ_NOTIFY_CHAT = -1005897595052  # группа «Bot control»
BIZ_NOTIFY_THREAD = None
# Отдельная группа/тема для конкретного рабочего аккаунта: {Telegram ID владельца аккаунта: (ID группы, ID темы)}
# Продажи из чата в BILLZ (savdo.py): тема, куда менеджеры пишут продажи; руководитель подтверждает «✅ Пробить»
SALE_CHAT_ID = -1001867137670
SALE_THREAD_ID = 727
SALE_PRODUCT_BARCODE = "2000000031842"  # единственный товар, которым пробиваются продажи по свободной цене
SALE_SHOP = "87"  # пока пробиваем только в магазине 87
SALE_ONLY_PAY = "uzum nasiya"  # пока только оплата Uzum Nasiya (остальные — «Hojayin sizsiz Eplomayman!!»)
SALE_BONUS_PCT = 29
SALE_CONFIRMERS = [6658459389]  # ТОЛЬКО этот аккаунт (@xupingleader) нажимает «✅ Пробить / Отмена / Проверить»
SALE_CONFIRM_MENTION = "@xupingleader"  # бонус клиенту от суммы продажи (кешбэк BILLZ)
BIZ_NOTIFY_ROUTES = {
    6658459389: (-1005897595052, 2),  # @xupingleader → «Bot control», тема 2
    2034048111: (-1005897595052, 7),  # аккаунт руководителя → «Bot control», тема 7
    8742801401: (-1005897595052, 308),  # Diyora Sayitjanova → «Bot control», тема 308
}
# Адрес личного кабинета (мини-приложения) — для кнопок «🏠 Личный кабинет / Kabinetim» в обоих ботах.
# Берётся из WEBAPP_URL в Railway; если там пусто или старый адрес PythonAnywhere — этот:
WEBAPP_URL_DEFAULT = "https://staff-bot-production-7a47ab13.up.railway.app"
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
if not WEBAPP_URL or "pythonanywhere" in WEBAPP_URL:
    WEBAPP_URL = WEBAPP_URL_DEFAULT
if not WEBAPP_URL.startswith("https://"):
    WEBAPP_URL = "https://" + WEBAPP_URL.removeprefix("http://")

# Где хранить базу и аватарки (на Railway — подключённый Volume /data)
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

# Часовой пояс (Ташкент = UTC+5). Москва: "Europe/Moscow"
TIMEZONE = "Asia/Tashkent"

# Цель в часах до высшего звания
GOAL_HOURS = 10000

# Звания по часам (цель делится на 4 равные ступени):
#   0–2 499 ч, 2 500–4 999, 5 000–7 499, 7 500–9 999, 10 000+
RANKS = ["Солдат", "Сержант", "Лейтенант", "Полковник", "Генерал"]

# Опоздание меньше стольких минут не считается
GRACE_MINUTES = 5

# Подтверждение прихода кружком (видеосообщением).
# После «Пришёл» бот просит кружок; приход засчитывается только с ним.
PROOF_REQUIRED = True
PROOF_MINUTES = 3        # сколько минут ждать кружок
PROOF_MIN_SECONDS = 3    # минимальная длина кружка
PROOF_CHAT_ID = None     # куда пересылать кружки (None — руководителям в личку)
PROOF_THREAD_ID = None   # тема в группе, если нужно

# Напоминания сотрудникам с графиком: за WINDOW_MINUTES до начала смены — отметить приход,
# за WINDOW_MINUTES до конца — отметить уход (раз в день каждое). False — выключить.
SHIFT_REMINDERS = True

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

# Штраф за опоздание: столько сум за КАЖДЫЙ начатый час опоздания (0 — без суммы).
# Не отметил приход до конца окна — сразу штраф за 1 час; пришёл позже — сумма пересчитывается по факту.
LATE_FINE_PER_HOUR = 50000

# Дни без штрафов за опоздание (бот не работал и т.п.): уже выписанные за этот день отменяются при запуске,
# новые не выписываются. Формат "ГГГГ-ММ-ДД". То же делает кнопка в «Команда» → «Отменить штрафы за опоздание».
NO_LATE_FINE_DAYS = ["2026-11-25"]  # бот был заморожен Telegram

# Вернуть название группам, которые по ошибке назвали «Shtraf/Bonus» (бот меняет, только если группа
# сейчас называется именно так). Если название было другим — поправьте здесь.
PENALTY_GROUP_RESTORE = {
    -1002579578901: "Atchet Omina",
    -1003280534380: "Atchet Moxira",
    -1003886108577: "Atchet Moxinur",
    -1003780512973: "Atchet Nilufar",
    -1003729921591: "Atchet Aziza",
}

# Название, которое бот ставит ТЕМЕ штрафов (тема «Штраф») в группах из PENALTY_CHATS. "" — не переименовывать.
PENALTY_CHAT_TITLE = "Shtraf/Bonus"

# Штрафы отдельных сотрудников — в свою группу/тему: {ID сотрудника: (ID группы, ID темы или None)}.
# Остальные — в PENALTY_CHAT_ID. Бот должен быть участником этой группы.
PENALTY_CHATS = {
    6658459389: (-1002579578901, None),  # Omina Sadikova
    675957862: (-1003280534380, 4),      # Moxira Isxakova — тема «Shtraf / Retro»
    8566968710: (-1003886108577, 157),   # Moxinur Yoldasheva — тема 157
    745848319: (-1003780512973, None),   # Nilufar Tursunbaeva
    6834141071: (-1003729921591, None),  # Aziza Tursunbaeva
}

# Папка, куда бот сохраняет аватарки сотрудников для кабинета
AVATAR_DIR = os.path.join(DATA_DIR, "avatars")

# Руководители (ID в Telegram): видят всю команду, ставят задачи,
# получают уведомления о приходе/уходе
ADMINS = [
    2034048111,  # Abdullohbe
]
# Постинг (кабинет → 📣 Постинг): группы и каналы, куда можно отправлять посты.
# Бот должен быть в них администратором. thread_id — тема группы (или None).
# lang — на каком языке пост уходит в эту группу: "uz", "ru" или "en".
# (Клиенты получают пост на языке, который выбрали в боте, сотрудники — на русском.)
# Закрытый канал для покупателей: кнопка «📢 Канал» в боте клиентов.
# Клиент с покупкой получает личную ссылку-заявку (24 часа), бот сам одобряет его заявку
# и отклоняет чужие. None — кнопки нет. Бот клиентов должен быть админом канала
# с правом «Пригласительные ссылки» (Invite users via link).
CLIENT_CHANNEL_ID = -1001214388881  # Xuping (Bronza) OPTOM
CLIENT_CHANNEL_MIN_PURCHASES = 1  # с какой суммы покупок (сум) открывается канал
CLIENT_CHANNEL_LINK_HOURS = 24

# После каждой покупки в магазине клиенту из бота приходит «спасибо» и новый баланс бонусов.
THANK_AFTER_PURCHASE = True

# Сотрудники, которым открыт раздел «📣 Постинг» (без начисления бонусов — это только руководитель)
POSTERS = [6658459389]  # Omina Sadikova
# Сотрудники, которые могут ставить задачи всем (как руководитель): в кабинете у них блок «Поставить задачу».
TASK_ASSIGNERS = [6834141071]  # Aziza Tursunbaeva

POST_CHATS = [
    {"title": "Xuping silver group", "chat_id": -1003935684670, "thread_id": None, "lang": "uz"},
    {"title": "XUPING (Silver Tarif)", "chat_id": -1004354596254, "thread_id": None, "lang": "uz"},
    {"title": "Xuping (Bronza) OPTOM", "chat_id": -1001214388881, "thread_id": None, "lang": "uz"},
    {"title": "Xuping Uzbekistan", "chat_id": -1002645398648, "thread_id": None, "lang": "uz"},
]

# Повторяющиеся задачи сотрудникам (каждый день / каждую неделю): во сколько утром они появляются
RECURRING_TASKS_TIME = "07:00"

# Личные проекты руководителя (кабинет → 📁 Проекты): во сколько присылать
# в бот список задач на день. None — не присылать.
PROJECTS_DIGEST_TIME = "08:30"

# Имена руководителей — для заявок и сообщений (например, по инвестициям)
ADMIN_NAMES = {
    2034048111: "Abdullohbe",
}

# Статусы клиентов. Статус задаёт руководитель в кабинете; если BILLZ подключён
# и у статуса задан min_purchases (сумма покупок, сум) — бот сам повышает статус,
# когда клиент набирает эту сумму. None — только вручную.
CLIENT_TIERS = {
    "bronza": {"title": "Bronza", "emoji": "🥉", "min_purchases": 0},
    "silver": {"title": "Silver", "emoji": "🥈", "min_purchases": None},
    "gold":   {"title": "Gold",   "emoji": "🥇", "min_purchases": None},
}

# ============ BILLZ (ERP) ============
# Ключ интеграции — в Railway → Variables: BILLZ_SECRET_TOKEN (в коде его нет).
# Без ключа покупки и бонусы клиентов показываются как «скоро».
BILLZ_CACHE_MINUTES = 3       # автообновление открытого кабинета (открытие кабинета, «🔄» и кнопки в боте — всегда свежие)
BILLZ_CREATE_CLIENTS = True   # клиентов из бота, которых нет в BILLZ, заводить в BILLZ (False — не заводить)

# Кому бот отвечает в личных сообщениях.
# Пустой список [] — бот отвечает всем: руководителям и сотрудникам из списков
# открывается их панель, всем остальным — панель клиента.
# Если вписать ID — бот будет молчать всем, кроме них.
BOT_ALLOWED_USERS = []

# ============ ИНВЕСТИЦИИ СОТРУДНИКОВ (по принципам шариата) ============
# Не вклад и не проценты: сотрудник инвестирует капитал в товар компании
# и получает долю прибыли от продаж. В текстах для людей — «инвестиция»,
# «капитал», «доля прибыли»; слова «вклад», «проценты», «ставка» не используются.
# Доступ к инвестициям — по заявке: сотрудник подаёт заявку на участие
# (тема «access»), руководитель открывает доступ.
# Руководитель подтверждает получение денег — с этого дня капитал работает.
# Каждый месяц 30-го числа (в феврале — в последний день) руководитель
# выбирает итог продаж месяца: слабый / средний / хороший.
# Ориентир прибыли за год по уровню (annual) — только для расчёта доли,
# сотрудникам цифры не показываются: за месяц распределяется 1/12 ориентира.
# По окончании срока — капитал + доля прибыли; досрочный выход — только капитал.
# Включён ли раздел. False — у всех «Скоро ⏳», бот не шлёт заявки и напоминания
# (код и данные сохраняются). Чтобы открыть инвестиции — поставьте True.
INVEST_ENABLED = False
INVEST_TERM_MONTHS = 12
INVEST_LEVELS = {
    "weak": {"title": "Слабый",  "emoji": "🟠", "annual": 18.0},   # ориентир прибыли за год
    "mid":  {"title": "Средний", "emoji": "🟡", "annual": 20.5},
    "good": {"title": "Хороший", "emoji": "🟢", "annual": 23.0},
}
INVEST_RATE_DAY = 30          # с какого числа можно оценить месяц
INVEST_RATE_TIME = "10:00"    # во сколько бот напомнит руководителю в этот день
INVEST_MIN = 100_000          # минимальная сумма инвестиции
INVEST_MAX = 100_000_000      # максимальная сумма одной заявки
INVEST_CURRENCY = "сум"
# Группа «Invest Hodimlar» — сюда приходят заявки, выплаты и оценка месяца.
# Бот должен быть в группе (лучше админом). None — всё руководителям в личку.
INVEST_CHAT_ID = -1004362402568
# Темы группы (число из ссылки https://t.me/c/4362402568/<число>)
INVEST_TOPICS = {
    "access": 9,  # заявки сотрудников на участие в инвестициях
    "new":    2,  # Invest — новые инвестиции
    "payout": 3,  # Oplata — выплаты по окончании срока
    "early":  4,  # qaytib olish — досрочный выход (возврат капитала)
    "rating": 1,  # Hisobot — оценка месяца (1 = General)
}


# Виды графиков. Сотруднику указывается "shift": ключ отсюда.
# Руководитель может задать график на каждый день недели в кабинете:
# вкладка «Команда» → нажать на имя сотрудника.
SHIFTS = {
    "free": {"title": "Свободный график", "short": "своб.", "start": None, "end": None},
    "9-18": {"title": "С 9:00 до 18:00", "short": "9–18", "start": "09:00", "end": "18:00"},
    "8-17": {"title": "С 8:00 до 17:00", "short": "8–17", "start": "08:00", "end": "17:00"},
    "half": {"title": "Пол смены 13:00–19:00", "short": "13–19", "start": "13:00", "end": "19:00"},
}

# Сотрудники.
#   name         — имя
#   role         — должность
#   shift        — график из SHIFTS ("free", "9-18", "8-17", "half")
#   days         — рабочие дни: 0=Пн 1=Вт 2=Ср 3=Чт 4=Пт 5=Сб 6=Вс
#   hours_before — сколько часов уже наработано до начала учёта
#   "shift": "free" — свободный график (
#   отметки в любое время, без опозданий и без автоштрафов)
EMPLOYEES = {
    9211646422: {"name": "Silver manager", "role": "Менеджер", "shift": "9-18", "days": [0, 1, 2, 3, 4], "hours_before": 0},
    7711346965: {"name": "Admin Silver", "role": "Админ", "shift": "free", "days": [0, 1, 2, 3, 4, 5], "hours_before": 0},
    6834141071: {"name": "Aziza Tursunbaeva", "role": "Сотрудник", "shift": "free", "days": [0, 1, 2, 3, 4, 5], "hours_before": 0},
    745848319: {"name": "Nilufar Tursunbaeva", "role": "Сотрудник", "shift": "free", "days": [0, 1, 2, 3, 4, 5], "hours_before": 0},
    6658459389: {"name": "Omina Sadikova", "role": "Сотрудник", "shift": "free", "days": [0, 1, 2, 3, 4, 5], "hours_before": 3500},
    8742801401: {"name": "Diyora Sayitjanova", "role": "Сотрудник", "shift": "free", "days": [0, 1, 2, 3, 4, 5], "hours_before": 1700},
    8566968710: {"name": "Moxinur Yoldasheva", "role": "Сотрудник", "shift": "free", "days": [0, 1, 2, 3, 4, 5], "hours_before": 750},
    652665561: {"name": "Javohir Ilhomov", "role": "Сотрудник", "shift": "free", "days": [0, 1, 2, 3, 4, 5], "hours_before": 7000},
    8808451748: {"name": "Komola RADI", "role": "Сотрудник", "shift": "free", "days": [0, 1, 2, 3, 4, 5], "hours_before": 1000},
    8993708822: {"name": "Sitora RADI", "role": "Сотрудник", "shift": "free", "days": [0, 1, 2, 3, 4, 5], "hours_before": 700},
    675957862: {"name": "Moxira Isxakova", "role": "Сотрудник", "shift": "free", "days": [0, 1, 2, 3, 4, 5], "hours_before": 4000},
    730908311: {"name": "Moxinur Manager", "role": "Менеджер", "shift": "free", "days": [0, 1, 2, 3, 4, 5], "hours_before": 200},
    6335687339: {"name": "Dilrabo Freshman", "role": "Сотрудник", "shift": "free", "days": [0, 1, 2, 3, 4, 5], "hours_before": 0},
    8670903983: {"name": "Jumayeva Nargiza", "role": "Сотрудник", "shift": "free", "days": [0, 1, 2, 3, 4, 5], "hours_before": 180},
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
    {"title": "Канал 2154024519", "chat_id": -1002154024519, "thread_id": None},  # t.me/c/2154024519
]

# Похвала: после каждого альбома с ценами бот отвечает отправителю — каждый раз случайная фраза
# из списка (подряд не повторяется). {mention} — отметка того, кто отправил. None — выключить.
RELAY_PRAISE = [
    "{mention} Siz to'g'ri yo'ldasiz, bosing jigarr💫❤️",
    "{mention} Voy, qanday chiroyli suratlar! Qo'lingiz dard ko'rmasin 📸✨",
    "{mention} Har bir kadr — san'at asari! Barakalla 👏💎",
    "{mention} Mana bu professionallik! Mijozlar ko'rib hayratda qoladi 😍",
    "{mention} Rasmlaringizdan nur yog'ilyapti, zo'rsiz! ☀️💛",
    "{mention} Ko'zim quvondi! Davom eting, siz eng zo'risiz 🔥",
    "{mention} Bu suratlarni ko'rib, hammasini sotib olgim keldi 😄🛍",
    "{mention} Ranglar, yorug'lik, kompozitsiya — hammasi joyida! 🎨👌",
    "{mention} Siz bilan ishlash — baxt! Ajoyib kadrlar uchun rahmat 💜",
    "{mention} Bugun ham a'lo darajada! Sizga qoyil qoldim 🙌",
    "{mention} Zargarlik buyumlarini sizdek hech kim ko'rsata olmaydi 💍✨",
    "{mention} Har safar oldingisidan ham chiroyliroq! Ofarin 🌟",
    "{mention} Mana shunaqa rasmlar savdoni ko'taradi! Rahmat sizga 📈❤️",
    "{mention} Kamera sizni yaxshi ko'radi, siz esa kamerani 😍📷",
    "{mention} Juda nafis chiqibdi! Didingizga gap yo'q 💐",
    "{mention} Shu ishni yurakdan qilayotganingiz bilinib turibdi 💗",
    "{mention} Super! Bu kadrlar to'g'ridan-to'g'ri jurnal muqovasiga 📖✨",
    "{mention} Siz — jamoamizning yulduzi! ⭐️ Barakalla",
    "{mention} Mijozlarimiz bu suratlarni ko'rib, albatta buyurtma beradi 🛒💫",
    "{mention} Bitta rasm ming so'zdan afzal — sizniki esa million so'zdan! 😄",
    "{mention} Nur, soya, rang — hammasi mukammal! Usta ekansiz 🎯",
    "{mention} Rahmat! Siz tufayli mahsulotlarimiz yanada chiroyli ko'rinadi 💎",
    "{mention} Ishingizga mehr bilan qaraysiz — bu darrov seziladi 🤍",
    "{mention} Bugungi suratlar — olov! 🔥🔥🔥",
    "{mention} Mana bu darajani saqlang, siz bilan faxrlanamiz 🏆",
    "{mention} Har bir detal shunday aniq — zo'r ish bo'pti! 🔍✨",
    "{mention} Kayfiyatimni ko'tardingiz, juda chiroyli! 🥰",
    "{mention} Siz suratga olsangiz, oddiy narsa ham durdonaga aylanadi 💫",
    "{mention} Charchamang! Bugun ham ajoyib natija 💪📸",
    "{mention} Qoyilmaqom! Shu tarzda davom etamiz, jigar 🚀❤️",
]

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
