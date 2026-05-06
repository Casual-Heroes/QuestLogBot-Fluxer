#!/usr/bin/env python3
"""
Migration: create gamebot_configs unified table.

Replaces vquest_configs, sdtd_configs, shroudquest_configs, valquest_configs
with a single gamebot_configs table. Also creates gamebot_players table
(replaces vrising_players, sdtd_players, etc.)

Run from /mnt/gamestoreage2/DiscordBots/questlogfluxer/:
    fluxerqlprj/bin/python3 migrations/create_gamebot_unified_table.py
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path("/etc/casual-heroes/warden.env"), override=True)

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_engine

engine = get_engine()

GAMEBOT_CONFIGS = """
CREATE TABLE IF NOT EXISTS gamebot_configs (
    id                       INT AUTO_INCREMENT PRIMARY KEY,

    -- AMP identity (set by auto-discovery, never manually)
    instance_name            VARCHAR(255) NOT NULL UNIQUE,
    instance_id              VARCHAR(64)  DEFAULT NULL,
    game_type                VARCHAR(100) NOT NULL DEFAULT '',
    amp_log_dir              VARCHAR(512) NOT NULL DEFAULT '',

    -- Log parsing (read from GenericModule.kvp by discovery)
    join_regex               TEXT         DEFAULT NULL,
    leave_regex              TEXT         DEFAULT NULL,

    -- Guild assignment (set by dashboard, null = unconfigured)
    guild_id                 VARCHAR(64)  DEFAULT NULL,
    platform                 VARCHAR(20)  DEFAULT 'fluxer',

    -- Channel assignments
    notif_channel_id         VARCHAR(64)  DEFAULT NULL,
    live_log_channel_id      VARCHAR(64)  DEFAULT NULL,
    stats_channel_id         VARCHAR(64)  DEFAULT NULL,
    server_update_channel_id VARCHAR(64)  DEFAULT NULL,

    -- Role
    admin_role_id            VARCHAR(64)  DEFAULT NULL,

    -- Display
    server_display_name      VARCHAR(255) DEFAULT NULL,
    embed_color              VARCHAR(16)  DEFAULT '#008080',

    -- Visibility toggles
    show_server_name         TINYINT(1)   DEFAULT 1,
    show_ip_port             TINYINT(1)   DEFAULT 1,
    show_password            TINYINT(1)   DEFAULT 0,
    show_usage_stats         TINYINT(1)   DEFAULT 1,
    show_player_count        TINYINT(1)   DEFAULT 1,
    show_top_5_players       TINYINT(1)   DEFAULT 0,

    -- Alert toggles
    alert_join_leave         TINYINT(1)   DEFAULT 1,
    alert_live_logs          TINYINT(1)   DEFAULT 0,

    -- Scheduler
    scheduler_hour           TINYINT(4)   DEFAULT 3,
    scheduler_minute         TINYINT(4)   DEFAULT 27,
    schedule_overrides       TEXT         DEFAULT NULL,

    -- Live panel message tracking
    serverinfo_message_id    VARCHAR(64)  DEFAULT NULL,
    serverchannel_message_id VARCHAR(64)  DEFAULT NULL,

    -- Server password (generic - applies to any game)
    server_password          VARCHAR(64)  DEFAULT NULL,

    -- Configured flag: False = discovered but no channels set yet
    configured               TINYINT(1)   DEFAULT 0,

    created_at               DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at               DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_guild (guild_id),
    INDEX idx_instance (instance_name),
    INDEX idx_configured (configured)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

GAMEBOT_PLAYERS = """
CREATE TABLE IF NOT EXISTS gamebot_players (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    instance_name  VARCHAR(255) NOT NULL,
    guild_id       VARCHAR(64)  NOT NULL,
    userid         VARCHAR(64)  DEFAULT NULL,
    username       VARCHAR(100) NOT NULL,
    joined_at      DATETIME     DEFAULT CURRENT_TIMESTAMP,
    last_seen      DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_instance_user (instance_name, userid),
    INDEX idx_instance (instance_name),
    INDEX idx_guild (guild_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

def migrate_existing_configs(conn):
    """Copy data from old per-game tables into gamebot_configs."""

    migrations = [
        {
            'table': 'vquest_configs',
            'instance_name_col': 'amp_instance_name',
            'game_type': 'V Rising',
            'password_col': 'vrising_password',
        },
        {
            'table': 'sdtd_configs',
            'instance_name_col': 'amp_instance_name',
            'game_type': 'Seven Days To Die',
            'password_col': 'sdtd_password',
        },
        {
            'table': 'shroudquest_configs',
            'instance_name_col': 'amp_instance_name',
            'game_type': 'Enshrouded',
            'password_col': 'enshrouded_password',
        },
        {
            'table': 'valquest_configs',
            'instance_name_col': 'amp_instance_name',
            'game_type': 'Valheim',
            'password_col': 'valheim_password',
        },
    ]

    from sqlalchemy import text

    for m in migrations:
        try:
            rows = conn.execute(text(f"SELECT * FROM {m['table']}")).fetchall()
        except Exception as e:
            print(f"  Skipping {m['table']}: {e}")
            continue

        for row in rows:
            r = dict(row._mapping)
            instance_name = r.get(m['instance_name_col']) or ''
            if not instance_name:
                continue

            # Derive log dir from instance name
            amp_log_dir = f"/mnt/gamestoreage2/ampinstances/{instance_name}/AMP_Logs"

            # Read KVP for regexes
            kvp_path = Path(f"/mnt/gamestoreage2/ampinstances/{instance_name}/GenericModule.kvp")
            join_regex = None
            leave_regex = None
            if kvp_path.exists():
                for line in kvp_path.read_text().splitlines():
                    if line.startswith('Console.UserJoinRegex='):
                        join_regex = line.split('=', 1)[1]
                    elif line.startswith('Console.UserLeaveRegex='):
                        leave_regex = line.split('=', 1)[1]

            guild_id = r.get('guild_id')
            # Stub guild_ids (vquest_main, shroud_main, val_main) are not real - skip
            real_guild = guild_id if guild_id and not guild_id.endswith('_main') else None
            configured = 1 if real_guild and r.get('notif_channel_id') else 0

            try:
                conn.execute(text("""
                    INSERT INTO gamebot_configs
                        (instance_name, game_type, amp_log_dir, join_regex, leave_regex,
                         guild_id, platform, notif_channel_id, live_log_channel_id,
                         stats_channel_id, server_update_channel_id, admin_role_id,
                         server_display_name, embed_color, show_server_name, show_ip_port,
                         show_password, show_usage_stats, show_player_count, show_top_5_players,
                         alert_join_leave, alert_live_logs, scheduler_hour, scheduler_minute,
                         schedule_overrides, server_password, configured)
                    VALUES
                        (:instance_name, :game_type, :amp_log_dir, :join_regex, :leave_regex,
                         :guild_id, :platform, :notif_channel_id, :live_log_channel_id,
                         :stats_channel_id, :server_update_channel_id, :admin_role_id,
                         :server_display_name, :embed_color, :show_server_name, :show_ip_port,
                         :show_password, :show_usage_stats, :show_player_count, :show_top_5_players,
                         :alert_join_leave, :alert_live_logs, :scheduler_hour, :scheduler_minute,
                         :schedule_overrides, :server_password, :configured)
                    ON DUPLICATE KEY UPDATE
                        game_type=VALUES(game_type),
                        amp_log_dir=VALUES(amp_log_dir),
                        join_regex=COALESCE(join_regex, VALUES(join_regex)),
                        leave_regex=COALESCE(leave_regex, VALUES(leave_regex))
                """), {
                    'instance_name': instance_name,
                    'game_type': m['game_type'],
                    'amp_log_dir': amp_log_dir,
                    'join_regex': join_regex,
                    'leave_regex': leave_regex,
                    'guild_id': real_guild,
                    'platform': r.get('platform', 'fluxer'),
                    'notif_channel_id': r.get('notif_channel_id') or None,
                    'live_log_channel_id': r.get('live_log_channel_id') or None,
                    'stats_channel_id': r.get('stats_channel_id') or None,
                    'server_update_channel_id': r.get('server_update_channel_id') or None,
                    'admin_role_id': r.get('admin_role_id') or None,
                    'server_display_name': r.get('server_display_name'),
                    'embed_color': r.get('embed_color', '#008080'),
                    'show_server_name': r.get('show_server_name', 1),
                    'show_ip_port': r.get('show_ip_port', 1),
                    'show_password': r.get('show_password', 0),
                    'show_usage_stats': r.get('show_usage_stats', 1),
                    'show_player_count': r.get('show_player_count', 1),
                    'show_top_5_players': r.get('show_top_5_players', 0),
                    'alert_join_leave': r.get('alert_join_leave', 1),
                    'alert_live_logs': r.get('alert_live_logs', 0),
                    'scheduler_hour': r.get('scheduler_hour', 3),
                    'scheduler_minute': r.get('scheduler_minute', 27),
                    'schedule_overrides': r.get('schedule_overrides'),
                    'server_password': r.get(m['password_col']),
                    'configured': configured,
                })
                print(f"  Migrated {m['table']} -> {instance_name} (guild={real_guild}, configured={configured})")
            except Exception as e:
                print(f"  ERROR migrating {instance_name}: {e}")

        conn.commit()


def main():
    print("Creating gamebot_configs table...")
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text(GAMEBOT_CONFIGS))
        conn.commit()
        print("  gamebot_configs: OK")

        conn.execute(text(GAMEBOT_PLAYERS))
        conn.commit()
        print("  gamebot_players: OK")

        print("\nMigrating existing per-game configs...")
        migrate_existing_configs(conn)

    print("\nDone. Old tables (vquest_configs etc.) are NOT dropped - kept for rollback.")
    print("Run the bot and verify, then drop old tables manually when satisfied.")


if __name__ == '__main__':
    main()
