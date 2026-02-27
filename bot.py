import asyncio
import json
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

REPLY_MAP_FILE = "reply_map.json"

# Store user chat IDs for admin reply forwarding
# key: admin message_id -> value: user chat_id
reply_map: dict[int, int] = {}
# Track users in "write to Lut" mode
write_mode: set[int] = set()


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


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="🎁 Открыть сертификат",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "✨ <b>Привет!</b>\n\n"
        "Тебас ждёт особенный подарок от <b>Клуба Подпольных Авантюристов</b>.\n\n"
        "Нажми кнопку внизу, чтобы узнать!) 👇",
        reply_markup=kb,
        parse_mode="HTML",
    )

    # Notify admin about /start
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

    if data.get("action") != "activate":
        return

    # Notify the recipient
    await message.answer(
        "🎉 <b>Сертификат успешно активирован!</b>\n\n"
        "Теперь выбери удобную дату для массажа 👇",
        reply_markup=build_dates_keyboard(),
        parse_mode="HTML",
    )

    # Notify admin
    if ADMIN_ID:
        user = message.from_user
        name = user.full_name or user.username or "Неизвестный"
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>{name}</b> активировала сертификат на массаж!",
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

    # Confirm to the recipient
    await callback.message.edit_text(
        f"✅ <b>Отлично!</b>\n\n"
        f"Ты записана на <b>{pretty}</b>.\n\n"
        f"Они сошлись. Волна и камень. Стихи и проза, лед и пламень.",
        parse_mode="HTML",
    )

    # Notify admin
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


# Admin command: /send <chat_id> — start conversation with a user by chat ID
# After sending, admin can reply to the sent message to continue the conversation
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

    # Text after chat_id
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


# Admin replies to forwarded messages -> send back to user
@dp.message(F.reply_to_message, F.from_user.id == ADMIN_ID)
async def on_admin_reply(message: types.Message):
    replied_id = message.reply_to_message.message_id
    user_chat_id = reply_map.get(replied_id)
    if not user_chat_id:
        return

    await message.copy_to(user_chat_id)
    await message.reply("✅ Отправлено!")


# User messages (write to Lut mode + any message)
@dp.message(F.text, ~F.text.startswith("/"))
@dp.message(~F.content_type.in_({"web_app_data"}), ~F.text)
async def on_user_message(message: types.Message):
    # Ignore admin's non-reply messages
    if message.from_user.id == ADMIN_ID:
        return

    user = message.from_user
    name = user.full_name or user.username or "Неизвестный"

    if user.id in write_mode:
        write_mode.discard(user.id)

    # Forward to admin
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


async def main():
    load_reply_map()
    print("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
