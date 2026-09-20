"""Telegram-бот с гороскопами.

Возможности:
  * выбор знака зодиака после /start;
  * гороскоп на сегодня / на завтра;
  * смена знака;
  * ежедневная утренняя рассылка;
  * кнопка со ссылкой на канал.

Тексты гороскопов лежат в horoscopes.json и перечитываются при каждом
обращении, поэтому файл можно редактировать без перезапуска бота.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import telebot
from dotenv import load_dotenv
from telebot import types
from telebot.apihelper import ApiTelegramException

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

# Читаем .env рядом с main.py (запасной вариант — файл с именем _env)
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / "_env")
DB_PATH = BASE_DIR / "users.db"
HOROSCOPES_PATH = BASE_DIR / "horoscopes.json"


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name}. См. файл .env.example")
    return value


BOT_TOKEN = require_env("BOT_TOKEN")
CHANNEL_URL = require_env("CHANNEL_URL")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
# Приводим к виду HH:MM (например, "9:00" -> "09:00")
SEND_TIME = datetime.strptime(os.getenv("SEND_TIME", "09:00"), "%H:%M").strftime("%H:%M")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("horoscope_bot")

class LoggingExceptionHandler(telebot.ExceptionHandler):
    """Пишет полный traceback любой ошибки в обработчиках, чтобы она не терялась."""

    def handle(self, exception):
        log.error("Ошибка в обработчике", exc_info=exception)
        return True


bot = telebot.TeleBot(BOT_TOKEN, exception_handler=LoggingExceptionHandler())

# Ключи должны совпадать с ключами в horoscopes.json
SIGNS = {
    "aries": "♈️ Овен",
    "taurus": "♉️ Телец",
    "gemini": "♊️ Близнецы",
    "cancer": "♋️ Рак",
    "leo": "♌️ Лев",
    "virgo": "♍️ Дева",
    "libra": "♎️ Весы",
    "scorpio": "♏️ Скорпион",
    "sagittarius": "♐️ Стрелец",
    "capricorn": "♑️ Козерог",
    "aquarius": "♒️ Водолей",
    "pisces": "♓️ Рыбы",
}

SIGN_PREFIX = "sign_"

BTN_TODAY = "🔮 На сегодня"
BTN_TOMORROW = "🔮 На завтра"
BTN_CHANGE_SIGN = "⚙️ Изменить знак"
BTN_CHANNEL = "📱 Наш канал"


def norm(text: str | None) -> str:
    """Убирает невидимый селектор эмодзи (U+FE0F) и пробелы по краям."""
    return (text or "").replace("\ufe0f", "").strip()


def is_button(label: str):
    """Фильтр для обработчика: текст сообщения совпадает с текстом кнопки."""
    expected = norm(label)
    return lambda m: norm(m.text) == expected


# ---------------------------------------------------------------------------
# База данных (SQLite)
# ---------------------------------------------------------------------------

@contextmanager
def db():
    """Соединение на одну операцию: коммит при успехе, откат при ошибке, всегда закрывается."""
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, sign TEXT)"
        )


def get_user_sign(user_id: int) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT sign FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else None


def save_user_sign(user_id: int, sign: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, sign) VALUES (?, ?)", (user_id, sign)
        )


def get_all_users() -> list[tuple[int, str]]:
    with db() as conn:
        return conn.execute("SELECT user_id, sign FROM users").fetchall()


def delete_user(user_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------------------
# Тексты гороскопов
# ---------------------------------------------------------------------------

def load_horoscopes() -> dict:
    """Читает horoscopes.json. При ошибке возвращает пустой словарь, а не роняет бота."""
    try:
        return json.loads(HOROSCOPES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.error("Файл %s не найден", HOROSCOPES_PATH)
    except json.JSONDecodeError as e:
        log.error("Ошибка в формате %s: %s", HOROSCOPES_PATH.name, e)
    return {}


def pick_text(data: dict, sign: str, day: str) -> str | None:
    """Достаёт текст для знака и дня ('today' / 'tomorrow'). None, если текста нет."""
    entry = data.get(sign)
    if not isinstance(entry, dict):
        return None
    text = entry.get(day)
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def main_menu() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton(BTN_TODAY),
        types.KeyboardButton(BTN_TOMORROW),
        types.KeyboardButton(BTN_CHANGE_SIGN),
        types.KeyboardButton(BTN_CHANNEL),
    )
    return markup


def sign_menu() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(
        *[
            types.InlineKeyboardButton(text=name, callback_data=f"{SIGN_PREFIX}{code}")
            for code, name in SIGNS.items()
        ]
    )
    return markup


def channel_menu() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(text="Перейти в канал", url=CHANNEL_URL))
    return markup


# ---------------------------------------------------------------------------
# Вспомогательные функции для ответов
# ---------------------------------------------------------------------------

def ask_sign(chat_id: int, text: str = "Выбери свой знак зодиака:") -> None:
    bot.send_message(chat_id, text, reply_markup=sign_menu())


def send_horoscope(chat_id: int, sign: str, day: str, reply_markup=None) -> None:
    text = pick_text(load_horoscopes(), sign, day)
    if text is None:
        bot.send_message(
            chat_id,
            "Гороскоп для вашего знака сейчас обновляется. Загляните чуть позже.",
            reply_markup=reply_markup,
        )
        return
    bot.send_message(chat_id, f"{SIGNS.get(sign, sign)}\n\n{text}", reply_markup=reply_markup)


def show_horoscope(message: types.Message, day: str) -> None:
    sign = get_user_sign(message.from_user.id)
    log.info("Гороскоп (%s) для %s, знак: %s", day, message.from_user.id, sign)
    if sign is None:
        ask_sign(message.chat.id, "Сначала выбери знак зодиака:")
        return
    send_horoscope(message.chat.id, sign, day, main_menu())


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def on_start(message: types.Message) -> None:
    sign = get_user_sign(message.from_user.id)
    log.info("/start от %s (сохранённый знак: %s)", message.from_user.id, sign)
    if sign in SIGNS:
        bot.send_message(
            message.chat.id,
            f"С возвращением! Ваш знак: {SIGNS[sign]}.",
            reply_markup=main_menu(),
        )
    else:
        ask_sign(
            message.chat.id,
            "Привет! Я бот канала. Выбери свой знак зодиака, "
            "чтобы получать персональный гороскоп:",
        )


@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith(SIGN_PREFIX))
def on_sign_selected(call: types.CallbackQuery) -> None:
    sign = call.data[len(SIGN_PREFIX):]
    log.info("Выбор знака от %s: %s", call.from_user.id, sign)
    if sign not in SIGNS:
        bot.answer_callback_query(call.id, "Неизвестный знак")
        return

    try:
        save_user_sign(call.from_user.id, sign)
    except sqlite3.Error:
        log.exception("Не удалось сохранить знак пользователя %s", call.from_user.id)
        bot.answer_callback_query(call.id, "Не удалось сохранить знак, попробуйте ещё раз", show_alert=True)
        return

    bot.answer_callback_query(call.id, f"Выбран знак: {SIGNS[sign]}")

    # Убираем кнопки выбора из старого сообщения, чтобы они не висели в чате
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except ApiTelegramException:
        pass

    # Сразу показываем гороскоп на сегодня
    send_horoscope(call.message.chat.id, sign, "today", main_menu())


@bot.message_handler(func=is_button(BTN_TODAY))
def on_today(message: types.Message) -> None:
    show_horoscope(message, "today")


@bot.message_handler(func=is_button(BTN_TOMORROW))
def on_tomorrow(message: types.Message) -> None:
    show_horoscope(message, "tomorrow")


@bot.message_handler(func=is_button(BTN_CHANGE_SIGN))
def on_change_sign(message: types.Message) -> None:
    ask_sign(message.chat.id, "Выбери новый знак зодиака:")


@bot.message_handler(func=is_button(BTN_CHANNEL))
def on_channel(message: types.Message) -> None:
    bot.send_message(
        message.chat.id,
        "Подписывайся на наш канал, там много интересного!",
        reply_markup=channel_menu(),
    )


@bot.message_handler(content_types=["text"])
def on_other_text(message: types.Message) -> None:
    """Любой другой текст. Должен быть зарегистрирован последним."""
    log.info("Необработанный текст от %s: %r", message.from_user.id, message.text)
    if get_user_sign(message.from_user.id) is None:
        ask_sign(message.chat.id, "Сначала выбери знак зодиака:")
    else:
        bot.send_message(message.chat.id, "Пожалуйста, используйте кнопки меню.", reply_markup=main_menu())


# ---------------------------------------------------------------------------
# Утренняя рассылка
# ---------------------------------------------------------------------------

def send_morning_horoscopes() -> None:
    data = load_horoscopes()
    users = get_all_users()
    sent = 0

    for user_id, sign in users:
        text = pick_text(data, sign, "today")
        if text is None:
            continue

        message = f"Доброе утро! Твой гороскоп на сегодня:\n\n{SIGNS.get(sign, sign)}\n\n{text}"
        try:
            bot.send_message(user_id, message)
            sent += 1
        except ApiTelegramException as e:
            if e.error_code == 403:  # пользователь заблокировал бота
                delete_user(user_id)
                log.info("Пользователь %s заблокировал бота, удалён из базы", user_id)
            else:
                log.warning("Не удалось отправить сообщение %s: %s", user_id, e)
        except Exception:
            log.exception("Ошибка при отправке пользователю %s", user_id)

        time.sleep(0.05)  # не превышаем лимиты Telegram (~30 сообщений/сек)

    log.info("Утренняя рассылка завершена: отправлено %s из %s", sent, len(users))


def run_scheduler() -> None:
    """Раз в 20 секунд проверяет время и раз в сутки запускает рассылку."""
    tz = ZoneInfo(TIMEZONE)
    last_sent_date = None

    while True:
        try:
            now = datetime.now(tz)
            if now.strftime("%H:%M") == SEND_TIME and last_sent_date != now.date():
                last_sent_date = now.date()
                send_morning_horoscopes()
        except Exception:
            log.exception("Ошибка в планировщике")
        time.sleep(20)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def main() -> None:
    init_db()

    try:
        me = bot.get_me()  # сразу проверяем, что токен рабочий
        bot.remove_webhook()  # с активным webhook polling не получает сообщений
    except ApiTelegramException as e:
        raise SystemExit(f"Telegram отклонил запрос при запуске: {e}\nПроверьте BOT_TOKEN.")

    threading.Thread(target=run_scheduler, daemon=True, name="scheduler").start()
    log.info("Бот @%s запущен. Рассылка в %s (%s)", me.username, SEND_TIME, TIMEZONE)

    # allowed_updates задаём явно: Telegram помнит список с прошлого запуска,
    # и если там не было callback_query, кнопки выбора знака молча не работают.
    bot.infinity_polling(skip_pending=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()