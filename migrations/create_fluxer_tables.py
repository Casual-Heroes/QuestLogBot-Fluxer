#!/usr/bin/env python3
# create_fluxer_tables.py - Create tables for the Fluxer bot
# Run once: python3 create_fluxer_tables.py

from config import get_engine
from sqlalchemy import text

engine = get_engine()

with engine.connect() as conn:
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS fluxer_member_xp (
            guild_id    BIGINT NOT NULL,
            user_id     BIGINT NOT NULL,
            username    VARCHAR(255) NOT NULL DEFAULT '',
            xp          FLOAT NOT NULL DEFAULT 0,
            level       INT NOT NULL DEFAULT 0,
            message_count INT NOT NULL DEFAULT 0,
            last_message_ts BIGINT NOT NULL DEFAULT 0,
            first_seen  BIGINT NOT NULL DEFAULT 0,
            last_active BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id),
            INDEX idx_xp (guild_id, xp DESC)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS fluxer_lfg_posts (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            guild_id    BIGINT NOT NULL,
            channel_id  BIGINT NOT NULL,
            user_id     BIGINT NOT NULL,
            username    VARCHAR(255) NOT NULL,
            game        VARCHAR(255) NOT NULL,
            description TEXT,
            created_at  BIGINT NOT NULL,
            expires_at  BIGINT NOT NULL,
            is_active   TINYINT(1) NOT NULL DEFAULT 1,
            INDEX idx_guild_active (guild_id, is_active, expires_at),
            INDEX idx_user (user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """))

    conn.commit()
    print("fluxer_member_xp - OK")
    print("fluxer_lfg_posts - OK")
