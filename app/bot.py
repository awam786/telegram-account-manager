import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from sqlalchemy import select, text

from .config import ADMIN_IDS, BOT_TOKEN
from .database import SessionLocal, engine
from .models import Admin, AuditLog, Base, TelegramGroup


dp = Dispatcher()


# ============================================================
# ADMIN CHECK
# ============================================================

def is_admin(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id in ADMIN_IDS
    )


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

async def create_tables():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

        # Account inventory table.
        # This stores management records only.
        # It does NOT store Telegram login credentials.
        await connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS managed_accounts (
                    id SERIAL PRIMARY KEY,
                    label VARCHAR(255) NOT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'pending',
                    notes TEXT,
                    created_by BIGINT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
        )

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


# ============================================================
# AUDIT LOG
# ============================================================

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


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message):
    if not is_admin(message):
        await message.answer(
            "⛔ You are not authorized to control this bot."
        )
        return

    await message.answer(
        "🤖 <b>Telegram Account Manager</b>\n\n"
        "🟢 System is online.\n\n"
        "Use /help to see available commands."
    )


# ============================================================
# HELP
# ============================================================

@dp.message(Command("help"))
async def help_handler(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    await message.answer(
        "🤖 <b>Admin Commands</b>\n\n"

        "📊 /status — system status\n"

        "➕ /active <code>ID</code> — activate a group/channel\n"
        "➖ /inactive <code>ID</code> — deactivate a group/channel\n"
        "📋 /groups — list registered targets\n\n"

        "👤 /addaccount <code>NAME</code> — add an account record\n"
        "👥 /accounts — list account records\n"
        "🔴 /removeaccount <code>ID</code> — remove an account record\n\n"

        "📜 /logs — recent admin activity\n"
        "❓ /help — show this menu"
    )


# ============================================================
# STATUS
# ============================================================

@dp.message(Command("status"))
async def status_handler(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    async with SessionLocal() as session:

        groups_result = await session.execute(
            select(TelegramGroup)
        )

        groups = groups_result.scalars().all()

        accounts_result = await session.execute(
            text(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE status = 'active'
                    ) AS active,
                    COUNT(*) FILTER (
                        WHERE status = 'pending'
                    ) AS pending
                FROM managed_accounts
                """
            )
        )

        account_stats = accounts_result.fetchone()

    active_groups = sum(
        1 for group in groups
        if group.is_active
    )

    inactive_groups = len(groups) - active_groups

    total_accounts = account_stats.total or 0
    active_accounts = account_stats.active or 0
    pending_accounts = account_stats.pending or 0

    await message.answer(
        "📊 <b>System Status</b>\n\n"

        f"👤 Admins: <code>{len(ADMIN_IDS)}</code>\n\n"

        f"📡 Registered targets: "
        f"<code>{len(groups)}</code>\n"

        f"🟢 Active targets: "
        f"<code>{active_groups}</code>\n"

        f"🔴 Inactive targets: "
        f"<code>{inactive_groups}</code>\n\n"

        f"👥 Account records: "
        f"<code>{total_accounts}</code>\n"

        f"🟢 Active accounts: "
        f"<code>{active_accounts}</code>\n"

        f"🟡 Pending accounts: "
        f"<code>{pending_accounts}</code>\n\n"

        "🗄️ Database: 🟢 Connected"
    )


# ============================================================
# ACTIVE TARGET
# ============================================================

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
        f"🆔 ID: <code>{telegram_id}</code>\n"
        "🟢 Status: Active"
    )


# ============================================================
# INACTIVE TARGET
# ============================================================

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
        await message.answer(
            "❌ Invalid Telegram ID."
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
        f"🆔 ID: <code>{telegram_id}</code>\n"
        "🔴 Status: Inactive"
    )


# ============================================================
# GROUPS
# ============================================================

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
            "📋 <b>Registered Targets</b>\n\n"
            "No groups or channels registered yet."
        )
        return

    lines = [
        "📋 <b>Registered Targets</b>",
        ""
    ]

    for group in groups:

        status = "🟢 Active" if group.is_active else "🔴 Inactive"

        title = group.title or "Unknown"

        lines.append(
            f"{status}\n"
            f"📌 {title}\n"
            f"🆔 <code>{group.telegram_id}</code>\n"
        )

    await message.answer(
        "\n".join(lines)
    )


# ============================================================
# ADD ACCOUNT RECORD
# ============================================================

@dp.message(Command("addaccount"))
async def add_account_handler(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2:
        await message.answer(
            "Usage:\n"
            "<code>/addaccount Account-001</code>\n\n"
            "This creates an account-management record."
        )
        return

    label = parts[1].strip()

    if len(label) > 255:
        await message.answer(
            "❌ Account name is too long."
        )
        return

    async with SessionLocal() as session:

        result = await session.execute(
            text(
                """
                INSERT INTO managed_accounts
                    (label, status, created_by)
                VALUES
                    (:label, 'pending', :created_by)
                RETURNING id
                """
            ),
            {
                "label": label,
                "created_by": message.from_user.id,
            },
        )

        account_id = result.scalar_one()

        await session.commit()

    await add_log(
        admin_id=message.from_user.id,
        action="ADD_ACCOUNT_RECORD",
        target=str(account_id),
        details=label,
    )

    await message.answer(
        "✅ <b>Account record added</b>\n\n"
        f"🆔 ID: <code>{account_id}</code>\n"
        f"👤 Name: <b>{label}</b>\n"
        "🟡 Status: Pending"
    )


# ============================================================
# LIST ACCOUNTS
# ============================================================

@dp.message(Command("accounts"))
async def accounts_handler(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    async with SessionLocal() as session:

        result = await session.execute(
            text(
                """
                SELECT id, label, status, created_at
                FROM managed_accounts
                ORDER BY id DESC
                """
            )
        )

        accounts = result.fetchall()

    if not accounts:
        await message.answer(
            "👥 <b>Accounts</b>\n\n"
            "No account records yet."
        )
        return

    lines = [
        "👥 <b>Account Manager</b>",
        ""
    ]

    for account in accounts:

        if account.status == "active":
            status = "🟢"
        elif account.status == "pending":
            status = "🟡"
        else:
            status = "🔴"

        lines.append(
            f"{status} <b>{account.label}</b>\n"
            f"ID: <code>{account.id}</code>\n"
            f"Status: {account.status}\n"
        )

    await message.answer(
        "\n".join(lines)
    )


# ============================================================
# REMOVE ACCOUNT RECORD
# ============================================================

@dp.message(Command("removeaccount"))
async def remove_account_handler(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2:
        await message.answer(
            "Usage:\n"
            "<code>/removeaccount 1</code>"
        )
        return

    try:
        account_id = int(parts[1].strip())
    except ValueError:
        await message.answer(
            "❌ Invalid account ID."
        )
        return

    async with SessionLocal() as session:

        result = await session.execute(
            text(
                """
                SELECT label
                FROM managed_accounts
                WHERE id = :id
                """
            ),
            {"id": account_id},
        )

        account = result.fetchone()

        if account is None:
            await message.answer(
                "❌ Account record not found."
            )
            return

        await session.execute(
            text(
                """
                DELETE FROM managed_accounts
                WHERE id = :id
                """
            ),
            {"id": account_id},
        )

        await session.commit()

    await add_log(
        admin_id=message.from_user.id,
        action="REMOVE_ACCOUNT_RECORD",
        target=str(account_id),
        details=account.label,
    )

    await message.answer(
        "🗑️ <b>Account record removed</b>\n\n"
        f"🆔 ID: <code>{account_id}</code>\n"
        f"👤 Name: {account.label}"
    )


# ============================================================
# LOGS
# ============================================================

@dp.message(Command("logs"))
async def logs_handler(message: Message):
    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    async with SessionLocal() as session:

        result = await session.execute(
            select(AuditLog)
            .order_by(AuditLog.id.desc())
            .limit(15)
        )

        logs = result.scalars().all()

    if not logs:
        await message.answer(
            "📜 No activity logs yet."
        )
        return

    lines = [
        "📜 <b>Recent Activity</b>",
        ""
    ]

    for log in logs:

        target = (
            f" — <code>{log.target}</code>"
            if log.target
            else ""
        )

        lines.append(
            f"• {log.action}{target}\n"
            f"  👤 Admin: <code>{log.admin_id}</code>"
        )

    await message.answer(
        "\n".join(lines)
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    await create_tables()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        )
    )

    try:
        print("🤖 Bot starting...")
        print("🗄️ Database initialized...")
        await dp.start_polling(bot)

    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
