"""Тексты для сотрудников на двух языках: uz (узбекский, латиница) и ru (русский).

Сотрудник выбирает язык при первом /start (потом — кнопкой «🌐 Til / Язык»
в боте или в личном кабинете). Пока язык не выбран — русский.
Интерфейс руководителя остаётся на русском.
"""

LANGS = {"uz": "🇺🇿 O'zbekcha", "ru": "🇷🇺 Русский"}
DEFAULT = "ru"

WEEKDAYS = {
    "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    "uz": ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"],
}

RANKS = {
    "ru": ["Солдат", "Сержант", "Лейтенант", "Полковник", "Генерал"],
    "uz": ["Askar", "Serjant", "Leytenant", "Polkovnik", "General"],
}

# Должности из config.py → по-узбекски (если должности нет в списке — показывается как есть)
ROLES_UZ = {"Сотрудник": "Xodim", "Менеджер": "Menejer", "Админ": "Admin", "Продавец": "Sotuvchi",
            "Кассир": "Kassir", "Фотограф": "Fotograf", "Руководитель": "Rahbar"}


def role(title: str, lang=None) -> str:
    return ROLES_UZ.get(title, title) if lang_of(lang) == "uz" else title


S = {
    # ---------- кнопки бота ----------
    "btn_in": {"ru": "✅ Пришёл", "uz": "✅ Keldim"},
    "btn_out": {"ru": "🚪 Ушёл", "uz": "🚪 Ketdim"},
    "btn_tasks": {"ru": "📋 Мои задачи", "uz": "📋 Vazifalarim"},
    "btn_cabinet": {"ru": "🏠 Личный кабинет", "uz": "🏠 Shaxsiy kabinet"},
    "open_cabinet": {"ru": "🏠 Откройте личный кабинет 👇", "uz": "🏠 Shaxsiy kabinetingizni oching 👇"},
    "open_cabinet_btn": {"ru": "🏠 Открыть кабинет", "uz": "🏠 Kabinetni ochish"},
    "btn_invest": {"ru": "💰 Инвестиции", "uz": "💰 Investitsiyalar"},
    "btn_lang": {"ru": "🌐 Til / Язык", "uz": "🌐 Til / Язык"},
    "btn_done_task": {"ru": "Готово", "uz": "Bajarildi"},

    # ---------- бот ----------
    "choose_lang": {"ru": "🌐 Tilni tanlang / Выберите язык", "uz": "🌐 Tilni tanlang / Выберите язык"},
    "lang_set": {"ru": "🇷🇺 Выбран русский язык", "uz": "🇺🇿 O'zbek tili tanlandi"},
    "hello": {"ru": "Здравствуйте, {name}! 👋", "uz": "Assalomu alaykum, {name}! 👋"},
    "help_staff": {
        "ru": "{btn_in} / {btn_out} — отметить приход и уход\n{btn_tasks} — ваши задачи\n",
        "uz": "{btn_in} / {btn_out} — kelish va ketishni belgilash\n{btn_tasks} — vazifalaringiz\n",
    },
    "help_cabinet": {"ru": "{btn} — часы, история, задачи", "uz": "{btn} — soatlar, tarix, vazifalar"},
    "use_buttons": {"ru": "Пользуйтесь кнопками внизу 👇", "uz": "Pastdagi tugmalardan foydalaning 👇"},
    "phone_saved": {"ru": "Номер сохранён ✅", "uz": "Raqam saqlandi ✅"},
    "your_id": {"ru": "Ваш ID: {id}", "uz": "Sizning ID: {id}"},
    "need_circle": {
        "ru": "Нужен именно кружок 📹 (круглое видео), не фото и не обычное видео.",
        "uz": "Aynan dumaloq video 📹 kerak — rasm yoki oddiy video emas.",
    },
    "proof_expired_bot": {
        "ru": "⌛ Время на кружок вышло, приход не засчитан.\nНажмите «{btn}» ещё раз.",
        "uz": "⌛ Dumaloq video uchun vaqt tugadi, kelish hisobga olinmadi.\n«{btn}» tugmasini yana bosing.",
    },
    "no_tasks": {"ru": "У вас нет открытых задач 🎉", "uz": "Ochiq vazifalaringiz yo'q 🎉"},
    "tasks_title": {"ru": "📋 Ваши задачи:", "uz": "📋 Vazifalaringiz:"},
    "task_due": {"ru": "📅 на {d}", "uz": "📅 {d} ga"},
    "task_review": {"ru": "⏳ на проверке", "uz": "⏳ tekshiruvda"},
    "btn_done": {"ru": "✅ Готово: {t}", "uz": "✅ Bajarildi: {t}"},
    "no_access": {"ru": "Нет доступа.", "uz": "Ruxsat yo'q."},
    "invest_soon": {"ru": "💰 Раздел «Инвестиции» скоро появится ⏳",
                    "uz": "💰 «Investitsiyalar» bo'limi tez orada ochiladi ⏳"},
    "only_staff": {"ru": "Только для сотрудников.", "uz": "Faqat xodimlar uchun."},

    # ---------- приход / уход ----------
    "not_staff": {"ru": "Вы не в списке сотрудников.", "uz": "Siz xodimlar ro'yxatida yo'qsiz."},
    "already_in": {"ru": "Вы уже отметили приход сегодня в {t}.", "uz": "Bugun kelganingizni soat {t} da belgilagansiz."},
    "shift_done": {"ru": "Сегодня смена уже завершена.", "uz": "Bugungi smena allaqachon tugagan."},
    "in_window_only": {
        "ru": "⏰ Отметить приход можно только с {a} до {b}.\nСейчас {t}.",
        "uz": "⏰ Kelishni faqat {a} dan {b} gacha belgilash mumkin.\nHozir {t}.",
    },
    "in_ok": {"ru": "✅ Приход отмечен в {t}.", "uz": "✅ Kelish soat {t} da belgilandi."},
    "tasks_today": {
        "ru": "\n📋 Задач на сегодня: {n} — загляните в «{btn}».",
        "uz": "\n📋 Bugungi vazifalar: {n} — «{btn}» bo'limiga qarang.",
    },
    "late": {"ru": "\n⚠️ Опоздание: {m}", "uz": "\n⚠️ Kechikish: {m}"},
    "on_time": {"ru": "\nВы вовремя 👍", "uz": "\nO'z vaqtida keldingiz 👍"},
    "not_in_yet": {"ru": "Сегодня вы ещё не отмечали приход.", "uz": "Bugun hali kelganingizni belgilamagansiz."},
    "out_not_before": {"ru": "⏰ Уйти можно не раньше {a}.\nСейчас {t}.",
                       "uz": "⏰ Soat {a} dan oldin ketish mumkin emas.\nHozir {t}."},
    "out_blocked": {
        "ru": "⛔ Уходить нельзя — есть незакрытые задачи:\n{lines}\n\n"
              "Нажмите «Готово» и дождитесь, пока руководитель примет задачу.",
        "uz": "⛔ Ketish mumkin emas — yopilmagan vazifalar bor:\n{lines}\n\n"
              "«Bajarildi» tugmasini bosing va rahbar vazifani qabul qilishini kuting.",
    },
    "out_ok": {"ru": "🚪 Уход отмечен в {t}.\nСегодня отработано: {w}",
               "uz": "🚪 Ketish soat {t} da belgilandi.\nBugun ishlangan vaqt: {w}"},
    "early": {"ru": "\n⚠️ Раньше графика на {m}", "uz": "\n⚠️ Grafikdan {m} oldin"},

    # ---------- кружок ----------
    "proof_prompt": {
        "ru": "📹 Запишите кружок, чтобы подтвердить приход.\n\n"
              "🔢 Скажите в кружке вслух число: <b>{code}</b>\n\n"
              "Нажмите на 🎤 справа внизу — значок станет 📷. Затем зажмите его и запишите видео "
              "(не короче {min_sec} сек).\n⏳ У вас {minutes} мин.",
        "uz": "📹 Kelganingizni tasdiqlash uchun dumaloq video yozing.\n\n"
              "🔢 Videoda ushbu sonni ovoz chiqarib ayting: <b>{code}</b>\n\n"
              "Pastki o'ng burchakdagi 🎤 belgisini bosing — u 📷 ga aylanadi. Keyin uni bosib turib video yozing "
              "(kamida {min_sec} soniya).\n⏳ Sizda {minutes} daqiqa bor.",
    },
    "proof_first": {"ru": "Сначала нажмите «{btn}», потом запишите кружок.",
                    "uz": "Avval «{btn}» tugmasini bosing, keyin dumaloq video yozing."},
    "proof_expired": {"ru": "⌛ Время на кружок вышло. Нажмите «{btn}» ещё раз.",
                      "uz": "⌛ Dumaloq video uchun vaqt tugadi. «{btn}» tugmasini yana bosing."},
    "proof_forwarded": {"ru": "❌ Пересланный кружок не подходит. Запишите новый.",
                        "uz": "❌ Uzatilgan (forward) video qabul qilinmaydi. Yangisini yozing."},
    "proof_short": {"ru": "❌ Слишком короткий кружок. Запишите не короче {sec} сек.",
                    "uz": "❌ Video juda qisqa. Kamida {sec} soniya yozing."},
    "proof_reused": {"ru": "❌ Этот кружок уже отправлялся раньше. Запишите новый.",
                     "uz": "❌ Bu video avval yuborilgan. Yangisini yozing."},
    "proof_closed": {"ru": "📹 Запишите кружок в чате с ботом — это подтвердит приход",
                     "uz": "📹 Bot bilan chatda dumaloq video yozing — bu kelishingizni tasdiqlaydi"},

    # ---------- штрафы ----------
    "penalty": {"ru": "🔴 Штраф", "uz": "🔴 Jarima"},
    "penalty_cancelled": {"ru": "✅ Штраф отменён", "uz": "✅ Jarima bekor qilindi"},
    "penalty_msg": {"ru": "{title}\n📅 {date}\nПричина: {reason}", "uz": "{title}\n📅 {date}\nSabab: {reason}"},
    "penalty_sum": {"ru": "\n💰 Сумма: {amount}", "uz": "\n💰 Summa: {amount}"},
    "late_reason": {"ru": "не отметил(а) приход до {t} (график {start})",
                    "uz": "kelishni soat {t} gacha belgilamagan (grafik {start})"},
    "late_short": {"ru": "опоздание", "uz": "kechikish"},

    # ---------- задачи ----------
    "task_new": {"ru": "📌 Новая задача:\n{t}", "uz": "📌 Yangi vazifa:\n{t}"},
    "task_not_found": {"ru": "Задача не найдена.", "uz": "Vazifa topilmadi."},
    "task_already_done": {"ru": "Задача уже принята.", "uz": "Vazifa allaqachon qabul qilingan."},
    "task_already_review": {"ru": "Задача уже на проверке ⏳", "uz": "Vazifa allaqachon tekshiruvda ⏳"},
    "self_task_added": {"ru": "✍️ Задача добавлена на сегодня", "uz": "✍️ Vazifa bugunga qo'shildi"},
    "self_task_added_tomorrow": {"ru": "✍️ Задача добавлена на завтра", "uz": "✍️ Vazifa ertaga qo'shildi"},
    "self_task_done": {"ru": "✅ Отлично, задача выполнена!", "uz": "✅ Ajoyib, vazifa bajarildi!"},
    "self_task_deleted": {"ru": "🗑 Задача удалена", "uz": "🗑 Vazifa o'chirildi"},
    "self_task_empty": {"ru": "Напишите задачу", "uz": "Vazifani yozing"},
    "self_task_limit": {"ru": "Не больше {n} своих задач — закройте старые", "uz": "{n} tadan ko'p bo'lmasin — eskilarini yoping"},
    "task_sent": {"ru": "Отправлено руководителю на проверку ⏳", "uz": "Rahbarga tekshirish uchun yuborildi ⏳"},
    "task_moved": {"ru": "📅 Задачу перенесли на {d}:\n{t}", "uz": "📅 Vazifa {d} ga ko'chirildi:\n{t}"},
    "task_accepted": {"ru": "✅ Задача принята:\n{t}", "uz": "✅ Vazifa qabul qilindi:\n{t}"},
    "task_returned": {"ru": "↩️ Задачу вернули на доработку:\n{t}", "uz": "↩️ Vazifa qayta ishlash uchun qaytarildi:\n{t}"},
    "none_left_moved": {"ru": "\nНа сегодня задач больше нет — можно уходить 👋",
                        "uz": "\nBugun boshqa vazifa yo'q — ketishingiz mumkin 👋"},
    "left_today": {"ru": "\nНа сегодня осталось задач: {n}", "uz": "\nBugun qolgan vazifalar: {n}"},
    "none_left": {"ru": "\nВсе задачи закрыты — можно уходить 👋", "uz": "\nBarcha vazifalar yopildi — ketishingiz mumkin 👋"},
    "left": {"ru": "\nОсталось задач: {n}", "uz": "\nQolgan vazifalar: {n}"},

    # ---------- график ----------
    "day_off": {"ru": "Выходной", "uz": "Dam olish kuni"},
    "free_schedule": {"ru": "Свободный график", "uz": "Erkin grafik"},
    "free_short": {"ru": "своб.", "uz": "erkin"},
    "any_time": {"ru": "в любое время", "uz": "istalgan vaqtda"},
    "range": {"ru": "С {a} до {b}", "uz": "{a} dan {b} gacha"},
    "week_updated": {"ru": "🗓 Ваш график на неделю обновлён:", "uz": "🗓 Haftalik grafigingiz yangilandi:"},
    "remind_in": {
        "ru": "⏰ Через {m} мин начало смены ({start}).\nОтметьте приход — «{btn}» — до {until}.",
        "uz": "⏰ {m} daqiqadan keyin smena boshlanadi ({start}).\nKelganingizni belgilang — «{btn}» — soat {until} gacha.",
    },
    "remind_out": {
        "ru": "⏰ Через {m} мин конец смены ({end}).\nНе забудьте отметить уход — «{btn}».",
        "uz": "⏰ {m} daqiqadan keyin smena tugaydi ({end}).\nKetishni belgilashni unutmang — «{btn}».",
    },

    # ---------- кабинет (ответы сервера) ----------
    "photo_updated": {"ru": "Фото обновлено 📸", "uz": "Rasm yangilandi 📸"},
    "photo_bad": {"ru": "Не удалось прочитать фото.", "uz": "Rasmni o'qib bo'lmadi."},
    "photo_big": {"ru": "Фото слишком большое.", "uz": "Rasm juda katta."},
    "photo_jpeg": {"ru": "Нужна картинка в формате JPEG.", "uz": "JPEG formatidagi rasm kerak."},
}


def lang_of(code) -> str:
    return code if code in LANGS else DEFAULT


def s(lang, key: str, **kw) -> str:
    text = S[key].get(lang_of(lang)) or S[key][DEFAULT]
    return text.format(**kw) if kw else text


def variants(key: str) -> list:
    """Все варианты текста (для распознавания нажатых кнопок на любом языке)."""
    return list(dict.fromkeys(S[key].values()))


def fmt_min(minutes, lang=None) -> str:
    h, m = divmod(int(minutes), 60)
    hh, mm = ("soat", "daq") if lang_of(lang) == "uz" else ("ч", "мин")
    if h and m:
        return f"{h} {hh} {m} {mm}"
    return f"{h} {hh}" if h else f"{m} {mm}"
