import asyncio
import hashlib
import hmac
import html as html_mod
import json
import os
import random
import sqlite3
from datetime import datetime
from urllib.parse import unquote

import aiohttp
from aiohttp import web
from aiohttp.web import middleware
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
API_BASE_URL = os.getenv("API_BASE_URL", "")
API_PORT = int(os.getenv("API_PORT", "8069"))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

REPLY_MAP_FILE = "reply_map.json"

reply_map: dict[int, int] = {}
write_mode: set[int] = set()

LLM_BASE_PROMPT = """Ты — Оракул Шкатулки Желаний. Твоя задача — зашифровать желание так, чтобы его нужно было РАЗГАДАТЬ.

ЗАДАЧА: Замаскируй желание — замени ключевые слова на неожиданные образы. Получатель должен подумать, прежде чем поймёт о чём речь.

ПРИМЕР:
Желание: "хочу посмотреть тебе в глаза"
ПЛОХО: "Хочу погрузиться в твои глаза, как искатель сокровищ" — это НЕ загадка, тут прямо написано "глаза"
ХОРОШО: "Мечтаю нырнуть в две кроличьи норы, где вместо часов тикают секреты" — тут надо догадаться что "две кроличьи норы" = глаза

ПРАВИЛА:
- 1-2 предложения, не больше
- НИКОГДА не используй слова из оригинального желания — заменяй их метафорами
- Сохраняй направление: для себя / для другого
- Образы из сказок, мифов, природы, космоса — но неожиданные
- Суть должна угадываться, но не сразу — это загадка, а не пересказ
- ТОЛЬКО фраза, без пояснений, кавычек и вступлений
- Язык: русский"""

LLM_STYLE_PROMPTS = [
    ("""Ты — Оракул Шкатулки Желаний. Дерзкий, горячий, без стеснения.

ЗАДАЧА: Перефразируй желание в пошловато-сексуальную метафору с двусмысленностью и жаром.

ПРАВИЛА:
- 1-2 предложения, не больше
- Сохраняй направление: если автор хочет что-то ДЛЯ СЕБЯ — фраза про автора. Если хочет СДЕЛАТЬ ДРУГОМУ — фраза про получателя.
- Пиши горячо, с откровенным подтекстом — но через метафору, не в лоб
- Образы: плавящийся шоколад, раскалённый песок, мокрые губы, шёлк на коже, запотевшие окна
- Двусмысленность на максимум — пусть щёки горят при чтении
- Суть желания должна считываться сквозь жар
- ТОЛЬКО фраза, без пояснений, кавычек и вступлений
- Язык: русский""", 20),
]

def get_llm_prompt() -> str:
    """Pick a prompt: base (85%) or random rare style."""
    roll = random.randint(1, 100)
    threshold = 0
    for prompt, weight in LLM_STYLE_PROMPTS:
        threshold += weight
        if roll <= threshold:
            return prompt
    return LLM_BASE_PROMPT


def load_reply_map():
    global reply_map
    try:
        with open(REPLY_MAP_FILE, "r") as f:
            raw = json.load(f)
        reply_map = {int(k): v for k, v in raw.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        reply_map = {}


def save_reply_map():
    with open(REPLY_MAP_FILE, "w") as f:
        json.dump(reply_map, f)


# ==================== DATABASE ====================

DB_FILE = "wishes.db"


def init_db():
    """Create tables if they don't exist."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wishes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_name TEXT,
            original_text TEXT NOT NULL,
            metaphor TEXT,
            source TEXT DEFAULT 'api',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            user_name TEXT,
            first_seen TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def register_user(user_id: int, user_name: str):
    """Register user for daily prompts."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, user_name) VALUES (?, ?)",
        (user_id, user_name),
    )
    conn.commit()
    conn.close()


def get_all_users() -> list[int]:
    """Get all registered user IDs."""
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    return [r[0] for r in rows]


def save_wish(user_id: int | None, user_name: str, original_text: str,
              metaphor: str | None, source: str = "api"):
    """Save a wish to the database."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT INTO wishes (user_id, user_name, original_text, metaphor, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, user_name, original_text, metaphor, source),
    )
    conn.commit()
    conn.close()


# ==================== LLM API ====================

async def call_llm(text: str) -> str | None:
    """Call Gemini API to metaphorically rephrase a wish."""
    if not GEMINI_API_KEY:
        return None

    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = get_llm_prompt()
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=LLM_MODEL,
            contents=f"{prompt}\n\nЖелание: {text}",
        )
        result = response.text
        return result.strip() if result else None
    except Exception as e:
        print(f"LLM call failed: {e}")
        return None


# ==================== TELEGRAM INIT DATA VALIDATION ====================

def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """Validate Telegram WebApp initData and return parsed data."""
    try:
        pairs = [chunk.split("=", 1) for chunk in init_data.split("&") if "=" in chunk]
        parsed = {k: v for k, v in pairs}

        received_hash = parsed.pop("hash", "")
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={unquote(v)}" for k, v in sorted(parsed.items())
        )

        secret_key = hmac.new(
            b"WebAppData", bot_token.encode(), hashlib.sha256
        ).digest()
        computed_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if computed_hash != received_hash:
            return None

        result = {}
        for k, v in parsed.items():
            decoded = unquote(v)
            try:
                result[k] = json.loads(decoded)
            except (json.JSONDecodeError, ValueError):
                result[k] = decoded
        return result
    except Exception:
        return None


# ==================== AIOHTTP WEB SERVER ====================

@middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as e:
            response = e

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Max-Age"] = "3600"
    return response


async def handle_wish(request):
    """API endpoint: receive wish, call LLM, return metaphor, notify admin."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    text = data.get("text", "").strip()
    uid = data.get("uid")

    if not text:
        return web.json_response({"error": "Empty wish"}, status=400)

    if len(text) > 500:
        return web.json_response({"error": "Too long"}, status=400)

    # Extract user info
    user_name = "Аноним"
    try:
        user_id = int(uid) if uid else None
    except (ValueError, TypeError):
        user_id = None

    # Get user name via Bot API
    if user_id:
        try:
            chat = await bot.get_chat(user_id)
            user_name = chat.full_name or chat.username or "Аноним"
        except Exception:
            pass

    # Call LLM
    metaphor = await call_llm(text)
    if metaphor is None:
        return web.json_response({"error": "Oracle unavailable"}, status=503)

    # Save to database
    save_wish(user_id, user_name, text, metaphor, source="api")

    # Send metaphor to user in bot chat
    if user_id:
        try:
            safe_metaphor = html_mod.escape(metaphor)
            await bot.send_message(
                user_id,
                f"🔮 <b>Оракул передал Люту:</b>\n\n"
                f"<i>{safe_metaphor}</i>",
                parse_mode="HTML",
            )
        except Exception as e:
            print(f"Failed to notify user: {e}")

    # Send to admin
    if ADMIN_ID:
        try:
            safe_name = html_mod.escape(user_name)
            safe_metaphor = html_mod.escape(metaphor)
            safe_text = html_mod.escape(text)
            await bot.send_message(
                ADMIN_ID,
                f"🔮 <b>Новое желание из Шкатулки!</b>\n\n"
                f"👤 От: <b>{safe_name}</b>\n\n"
                f"✨ <b>Метафора:</b>\n<i>{safe_metaphor}</i>\n\n"
                f"📝 <b>Оригинал:</b>\n<tg-spoiler>{safe_text}</tg-spoiler>",
                parse_mode="HTML",
            )
        except Exception as e:
            print(f"Failed to notify admin: {e}")

    return web.json_response({"metaphor": metaphor})


def create_app():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_post("/api/wish", handle_wish)
    return app


# ==================== BOT HANDLERS ====================

def get_webapp_url(user_id: int = None):
    """Build webapp URL with API base and user_id parameters."""
    url = WEBAPP_URL
    if API_BASE_URL:
        sep = "&" if "?" in url else "?"
        url += f"{sep}api={API_BASE_URL}"
    if user_id:
        sep = "&" if "?" in url else "?"
        url += f"{sep}uid={user_id}"
    return url


def get_certificate_url():
    """Derive certificate URL from webapp URL."""
    if "index.html" in WEBAPP_URL:
        return WEBAPP_URL.replace("index.html", "certificate.html")
    url = WEBAPP_URL.rstrip("/")
    return url + "/certificate.html"


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    register_user(user.id, user.full_name or user.username or "Аноним")

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="🔮 Шкатулка Желаний",
                    web_app=WebAppInfo(url=get_webapp_url(message.from_user.id)),
                )
            ],
            [
                KeyboardButton(
                    text="🎁 Мой сертификат",
                    web_app=WebAppInfo(url=get_certificate_url()),
                )
            ],
        ],
        resize_keyboard=True,
    )
    await message.answer(
        "✨ <b>Привет!</b>\n\n"
        "Добро пожаловать в <b>Клуб Подпольных Авантюристов</b>.\n\n"
        "🔮 <b>Шкатулка Желаний</b> — нашепчи своё желание,\n"
        "оракул превратит его в загадку и отправит Люту.\n\n"
        "🎁 <b>Сертификат</b> — твой подарок на массаж.\n\n"
        "Выбирай 👇",
        reply_markup=kb,
        parse_mode="HTML",
    )

    if ADMIN_ID:
        user = message.from_user
        name = user.full_name or user.username or "Неизвестный"
        username = f" (@{user.username})" if user.username else ""
        await bot.send_message(
            ADMIN_ID,
            f"👀 <b>{name}</b>{username} запустил(а) бота\n"
            f"ID: <code>{user.id}</code>",
            parse_mode="HTML",
        )


@dp.message(F.web_app_data)
async def on_web_app_data(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
    except (json.JSONDecodeError, AttributeError):
        return

    action = data.get("action")

    if action == "activate":
        # Certificate activation (legacy)
        await message.answer(
            "🎉 <b>Сертификат успешно активирован!</b>\n\n"
            "Теперь выбери удобную дату для массажа 👇",
            reply_markup=build_dates_keyboard(),
            parse_mode="HTML",
        )
        if ADMIN_ID:
            user = message.from_user
            name = user.full_name or user.username or "Неизвестный"
            await bot.send_message(
                ADMIN_ID,
                f"🔔 <b>{name}</b> активировала сертификат на массаж!",
                parse_mode="HTML",
            )

    elif action == "wish":
        # Fallback: wish sent via sendData (no API server available)
        text = data.get("text", "").strip()
        if not text:
            return

        await message.answer(
            "🔮 <b>Оракул получил твоё желание!</b>\n"
            "Зашифровываю...",
            parse_mode="HTML",
        )

        metaphor = await call_llm(text)

        # Save to database
        user = message.from_user
        save_wish(
            user.id,
            user.full_name or user.username or "Аноним",
            text, metaphor, source="sendData",
        )

        if metaphor:
            safe_metaphor = html_mod.escape(metaphor)
            await message.answer(
                f"✨ <b>Оракул говорит:</b>\n\n"
                f"<i>{safe_metaphor}</i>\n\n"
                f"Отправлено Люту!",
                parse_mode="HTML",
            )
            if ADMIN_ID:
                user = message.from_user
                name = html_mod.escape(user.full_name or user.username or "Неизвестная")
                safe_text = html_mod.escape(text)
                sent = await bot.send_message(
                    ADMIN_ID,
                    f"🔮 <b>Новое желание из Шкатулки!</b>\n\n"
                    f"👤 От: <b>{name}</b>\n\n"
                    f"✨ <b>Метафора:</b>\n<i>{safe_metaphor}</i>\n\n"
                    f"📝 <b>Оригинал:</b>\n<tg-spoiler>{safe_text}</tg-spoiler>",
                    parse_mode="HTML",
                )
                reply_map[sent.message_id] = message.from_user.id
                save_reply_map()
        else:
            await message.answer(
                "😔 Оракул сейчас медитирует. Попробуй позже!",
                parse_mode="HTML",
            )


def build_dates_keyboard() -> InlineKeyboardMarkup:
    dates = [
        ("28 февраля", "2026-02-28"),
        ("7 марта", "2026-03-07"),
        ("26 марта (особенно:))", "2026-03-26"),
    ]
    buttons = [
        [InlineKeyboardButton(text=f"📅 {label}", callback_data=f"date:{value}")]
        for label, value in dates
    ]
    buttons.append(
        [InlineKeyboardButton(text="✍️ Написать Люту (он ждёт)", callback_data="date:custom")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data.startswith("date:"))
async def on_date_selected(callback: types.CallbackQuery):
    raw = callback.data.split(":", 1)[1]

    if raw == "custom":
        write_mode.add(callback.from_user.id)
        await callback.message.answer(
            "✍️ <b>Напиши что угодно</b> — Лют получит твоё сообщение!\n\n"
            "(секретная связь)\n\n"
            "<i>Кнопки с датами всё ещё доступны выше ☝️</i>",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    try:
        d = datetime.strptime(raw, "%Y-%m-%d")
        pretty = d.strftime("%d.%m.%Y (%A)")
    except ValueError:
        pretty = raw

    await callback.message.edit_text(
        f"✅ <b>Отлично!</b>\n\n"
        f"Ты записана на <b>{pretty}</b>.\n\n"
        f"Они сошлись. Волна и камень. Стихи и проза, лед и пламень.",
        parse_mode="HTML",
    )

    if ADMIN_ID:
        user = callback.from_user
        name = user.full_name or user.username or "Неизвестный"
        sent = await bot.send_message(
            ADMIN_ID,
            f"📋 <b>{name}</b> выбрала дату массажа: <b>{pretty}</b>",
            parse_mode="HTML",
        )
        reply_map[sent.message_id] = callback.from_user.id
        save_reply_map()

    await callback.answer("Записано!")


@dp.message(Command("send"), F.from_user.id == ADMIN_ID)
async def cmd_send(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Формат: /send <chat_id> [текст]\nИли: /send <chat_id> + реплай на контент")
        return

    try:
        target_chat_id = int(parts[1].split()[0])
    except ValueError:
        await message.reply("Неверный chat_id")
        return

    text_after_id = parts[1].split(maxsplit=1)[1] if len(parts[1].split()) > 1 else None

    if text_after_id:
        sent = await bot.send_message(target_chat_id, text_after_id)
        reply_map[message.message_id] = target_chat_id
        save_reply_map()
        await message.reply(f"✅ Отправлено в чат {target_chat_id}")
    elif message.reply_to_message:
        await message.reply_to_message.copy_to(target_chat_id)
        reply_map[message.message_id] = target_chat_id
        save_reply_map()
        await message.reply(f"✅ Контент отправлен в чат {target_chat_id}")
    else:
        await message.reply("Добавь текст после chat_id или реплайни на сообщение с контентом")


@dp.message(F.reply_to_message, F.from_user.id == ADMIN_ID)
async def on_admin_reply(message: types.Message):
    replied_id = message.reply_to_message.message_id
    user_chat_id = reply_map.get(replied_id)
    if not user_chat_id:
        return

    await message.copy_to(user_chat_id)
    await message.reply("✅ Отправлено!")


@dp.message(Command("prompt"), F.from_user.id == ADMIN_ID)
async def cmd_prompt(message: types.Message):
    """Admin sends a prompt to all users right now."""
    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        await message.reply("Формат: /prompt текст подсказки")
        return

    prompt_text = text[1].strip()
    users = [uid for uid in get_all_users() if uid != ADMIN_ID]
    if not users:
        await message.reply("Нет зарегистрированных юзеров.")
        return

    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid, prompt_text, parse_mode="HTML")
            sent += 1
        except Exception:
            pass
    await message.reply(f"✅ Отправлено {sent} юзерам")


@dp.message(Command("wish"), F.from_user.id == ADMIN_ID)
async def cmd_admin_wish(message: types.Message):
    """Admin sends a wish to a specific user — Oracle encrypts it."""
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Формат: /wish &lt;user_id&gt; текст желания")
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.reply("Неверный user_id")
        return

    wish_text = parts[2].strip()
    await message.reply("🔮 Зашифровываю...")

    metaphor = await call_llm(wish_text)
    if not metaphor:
        await message.reply("😔 Оракул сейчас медитирует.")
        return

    safe_metaphor = html_mod.escape(metaphor)
    try:
        await bot.send_message(
            target_id,
            f"🔮 <b>Оракул передаёт от Люта:</b>\n\n"
            f"<i>{safe_metaphor}</i>",
            parse_mode="HTML",
        )
        await message.reply(
            f"✅ Отправлено!\n\n"
            f"<b>Метафора:</b>\n<i>{safe_metaphor}</i>",
            parse_mode="HTML",
        )
    except Exception as e:
        await message.reply(f"Не удалось отправить: {e}")


@dp.message(F.text, ~F.text.startswith("/"))
@dp.message(~F.content_type.in_({"web_app_data"}), ~F.text)
async def on_user_message(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        return

    user = message.from_user
    name = user.full_name or user.username or "Неизвестный"

    if user.id in write_mode:
        write_mode.discard(user.id)

    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            f"💬 <b>{name}:</b>\n<i>↩️ Ответь реплаем на сообщение ниже — она получит</i>",
            parse_mode="HTML",
        )
        forwarded = await message.forward(ADMIN_ID)
        reply_map[forwarded.message_id] = message.from_user.id
        save_reply_map()

    await message.answer("✅ Сообщение отправлено Люту!")


# ==================== MAIN ====================

async def main():
    load_reply_map()
    init_db()

    # Start aiohttp API server
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    print(f"API server started on 0.0.0.0:{API_PORT}")

    # Start bot polling
    print("Bot started")
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
