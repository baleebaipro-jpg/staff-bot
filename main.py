"""Запуск для Railway: кабинет (сайт) и бот в одной программе.

Кабинет работает всегда. Если бот не смог подключиться к Telegram (неверный / сменённый токен, бот заморожен,
нет связи) — программа НЕ падает: пишет понятную ошибку в лог и пробует снова каждые 30 секунд.
"""
import asyncio
import logging
import os
import threading
import time

from waitress import serve

import bot
import webapp

RETRY_SECONDS = 30


def run_web():
    serve(webapp.app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threads=4)


def run_bot_forever():
    while True:
        asyncio.set_event_loop(asyncio.new_event_loop())  # прошлый цикл run_polling закрывает
        try:
            bot.main()
            logging.warning("Бот остановился — перезапуск через %s с", RETRY_SECONDS)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 — кабинет должен жить, даже если бот не подключился
            name = type(e).__name__
            hint = (" — токен не принят Telegram: скопируйте токен из @BotFather (RADI TEAM → API Token → Copy) "
                    "в Railway → Variables → BOT_TOKEN") if name in ("InvalidToken", "Unauthorized") else ""
            logging.error("❌ Бот не запустился: %s: %s%s. Повтор через %s с (кабинет работает)", name, e, hint, RETRY_SECONDS)
        time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    run_bot_forever()
