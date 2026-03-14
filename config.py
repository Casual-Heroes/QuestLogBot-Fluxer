# config.py - QuestLog Fluxer Bot Configuration

import os
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import QueuePool

# Load secrets - same secrets file as wardenbot (shared DB)
_secrets_path = Path("/etc/casual-heroes/warden.env")
if _secrets_path.exists():
    load_dotenv(_secrets_path, override=True)
else:
    load_dotenv(override=True)

# ====== Logging ======

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

_logging_configured = False
root_logger = logging.getLogger()

if not _logging_configured:
    for logger_name in list(logging.Logger.manager.loggerDict.keys()):
        logging.getLogger(logger_name).handlers.clear()
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, LOG_LEVEL))
    handler.setFormatter(logging.Formatter(
        '[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    root_logger.setLevel(getattr(logging, LOG_LEVEL))
    root_logger.addHandler(handler)
    _logging_configured = True

logger = logging.getLogger("fluxer")
logger.handlers.clear()
logger.propagate = True

# ====== Bot Config ======

COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
IS_PRODUCTION = os.getenv("ENVIRONMENT", "development").lower() == "production"

# QuestLog web platform internal API (for bot <-> web comms)
QUESTLOG_INTERNAL_API_URL = os.getenv("QUESTLOG_INTERNAL_API_URL", "https://casual-heroes.com/ql")
QUESTLOG_BOT_SECRET = os.getenv("QUESTLOG_BOT_SECRET", "")

# IGDB (uses Twitch OAuth) - same creds as wardenbot
IGDB_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID") or os.getenv("IGDB_CLIENT_ID", "")
IGDB_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET") or os.getenv("IGDB_CLIENT_SECRET", "")

# Early access - comma-separated Fluxer guild IDs where !invite command is allowed
# e.g. EARLY_ACCESS_GUILD_IDS=1474761008438513445
EARLY_ACCESS_GUILD_IDS_RAW = os.getenv("EARLY_ACCESS_GUILD_IDS", "")


def get_bot_token() -> str:
    token = os.getenv("FLUXER_BOT_TOKEN")
    if not token:
        raise ValueError("FLUXER_BOT_TOKEN not set in environment.")
    return token


# ====== Database (shared with wardenbot) ======

_engine = None
_session_factory = None


def get_database_url() -> str:
    DB_HOST = os.getenv("DB_HOST")
    DB_PORT = os.getenv("DB_PORT", "3306")
    DB_SOCKET = os.getenv("DB_SOCKET")
    DB_USERNAME = os.getenv("DB_USERNAME")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_NAME = os.getenv("DB_NAME", "warden")

    if not all([DB_USERNAME, DB_PASSWORD]):
        raise ValueError("DB_USERNAME and DB_PASSWORD must be set.")

    encoded_password = quote_plus(DB_PASSWORD)

    if DB_SOCKET:
        return (
            f"mysql+mysqlconnector://{DB_USERNAME}:{encoded_password}"
            f"@/{DB_NAME}"
            f"?unix_socket={DB_SOCKET}&charset=utf8mb4&collation=utf8mb4_unicode_ci"
        )
    if not DB_HOST:
        raise ValueError("Either DB_HOST or DB_SOCKET must be set.")
    return (
        f"mysql+mysqlconnector://{DB_USERNAME}:{encoded_password}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
        f"?charset=utf8mb4&collation=utf8mb4_unicode_ci"
    )


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_database_url(),
            echo=os.getenv("DB_ECHO", "false").lower() == "true",
            poolclass=QueuePool,
            pool_size=10,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_timeout=30,
            connect_args={"connect_timeout": 10, "charset": "utf8mb4", "autocommit": False},
        )
        logger.info("Database engine created")
    return _engine


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = scoped_session(
            sessionmaker(bind=get_engine(), autocommit=False, autoflush=True, expire_on_commit=False)
        )
    return _session_factory


@contextmanager
def db_session_scope():
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"DB transaction failed: {e}", exc_info=True)
        raise
    finally:
        session.close()
