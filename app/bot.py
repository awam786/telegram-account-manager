import asyncio
import json
from datetime import datetime, timezone

import redis.asyncio as redis

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from sqlalchemy import select, text

from .config import ADMIN_IDS, BOT_TOKEN, REDIS_URL
from .database import SessionLocal, engine
from .models import Admin, AuditLog, Base, TelegramGroup


dp = Dispatcher()

redis_client = None


# ============================================================
# ADMIN CHECK
# ============================================================

def is_admin(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id in ADMIN_IDS
    )


# ============================================================
# REDIS
# ============================================================

async def init_redis():
    global redis_client

    redis_client = redis.from_url(
        REDIS_URL,
        decode_responses=True,
    )

    await redis_client.ping()

    print("🔴 Redis connected")


async def close_redis():
    global redis_client

    if redis_client:
        await redis_client.close()
        redis_client = None


async def queue_job(
    job_type: str,
    payload: dict,
):
    if redis_client is None:
        raise RuntimeError("Redis is not connected")

    job = {
        "type": job_type,
        "payload": payload,
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    await redis_client.rpush(
        "account_manager:jobs",
        json.dumps(job),
    )


async def queue_length() -> int:
    if redis_client is None:
        return 0

    return await redis_client.llen(
        "account_manager:jobs"
    )


# ============================================================
# DATABASE INITIALIZATION + MIGRATION
# ============================================================

async def create_tables():

    async with engine.begin() as connection:

        await connection.run_sync(
            Base.metadata.create_all
        )

        # ----------------------------------------------------
        # ACCOUNT MANAGEMENT TABLE
        # ----------------------------------------------------

        await connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS managed_accounts (
                    id SERIAL PRIMARY KEY,
                    label VARCHAR(255) NOT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'pending',
                    health VARCHAR(50) NOT NULL DEFAULT 'unknown',
                    notes TEXT,
                    last_health_check TIMESTAMPTZ,
                    last_seen TIMESTAMPTZ,
                    reconnect_attempts INTEGER NOT NULL DEFAULT 0,
                    created_by BIGINT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
        )

        # ----------------------------------------------------
        # MIGRATE EXISTING managed_accounts TABLE
        # ----------------------------------------------------

        await connection.execute(
            text(
                """
                ALTER TABLE managed_accounts
                ADD COLUMN IF NOT EXISTS health
                    VARCHAR(50) NOT NULL DEFAULT 'unknown'
                """
            )
        )

        await connection.execute(
            text(
                """
                ALTER TABLE managed_accounts
                ADD COLUMN IF NOT EXISTS last_health_check
                    TIMESTAMPTZ
                """
            )
        )

        await connection.execute(
            text(
                """
                ALTER TABLE managed_accounts
                ADD COLUMN IF NOT EXISTS last_seen
                    TIMESTAMPTZ
                """
            )
        )

        await connection.execute(
            text(
                """
                ALTER TABLE managed_accounts
                ADD COLUMN IF NOT EXISTS reconnect_attempts
                    INTEGER NOT NULL DEFAULT 0
                """
            )
        )

        await connection.execute(
            text(
                """
                ALTER TABLE managed_accounts
                ADD COLUMN IF NOT EXISTS updated_at
                    TIMESTAMPTZ DEFAULT NOW()
                """
            )
        )

        # ----------------------------------------------------
        # ACCOUNT TAGS
        # ----------------------------------------------------

        await connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS account_tags (
                    id SERIAL PRIMARY KEY,

                    account_id INTEGER NOT NULL
                        REFERENCES managed_accounts(id)
                        ON DELETE CASCADE,

                    tag VARCHAR(100) NOT NULL,

                    created_at TIMESTAMPTZ DEFAULT NOW(),

                    UNIQUE(account_id, tag)
                )
                """
            )
        )

        # ----------------------------------------------------
        # SYSTEM SETTINGS
        # ----------------------------------------------------

        await connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS system_settings (
                    key VARCHAR(100) PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
        )

        # ----------------------------------------------------
        # DEFAULT EMERGENCY STATE
        # ----------------------------------------------------

        await connection.execute(
            text(
                """
                INSERT INTO system_settings (key, value)
                VALUES ('emergency_stop', 'false')
                ON CONFLICT (key) DO NOTHING
                """
            )
        )

    # --------------------------------------------------------
    # REGISTER ADMINS
    # --------------------------------------------------------

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
# EMERGENCY STATE
# ============================================================

async def emergency_stopped() -> bool:

    async with SessionLocal() as session:

        result = await session.execute(
            text(
                """
                SELECT value
                FROM system_settings
                WHERE key = 'emergency_stop'
                """
            )
        )

        row = result.fetchone()

    return bool(
        row and str(row.value).lower() == "true"
    )


async def set_emergency_stop(
    stopped: bool,
):

    async with SessionLocal() as session:

        await session.execute(
            text(
                """
                INSERT INTO system_settings
                    (key, value, updated_at)
                VALUES
                    ('emergency_stop', :value, NOW())
                ON CONFLICT (key)
                DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = NOW()
                """
            ),
            {
                "value": "true" if stopped else "false"
            },
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

    stopped = await emergency_stopped()

    system_status = (
        "🔴 EMERGENCY STOP ACTIVE"
        if stopped
        else "🟢 System operational"
    )

    await message.answer(
        "🤖 <b>Telegram Account Manager</b>\n\n"
        f"{system_status}\n\n"
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
        "🤖 <b>Account Manager</b>\n\n"

        "📊 <b>System</b>\n"
        "/status\n"
        "/health\n"
        "/queue\n"
        "/stop\n"
        "/resume\n\n"

        "👥 <b>Accounts</b>\n"
        "/addaccount NAME\n"
        "/accounts\n"
        "/accounts active\n"
        "/accounts pending\n"
        "/accounts disconnected\n"
        "/account ID\n"
        "/disconnect ID\n"
        "/removeaccount ID\n\n"

        "🏷️ <b>Tags</b>\n"
        "/tag ID TAG\n"
        "/untag ID TAG\n"
        "/tags\n\n"

        "📡 <b>Targets</b>\n"
        "/active ID\n"
        "/inactive ID\n"
        "/groups\n\n"

        "📜 <b>Administration</b>\n"
        "/logs\n"
        "/help"
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
                    ) AS pending,

                    COUNT(*) FILTER (
                        WHERE status = 'disconnected'
                    ) AS disconnected,

                    COUNT(*) FILTER (
                        WHERE health = 'healthy'
                    ) AS healthy
                FROM managed_accounts
                """
            )
        )

        stats = accounts_result.fetchone()

    stopped = await emergency_stopped()

    qsize = await queue_length()

    active_groups = sum(
        1 for group in groups
        if group.is_active
    )

    inactive_groups = (
        len(groups) - active_groups
    )

    await message.answer(
        "📊 <b>System Status</b>\n\n"

        f"🤖 Bot: 🟢 Online\n"
        f"🗄️ PostgreSQL: 🟢 Connected\n"
        f"🔴 Redis: "
        f"{'🟢 Connected' if redis_client else '🔴 Offline'}\n\n"

        f"👤 Admins: <code>{len(ADMIN_IDS)}</code>\n\n"

        f"👥 Accounts: "
        f"<code>{stats.total or 0}</code>\n"

        f"🟢 Active: "
        f"<code>{stats.active or 0}</code>\n"

        f"🟡 Pending: "
        f"<code>{stats.pending or 0}</code>\n"

        f"🔴 Disconnected: "
        f"<code>{stats.disconnected or 0}</code>\n"

        f"💚 Healthy: "
        f"<code>{stats.healthy or 0}</code>\n\n"

        f"📡 Targets: "
        f"<code>{len(groups)}</code>\n"

        f"🟢 Active targets: "
        f"<code>{active_groups}</code>\n"

        f"🔴 Inactive targets: "
        f"<code>{inactive_groups}</code>\n\n"

        f"📦 Redis queue: "
        f"<code>{qsize}</code>\n\n"

        f"🛑 Emergency stop: "
        f"{'🔴 ON' if stopped else '🟢 OFF'}"
    )


# ============================================================
# ACCOUNT DETAILS
# ============================================================

@dp.message(Command("account"))
async def account_handler(message: Message):

    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2:
        await message.answer(
            "Usage:\n"
            "<code>/account 1</code>"
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
                SELECT
                    id,
                    label,
                    status,
                    health,
                    notes,
                    last_health_check,
                    last_seen,
                    reconnect_attempts,
                    created_by,
                    created_at,
                    updated_at
                FROM managed_accounts
                WHERE id = :id
                """
            ),
            {"id": account_id},
        )

        account = result.fetchone()

        if account is None:
            await message.answer(
                "❌ Account not found."
            )
            return

        tags_result = await session.execute(
            text(
                """
                SELECT tag
                FROM account_tags
                WHERE account_id = :id
                ORDER BY tag
                """
            ),
            {"id": account_id},
        )

        tags = tags_result.fetchall()

    tag_text = (
        ", ".join(row.tag for row in tags)
        if tags
        else "None"
    )

    await message.answer(
        "👤 <b>Account Details</b>\n\n"

        f"🆔 ID: <code>{account.id}</code>\n"
        f"📌 Name: <b>{account.label}</b>\n"
        f"📊 Status: <code>{account.status}</code>\n"
        f"💚 Health: <code>{account.health}</code>\n"
        f"🏷️ Tags: <code>{tag_text}</code>\n\n"

        f"🔄 Reconnect attempts: "
        f"<code>{account.reconnect_attempts}</code>\n"

        f"📝 Notes: "
        f"<code>{account.notes or 'None'}</code>\n\n"

        f"❤️ Last health check: "
        f"<code>{account.last_health_check or 'Never'}</code>\n"

        f"👀 Last seen: "
        f"<code>{account.last_seen or 'Never'}</code>\n\n"

        f"📅 Created: "
        f"<code>{account.created_at}</code>"
    )


# ============================================================
# ADD ACCOUNT
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
            "<code>/addaccount Account-001</code>"
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
                    (
                        label,
                        status,
                        health,
                        created_by
                    )
                VALUES
                    (
                        :label,
                        'pending',
                        'unknown',
                        :created_by
                    )
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
        action="ADD_ACCOUNT",
        target=str(account_id),
        details=label,
    )

    await message.answer(
        "✅ <b>Account added</b>\n\n"
        f"🆔 ID: <code>{account_id}</code>\n"
        f"👤 Name: <b>{label}</b>\n"
        "🟡 Status: Pending\n"
        "⚪ Health: Unknown"
    )


# ============================================================
# ACCOUNTS
# ============================================================

@dp.message(Command("accounts"))
async def accounts_handler(message: Message):

    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    parts = message.text.split(maxsplit=1)

    status_filter = None

    if len(parts) == 2:
        status_filter = parts[1].strip().lower()

    valid_filters = {
        "active",
        "pending",
        "disconnected",
    }

    if status_filter and status_filter not in valid_filters:
        await message.answer(
            "Available filters:\n\n"
            "/accounts\n"
            "/accounts active\n"
            "/accounts pending\n"
            "/accounts disconnected"
        )
        return

    async with SessionLocal() as session:

        if status_filter:

            result = await session.execute(
                text(
                    """
                    SELECT
                        id,
                        label,
                        status,
                        health
                    FROM managed_accounts
                    WHERE status = :status
                    ORDER BY id DESC
                    """
                ),
                {
                    "status": status_filter
                },
            )

        else:

            result = await session.execute(
                text(
                    """
                    SELECT
                        id,
                        label,
                        status,
                        health
                    FROM managed_accounts
                    ORDER BY id DESC
                    """
                )
            )

        accounts = result.fetchall()

    title = (
        f"👥 <b>Accounts — {status_filter}</b>"
        if status_filter
        else "👥 <b>All Accounts</b>"
    )

    if not accounts:
        await message.answer(
            f"{title}\n\n"
            "No accounts found."
        )
        return

    lines = [
        title,
        ""
    ]

    for account in accounts:

        if account.status == "active":
            status_icon = "🟢"
        elif account.status == "pending":
            status_icon = "🟡"
        elif account.status == "disconnected":
            status_icon = "🔴"
        else:
            status_icon = "⚪"

        lines.append(
            f"{status_icon} <b>{account.label}</b>\n"
            f"ID: <code>{account.id}</code>\n"
            f"Status: <code>{account.status}</code>\n"
            f"Health: <code>{account.health}</code>\n"
        )

    await message.answer(
        "\n".join(lines)
    )


# ============================================================
# DISCONNECT
# ============================================================

@dp.message(Command("disconnect"))
async def disconnect_handler(message: Message):

    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2:
        await message.answer(
            "Usage:\n"
            "<code>/disconnect 1</code>"
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
                "❌ Account not found."
            )
            return

        await session.execute(
            text(
                """
                UPDATE managed_accounts
                SET
                    status = 'disconnected',
                    health = 'offline',
                    updated_at = NOW()
                WHERE id = :id
                """
            ),
            {"id": account_id},
        )

        await session.commit()

    await add_log(
        admin_id=message.from_user.id,
        action="DISCONNECT_ACCOUNT",
        target=str(account_id),
        details=account.label,
    )

    await message.answer(
        "🔴 <b>Account disconnected</b>\n\n"
        f"🆔 ID: <code>{account_id}</code>\n"
        f"👤 Name: <b>{account.label}</b>\n"
        "Status: Disconnected"
    )


# ============================================================
# REMOVE ACCOUNT
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
                "❌ Account not found."
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
        action="REMOVE_ACCOUNT",
        target=str(account_id),
        details=account.label,
    )

    await message.answer(
        "🗑️ <b>Account removed</b>\n\n"
        f"ID: <code>{account_id}</code>\n"
        f"Name: <b>{account.label}</b>"
    )


# ============================================================
# HEALTH
# ============================================================

@dp.message(Command("health"))
async def health_handler(message: Message):

    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    async with SessionLocal() as session:

        result = await session.execute(
            text(
                """
                SELECT
                    COUNT(*) AS total,

                    COUNT(*) FILTER (
                        WHERE health = 'healthy'
                    ) AS healthy,

                    COUNT(*) FILTER (
                        WHERE health = 'degraded'
                    ) AS degraded,

                    COUNT(*) FILTER (
                        WHERE health = 'offline'
                    ) AS offline,

                    COUNT(*) FILTER (
                        WHERE health = 'unknown'
                    ) AS unknown
                FROM managed_accounts
                """
            )
        )

        stats = result.fetchone()

    await message.answer(
        "💚 <b>Account Health</b>\n\n"

        f"👥 Total: <code>{stats.total or 0}</code>\n\n"

        f"🟢 Healthy: "
        f"<code>{stats.healthy or 0}</code>\n"

        f"🟡 Degraded: "
        f"<code>{stats.degraded or 0}</code>\n"

        f"🔴 Offline: "
        f"<code>{stats.offline or 0}</code>\n"

        f"⚪ Unknown: "
        f"<code>{stats.unknown or 0}</code>"
    )


# ============================================================
# TAG ACCOUNT
# ============================================================

@dp.message(Command("tag"))
async def tag_handler(message: Message):

    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    parts = message.text.split()

    if len(parts) != 3:
        await message.answer(
            "Usage:\n"
            "<code>/tag 1 vip</code>"
        )
        return

    try:
        account_id = int(parts[1])
    except ValueError:
        await message.answer(
            "❌ Invalid account ID."
        )
        return

    tag = parts[2].strip().lower()

    if len(tag) > 100:
        await message.answer(
            "❌ Tag is too long."
        )
        return

    async with SessionLocal() as session:

        account_result = await session.execute(
            text(
                """
                SELECT label
                FROM managed_accounts
                WHERE id = :id
                """
            ),
            {"id": account_id},
        )

        account = account_result.fetchone()

        if account is None:
            await message.answer(
                "❌ Account not found."
            )
            return

        await session.execute(
            text(
                """
                INSERT INTO account_tags
                    (account_id, tag)
                VALUES
                    (:account_id, :tag)
                ON CONFLICT (account_id, tag)
                DO NOTHING
                """
            ),
            {
                "account_id": account_id,
                "tag": tag,
            },
        )

        await session.commit()

    await add_log(
        admin_id=message.from_user.id,
        action="ADD_TAG",
        target=str(account_id),
        details=tag,
    )

    await message.answer(
        "🏷️ <b>Tag added</b>\n\n"
        f"Account: <code>{account_id}</code>\n"
        f"Tag: <code>{tag}</code>"
    )


# ============================================================
# REMOVE TAG
# ============================================================

@dp.message(Command("untag"))
async def untag_handler(message: Message):

    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    parts = message.text.split()

    if len(parts) != 3:
        await message.answer(
            "Usage:\n"
            "<code>/untag 1 vip</code>"
        )
        return

    try:
        account_id = int(parts[1])
    except ValueError:
        await message.answer(
            "❌ Invalid account ID."
        )
        return

    tag = parts[2].strip().lower()

    async with SessionLocal() as session:

        await session.execute(
            text(
                """
                DELETE FROM account_tags
                WHERE account_id = :account_id
                AND tag = :tag
                """
            ),
            {
                "account_id": account_id,
                "tag": tag,
            },
        )

        await session.commit()

    await add_log(
        admin_id=message.from_user.id,
        action="REMOVE_TAG",
        target=str(account_id),
        details=tag,
    )

    await message.answer(
        "🗑️ <b>Tag removed</b>\n\n"
        f"Account: <code>{account_id}</code>\n"
        f"Tag: <code>{tag}</code>"
    )


# ============================================================
# TAGS
# ============================================================

@dp.message(Command("tags"))
async def tags_handler(message: Message):

    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    async with SessionLocal() as session:

        result = await session.execute(
            text(
                """
                SELECT
                    tag,
                    COUNT(*) AS account_count
                FROM account_tags
                GROUP BY tag
                ORDER BY tag
                """
            )
        )

        tags = result.fetchall()

    if not tags:
        await message.answer(
            "🏷️ <b>Tags</b>\n\n"
            "No tags created yet."
        )
        return

    lines = [
        "🏷️ <b>Account Tags</b>",
        ""
    ]

    for row in tags:
        lines.append(
            f"• <code>{row.tag}</code> — "
            f"{row.account_count} account(s)"
        )

    await message.answer(
        "\n".join(lines)
    )


# ============================================================
# STOP
# ============================================================

@dp.message(Command("stop"))
async def stop_handler(message: Message):

    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    await set_emergency_stop(True)

    await add_log(
        admin_id=message.from_user.id,
        action="EMERGENCY_STOP",
        details="Global job processing stopped.",
    )

    await message.answer(
        "🛑 <b>EMERGENCY STOP ACTIVATED</b>\n\n"
        "Queued account-management jobs are now "
        "marked to remain paused.\n\n"
        "Database remains available."
    )


# ============================================================
# RESUME
# ============================================================

@dp.message(Command("resume"))
async def resume_handler(message: Message):

    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    await set_emergency_stop(False)

    await add_log(
        admin_id=message.from_user.id,
        action="RESUME",
        details="Global job processing resumed.",
    )

    await message.answer(
        "▶️ <b>System resumed</b>\n\n"
        "Queued account-management jobs may now "
        "continue processing."
    )


# ============================================================
# QUEUE
# ============================================================

@dp.message(Command("queue"))
async def queue_handler(message: Message):

    if not is_admin(message):
        await message.answer("⛔ Unauthorized.")
        return

    size = await queue_length()

    stopped = await emergency_stopped()

    await message.answer(
        "📦 <b>Redis Queue</b>\n\n"
        f"Jobs waiting: <code>{size}</code>\n"
        f"Processing: "
        f"{'🔴 Paused' if stopped else '🟢 Enabled'}"
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

            group = TelegramGroup(
                telegram_id=telegram_id,
                is_active=True,
                created_by=message.from_user.id,
            )

            session.add(group)

        else:
            group.is_active = True

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
                "ℹ️ Target is not registered."
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
        "🔴 <b>Target deactivated</b>\n\n"
        f"🆔 ID: <code>{telegram_id}</code>"
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

        status = (
            "🟢 Active"
            if group.is_active
            else "🔴 Inactive"
        )

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
            .limit(20)
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
            f"• <b>{log.action}</b>{target}\n"
            f"  👤 Admin: "
            f"<code>{log.admin_id}</code>\n"
        )

    await message.answer(
        "\n".join(lines)
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    await create_tables()

    await init_redis()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        )
    )

    try:

        print("🤖 Bot starting...")
        print("🗄️ PostgreSQL initialized...")
        print("🔴 Redis initialized...")

        await dp.start_polling(bot)

    finally:

        await bot.session.close()

        await close_redis()

        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
