import asyncio

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from sqlalchemy import select

from .config import ADMIN_IDS, BOT_TOKEN
from .database import SessionLocal, engine
from .models import Admin, AuditLog, Base, TelegramGroup


dp = Dispatcher()


def is_admin(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id in ADMIN_IDS
    )


async def create_tables():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    # Make sure configured admins exist in the database.
    async with SessionLocal() as session:
        for telegram_id in ADMIN_IDS:
            result = await session.execute(
                select(Admin).where(
                    Admin.telegram_id == telegram_id
                )
            )

            admin = result.scalar_one_or_none()

            if admin is None:
                session.add(
                    Admin(
                        telegram_id=telegram_id
                    )
                )

        await session.commit()


async def add_log(
    admin_id: int,
    action: str,
    target: str | None = None,
    details: str | None = None,
):
    async with SessionLocal() as session:
        session.add(
            AuditLog(
                admin_id=admin_id,
                action=action,
                target=target,
                details=details,
            )
        )

        await session.commit()


@dp.message(CommandStart())
async def start_handler(message: Message):
    if not is_admin(message):
        await message.answer(
            "⛔ You are not authorized to control this bot."
        )
        return

    await message.answer(
        "🤖 <b>Telegram Account Manager</b>\n\n"
        "System is online.\n\n"
        "Use /help to see available commands."
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    await message.answer(
        "🤖 <b>Admin Commands</b>\n\n"
        "📊 /status — system status\n"
        "➕ /active &lt;ID&gt; — activate a group/channel\n"
        "➖ /inactive &lt;ID&gt; — deactivate a group/channel\n"
        "📋 /groups — list registered groups/channels\n"
        "❓ /help — show this menu"
    )


@dp.message(Command("status"))
async def status_handler(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    async with SessionLocal() as session:
        result = await session.execute(
            select(TelegramGroup)
        )

        groups = result.scalars().all()

    active = sum(
        1 for group in groups if group.is_active
    )

    inactive = len(groups) - active

    await message.answer(
        "📊 <b>System Status</b>\n\n"
        f"👤 Admins: <code>{len(ADMIN_IDS)}</code>\n"
        f"📡 Registered targets: <code>{len(groups)}</code>\n"
        f"🟢 Active: <code>{active}</code>\n"
        f"🔴 Inactive: <code>{inactive}</code>\n"
        "\n"
        "Database: 🟢 Connected"
    )


@dp.message(Command("active"))
async def active_handler(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2:
        await message.answer(
            "Usage:\n"
            "<code>/active -1001234567890</code>"
        )
        return

    try:
        telegram_id = int(parts[1].strip())
    except ValueError:
        await message.answer(
            "❌ Invalid Telegram ID.\n\n"
            "Example:\n"
            "<code>/active -1001234567890</code>"
        )
        return

    async with SessionLocal() as session:
        result = await session.execute(
            select(TelegramGroup).where(
                TelegramGroup.telegram_id == telegram_id
            )
        )

        group = result.scalar_one_or_none()

        if group is None:
            group = TelegramGroup(
                telegram_id=telegram_id,
                is_active=True,
                created_by=message.from_user.id,
            )

            session.add(group)
            result_text = "registered and activated"
        else:
            group.is_active = True
            result_text = "activated"

        await session.commit()

    await add_log(
        admin_id=message.from_user.id,
        action="ACTIVE_TARGET",
        target=str(telegram_id),
    )

    await message.answer(
        "✅ <b>Target activated</b>\n\n"
        f"ID: <code>{telegram_id}</code>\n"
        f"Status: 🟢 {result_text}"
    )


@dp.message(Command("inactive"))
async def inactive_handler(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2:
        await message.answer(
            "Usage:\n"
            "<code>/inactive -1001234567890</code>"
        )
        return

    try:
        telegram_id = int(parts[1].strip())
    except ValueError:
        await message.answer("❌ Invalid Telegram ID.")
        return

    async with SessionLocal() as session:
        result = await session.execute(
            select(TelegramGroup).where(
                TelegramGroup.telegram_id == telegram_id
            )
        )

        group = result.scalar_one_or_none()

        if group is None:
            await message.answer(
                "ℹ️ This target is not registered."
            )
            return

        group.is_active = False
        await session.commit()

    await add_log(
        admin_id=message.from_user.id,
        action="INACTIVE_TARGET",
        target=str(telegram_id),
    )

    await message.answer(
        "✅ <b>Target deactivated</b>\n\n"
        f"ID: <code>{telegram_id}</code>\n"
        "Status: 🔴 Inactive"
    )


@dp.message(Command("groups"))
async def groups_handler(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    async with SessionLocal() as session:
        result = await session.execute(
            select(TelegramGroup).order_by(
                TelegramGroup.id.desc()
            )
        )

        groups = result.scalars().all()

    if not groups:
        await message.answer(
            "📋 No groups/channels registered yet."
        )
        return

    lines = ["📋 <b>Registered Targets</b>\n"]

    for group in groups:
        status = "🟢" if group.is_active else "🔴"

        title = group.title or "Unknown"

        lines.append(
            f"{status} <b>{title}</b>\n"
            f"ID: <code>{group.telegram_id}</code>\n"
        )

    await message.answer(
        "\n".join(lines)
    )


async def main():
    await create_tables()

    bot = Bot(token=BOT_TOKEN)

    try:
        print("🤖 Bot starting...")
        print("🗄️ Database initialized...")
        await dp.start_polling(bot)

    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
