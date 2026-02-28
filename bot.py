import asyncio
import html as html_mod
import json
import os
import random
import sqlite3
from datetime import datetime

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

# Oracle creation/edit state
oracle_create_mode: dict[int, str] = {}   # user_id → "awaiting_name" | "awaiting_description" | "awaiting_edit_description"
oracle_draft: dict[int, dict] = {}         # user_id → {"name": ..., "oracle_id": ...}

ORACLE_LEVELS = {
    1: {"max_uses": 3,    "name": "Новичок"},
    2: {"max_uses": 10,   "name": "Мастер"},
    3: {"max_uses": None, "name": "Великий"},   # безлимит
}

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
    """Pick a prompt: base (80%) or random rare style (20%)."""
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS custom_oracles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # Migrations: add columns if missing
    cursor = conn.execute("PRAGMA table_info(users)")
    user_cols = {row[1] for row in cursor.fetchall()}
    if "can_create_oracle" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN can_create_oracle INTEGER DEFAULT 0")
    if "active_oracle_id" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN active_oracle_id INTEGER DEFAULT NULL")
    if "tasks_completed" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN tasks_completed INTEGER DEFAULT 0")

    cursor = conn.execute("PRAGMA table_info(custom_oracles)")
    oracle_cols = {row[1] for row in cursor.fetchall()}
    if "level" not in oracle_cols:
        conn.execute("ALTER TABLE custom_oracles ADD COLUMN level INTEGER DEFAULT 1")
    if "uses" not in oracle_cols:
        conn.execute("ALTER TABLE custom_oracles ADD COLUMN uses INTEGER DEFAULT 0")

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

async def check_oracle_unlock(user_id: int | None):
    """Check if user reached 3 wishes and unlock oracle creation."""
    if not user_id:
        return
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT can_create_oracle FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row or row[0]:
        conn.close()
        return
    count = conn.execute(
        "SELECT COUNT(*) FROM wishes WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    if count >= 3:
        conn.execute(
            "UPDATE users SET can_create_oracle = 1 WHERE user_id = ?", (user_id,)
        )
        conn.commit()
        conn.close()
        try:
            await bot.send_message(
                user_id,
                "🎉 <b>Ты отправил(а) 3 шифра!</b>\n"
                "Теперь можешь создать своего Оракула — напиши /oracle",
                parse_mode="HTML",
            )
        except Exception:
            pass
    else:
        conn.close()


def check_oracle_limit(user_id: int | None) -> tuple[bool, str | None]:
    """Check if user's active custom oracle has remaining uses.
    Returns (allowed, error_message). Standard oracle is always allowed.
    """
    if not user_id:
        return True, None
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT active_oracle_id FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row or not row[0]:
        conn.close()
        return True, None
    oracle = conn.execute(
        "SELECT name, level, uses FROM custom_oracles WHERE id = ?", (row[0],)
    ).fetchone()
    conn.close()
    if not oracle:
        return True, None
    name, level, uses = oracle
    level_info = ORACLE_LEVELS.get(level, ORACLE_LEVELS[1])
    max_uses = level_info["max_uses"]
    if max_uses is not None and uses >= max_uses:
        safe_name = html_mod.escape(name)
        return False, (
            f"🔒 Оракул «{safe_name}» достиг лимита ({uses}/{max_uses}) на уровне {level}.\n"
            f"Шифр отправлен через стандартного Оракула."
        )
    return True, None


async def increment_oracle_use(user_id: int | None):
    """Increment use counter for user's active oracle and check level-up."""
    if not user_id:
        return
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT active_oracle_id FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row or not row[0]:
        conn.close()
        return
    oracle_id = row[0]
    conn.execute(
        "UPDATE custom_oracles SET uses = uses + 1 WHERE id = ?", (oracle_id,)
    )
    oracle = conn.execute(
        "SELECT name, level, uses FROM custom_oracles WHERE id = ?", (oracle_id,)
    ).fetchone()
    conn.commit()
    if not oracle:
        conn.close()
        return
    name, level, uses = oracle
    safe_name = html_mod.escape(name)
    if level == 1 and uses >= 3:
        # Check if user already has enough tasks for level 3
        user_row = conn.execute(
            "SELECT tasks_completed FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        tasks = user_row[0] if user_row else 0
        new_level = 3 if tasks >= 2 else 2
        conn.execute(
            "UPDATE custom_oracles SET level = ? WHERE id = ?",
            (new_level, oracle_id),
        )
        conn.commit()
        conn.close()
        try:
            if new_level == 3:
                await bot.send_message(
                    user_id,
                    f"⬆️ <b>Оракул «{safe_name}» сразу достиг уровня 3 — Великий!</b>\n"
                    f"Безлимитные запросы!",
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    user_id,
                    f"⬆️ <b>Оракул «{safe_name}» достиг уровня 2 — Мастер!</b>\n"
                    f"Теперь доступно до 10 запросов.",
                    parse_mode="HTML",
                )
        except Exception:
            pass
    else:
        conn.close()


def get_limit_hit_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown when oracle limit is reached."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="↩️ Переключить на стандартного",
            callback_data="oracle_reset_standard",
        )],
        [InlineKeyboardButton(
            text="📋 Мои оракулы",
            callback_data="oracle_list",
        )],
    ])


def get_user_oracle_prompt(user_id: int | None) -> str | None:
    """Get the active custom oracle prompt for a user, or None for default."""
    if not user_id:
        return None
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT active_oracle_id FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row or not row[0]:
        conn.close()
        return None
    oracle_row = conn.execute(
        "SELECT prompt FROM custom_oracles WHERE id = ?", (row[0],)
    ).fetchone()
    conn.close()
    return oracle_row[0] if oracle_row else None


async def call_llm(text: str, user_id: int | None = None) -> str | None:
    """Call Gemini API to metaphorically rephrase a wish."""
    if not GEMINI_API_KEY:
        return None

    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        custom_prompt = get_user_oracle_prompt(user_id)
        prompt = custom_prompt if custom_prompt else get_llm_prompt()
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



async def generate_oracle_prompt(description: str) -> str | None:
    """Use LLM to generate a system prompt from a user description."""
    if not GEMINI_API_KEY:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        meta_prompt = (
            "Ты — генератор системных промптов для Оракула Шкатулки Желаний.\n"
            "Оракул получает желание и должен зашифровать его в метафору-загадку.\n\n"
            "Пользователь хочет Оракула с таким характером:\n"
            f"{description}\n\n"
            "Напиши системный промпт для этого Оракула. Промпт должен:\n"
            "- Описать характер и стиль речи Оракула\n"
            "- Содержать правила: 1-2 предложения, метафоры вместо прямых слов, "
            "сохранять направление (для себя/для другого), только фраза без пояснений, русский язык\n"
            "- Быть готовым к использованию как system prompt\n\n"
            "Ответь ТОЛЬКО текстом промпта, без пояснений."
        )
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=LLM_MODEL,
            contents=meta_prompt,
        )
        result = response.text
        return result.strip() if result else None
    except Exception as e:
        print(f"Generate oracle prompt failed: {e}")
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
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
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

    # Check oracle limit — fallback to standard if exceeded
    allowed, limit_msg = check_oracle_limit(user_id)
    if not allowed:
        if user_id:
            try:
                limit_kb = get_limit_hit_keyboard()
                await bot.send_message(
                    user_id, limit_msg,
                    parse_mode="HTML", reply_markup=limit_kb,
                )
            except Exception:
                pass

    # Call LLM (if limit hit, pass user_id=None to force standard oracle)
    metaphor = await call_llm(text, user_id=user_id if allowed else None)
    if metaphor is None:
        return web.json_response({"error": "Oracle unavailable"}, status=503)

    # Save to database
    save_wish(user_id, user_name, text, metaphor, source="api")

    # Increment oracle use counter + check level-up
    if allowed:
        await increment_oracle_use(user_id)

    # Check oracle unlock
    await check_oracle_unlock(user_id)

    # Send metaphor to user in bot chat
    if user_id:
        try:
            safe_metaphor = html_mod.escape(metaphor)
            await bot.send_message(
                user_id,
                f"🔮 <b>Оракул передал шифр Люту:</b>\n\n"
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


async def handle_oracles(request):
    """API endpoint: return user's oracles and active oracle."""
    uid = request.query.get("uid")
    if not uid:
        return web.json_response({"error": "Missing uid"}, status=400)
    try:
        user_id = int(uid)
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid uid"}, status=400)

    conn = sqlite3.connect(DB_FILE)
    oracles = conn.execute(
        "SELECT id, name, level, uses FROM custom_oracles WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    user_row = conn.execute(
        "SELECT active_oracle_id, can_create_oracle FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    wishes_count = conn.execute(
        "SELECT COUNT(*) FROM wishes WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    conn.close()

    active_id = user_row[0] if user_row and user_row[0] else None
    can_create = bool(user_row[1]) if user_row else False
    oracle_list = []
    for oid, name, level, uses in oracles:
        lvl_info = ORACLE_LEVELS.get(level, ORACLE_LEVELS[1])
        oracle_list.append({
            "id": oid,
            "name": name,
            "level": level,
            "uses": uses,
            "max_uses": lvl_info["max_uses"],
            "level_name": lvl_info["name"],
        })

    return web.json_response({
        "oracles": oracle_list,
        "active_id": active_id,
        "can_create": can_create,
        "wishes_count": wishes_count,
    })


async def handle_oracle_select(request):
    """API endpoint: switch user's active oracle."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    uid = data.get("uid")
    oracle_id = data.get("oracle_id")
    if uid is None:
        return web.json_response({"error": "Missing uid"}, status=400)
    try:
        user_id = int(uid)
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid uid"}, status=400)

    conn = sqlite3.connect(DB_FILE)
    if oracle_id is None or oracle_id == 0:
        conn.execute(
            "UPDATE users SET active_oracle_id = NULL WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        conn.close()
        return web.json_response({"ok": True, "active_id": None})

    try:
        oracle_id = int(oracle_id)
    except (ValueError, TypeError):
        conn.close()
        return web.json_response({"error": "Invalid oracle_id"}, status=400)

    row = conn.execute(
        "SELECT name FROM custom_oracles WHERE id = ? AND user_id = ?",
        (oracle_id, user_id),
    ).fetchone()
    if not row:
        conn.close()
        return web.json_response({"error": "Oracle not found"}, status=404)

    conn.execute(
        "UPDATE users SET active_oracle_id = ? WHERE user_id = ?",
        (oracle_id, user_id),
    )
    conn.commit()
    conn.close()
    return web.json_response({"ok": True, "active_id": oracle_id})


def create_app():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_post("/api/wish", handle_wish)
    app.router.add_get("/api/oracles", handle_oracles)
    app.router.add_post("/api/oracle/select", handle_oracle_select)
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
        "📋 <b>Команды:</b>\n"
        "/help — справка\n"
        "/oracle — управление Оракулами\n\n"
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

        # Check oracle limit
        uid = message.from_user.id
        allowed, limit_msg = check_oracle_limit(uid)
        if not allowed:
            limit_kb = get_limit_hit_keyboard()
            await message.answer(limit_msg, parse_mode="HTML", reply_markup=limit_kb)

        metaphor = await call_llm(text, user_id=uid if allowed else None)

        # Save to database
        user = message.from_user
        save_wish(
            user.id,
            user.full_name or user.username or "Аноним",
            text, metaphor, source="sendData",
        )

        # Increment oracle use counter + check level-up
        if allowed:
            await increment_oracle_use(user.id)

        # Check oracle unlock
        await check_oracle_unlock(user.id)

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
        ("28 марта", "2026-03-28"),
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
    admin_id = message.from_user.id

    # Check admin's oracle limit
    allowed, limit_msg = check_oracle_limit(admin_id)
    if not allowed:
        await message.reply(f"{limit_msg}\nИспользую стандартного.", parse_mode="HTML")

    await message.reply("🔮 Зашифровываю...")

    metaphor = await call_llm(wish_text, user_id=admin_id if allowed else None)
    if not metaphor:
        await message.reply("😔 Оракул сейчас медитирует.")
        return

    # Increment admin's oracle use
    if allowed:
        await increment_oracle_use(admin_id)

    safe_metaphor = html_mod.escape(metaphor)
    try:
        await bot.send_message(
            target_id,
            f"🔮 <b>Оракул передаёт шифр от Люта:</b>\n\n"
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


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Show help — different output for admin vs regular user."""
    text = (
        "🔮 <b>Шкатулка Желаний — помощь</b>\n\n"
        "/help — эта справка\n"
        "/oracle — управление своими Оракулами\n"
    )
    if message.from_user.id == ADMIN_ID:
        text += (
            "\n👑 <b>Команды админа:</b>\n"
            "/wish &lt;user_id&gt; текст — отправить шифр юзеру\n"
            "/prompt текст — отправить подсказку всем\n"
            "/send &lt;chat_id&gt; текст — отправить сообщение\n"
            "/grant &lt;user_id&gt; — дать доступ к созданию Оракула\n"
            "/taskdone &lt;user_id&gt; — засчитать задание юзеру\n"
        )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("grant"), F.from_user.id == ADMIN_ID)
async def cmd_grant(message: types.Message):
    """Admin grants oracle creation access to a user."""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Формат: /grant &lt;user_id&gt;", parse_mode="HTML")
        return
    try:
        target_id = int(parts[1].strip())
    except ValueError:
        await message.reply("Неверный user_id")
        return
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.execute(
        "UPDATE users SET can_create_oracle = 1 WHERE user_id = ?", (target_id,)
    )
    conn.commit()
    if cursor.rowcount == 0:
        conn.close()
        await message.reply("❌ Юзер не найден в базе (не запускал бота)")
        return
    conn.close()
    await message.reply(f"✅ Доступ к созданию Оракула выдан для {target_id}")
    try:
        await bot.send_message(
            target_id,
            "🎉 <b>Тебе открыт доступ к созданию своего Оракула!</b>\n"
            "Напиши /oracle чтобы начать.",
            parse_mode="HTML",
        )
    except Exception:
        pass


@dp.message(Command("taskdone"), F.from_user.id == ADMIN_ID)
async def cmd_taskdone(message: types.Message):
    """Admin confirms a user completed a task. 2 tasks → level 3 for all oracles."""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Формат: /taskdone &lt;user_id&gt;", parse_mode="HTML")
        return
    try:
        target_id = int(parts[1].strip())
    except ValueError:
        await message.reply("Неверный user_id")
        return

    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT tasks_completed FROM users WHERE user_id = ?", (target_id,)
    ).fetchone()
    if not row:
        conn.close()
        await message.reply("❌ Юзер не найден в базе")
        return
    old_count = row[0]
    new_count = old_count + 1
    conn.execute(
        "UPDATE users SET tasks_completed = ? WHERE user_id = ?",
        (new_count, target_id),
    )

    upgraded = 0
    if new_count >= 2:
        cursor = conn.execute(
            "UPDATE custom_oracles SET level = 3 WHERE user_id = ? AND level = 2",
            (target_id,),
        )
        upgraded = cursor.rowcount

    conn.commit()
    conn.close()

    await message.reply(f"✅ Задание засчитано для {target_id} ({new_count}/2)")

    try:
        if new_count >= 2 and old_count < 2 and upgraded > 0:
            await bot.send_message(
                target_id,
                "🏆 <b>Ты выполнил(а) 2 задания!</b>\n"
                "Все твои оракулы достигли уровня 3 — <b>безлимит!</b>",
                parse_mode="HTML",
            )
        elif new_count >= 2 and old_count < 2:
            await bot.send_message(
                target_id,
                "🏆 <b>Ты выполнил(а) 2 задания!</b>\n"
                "Новые оракулы уровня 2 будут автоматически прокачаны до уровня 3.",
                parse_mode="HTML",
            )
        elif new_count < 2:
            await bot.send_message(
                target_id,
                f"🎉 <b>Задание засчитано!</b> ({new_count}/2)\n"
                "Ещё одно — и твои оракулы получат безлимит!",
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                target_id,
                f"🎉 <b>Задание засчитано!</b> ({new_count} выполнено)",
                parse_mode="HTML",
            )
    except Exception:
        pass


def get_oracle_list_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Build inline keyboard with user's oracles + create button."""
    conn = sqlite3.connect(DB_FILE)
    oracles = conn.execute(
        "SELECT id, name, level, uses FROM custom_oracles WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    active_row = conn.execute(
        "SELECT active_oracle_id FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    active_id = active_row[0] if active_row and active_row[0] else None

    buttons = []
    for oid, name, level, uses in oracles:
        marker = " ✅" if oid == active_id else ""
        lvl_info = ORACLE_LEVELS.get(level, ORACLE_LEVELS[1])
        max_u = lvl_info["max_uses"]
        lvl_name = lvl_info["name"]
        progress = f"{uses}/∞" if max_u is None else f"{uses}/{max_u}"
        # Row 1: info + select
        buttons.append([
            InlineKeyboardButton(
                text=f"🔮 {name}{marker} — {lvl_name} {progress}",
                callback_data=f"oracle_select:{oid}",
            ),
        ])
        # Row 2: actions
        buttons.append([
            InlineKeyboardButton(text="👁", callback_data=f"oracle_info:{oid}"),
            InlineKeyboardButton(text="✏️", callback_data=f"oracle_edit:{oid}"),
            InlineKeyboardButton(text="🗑", callback_data=f"oracle_delete:{oid}"),
        ])
    if active_id:
        buttons.append([
            InlineKeyboardButton(
                text="↩️ Стандартный Оракул",
                callback_data="oracle_select:0",
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="➕ Создать нового", callback_data="oracle_create")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(Command("oracle"))
async def cmd_oracle(message: types.Message):
    """Show user's custom oracles."""
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT can_create_oracle FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()

    if not row or (not row[0] and user_id != ADMIN_ID):
        await message.answer(
            "🔒 Создание Оракулов пока недоступно.\n"
            "Отправь 3 шифра через Шкатулку, чтобы разблокировать!"
        )
        return

    kb = get_oracle_list_keyboard(user_id)
    await message.answer(
        "🔮 <b>Твои Оракулы</b>\n\n"
        "Выбери активного или создай нового:\n\n"
        "📊 <b>Прокачка оракулов:</b>\n"
        "▫️ Лвл 1 (Новичок) → макс 3 запроса\n"
        "▫️ Лвл 2 (Мастер) → макс 10 запросов\n"
        "▫️ Лвл 3 (Великий) → безлимит ∞\n\n"
        "⬆️ <b>Как прокачать:</b>\n"
        "• Лвл 1 → 2: используй любого оракула 3 раза\n"
        "• Лвл 2 → 3: выполни 2 задания от Люта",
        reply_markup=kb,
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "oracle_create")
async def on_oracle_create(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT can_create_oracle FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    if not row or (not row[0] and user_id != ADMIN_ID):
        await callback.answer("🔒 Нет доступа", show_alert=True)
        return

    oracle_create_mode[user_id] = "awaiting_name"
    oracle_draft[user_id] = {}
    await callback.message.answer(
        "✍️ <b>Как назвать Оракула?</b>\n\n"
        "Например: Нерд, Поэт, Пират\n\n"
        "<i>Напиши «отмена» чтобы отменить</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("oracle_select:"))
async def on_oracle_select(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    oracle_id = int(callback.data.split(":")[1])
    conn = sqlite3.connect(DB_FILE)
    if oracle_id == 0:
        conn.execute(
            "UPDATE users SET active_oracle_id = NULL WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        conn.close()
        await callback.answer("✅ Стандартный Оракул активирован")
        kb = get_oracle_list_keyboard(user_id)
        try:
            await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        return
    # Verify oracle belongs to user
    row = conn.execute(
        "SELECT name FROM custom_oracles WHERE id = ? AND user_id = ?",
        (oracle_id, user_id),
    ).fetchone()
    if not row:
        conn.close()
        await callback.answer("Оракул не найден", show_alert=True)
        return
    conn.execute(
        "UPDATE users SET active_oracle_id = ? WHERE user_id = ?",
        (oracle_id, user_id),
    )
    conn.commit()
    conn.close()
    await callback.answer(f"✅ Оракул «{row[0]}» активирован")
    kb = get_oracle_list_keyboard(user_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("oracle_delete:"))
async def on_oracle_delete(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    oracle_id = int(callback.data.split(":")[1])
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT name FROM custom_oracles WHERE id = ? AND user_id = ?",
        (oracle_id, user_id),
    ).fetchone()
    conn.close()
    if not row:
        await callback.answer("Оракул не найден", show_alert=True)
        return
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🗑 Да, удалить «{row[0]}»",
            callback_data=f"oracle_confirm_delete:{oracle_id}",
        )],
        [InlineKeyboardButton(
            text="↩️ Отмена",
            callback_data="oracle_cancel_delete",
        )],
    ])
    try:
        await callback.message.edit_reply_markup(reply_markup=confirm_kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("oracle_confirm_delete:"))
async def on_oracle_confirm_delete(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    oracle_id = int(callback.data.split(":")[1])
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT name FROM custom_oracles WHERE id = ? AND user_id = ?",
        (oracle_id, user_id),
    ).fetchone()
    if not row:
        conn.close()
        await callback.answer("Оракул не найден", show_alert=True)
        return
    conn.execute("DELETE FROM custom_oracles WHERE id = ?", (oracle_id,))
    conn.execute(
        "UPDATE users SET active_oracle_id = NULL WHERE user_id = ? AND active_oracle_id = ?",
        (user_id, oracle_id),
    )
    conn.commit()
    conn.close()
    await callback.answer(f"🗑 Оракул «{row[0]}» удалён")
    kb = get_oracle_list_keyboard(user_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


@dp.callback_query(F.data == "oracle_cancel_delete")
async def on_oracle_cancel_delete(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    kb = get_oracle_list_keyboard(user_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    await callback.answer("Отменено")


@dp.callback_query(F.data == "oracle_reset_standard")
async def on_oracle_reset_standard(callback: types.CallbackQuery):
    """Switch to standard oracle from the limit-hit keyboard."""
    user_id = callback.from_user.id
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "UPDATE users SET active_oracle_id = NULL WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()
    try:
        await callback.message.edit_text(
            "✅ Переключено на стандартного Оракула.",
        )
    except Exception:
        pass
    await callback.answer("✅ Стандартный Оракул активирован")


@dp.callback_query(F.data == "oracle_list")
async def on_oracle_list(callback: types.CallbackQuery):
    """Show oracle list from the limit-hit keyboard."""
    user_id = callback.from_user.id
    kb = get_oracle_list_keyboard(user_id)
    await callback.message.answer(
        "🔮 <b>Твои Оракулы</b>\n\nВыбери активного:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("oracle_edit:"))
async def on_oracle_edit(callback: types.CallbackQuery):
    """Enter edit mode for an oracle via inline button."""
    user_id = callback.from_user.id
    oracle_id = int(callback.data.split(":")[1])
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT name FROM custom_oracles WHERE id = ? AND user_id = ?",
        (oracle_id, user_id),
    ).fetchone()
    conn.close()
    if not row:
        await callback.answer("Оракул не найден", show_alert=True)
        return
    safe_name = html_mod.escape(row[0])
    oracle_create_mode[user_id] = "awaiting_edit_description"
    oracle_draft[user_id] = {"oracle_id": oracle_id}
    await callback.message.answer(
        f"✏️ <b>Редактирование оракула «{safe_name}»</b>\n\n"
        "Опиши новый характер оракула.\n"
        "Например: <i>«саркастичный философ, который цитирует Ницше»</i>\n\n"
        "<i>Напиши «отмена» чтобы отменить</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("oracle_info:"))
async def on_oracle_info(callback: types.CallbackQuery):
    """Show oracle prompt preview as alert popup."""
    user_id = callback.from_user.id
    oracle_id = int(callback.data.split(":")[1])
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT name, prompt FROM custom_oracles WHERE id = ? AND user_id = ?",
        (oracle_id, user_id),
    ).fetchone()
    conn.close()
    if not row:
        await callback.answer("Оракул не найден", show_alert=True)
        return
    name, prompt = row
    header = f"🔮 «{name}»:\n"
    max_preview = 200 - len(header)
    preview = prompt[:max_preview - 3] + "..." if len(prompt) > max_preview else prompt
    await callback.answer(header + preview, show_alert=True)


@dp.callback_query(F.data.startswith("oracle_activate:"))
async def on_oracle_activate_after_create(callback: types.CallbackQuery):
    """Activate oracle right after creation."""
    user_id = callback.from_user.id
    oracle_id = int(callback.data.split(":")[1])
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT name FROM custom_oracles WHERE id = ? AND user_id = ?",
        (oracle_id, user_id),
    ).fetchone()
    if not row:
        conn.close()
        await callback.answer("Оракул не найден", show_alert=True)
        return
    conn.execute(
        "UPDATE users SET active_oracle_id = ? WHERE user_id = ?",
        (oracle_id, user_id),
    )
    conn.commit()
    conn.close()
    safe_name = html_mod.escape(row[0])
    await callback.message.edit_text(
        f"✅ Оракул «{safe_name}» создан и активирован!",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "oracle_activate_no")
async def on_oracle_activate_no(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "✅ Оракул создан, но не активирован.\n"
        "Выбрать его можно в /oracle",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(Command("editoracle"))
async def cmd_editoracle(message: types.Message):
    """Edit an existing oracle: /editoracle <id> новое описание"""
    user_id = message.from_user.id
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(
            "Формат: /editoracle &lt;id&gt; новое описание",
            parse_mode="HTML",
        )
        return
    try:
        oracle_id = int(parts[1])
    except ValueError:
        await message.reply("Неверный id оракула")
        return
    description = parts[2].strip()
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT name FROM custom_oracles WHERE id = ? AND user_id = ?",
        (oracle_id, user_id),
    ).fetchone()
    if not row:
        conn.close()
        await message.reply("Оракул не найден")
        return
    conn.close()
    await message.reply("🔄 Пересоздаю промпт...")
    new_prompt = await generate_oracle_prompt(description)
    if not new_prompt:
        await message.reply("😔 Не удалось сгенерировать промпт. Попробуй позже.")
        return
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "UPDATE custom_oracles SET prompt = ? WHERE id = ? AND user_id = ?",
        (new_prompt, oracle_id, user_id),
    )
    conn.commit()
    conn.close()
    safe_name = html_mod.escape(row[0])
    await message.reply(
        f"✅ Оракул «{safe_name}» обновлён!",
        parse_mode="HTML",
    )


@dp.message(F.text, ~F.text.startswith("/"))
@dp.message(~F.content_type.in_({"web_app_data"}), ~F.text)
async def on_user_message(message: types.Message):
    user_id = message.from_user.id

    # Handle oracle creation flow
    if user_id in oracle_create_mode and message.text:
        if message.text.strip().lower() in ("отмена", "cancel", "/cancel"):
            del oracle_create_mode[user_id]
            oracle_draft.pop(user_id, None)
            await message.answer("❌ Создание оракула отменено.")
            return

        mode = oracle_create_mode[user_id]

        if mode == "awaiting_name":
            name = message.text.strip()
            if len(name) > 50:
                await message.answer("Слишком длинное имя. Максимум 50 символов.")
                return
            oracle_draft[user_id] = {"name": name}
            oracle_create_mode[user_id] = "awaiting_description"
            safe_name = html_mod.escape(name)
            await message.answer(
                f"👍 Оракул будет называться <b>«{safe_name}»</b>\n\n"
                "Теперь опиши, какой он должен быть.\n"
                "Например: <i>«дерзкий технический нерд, который всё объясняет через код»</i>",
                parse_mode="HTML",
            )
            return

        if mode == "awaiting_description":
            description = message.text.strip()
            draft = oracle_draft.get(user_id, {})
            oracle_name = draft.get("name", "Оракул")
            safe_name = html_mod.escape(oracle_name)

            await message.answer(f"🔮 Создаю Оракула <b>«{safe_name}»</b>...", parse_mode="HTML")
            prompt = await generate_oracle_prompt(description)
            if not prompt:
                await message.answer("😔 Не удалось создать Оракула. Попробуй ещё раз.")
                del oracle_create_mode[user_id]
                oracle_draft.pop(user_id, None)
                return

            conn = sqlite3.connect(DB_FILE)
            cursor = conn.execute(
                "INSERT INTO custom_oracles (user_id, name, prompt) VALUES (?, ?, ?)",
                (user_id, oracle_name, prompt),
            )
            new_id = cursor.lastrowid
            conn.commit()
            conn.close()

            del oracle_create_mode[user_id]
            oracle_draft.pop(user_id, None)

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Да, сделать активным",
                        callback_data=f"oracle_activate:{new_id}",
                    ),
                    InlineKeyboardButton(
                        text="Нет",
                        callback_data="oracle_activate_no",
                    ),
                ]
            ])
            await message.answer(
                f"✅ Оракул <b>«{safe_name}»</b> создан!\n\n"
                "Хочешь сделать его активным?",
                reply_markup=kb,
                parse_mode="HTML",
            )
            return

        if mode == "awaiting_edit_description":
            description = message.text.strip()
            draft = oracle_draft.get(user_id, {})
            oracle_id = draft.get("oracle_id")
            if not oracle_id:
                del oracle_create_mode[user_id]
                oracle_draft.pop(user_id, None)
                return

            await message.answer("🔄 Пересоздаю промпт...")
            new_prompt = await generate_oracle_prompt(description)
            if not new_prompt:
                await message.answer("😔 Не удалось сгенерировать. Попробуй позже.")
                del oracle_create_mode[user_id]
                oracle_draft.pop(user_id, None)
                return

            conn = sqlite3.connect(DB_FILE)
            conn.execute(
                "UPDATE custom_oracles SET prompt = ? WHERE id = ? AND user_id = ?",
                (new_prompt, oracle_id, user_id),
            )
            conn.commit()
            conn.close()

            del oracle_create_mode[user_id]
            oracle_draft.pop(user_id, None)
            await message.answer("✅ Оракул обновлён!")
            return

    if user_id == ADMIN_ID:
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
