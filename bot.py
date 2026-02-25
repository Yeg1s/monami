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
        "Тебя ждёт особенный подарок от <b>Клуба Подпольных Авантюристов</b>.\n\n"
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
        ("7 марта", "2026-03-07"),
        ("26 марта", "2026-03-26"),
    ]
    buttons = [
        [InlineKeyboardButton(text=f"📅 {label}", callback_data=f"date:{value}")]
        for label, value in dates
    ]
    buttons.append(
        [InlineKeyboardButton(text="📅 Другая дата", callback_data="date:custom")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data.startswith("date:"))
async def on_date_selected(callback: types.CallbackQuery):
    raw = callback.data.split(":", 1)[1]

    if raw == "custom":
        await callback.message.answer(
            "📅 Напиши мне удобную дату, и я передам её организатору!",
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
        await bot.send_message(
            ADMIN_ID,
            f"📋 <b>{name}</b> выбрала дату массажа: <b>{pretty}</b>",
            parse_mode="HTML",
        )

    await callback.answer("Записано!")


async def main():
    print("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
