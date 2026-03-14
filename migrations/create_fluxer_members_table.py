"""
Migration: Create web_fluxer_members table for dedicated guild member storage.

Run with: python Scripts/create_fluxer_members_table.py

This table stores Fluxer user profiles per-guild, populated by:
  - on_member_join events (immediate)
  - on_message author capture (organic growth)
  - Startup refresh via bot.fetch_user() for known users
  - on_member_remove events (marks left_at)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from config import get_database_url


def run():
    engine = create_engine(get_database_url())
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS web_fluxer_members (
                guild_id     BIGINT NOT NULL,
                user_id      BIGINT NOT NULL,
                username     VARCHAR(255) NOT NULL DEFAULT '',
                global_name  VARCHAR(255),
                avatar_hash  VARCHAR(255),
                roles        TEXT,
                joined_at    BIGINT,
                left_at      BIGINT,
                last_seen    BIGINT NOT NULL DEFAULT 0,
                synced_at    BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id),
                INDEX idx_wfm_guild (guild_id),
                INDEX idx_wfm_user (user_id),
                INDEX idx_wfm_last_seen (guild_id, last_seen DESC)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        conn.commit()
        print("web_fluxer_members - OK")


if __name__ == "__main__":
    run()
