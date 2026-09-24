import os


BOT_TOKEN = os.getenv("BOT_TOKEN")

DATABASE_URL = os.getenv("DATABASE_URL")

REDIS_URL = os.getenv("REDIS_URL")

ADMIN_IDS = {
    int(user_id.strip())
    for user_id in os.getenv(
        "ADMIN_IDS",
        ""
    ).split(",")
    if user_id.strip()
}


if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not configured"
    )


if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not configured"
    )


if not REDIS_URL:
    raise RuntimeError(
        "REDIS_URL is not configured"
    )


if not ADMIN_IDS:
    raise RuntimeError(
        "ADMIN_IDS is not configured"
    )


if DATABASE_URL.startswith("postgres://"):

    DATABASE_URL = DATABASE_URL.replace(
        "postgres://",
        "postgresql+asyncpg://",
        1
    )

elif DATABASE_URL.startswith("postgresql://"):

    DATABASE_URL = DATABASE_URL.replace(
        "postgresql://",
        "postgresql+asyncpg://",
        1
    )
