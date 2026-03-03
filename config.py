# config.py - QuestLog Fluxer Bot Configuration

import os
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
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

# ====== Fluxer API Config ======

FLUXER_API_BASE = os.getenv("FLUXER_API_BASE", "https://api.fluxer.app")
FLUXER_API_VERSION = os.getenv("FLUXER_API_VERSION", "1")
FLUXER_GATEWAY_URL = os.getenv("FLUXER_GATEWAY_URL", "wss://gateway.fluxer.app")
FLUXER_API_URL = f"{FLUXER_API_BASE}/v{FLUXER_API_VERSION}"

COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
IS_PRODUCTION = os.getenv("ENVIRONMENT", "development").lower() == "production"


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
