#!/usr/bin/env python3
"""
Migration: create sdtd_class_assignments table.

Stores which SYNAPSE class each 7DTD player has chosen.
One row per player per guild. Class is locked once set.

Run from /mnt/gamestoreage2/DiscordBots/questlogfluxer/:
    fluxerqlprj/bin/python3 migrations/create_sdtd_class_assignments.py
"""

import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path("/etc/casual-heroes/warden.env"), override=True)

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_engine

engine = get_engine()

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS sdtd_class_assignments (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    guild_id       VARCHAR(64)  NOT NULL,
    steamid        VARCHAR(64)  NOT NULL,
    character_name VARCHAR(255) NOT NULL DEFAULT '',
    class_name     VARCHAR(64)  NOT NULL,
    assigned_at    BIGINT       NOT NULL DEFAULT 0,
    UNIQUE KEY uq_guild_steam (guild_id, steamid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

with engine.connect() as conn:
    conn.execute(__import__('sqlalchemy').text(CREATE_TABLE))
    conn.commit()
    print("Created sdtd_class_assignments table.")
