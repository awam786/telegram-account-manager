import os


BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured")
