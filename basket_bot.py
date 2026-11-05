# -*- coding: utf-8 -*-
"""
Telegram-бот для отслеживания режима «Баскет» в Null's Brawl.
Версия для Render (или любого хостинга с переменными окружения).

Точка отсчёта (якорь) — 20:00 по Москве, в этот момент начинается
первый режим цикла (Пожиратели душ). Полный цикл = 9 режимов по 30 минут
= 270 минут = 4,5 часа, после чего всё повторяется по кругу.

Расписание (внутри цикла, от якоря):
  1. Пожиратели душ  20:00–20:30   (смещение 0 мин)
  2. Зачистка        20:30–21:00   (30 мин)
  3. Кради подарок   21:00–21:30   (60 мин)
  4. Пейнтбол        21:30–22:00   (90 мин)
  5. Аэрбой          22:00–22:30   (120 мин)
  6. Вышибала        22:30–23:00   (150 мин)
  7. Волейбол        23:00–23:30   (180 мин)
  8. Хоккей          23:30–00:00   (210 мин)
  9. Баскет          00:00–00:30   (240 мин)

Баскет смещён на 240 минут от якоря при цикле 270 минут, поэтому
начинается каждые 4,5 часа в: 00:00, 04:30, 09:00, 13:30, 18:00,
22:30, 03:00, 07:30, 12:00, 16:30, 21:00, 01:30, 06:00, 10:30,
15:00, 19:30 — и снова 00:00 (цикл не вкладывается в сутки ровно,
поэтому время плавает по кругу).

Бот считает всё от 20:00 Europe/Moscow, а не от момента запуска,
поэтому перезапуск (в том числе «засыпание» хостинга) ничего не ломает.

Токен и ID чата берутся из переменных окружения:
  BOT_TOKEN, CHAT_ID (задаются на хостинге, НЕ хранятся в коде)
"""

import time
import os
import threading
import http.server

import pytz
import schedule
import telebot
from telebot import types
from datetime import datetime, timedelta

# ======================= НАСТРОЙКИ =======================

# Токен бота из переменных окружения (секрет на хостинге)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# ID чата для напоминаний (секрет на хостинге), например "123456789"
CHAT_ID = int(os.getenv("CHAT_ID", "0"))

# Порт веб-сервера: Render задаёт PORT сам, иначе дефолт 7860
PORT = int(os.getenv("PORT", "7860"))

# ======================= РОТАЦИЯ РЕЖИМОВ =======================

MOSCOW = pytz.timezone("Europe/Moscow")

ANCHOR_HOUR = 20      # якорь: 20:00 по Москве — начало цикла
SLOT_MIN = 30         # длительность одного режима, минут
BASKET_INDEX = 8      # баскет — 9-й по счёту (индексация с нуля)

MODES = [
    "Пожиратели душ",   # 20:00
    "Зачистка",         # 20:30
    "Кради подарок",    # 21:00
    "Пейнтбол",         # 21:30
    "Аэрбой",           # 22:00
    "Вышибала",         # 22:30
    "Волейбол",         # 23:00
    "Хоккей",           # 23:30
    "Баскет",           # 00:00
]

CYCLE_SEC = SLOT_MIN * 60 * len(MODES)              # 270 минут = 16200 сек
BASKET_OFFSET_SEC = BASKET_INDEX * SLOT_MIN * 60    # 240 минут = 14400 сек


# ======================= РАСЧЁТ РЕЖИМА =======================

def get_anchor(now):
    """Вернуть последний прошедший якорь — ближайший задним числом 20:00 по Москве.

    Если сейчас раньше 20:00, якорем считается вчерашний 20:00,
    поэтому расчёт работает в любое время суток.
    """
    anchor = now.replace(hour=ANCHOR_HOUR, minute=0, second=0, microsecond=0)
    if now < anchor:
        anchor = anchor - timedelta(days=1)
    return anchor


def get_state(now=None):
    """Посчитать текущее состояние ротации.

    Возвращает словарь:
      mode            — название текущего режима;
      slot            — номер режима в цикле (0..8);
      pos_sec         — сколько секунд прошло от начала цикла;
      in_basket       — идёт ли сейчас баскет;
      minutes_left    — сколько минут осталось до конца текущего режима;
      to_basket_sec   — секунд до начала ближайшего баскета
                        (0, если баскет уже идёт).
    """
    if now is None:
        now = datetime.now(MOSCOW)

    # Сколько секунд прошло с последнего 20:00
    elapsed = (now - get_anchor(now)).total_seconds()
    # Позиция внутри текущего цикла (по кругу)
    pos_sec = int(elapsed) % CYCLE_SEC

    slot = pos_sec // (SLOT_MIN * 60)
    in_basket = (slot == BASKET_INDEX)

    # Секунд до ближайшего начала баскета
    if in_basket:
        to_basket_sec = 0
    else:
        to_basket_sec = (BASKET_OFFSET_SEC - pos_sec) % CYCLE_SEC

    # Сколько секунд осталось до конца текущего режима
    slot_left_sec = SLOT_MIN * 60 - (pos_sec % (SLOT_MIN * 60))

    return {
        "mode": MODES[slot],
        "slot": slot,
        "pos_sec": pos_sec,
        "in_basket": in_basket,
        "minutes_left": slot_left_sec // 60,
        "to_basket_sec": to_basket_sec,
        "now": now,
    }


def minutes_to_basket_text(state):
    """Строка «до баскета: X минут» с учётом идущего баскета."""
    if state["in_basket"]:
        return "Баскет уже идёт! До конца: {} мин".format(state["minutes_left"])
    return "До баскета: {} мин".format(state["to_basket_sec"] // 60)


# ======================= ТЕЛЕГРАМ-БОТ =======================

bot = telebot.TeleBot(BOT_TOKEN)


def main_keyboard():
    """Inline-клавиатура с одной кнопкой запроса текущего режима."""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "🏀 Какой режим сейчас?", callback_data="current_mode"
    ))
    return kb


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    """Приветствие с inline-кнопкой."""
    state = get_state()
    bot.send_message(
        message.chat.id,
        "Привет! Я слежу за ротацией режимов Null's Brawl.\n"
        "Баскет начинается каждые 4,5 часа (00:00, 04:30, 09:00, 13:30, "
        "18:00, 22:30 и далее по кругу).\n"
        "Сейчас идёт: {}. {}\n\n"
        "Я буду напоминать за 5 минут до баскета и о его начале.".format(
            state["mode"], minutes_to_basket_text(state)
        ),
        reply_markup=main_keyboard(),
    )


@bot.message_handler(func=lambda m: True)
def any_message(message):
    """Любое сообщение — показываем кнопку (на случай утери клавиатуры)."""
    state = get_state()
    bot.send_message(
        message.chat.id,
        "Сейчас идёт: {}. {}".format(
            state["mode"], minutes_to_basket_text(state)
        ),
        reply_markup=main_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "current_mode")
def cb_current_mode(call):
    """Ответ на кнопку «Какой режим сейчас?»."""
    state = get_state()
    if state["in_basket"]:
        text = "Сейчас идёт: Баскет! У тебя {} мин".format(state["minutes_left"])
    else:
        text = "Сейчас идёт: {}. {}".format(
            state["mode"], minutes_to_basket_text(state)
        )
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, text, reply_markup=main_keyboard())


# ======================= НАПОМИНАНИЯ =======================

# Ключи последнего отправленного напоминания — защита от дублей
last_start_key = None   # ключ напоминания «Баскет начался»
last_warn_key = None    # ключ напоминания «за 5 минут»


def notification_job():
    """Проверяется каждую минуту.

    1) Если до баскета осталось ровно 5 минут — предупреждение.
    2) Если баскет только что начался — сообщение о старте.

    Ключи-идентификаторы (время начала конкретного баскета) не дают
    отправить одно и то же напоминание дважды.
    """
    global last_start_key, last_warn_key

    state = get_state()
    now = state["now"]

    # Уникальный ключ конкретного баскета: момент его начала.
    # Формула устойчива: внутри одного баскета ключ не меняется
    # от минуты к минуте, поэтому дубликатов не будет.
    basket_start = now - timedelta(
        seconds=(state["pos_sec"] - BASKET_OFFSET_SEC) % CYCLE_SEC
    )
    key = basket_start.replace(second=0, microsecond=0).isoformat()

    # ⏰ За 5 минут до баскета (окно 0–5 минут, ключ не даёт повторить)
    if (
        not state["in_basket"]
        and 0 < state["to_basket_sec"] <= 5 * 60
        and key != last_warn_key
    ):
        bot.send_message(CHAT_ID, "⏰ Через 5 минут начнётся баскет!")
        last_warn_key = key

    # 🏀 Баскет начался. Шлём только в первые 2 минуты режима,
    # чтобы после перезапуска посреди баскета не приходило
    # устаревшее «У тебя 30 минут».
    minutes_into_basket = (state["pos_sec"] - BASKET_OFFSET_SEC) // 60
    if (
        state["in_basket"]
        and minutes_into_basket <= 2
        and key != last_start_key
    ):
        bot.send_message(CHAT_ID, "🏀 Баскет начался! У тебя 30 минут")
        last_start_key = key


# ======================= ВЕБ-СЕРВЕР (ПИНГ) =======================

def run_health_server():
    """Мини HTTP-сервер на PORT.

    На любой GET отвечаем простой статусной страницей — её удобно
    пинговать (например, через UptimeRobot), чтобы бесплатный
    хостинг не усыплял бота от «неактивности».
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state = get_state()
            body = (
                "Null's Brawl Basket Bot is running.<br>"
                "Now (Moscow): {}<br>Mode: {}<br>{}".format(
                    state["now"].strftime("%d.%m %H:%M"),
                    state["mode"],
                    minutes_to_basket_text(state),
                )
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass  # не захламляем логи запросами пинга

    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.serve_forever()


# ======================= ЗАПУСК 24/7 =======================

def scheduler_loop():
    """Отдельный поток: гоняет schedule каждую секунду,
    проверка напоминаний выполняется раз в минуту."""
    while True:
        schedule.run_pending()
        time.sleep(1)


schedule.every(1).minutes.do(notification_job)

if __name__ == "__main__":
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit(
            "Не заданы секреты! Добавь переменные окружения "
            "BOT_TOKEN и CHAT_ID в настройках хостинга."
        )

    # Веб-сервер для пинга (порт задаёт хостинг через PORT)
    threading.Thread(target=run_health_server, daemon=True).start()
    # Планировщик напоминаний
    threading.Thread(target=scheduler_loop, daemon=True).start()

    print("Бот запущен. Polling 24/7...")
    # infinity_polling сам переподключается при сбоях сети
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
