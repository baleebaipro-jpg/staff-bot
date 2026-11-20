"""Запуск для Railway: кабинет (сайт) и бот в одной программе."""
import os
import threading

from waitress import serve

import bot
import webapp


def run_web():
    serve(webapp.app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threads=4)


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    bot.main()
