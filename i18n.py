"""Тексты для клиентов на трёх языках: uz (узбекский), ru (русский), en (английский).
Служебная часть (руководитель и сотрудники) — на русском."""

LANGS = {"uz": "🇺🇿 O'zbekcha", "ru": "🇷🇺 Русский", "en": "🇬🇧 English"}
DEFAULT = "uz"

T = {
    "choose_lang": {
        "uz": "🌐 Tilni tanlang / Выберите язык / Choose language",
        "ru": "🌐 Tilni tanlang / Выберите язык / Choose language",
        "en": "🌐 Tilni tanlang / Выберите язык / Choose language",
    },
    "lang_set": {
        "uz": "🇺🇿 O'zbek tili tanlandi",
        "ru": "🇷🇺 Выбран русский язык",
        "en": "🇬🇧 English selected",
    },
    "greet": {
        "uz": "Assalomu alaykum, {name}! 👋",
        "ru": "Здравствуйте, {name}! 👋",
        "en": "Hello, {name}! 👋",
    },
    "ask_phone": {
        "uz": "Xaridlaringiz va bonuslaringizni ko'rish uchun telefon raqamingizni yuboring — quyidagi tugmani bosing 👇",
        "ru": "Чтобы видеть свои покупки и бонусы, поделитесь номером телефона — нажмите кнопку ниже 👇",
        "en": "To see your purchases and bonuses, please share your phone number — tap the button below 👇",
    },
    "btn_phone": {
        "uz": "📱 Raqamni yuborish",
        "ru": "📱 Поделиться номером",
        "en": "📱 Share phone number",
    },
    "wrong_contact": {
        "uz": "Iltimos, o'z raqamingizni tugma orqali yuboring 👇",
        "ru": "Пожалуйста, отправьте свой номер кнопкой 👇",
        "en": "Please send your own number using the button 👇",
    },
    "found": {
        "uz": "Sizni topdik! ✅",
        "ru": "Мы вас нашли! ✅",
        "en": "We found you! ✅",
    },
    "ask_name": {
        "uz": "Rahmat! ✅\nIsm va familiyangizni yozing (masalan: Dilnoza Karimova):",
        "ru": "Спасибо! ✅\nНапишите имя и фамилию (например: Дилноза Каримова):",
        "en": "Thank you! ✅\nPlease type your first and last name (e.g. Dilnoza Karimova):",
    },
    "bad_name": {
        "uz": "Iltimos, ism va familiyani yozing (ikki so'z).",
        "ru": "Пожалуйста, напишите имя и фамилию (два слова).",
        "en": "Please type your first and last name (two words).",
    },
    "ask_birth": {
        "uz": "Tug'ilgan sanangizni yozing (KK.OO.YYYY, masalan 15.03.1995):",
        "ru": "Напишите дату рождения (ДД.ММ.ГГГГ, например 15.03.1995):",
        "en": "Please type your date of birth (DD.MM.YYYY, e.g. 15.03.1995):",
    },
    "bad_birth": {
        "uz": "Sana noto'g'ri. Namuna: 15.03.1995",
        "ru": "Неверная дата. Пример: 15.03.1995",
        "en": "Invalid date. Example: 15.03.1995",
    },
    "registered": {
        "uz": "Ro'yxatdan o'tdingiz! 🎉",
        "ru": "Регистрация завершена! 🎉",
        "en": "You're all set! 🎉",
    },
    "status_line": {
        "uz": "Statusingiz: {tier}",
        "ru": "Ваш статус: {tier}",
        "en": "Your status: {tier}",
    },
    "menu": {
        "uz": "Bo'limni tanlang 👇",
        "ru": "Выберите раздел 👇",
        "en": "Choose a section 👇",
    },
    "btn_purchases": {"uz": "🛍 Xaridlarim", "ru": "🛍 Мои покупки", "en": "🛍 My purchases"},
    "btn_bonus": {"uz": "🎁 Bonuslar", "ru": "🎁 Бонусы", "en": "🎁 Bonuses"},
    "btn_news": {"uz": "📰 Yangiliklar", "ru": "📰 Новости", "en": "📰 News"},
    "btn_invest": {"uz": "💰 Investitsiyalar", "ru": "💰 Инвестиции", "en": "💰 Investments"},
    "btn_cabinet": {"uz": "🏠 Kabinetim", "ru": "🏠 Мой кабинет", "en": "🏠 My account"},
    "btn_lang": {"uz": "🌐 Til", "ru": "🌐 Язык", "en": "🌐 Language"},
    "soon_purchases": {
        "uz": "🛍 Tez orada bu yerda xaridlaringiz umumiy summasi va tarixi paydo bo'ladi ⏳",
        "ru": "🛍 Скоро здесь будет общая сумма ваших покупок и их история ⏳",
        "en": "🛍 Your total purchases and history will appear here soon ⏳",
    },
    "soon_bonus": {
        "uz": "🎁 Tez orada bu yerda bonuslaringiz paydo bo'ladi ⏳",
        "ru": "🎁 Скоро здесь будут ваши бонусы ⏳",
        "en": "🎁 Your bonuses will appear here soon ⏳",
    },
    "soon_invest": {
        "uz": "💰 «Investitsiyalar» bo'limi tez orada paydo bo'ladi ⏳",
        "ru": "💰 Раздел «Инвестиции» скоро появится ⏳",
        "en": "💰 The Investments section is coming soon ⏳",
    },
    "no_news": {
        "uz": "📰 Hozircha yangiliklar yo'q — tez orada!",
        "ru": "📰 Новостей пока нет — скоро появятся!",
        "en": "📰 No news yet — stay tuned!",
    },
    "news_title": {"uz": "📰 Yangiliklar", "ru": "📰 Новости", "en": "📰 News"},
    "tier_changed": {
        "uz": "🎉 Sizning yangi statusingiz: {tier}",
        "ru": "🎉 Ваш статус клиента: {tier}",
        "en": "🎉 Your new status: {tier}",
    },
}


def lang_of(code) -> str:
    return code if code in LANGS else DEFAULT


def t(lang, key: str, **kw) -> str:
    text = T[key].get(lang_of(lang)) or T[key][DEFAULT]
    return text.format(**kw) if kw else text


def all_variants(key: str) -> set:
    """Все переводы кнопки — чтобы понять, что нажал клиент на любом языке."""
    return set(T[key].values())
