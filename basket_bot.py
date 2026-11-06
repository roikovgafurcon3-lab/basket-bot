# -*- coding: utf-8 -*-
"""
Telegram-бот для отслеживания режима Баскет в Null's Brawl.
Версия с подписчиками и настройками уведомлений.

Точка отсчёта — 20:00 Europe/Moscow (Пожиратели душ).
Цикл 270 мин (9 режимов по 30 мин). Баскет смещён на 240 мин:
00:00, 04:30, 09:00, 13:30, 18:00, 22:30 и далее по кругу.

Напоминания теперь идут ВСЕМ подписчикам, а не одному CHAT_ID.
Каждый пользователь включает/выключает их сам: /settings или кнопка.
"""

import base64
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta

import pytz
import schedule
import telebot
from telebot import types

# ======================= НАСТРОЙКИ =======================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = int(os.getenv("CHAT_ID", "0") or 0)
PORT = int(os.getenv("PORT", "8080"))

# Для сохранения подписчиков в репозиторий (чтобы не терялись при перезапуске воркера)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "") or os.getenv("GH_TOKEN", "")
GH_REPO = os.getenv("GH_REPO", "") or os.getenv("GITHUB_REPOSITORY", "")

SUBSCRIBERS_FILE = "subscribers.json"

MOSCOW = pytz.timezone("Europe/Moscow")
ANCHOR_HOUR = 20
SLOT_MIN = 30
BASKET_INDEX = 8

MODES = [
    "Пожиратели душ",
    "Зачистка",
    "Кради подарок",
    "Пейнтбол",
    "Аэрбой",
    "Вышибала",
    "Волейбол",
    "Хоккей",
    "Баскет",
]

CYCLE_SEC = SLOT_MIN * 60 * len(MODES)
BASKET_OFFSET_SEC = BASKET_INDEX * SLOT_MIN * 60

# ======================= ПОДПИСЧИКИ =======================

subscribers = {}


def load_subscribers():
    global subscribers
    subscribers = {}
    # 1) Пробуем прочитать из файла в репозитории
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                subscribers = data
    except Exception:
        subscribers = {}
    # 2) Если файла нет или пуст — стартовый подписчик = владелец
    if CHAT_ID and str(CHAT_ID) not in subscribers:
        subscribers[str(CHAT_ID)] = {
            "enabled": True,
            "username": "",
            "first_name": "",
        }


def save_subscribers_local():
    try:
        with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
            json.dump(subscribers, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Не удалось сохранить подписчиков локально: %s" % e)


def push_subscribers_to_github():
    if not GITHUB_TOKEN or not GH_REPO:
        return
    try:
        content = json.dumps(subscribers, ensure_ascii=False, indent=2).encode("utf-8")
        b64 = base64.b64encode(content).decode("ascii")
        url = "https://api.github.com/repos/%s/contents/%s" % (GH_REPO, SUBSCRIBERS_FILE)

        # Узнаём текущий sha, если файл уже есть (иначе 404 — создаём без sha)
        sha = None
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": "token " + GITHUB_TOKEN,
                "Accept": "application/vnd.github+json",
                "User-Agent": "basket-bot",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                info = json.loads(resp.read().decode("utf-8"))
                sha = info.get("sha")
        except Exception:
            sha = None

        payload = {
            "message": "Update subscribers",
            "content": b64,
        }
        if sha:
            payload["sha"] = sha

        req2 = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "token " + GITHUB_TOKEN,
                "Accept": "application/vnd.github+json",
                "User-Agent": "basket-bot",
                "Content-Type": "application/json",
            },
            method="PUT",
        )
        with urllib.request.urlopen(req2, timeout=15) as resp:
            print("Подписчики сохранены в GitHub: %s" % resp.status)
    except Exception as e:
        print("Не удалось сохранить подписчиков в GitHub: %s" % e)


def save_subscribers():
    save_subscribers_local()
    threading.Thread(target=push_subscribers_to_github, daemon=True).start()


def ensure_subscriber(chat):
    cid = str(chat.id)
    if cid not in subscribers:
        subscribers[cid] = {
            "enabled": True,
            "username": (chat.username or ""),
            "first_name": (chat.first_name or ""),
        }
        save_subscribers()
    else:
        # Обновляем ник/имя без изменения enabled
        try:
            if chat.username:
                subscribers[cid]["username"] = chat.username
            if chat.first_name:
                subscribers[cid]["first_name"] = chat.first_name
        except Exception:
            pass
    return subscribers[cid]


def is_enabled(chat_id):
    info = subscribers.get(str(chat_id))
    if info is None:
        return str(chat_id) == str(CHAT_ID)
    return bool(info.get("enabled", True))


def get_enabled_ids():
    ids = []
    for cid, info in subscribers.items():
        try:
            if info.get("enabled", True):
                ids.append(int(cid))
        except Exception:
            continue
    return ids


load_subscribers()

# ======================= РАСЧЁТ РЕЖИМА =======================

def get_anchor(now):
    anchor = now.replace(hour=ANCHOR_HOUR, minute=0, second=0, microsecond=0)
    if now < anchor:
        anchor = anchor - timedelta(days=1)
    return anchor


def get_state(now=None):
    if now is None:
        now = datetime.now(MOSCOW)
    elapsed = (now - get_anchor(now)).total_seconds()
    pos_sec = int(elapsed) % CYCLE_SEC
    slot = pos_sec // (SLOT_MIN * 60)
    in_basket = (slot == BASKET_INDEX)
    if in_basket:
        to_basket_sec = 0
    else:
        to_basket_sec = (BASKET_OFFSET_SEC - pos_sec) % CYCLE_SEC
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
    if state["in_basket"]:
        return "Баскет уже идёт! До конца: {} мин".format(state["minutes_left"])
    return "До баскета: {} мин".format(state["to_basket_sec"] // 60)


# ======================= ТЕЛЕГРАМ-БОТ =======================

bot = telebot.TeleBot(BOT_TOKEN)


def main_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "🏀 Какой режим сейчас?", callback_data="current_mode"
    ))
    kb.add(types.InlineKeyboardButton(
        "⚙️ Настройки", callback_data="settings"
    ))
    return kb


def settings_keyboard(chat_id):
    kb = types.InlineKeyboardMarkup()
    enabled = is_enabled(chat_id)
    kb.add(types.InlineKeyboardButton(
        "🔔 Уведомления: ВКЛ" if enabled else "🔕 Уведомления: ВЫКЛ",
        callback_data="toggle_notify"
    ))
    kb.add(types.InlineKeyboardButton(
        "🏀 Какой режим сейчас?", callback_data="current_mode"
    ))
    kb.add(types.InlineKeyboardButton(
        "⬅️ Назад", callback_data="back_main"
    ))
    return kb


def send_to_all(text):
    ids = get_enabled_ids()
    if not ids:
        print("Нет подписчиков с включёнными уведомлениями")
        return
    for cid in ids:
        try:
            bot.send_message(cid, text)
        except Exception as e:
            print("send to %s failed: %s" % (cid, e))
            try:
                if "blocked" in str(e).lower():
                    if str(cid) in subscribers:
                        subscribers[str(cid)]["enabled"] = False
                        save_subscribers()
            except Exception:
                pass


@bot.message_handler(commands=["start"])
def cmd_start(message):
    info = ensure_subscriber(message.chat)
    state = get_state()
    status = "включены" if info.get("enabled") else "выключены"
    bot.send_message(
        message.chat.id,
        "Привет! Я слежу за режимом «Баскет» в Null's Brawl.\n\n"
        "Сейчас идёт: {}. {}\n\n"
        "Уведомления о баскете для тебя: {}.\n"
        "Чтобы включить/выключить — жми «Настройки» или напиши /settings.".format(
            state["mode"], minutes_to_basket_text(state), status
        ),
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(
        message.chat.id,
        "Команды бота:\n"
        "/start — включить/обновить подписку и показать режим\n"
        "/settings — настройки уведомлений (вкл/выкл кнопкой)\n"
        "/enable — включить напоминания о баскете\n"
        "/disable — выключить напоминания о баскете\n"
        "/status — текущий режим и статус подписки\n"
        "/test — тестовое сообщение только тебе\n"
        "/help — это меню\n\n"
        "Баскет начинается каждые 4,5 часа: 00:00, 04:30, 09:00, 13:30, 18:00, 22:30.",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["settings"])
def cmd_settings(message):
    ensure_subscriber(message.chat)
    enabled = is_enabled(message.chat.id)
    bot.send_message(
        message.chat.id,
        "⚙️ Настройки\n\n"
        "Уведомления о баскете: {}\n\n"
        "Нажми кнопку, чтобы переключить.".format(
            "ВКЛ ✅" if enabled else "ВЫКЛ 🔕"
        ),
        reply_markup=settings_keyboard(message.chat.id),
    )


@bot.message_handler(commands=["enable"])
def cmd_enable(message):
    info = ensure_subscriber(message.chat)
    info["enabled"] = True
    save_subscribers()
    bot.send_message(
        message.chat.id,
        "✅ Уведомления о баскете включены. Буду напоминать за 5 минут и о старте.",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["disable"])
def cmd_disable(message):
    info = ensure_subscriber(message.chat)
    info["enabled"] = False
    save_subscribers()
    bot.send_message(
        message.chat.id,
        "🔕 Уведомления о баскете выключены. Включить — /enable или /settings.",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["status"])
def cmd_status(message):
    info = ensure_subscriber(message.chat)
    state = get_state()
    total = len(subscribers)
    enabled_n = len(get_enabled_ids())
    bot.send_message(
        message.chat.id,
        "Сейчас идёт: {}. {}\n\n"
        "Твои уведомления: {}\n"
        "Подписчиков всего: {} (с включёнными: {})".format(
            state["mode"],
            minutes_to_basket_text(state),
            "ВКЛ" if info.get("enabled") else "ВЫКЛ",
            total,
            enabled_n,
        ),
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["test"])
def cmd_test(message):
    ensure_subscriber(message.chat)
    state = get_state()
    bot.send_message(
        message.chat.id,
        "✅ Тест: бот до тебя доходит.\nСейчас: {}. {}".format(
            state["mode"], minutes_to_basket_text(state)
        ),
        reply_markup=main_keyboard(),
    )


# ======================= АДМИНКА (только владелец) =======================

def is_admin(chat_id):
    return str(chat_id) == str(CHAT_ID)


def admin_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast_help"))
    kb.add(types.InlineKeyboardButton("👥 Подписчики", callback_data="admin_subs"))
    kb.add(types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"))
    return kb


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_admin(message.chat.id):
        bot.send_message(message.chat.id, "⛔ Доступ запрещён. Только владелец.")
        return
    bot.send_message(
        message.chat.id,
        "👑 Админка\n\nВыбери действие:",
        reply_markup=admin_keyboard(),
    )


@bot.message_handler(commands=["subs"])
def cmd_subs(message):
    if not is_admin(message.chat.id):
        bot.send_message(message.chat.id, "⛔ Доступ запрещён.")
        return
    lines = []
    for cid, info in subscribers.items():
        uname = "@" + info.get("username", "") if info.get("username") else "без ника"
        status = "✅" if info.get("enabled") else "🔕"
        lines.append(status + " " + str(cid) + " (" + uname + ")")
    text = "👥 Подписчики (" + str(len(subscribers)) + "):\n" + ("\n".join(lines) if lines else "никого нет")
    bot.send_message(message.chat.id, text, reply_markup=admin_keyboard())


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not is_admin(message.chat.id):
        bot.send_message(message.chat.id, "⛔ Доступ запрещён.")
        return
    state = get_state()
    bot.send_message(
        message.chat.id,
        "📊 Статистика\n\n"
        "Сейчас: {}\n"
        "{}\n\n"
        "Подписчиков всего: {}\n"
        "С включёнными уведомлениями: {}".format(
            state["mode"], minutes_to_basket_text(state),
            len(subscribers), len(get_enabled_ids()),
        ),
        reply_markup=admin_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text and m.text.startswith("/broadcast"))
def cmd_broadcast(message):
    if not is_admin(message.chat.id):
        bot.send_message(message.chat.id, "⛔ Доступ запрещён.")
        return
    text = message.text[len("/broadcast"):].strip()
    if not text:
        bot.send_message(
            message.chat.id,
            "📢 Использование: /broadcast <текст>\n\nПример: /broadcast Всем привет!",
            reply_markup=admin_keyboard(),
        )
        return
    ids = get_enabled_ids()
    ok = 0
    fail = 0
    for cid in ids:
        try:
            bot.send_message(cid, "📢 Рассылка от админа:\n\n" + text)
            ok += 1
        except Exception as e:
            fail += 1
            print("broadcast to %s failed: %s" % (cid, e))
    bot.send_message(
        message.chat.id,
        "✅ Рассылка отправлена: {}/{} (ошибок: {})".format(ok, len(ids), fail),
        reply_markup=admin_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast_help")
def cb_admin_broadcast_help(call):
    if not is_admin(call.message.chat.id):
        bot.answer_callback_query(call.id, text="⛔ Только владелец", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "📢 Рассылка\n\nНапиши команду: /broadcast <текст>\n\nПример: /broadcast Баскет через 30 минут!",
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_subs")
def cb_admin_subs(call):
    if not is_admin(call.message.chat.id):
        bot.answer_callback_query(call.id, text="⛔ Только владелец", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    lines = []
    for cid, info in subscribers.items():
        uname = "@" + info.get("username", "") if info.get("username") else "без ника"
        status = "✅" if info.get("enabled") else "🔕"
        lines.append(status + " " + str(cid) + " (" + uname + ")")
    text = "👥 Подписчики (" + str(len(subscribers)) + "):\n" + ("\n".join(lines) if lines else "никого нет")
    bot.send_message(call.message.chat.id, text, reply_markup=admin_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "admin_stats")
def cb_admin_stats(call):
    if not is_admin(call.message.chat.id):
        bot.answer_callback_query(call.id, text="⛔ Только владелец", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    state = get_state()
    bot.send_message(
        call.message.chat.id,
        "📊 Статистика\n\n"
        "Сейчас: {}\n"
        "{}\n\n"
        "Подписчиков всего: {}\n"
        "С включёнными уведомлениями: {}".format(
            state["mode"], minutes_to_basket_text(state),
            len(subscribers), len(get_enabled_ids()),
        ),
        reply_markup=admin_keyboard(),
    )


@bot.message_handler(func=lambda m: True)
def any_message(message):
    if str(message.chat.id) not in subscribers:
        info = ensure_subscriber(message.chat)
        state = get_state()
        bot.send_message(
            message.chat.id,
            "Сейчас идёт: {}. {}\n\n"
            "Я автоматически включил тебе напоминания о баскете.\n"
            "Выключить можно в /settings.".format(
                state["mode"], minutes_to_basket_text(state)
            ),
            reply_markup=main_keyboard(),
        )
        return
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
    state = get_state()
    if state["in_basket"]:
        text = "Сейчас идёт: Баскет! У тебя {} мин".format(state["minutes_left"])
    else:
        text = "Сейчас идёт: {}. {}".format(
            state["mode"], minutes_to_basket_text(state)
        )
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, text, reply_markup=main_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "settings")
def cb_settings(call):
    ensure_subscriber(call.message.chat)
    enabled = is_enabled(call.message.chat.id)
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "⚙️ Настройки\n\nУведомления о баскете: {}".format(
            "ВКЛ ✅" if enabled else "ВЫКЛ 🔕"
        ),
        reply_markup=settings_keyboard(call.message.chat.id),
    )


@bot.callback_query_handler(func=lambda call: call.data == "toggle_notify")
def cb_toggle(call):
    info = ensure_subscriber(call.message.chat)
    info["enabled"] = not info.get("enabled", True)
    save_subscribers()
    bot.answer_callback_query(
        call.id,
        text="Уведомления включены" if info["enabled"] else "Уведомления выключены",
        show_alert=False,
    )
    bot.send_message(
        call.message.chat.id,
        "⚙️ Настройки\n\nУведомления о баскете: {}".format(
            "ВКЛ ✅" if info["enabled"] else "ВЫКЛ 🔕"
        ),
        reply_markup=settings_keyboard(call.message.chat.id),
    )


@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def cb_back(call):
    bot.answer_callback_query(call.id)
    state = get_state()
    bot.send_message(
        call.message.chat.id,
        "Сейчас идёт: {}. {}".format(
            state["mode"], minutes_to_basket_text(state)
        ),
        reply_markup=main_keyboard(),
    )


# ======================= НАПОМИНАНИЯ (ВСЕМ ПОДПИСЧИКАМ) =======================

last_start_key = None
last_warn_key = None


def notification_job():
    global last_start_key, last_warn_key
    state = get_state()
    now = state["now"]
    basket_start = now - timedelta(
        seconds=(state["pos_sec"] - BASKET_OFFSET_SEC) % CYCLE_SEC
    )
    key = basket_start.replace(second=0, microsecond=0).isoformat()

    if (
        not state["in_basket"]
        and 0 < state["to_basket_sec"] <= 5 * 60
        and key != last_warn_key
    ):
        send_to_all("⏰ Через 5 минут начнётся баскет!")
        last_warn_key = key

    minutes_into_basket = (state["pos_sec"] - BASKET_OFFSET_SEC) // 60
    if (
        state["in_basket"]
        and minutes_into_basket <= 2
        and key != last_start_key
    ):
        send_to_all("🏀 Баскет начался! У тебя 30 минут")
        last_start_key = key


# ======================= ВЕБ-СЕРВЕР (ПИНГ) =======================

import http.server


def run_health_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state = get_state()
            body = (
                "Null's Brawl Basket Bot is running.<br>"
                "Now (Moscow): {}<br>Mode: {}<br>{}<br>"
                "Subscribers: {} (enabled: {})".format(
                    state["now"].strftime("%d.%m %H:%M"),
                    state["mode"],
                    minutes_to_basket_text(state),
                    len(subscribers),
                    len(get_enabled_ids()),
                )
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.serve_forever()


# ======================= ЗАПУСК 24/7 =======================

def scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(1)


schedule.every(1).minutes.do(notification_job)

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN в переменных окружения!")
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    print("Бот запущен. Polling 24/7... подписчиков: %d (вкл: %d)" % (
        len(subscribers), len(get_enabled_ids())))
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
