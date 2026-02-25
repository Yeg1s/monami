import asyncio
import json
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
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

# Store user chat IDs for admin reply forwarding
# key: admin message_id -> value: user chat_id
reply_map: dict[int, int] = {}
# Track users in "write to Lut" mode
write_mode: set[int] = set()


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
        ("25 февраля", "2026-02-25"),
        ("28 февраля", "2026-02-28"),
        ("7 марта", "2026-03-07"),
        ("26 марта", "2026-03-26"),
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

    await callback.answer("Записано!")


# Admin replies to forwarded messages -> send back to user
@dp.message(F.reply_to_message, F.from_user.id == ADMIN_ID)
async def on_admin_reply(message: types.Message):
    replied_id = message.reply_to_message.message_id
    user_chat_id = reply_map.get(replied_id)
    if not user_chat_id:
        return

    sent = await bot.send_message(
        user_chat_id,
        f"💌 <b>Сообщение от Люта:</b>\n\n{message.text}",
        parse_mode="HTML",
    )
    reply_map[sent.message_id] = user_chat_id
    await message.reply("✅ Отправлено!")


# User free-text messages (write to Lut mode + any message)
@dp.message(F.text, ~F.text.startswith("/"))
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
        sent = await bot.send_message(
            ADMIN_ID,
            f"💬 <b>{name}:</b>\n\n{message.text}\n\n<i>↩️ Ответь реплаем — она получит</i>",
            parse_mode="HTML",
        )
        reply_map[sent.message_id] = message.from_user.id

    await message.answer("✅ Сообщение отправлено Люту!")


async def main():
    print("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
