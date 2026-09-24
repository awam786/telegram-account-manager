import asyncio

from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import CommandStart

from .config import BOT_TOKEN


dp = Dispatcher()


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "🤖 Telegram Account Manager\n\n"
        "System is online."
    )


async def main():
    bot = Bot(token=BOT_TOKEN)

    try:
        print("🤖 Bot starting...")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
