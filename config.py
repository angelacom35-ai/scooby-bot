"""
config.py — настройки Скубі • Кеш v1.1.10 RC

Совместимость: Python 3.10+, python-telegram-bot 21.1.1, Android Bot Hosting.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

CONFIG_DIR = Path(__file__).resolve().parent
load_dotenv(CONFIG_DIR / ".env")

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env")

_raw_admins = os.getenv("ADMIN_IDS", "")
_parsed_admins = []
_skipped_admins = []
for _part in _raw_admins.split(","):
    _part = _part.strip()
    if not _part:
        continue
    if _part.isdigit():
        _parsed_admins.append(int(_part))
    else:
        _skipped_admins.append(_part)

if not _parsed_admins:
    raise ValueError(
        f"❌ ADMIN_IDS пуст или не содержит валидных ID. "
        f"Исходное значение: '{_raw_admins}'"
    )

if _skipped_admins:
    logger.warning(f"⚠️ В ADMIN_IDS пропущены некорректные значения: {_skipped_admins}")

ADMIN_IDS = _parsed_admins

START_BONUS = int(os.getenv("START_BONUS", "50"))
REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", "100"))

if START_BONUS < 0 or REFERRAL_BONUS < 0:
    raise ValueError("❌ Бонусы не могут быть отрицательными!")

if START_BONUS > 100_000 or REFERRAL_BONUS > 100_000:
    raise ValueError(
        f"❌ Бонусы выглядят подозрительно большими "
        f"(START={START_BONUS}, REF={REFERRAL_BONUS}). Проверь .env"
    )

_default_db = CONFIG_DIR / "data" / "scooby.db"
DB_NAME = os.getenv("DB_NAME", str(_default_db))

SCHEMA_VERSION = 5
MAX_BACKUPS = int(os.getenv("MAX_BACKUPS", "3"))

if MAX_BACKUPS < 1:
    raise ValueError("❌ MAX_BACKUPS должен быть >= 1")

logger.info("⚙️ Config loaded:")
logger.info(f"   Admins: {len(ADMIN_IDS)}")
logger.info(f"   DB: {DB_NAME}")
logger.info(f"   Schema version: {SCHEMA_VERSION}")
logger.info(f"   Backups to keep: {MAX_BACKUPS}")
logger.info(f"   START_BONUS: {START_BONUS}, REFERRAL_BONUS: {REFERRAL_BONUS}")
