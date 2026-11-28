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
    "purchases_info": {
        "uz": "🛍 Xaridlaringiz umumiy summasi: {sum}",
        "ru": "🛍 Общая сумма ваших покупок: {sum}",
        "en": "🛍 Your total purchases: {sum}",
    },
    "bonus_info": {
        "uz": "🎁 Bonus (keshbek) balansingiz: {sum}",
        "ru": "🎁 Ваш бонусный (кешбэк) баланс: {sum}",
        "en": "🎁 Your bonus (cashback) balance: {sum}",
    },
    "billz_not_found": {
        "uz": "Sizning raqamingiz hali do'kon bazasida topilmadi. Birinchi xariddan keyin shu raqam bilan bu yerda xaridlar va bonuslar paydo bo'ladi 🛍",
        "ru": "Ваш номер пока не найден в базе магазина. После первой покупки с этим номером здесь появятся покупки и бонусы 🛍",
        "en": "Your number isn't in the store database yet. After your first purchase with this number, purchases and bonuses will appear here 🛍",
    },
    "billz_error": {
        "uz": "Hozircha ma'lumotni olib bo'lmadi, birozdan keyin qayta urinib ko'ring 🙏",
        "ru": "Сейчас не удалось получить данные, попробуйте чуть позже 🙏",
        "en": "Couldn't get the data right now, please try a bit later 🙏",
    },
    "currency": {"uz": "so'm", "ru": "сум", "en": "UZS"},
    "btn_purchases": {"uz": "🛍 Xaridlarim", "ru": "🛍 Мои покупки", "en": "🛍 My purchases"},
    "btn_bonus": {"uz": "🎁 Bonuslar", "ru": "🎁 Бонусы", "en": "🎁 Bonuses"},
    "btn_news": {"uz": "📰 Yangiliklar", "ru": "📰 Новости", "en": "📰 News"},
    "btn_invest": {"uz": "💰 Investitsiyalar", "ru": "💰 Инвестиции", "en": "💰 Investments"},
    "btn_cabinet": {"uz": "🏠 Kabinetim", "ru": "🏠 Мой кабинет", "en": "🏠 My account"},
    "open_cabinet": {"uz": "🏠 Kabinetingizni oching 👇", "ru": "🏠 Откройте ваш кабинет 👇", "en": "🏠 Open your account 👇"},
    "open_cabinet_btn": {"uz": "🏠 Kabinetni ochish", "ru": "🏠 Открыть кабинет", "en": "🏠 Open account"},
    "btn_lang": {"uz": "🌐 Til", "ru": "🌐 Язык", "en": "🌐 Language"},
    "thanks_purchase": {
        "uz": "💜 {name}, xaridingiz uchun katta rahmat!\nBizni tanlaganingizni juda qadrlaymiz ✨ Sizni yana kutib qolamiz 🤗\n\n🛍 Xarid summasi: {sum}\n🎁 Bonuslaringiz: {bonus}",
        "ru": "💜 {name}, спасибо за покупку!\nМы очень ценим, что вы выбираете нас ✨ Ждём вас снова 🤗\n\n🛍 Сумма покупки: {sum}\n🎁 Ваши бонусы: {bonus}",
        "en": "💜 {name}, thank you for your purchase!\nWe truly appreciate you choosing us ✨ We can't wait to see you again 🤗\n\n🛍 Purchase: {sum}\n🎁 Your bonuses: {bonus}",
    },
    "debt_paid": {
        "uz": "✅ Qarz bo'yicha to'lov qabul qilindi: {paid}\n💳 Qolgan qarz: {left}\n\nRahmat! 💜",
        "ru": "✅ Получена оплата по долгу: {paid}\n💳 Остаток долга: {left}\n\nСпасибо! 💜",
        "en": "✅ Debt payment received: {paid}\n💳 Remaining debt: {left}\n\nThank you! 💜",
    },
    "debt_paid_full": {
        "uz": "🎉 Qarz to'liq yopildi! To'lov: {paid}\nRahmat, sizni yana kutamiz 💜",
        "ru": "🎉 Долг полностью погашен! Оплата: {paid}\nСпасибо, ждём вас снова 💜",
        "en": "🎉 Your debt is fully paid! Payment: {paid}\nThank you, see you again 💜",
    },
    "erp_earned": {"uz": "➕ Ballar qo'shildi: {sum}", "ru": "➕ Начислено баллов: {sum}", "en": "➕ Points earned: {sum}"},
    "erp_spent": {"uz": "➖ Ballar bilan to'landi: {sum}", "ru": "➖ Оплачено баллами: {sum}", "en": "➖ Paid with points: {sum}"},
    "erp_frozen": {"uz": "🧊 Bonus {sum} — {date} gacha muzlatilgan", "ru": "🧊 Бонус {sum} — заморожен до {date}",
                   "en": "🧊 Bonus {sum} — frozen until {date}"},
    "erp_released": {"uz": "🔓 Bonusingiz {sum} ochildi va ballaringizga o'tkazildi!\n🎁 Ballar: {bal}",
                     "ru": "🔓 Ваш бонус {sum} разморожен и переведён в баллы!\n🎁 Баллы: {bal}",
                     "en": "🔓 Your bonus {sum} is unlocked and added to your points!\n🎁 Points: {bal}"},
    "btn_debt": {"uz": "💳 Qarz", "ru": "💳 Долг", "en": "💳 Debt"},
    "debt_total": {"uz": "💳 Qarzingiz: {sum}", "ru": "💳 Ваш долг: {sum}", "en": "💳 Your debt: {sum}"},
    "debt_item": {"uz": "• Buyurtma {order}: qoldiq {left}", "ru": "• Заказ {order}: осталось {left}",
                  "en": "• Order {order}: {left} left"},
    "debt_due": {"uz": ", to'lash muddati {date}", "ru": ", погасить до {date}", "en": ", due {date}"},
    "debt_overdue": {"uz": " — ⏰ muddati o'tgan", "ru": " — ⏰ просрочен", "en": " — ⏰ overdue"},
    "debt_footer": {"uz": "To'lov bo'yicha savollar bo'lsa — do'konimizga murojaat qiling 🙏",
                    "ru": "По оплате — обращайтесь в наш магазин 🙏", "en": "For payment questions, please contact our store 🙏"},
    "debt_none": {"uz": "✅ Sizda qarz yo'q", "ru": "✅ Долгов нет", "en": "✅ You have no debt"},
    "btn_channel": {"uz": "📢 Kanal", "ru": "📢 Канал", "en": "📢 Channel"},
    "btn_manager": {"uz": "👤 Menejerim", "ru": "👤 Мой менеджер", "en": "👤 My manager"},
    "manager_locked": {
        "uz": "👤 Shaxsiy menejer — Silver va Gold mijozlari uchun.\nSizning statusingiz: {tier}. Silver statusiga o'tganingizda shaxsiy menejeringiz paydo bo'ladi ✨",
        "ru": "👤 Личный менеджер — для клиентов Silver и Gold.\nВаш статус: {tier}. Как только вы перейдёте на Silver, у вас появится личный менеджер ✨",
        "en": "👤 A personal manager is available for Silver and Gold clients.\nYour status: {tier}. Once you reach Silver, you'll get your own personal manager ✨",
    },
    "manager_none": {
        "uz": "👤 Shaxsiy menejeringiz tez orada tayinlanadi. Biz sizga xabar beramiz 💜",
        "ru": "👤 Ваш личный менеджер скоро будет назначен — мы сообщим 💜",
        "en": "👤 Your personal manager will be assigned soon — we'll let you know 💜",
    },
    "manager_info": {
        "uz": "👤 Shaxsiy menejeringiz: {name}\n\nTanlov, buyurtma, o'lcham va boshqa savollar bo'yicha bemalol yozing 💜",
        "ru": "👤 Ваш личный менеджер: {name}\n\nПишите по подбору, заказам, размерам и любым вопросам 💜",
        "en": "👤 Your personal manager: {name}\n\nFeel free to write about styling, orders, sizes or any questions 💜",
    },
    "manager_btn": {"uz": "✍️ Menejerga yozish", "ru": "✍️ Написать менеджеру", "en": "✍️ Message manager"},
    "manager_assigned": {
        "uz": "👤 Sizga shaxsiy menejer tayinlandi: {name}\nMenyudagi «👤 Menejerim» tugmasi orqali yozishingiz mumkin 💜",
        "ru": "👤 Вам назначен личный менеджер: {name}\nНаписать можно через кнопку «👤 Мой менеджер» в меню 💜",
        "en": "👤 You've been assigned a personal manager: {name}\nYou can message them via «👤 My manager» in the menu 💜",
    },
    "channel_link": {
        "uz": "📢 Bu havola faqat siz uchun — 24 soat amal qiladi.\nTugmani bosing va «Qo'shilish so'rovi»ni yuboring — bot uni darhol tasdiqlaydi ✅",
        "ru": "📢 Эта ссылка — только для вас, действует 24 часа.\nНажмите кнопку и отправьте заявку на вступление — бот сразу её одобрит ✅",
        "en": "📢 This link is just for you and works for 24 hours.\nTap the button and send a join request — the bot will approve it right away ✅",
    },
    "channel_btn": {"uz": "📢 Kanalga qo'shilish", "ru": "📢 Вступить в канал", "en": "📢 Join the channel"},
    "channel_no_purchase": {
        "uz": "📢 Kanal xaridorlarimiz uchun. Birinchi xariddan keyin bu yerda shaxsiy havola olasiz 🛍",
        "ru": "📢 Канал — для наших покупателей. После первой покупки здесь можно будет получить личную ссылку 🛍",
        "en": "📢 The channel is for our customers. After your first purchase you'll get a personal link here 🛍",
    },
    "channel_already": {"uz": "✅ Siz allaqachon kanaldasiz", "ru": "✅ Вы уже в канале", "en": "✅ You're already in the channel"},
    "channel_welcome": {"uz": "🎉 Kanalga xush kelibsiz!", "ru": "🎉 Добро пожаловать в канал!", "en": "🎉 Welcome to the channel!"},
    "channel_error": {
        "uz": "Hozircha havola yaratib bo'lmadi, birozdan keyin urinib ko'ring 🙏",
        "ru": "Сейчас не удалось создать ссылку, попробуйте чуть позже 🙏",
        "en": "Couldn't create a link right now, please try a bit later 🙏",
    },
    "btn_logout": {"uz": "🚪 Chiqish", "ru": "🚪 Выйти", "en": "🚪 Log out"},
    "logout_confirm": {
        "uz": "🚪 Chiqasizmi? Til, raqam va ma'lumotlaringiz o'chiriladi — botni qaytadan boshlaysiz.",
        "ru": "🚪 Выйти? Язык, номер и данные будут сброшены — вы начнёте заново.",
        "en": "🚪 Log out? Your language, number and details will be reset — you'll start over.",
    },
    "logout_yes": {"uz": "✅ Ha, chiqish", "ru": "✅ Да, выйти", "en": "✅ Yes, log out"},
    "logout_no": {"uz": "✕ Yo'q", "ru": "✕ Нет", "en": "✕ No"},
    "logout_done": {"uz": "🚪 Siz chiqdingiz.", "ru": "🚪 Вы вышли.", "en": "🚪 You've logged out."},
    "logout_cancel": {"uz": "Yaxshi, davom etamiz 👌", "ru": "Хорошо, продолжаем 👌", "en": "Okay, let's continue 👌"},
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


def clean_texts(texts) -> dict:
    """{'uz': '...', 'ru': '', 'en': None} → только заполненные языки."""
    if not isinstance(texts, dict):
        return {}
    return {k: str(texts[k]).strip() for k in LANGS if texts.get(k) and str(texts[k]).strip()}  # порядок uz, ru, en


def pick(texts: dict, lang) -> str:
    """Текст на нужном языке; если перевода нет — на основном (uz → ru → en)."""
    texts = texts or {}
    for k in (lang_of(lang), DEFAULT, "ru", "en"):
        if texts.get(k):
            return texts[k]
    return ""


def t(lang, key: str, **kw) -> str:
    text = T[key].get(lang_of(lang)) or T[key][DEFAULT]
    return text.format(**kw) if kw else text


def all_variants(key: str) -> set:
    """Все переводы кнопки — чтобы понять, что нажал клиент на любом языке."""
    return set(T[key].values())
