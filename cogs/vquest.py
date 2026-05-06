# cogs/vquest.py - V Rising server management for QuestLogFluxer
#
# Ports the VQuestBot (Discord) to Fluxer. Logic copied 1:1 from:
#   /mnt/gamestoreage2/DiscordBots/vquestbot-gh/vquest_commands.py
#   /mnt/gamestoreage2/DiscordBots/vquestbot-gh/vquest_scheduler.py
#   /mnt/gamestoreage2/DiscordBots/vquestbot-gh/vquest_monitor.py
#
# Key differences from Discord version:
#   - Uses Fluxer Cog pattern (extends Cog, calls super().__init__(bot))
#   - Background tasks started via asyncio.ensure_future() in on_ready
#   - Commands use @Cog.command(name='...') NOT /vrising slash group
#   - Channel sends use await bot._http.send_message(str(channel_id), embeds=[embed])
#   - DB uses db_session_scope() from config (shared warden DB)
#   - AMP uses per-instance credentials (VRISING_SITE_USER / VRISING_SITE_PASSWORD)
#   - WarEventGameSettings written to JSON file on disk (same path as Discord bot)
#   - No buttons/views (Fluxer has no component support)
#   - Live serverinfo panel: delete old + repost every 30s (mirrors Discord refresh_serverinfo)

import asyncio
import datetime
import importlib.util
import json
import logging
import os
import requests
from pathlib import Path

import fluxer
from fluxer import Cog
from sqlalchemy import text

from config import logger, db_session_scope
from cogs.permissions import is_bot_manager

# AMP credentials
AMP_URL      = os.getenv('AMP_URL', '')
AMP_USER     = os.getenv('AMP_USER', '')
AMP_PASSWORD = os.getenv('AMP_PASSWORD', '')

# RCON credentials (Source RCON, direct to V Rising server)
VRISING_RCON_HOST     = os.getenv('VRISING_RCON_HOST', '')
VRISING_RCON_PORT     = int(os.getenv('VRISING_RCON_PORT', '25575'))
VRISING_RCON_PASSWORD = os.getenv('VRISING_RCON_PASSWORD', '')

VRISING_COLOR = 0x8B0000  # dark red

# Path to WarEventGameSettings JSON - same path as Discord bot
WAR_EVENT_JSON_PATH = Path("/mnt/vrising/v-rising/1829350/save-data/Settings/ServerGameSettings.json")

INTERVAL_ENUM = {
    0: "Minimum (30 mins-1 hr)",
    1: "VeryShort (1.5 hrs)",
    2: "Short (2 hrs)",
    3: "Medium (4 hrs)",
    4: "Long (8 hrs)",
    5: "VeryLong (12 hrs)",
    6: "Extensive (24 hrs)",
    7: "Maximum",
}
DURATION_ENUM = {
    0: "Minimum (15 mins)",
    1: "VeryShort (20 mins)",
    2: "Short (25 mins)",
    3: "Medium (30 mins)",
    4: "Long (35 mins)",
    5: "VeryLong (45 mins)",
    6: "Extensive (1 hr)",
    7: "Maximum (2 hrs)",
}

# -------------------------------------------------------------------------
# DB helpers - read from gamebot_configs (unified table, V Rising rows only)
# Column aliases map old vquest_configs names to gamebot_configs equivalents:
#   amp_instance_name  <- instance_name
#   vrising_password   <- server_password
#   serverinfo_id      <- serverinfo_message_id
#   pvec_messageid     <- NULL (not used in gamebot_configs, handled separately)
# -------------------------------------------------------------------------

_VQUEST_SELECT = (
    "SELECT guild_id, notif_channel_id, live_log_channel_id, "
    "server_update_channel_id, admin_role_id, "
    "instance_name AS amp_instance_name, "
    "server_display_name, server_password AS vrising_password, "
    "show_ip_port, show_password, show_player_count, show_top_5_players, "
    "alert_join_leave, alert_live_logs, "
    "NULL AS pvec_messageid, "
    "serverinfo_message_id AS serverinfo_id, "
    "serverchannel_message_id, "
    "schedule_overrides, scheduler_hour, scheduler_minute, schedule_enabled "
    "FROM gamebot_configs WHERE game_type = 'V Rising'"
)


def _load_guild_config(guild_id: str) -> dict | None:
    try:
        with db_session_scope() as db:
            row = db.execute(
                text(_VQUEST_SELECT + " AND guild_id = :g LIMIT 1"),
                {'g': guild_id},
            ).fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error(f"VQuestCog: config load failed for guild {guild_id}: {e}")
        return None


def _load_all_configs() -> list[dict]:
    try:
        with db_session_scope() as db:
            rows = db.execute(text(_VQUEST_SELECT)).fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception as e:
        logger.error(f"VQuestCog: failed to load all configs: {e}")
        return []



# -------------------------------------------------------------------------
# AMP helpers
# -------------------------------------------------------------------------

async def _get_amp_instance(instance_name: str):
    """Connect via ADS account, find the V Rising instance, re-auth with scoped account."""
    from ampapi.dataclass import APIParams
    from ampapi.bridge import Bridge
    from ampapi.controller import AMPControllerInstance as _AMPCtrl

    params = APIParams(url=AMP_URL, user=AMP_USER, password=AMP_PASSWORD)
    Bridge(api_params=params)
    ctrl = _AMPCtrl()
    await ctrl.get_instances()
    return next((i for i in ctrl.instances if i.instance_name == instance_name), None)


async def _get_server_status(instance_name: str) -> dict:
    """Return state label + metrics. Mirrors web dashboard logic 1:1."""
    from ampapi.dataclass import AMPInstanceState
    try:
        instance = await _get_amp_instance(instance_name)
        if not instance:
            return {'ok': False, 'error': f'Instance {instance_name} not found'}

        app_state_name = instance.app_state.name if hasattr(instance.app_state, 'name') else 'undefined'
        _APP_STATE_LABEL = {
            'undefined': 'Unknown', 'stopped': 'Stopped', 'pre_start': 'Pre-Start',
            'configuring': 'Configuring', 'starting': 'Starting', 'ready': 'Running',
            'restarting': 'Restarting', 'stopping': 'Stopping', 'sleeping': 'Sleeping',
            'failed': 'Failed', 'suspended': 'Suspended',
        }
        state_label = _APP_STATE_LABEL.get(app_state_name, app_state_name.replace('_', ' ').title())

        metrics = {}
        uptime = ''
        status_raw = {}
        if instance.app_state == AMPInstanceState.ready:
            try:
                st = await instance.get_status(format_data=False)
                if isinstance(st, dict):
                    metrics = st.get('metrics', {})
                    uptime = st.get('uptime', '')
                    status_raw = st
            except Exception:
                pass

        cpu_pct  = metrics.get('cpu_usage', {}).get('percent', 0)
        ram_pct  = metrics.get('memory_usage', {}).get('percent', 0)
        mem_used = metrics.get('memory_usage', {}).get('raw_value', 0)
        mem_tot  = metrics.get('memory_usage', {}).get('max_value', 0)
        players  = metrics.get('active_users', {}).get('raw_value', 0)
        max_plyr = metrics.get('active_users', {}).get('max_value', 0)
        is_running = (state_label == 'Running')

        # Port summaries for IP:port
        ip = 'Unknown'
        port = 'Unknown'
        try:
            ports = await instance.get_port_summaries(format_data=False)
            valid_ports = [p for p in ports if not p.get('internalonly', False) and p.get('port') is not None]
            preferred_order = ['Game Port', 'Game and Mods Port', 'Query Port']
            game_port = next((p for name in preferred_order for p in valid_ports if name.lower() in p.get('name', '').lower()), None)
            if not game_port and valid_ports:
                game_port = valid_ports[0]
            if game_port:
                ip = (
                    game_port.get('ip')
                    or game_port.get('hostname')
                    or requests.get('https://ifconfig.me', timeout=5).text.strip()
                    or 'Unknown'
                )
                port = str(game_port.get('port'))
        except Exception:
            pass

        return {
            'ok': True, 'state': state_label, 'is_running': is_running, 'uptime': uptime,
            'cpu_pct': cpu_pct, 'ram_pct': ram_pct,
            'mem_used_mb': mem_used, 'mem_total_mb': mem_tot,
            'players': players, 'players_max': max_plyr,
            'ip': ip, 'port': port,
            'instance': instance,
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}


# -------------------------------------------------------------------------
# RCON helper - Source RCON protocol, direct to V Rising server
# -------------------------------------------------------------------------

async def _send_rcon(command: str) -> str | None:
    """
    Send a command via Source RCON to the V Rising server.
    Returns the response string, or None on failure.
    """
    if not VRISING_RCON_HOST or not VRISING_RCON_PASSWORD:
        logger.warning('VQuestCog: RCON not configured (VRISING_RCON_HOST/PASSWORD missing)')
        return None

    try:
        import asyncio
        # Source RCON protocol:
        # Packet: size(4) + id(4) + type(4) + body(null-terminated) + null byte
        # Auth packet type = 3, command packet type = 2
        # Response type = 0

        def _build_packet(pid: int, ptype: int, body: str) -> bytes:
            body_encoded = body.encode('utf-8') + b'\x00\x00'
            size = 4 + 4 + len(body_encoded)
            import struct
            return struct.pack('<iii', size, pid, ptype) + body_encoded

        def _parse_packet(data: bytes):
            import struct
            if len(data) < 12:
                return None, None, None
            size, pid, ptype = struct.unpack('<iii', data[:12])
            body = data[12:12 + size - 8].rstrip(b'\x00').decode('utf-8', errors='replace')
            return pid, ptype, body

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(VRISING_RCON_HOST, VRISING_RCON_PORT),
            timeout=10
        )

        try:
            # Authenticate
            writer.write(_build_packet(1, 3, VRISING_RCON_PASSWORD))
            await writer.drain()
            auth_resp = await asyncio.wait_for(reader.read(4096), timeout=5)
            pid, _, _ = _parse_packet(auth_resp)
            if pid == -1:
                logger.error('VQuestCog: RCON authentication failed')
                return None

            # Send command then read until connection closes
            writer.write(_build_packet(2, 2, command))
            await writer.drain()

            chunks = []
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
                    if not chunk:
                        break
                    chunks.append(chunk)
                except asyncio.TimeoutError:
                    break

            resp_data = b''.join(chunks)
            _, _, body = _parse_packet(resp_data)
            logger.info(f'VQuestCog: RCON [{command}] -> {body!r}')
            return body

        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    except asyncio.TimeoutError:
        logger.warning(f'VQuestCog: RCON timeout sending [{command}]')
        return None
    except Exception as e:
        logger.warning(f'VQuestCog: RCON error [{command}]: {e}')
        return None


# -------------------------------------------------------------------------
# Embed builder - mirrors vquest_commands.py build_serverinfo_embed 1:1
# -------------------------------------------------------------------------

async def _build_serverinfo_embed(cfg: dict, guild_id: str) -> fluxer.Embed:
    """Full serverinfo embed: status, IP, players from DB, CPU/RAM, uptime, password.
    Mirrors VQuestBot build_serverinfo_embed() 1:1."""
    instance_name = cfg.get('amp_instance_name', '')
    display_name = cfg.get('server_display_name') or 'Unknown Server'

    embed = fluxer.Embed(title="V Rising Server Info", color=VRISING_COLOR)

    if not instance_name:
        embed.description = "AMP not linked - no instance configured for this server."
        embed.set_footer(text="Powered by VQuest - Casual Heroes Hosting")
        return embed

    status = await _get_server_status(instance_name)

    if not status.get('ok'):
        embed.description = f"Could not reach AMP: {status.get('error', 'Unknown error')}"
        embed.set_footer(text="Powered by VQuest - Casual Heroes Hosting")
        return embed

    is_running = status['is_running']
    cpu = status['cpu_pct']
    ram = status['ram_pct']
    users = status['players']
    max_users = status['players_max']
    uptime = status['uptime'] or 'N/A'
    ip = status['ip']
    port = status['port']
    server_password = cfg.get('vrising_password') or None

    # Load players from DB - mirrors Discord build_serverinfo_embed 1:1
    try:
        with db_session_scope() as db:
            rows = db.execute(
                text("SELECT character_name FROM vrising_players WHERE guild_id = :g"),
                {'g': guild_id},
            ).fetchall()
        current_players = [r.character_name for r in rows if r.character_name and r.character_name.strip()]
    except Exception:
        current_players = []

    # Build player list - same logic as Discord version
    if users > 0 and current_players:
        player_field_title = "Currently Online Players:"
        player_list = ""
        for idx, player in enumerate(current_players[:5], start=1):
            player_list += f"{idx}. {player.ljust(16)} (Online now)\n"
    else:
        player_field_title = "Players:"
        player_list = "No players online." if not current_players else '\n'.join(
            f"{idx}. {p}" for idx, p in enumerate(current_players[:5], 1)
        )

    embed.add_field(name="Server Status", value="Online" if is_running else "Offline", inline=False)
    embed.add_field(name="Server Name", value=f"```{display_name}```", inline=False)
    embed.add_field(name="IP Address", value=f"```{ip}:{port}```", inline=False)
    if server_password:
        embed.add_field(name="Server Password", value=f"```{server_password}```", inline=False)
    embed.add_field(name="CPU Usage", value=f"{cpu}%" if isinstance(cpu, (int, float)) else str(cpu), inline=True)
    embed.add_field(name="Memory Usage", value=f"{ram}%" if isinstance(ram, (int, float)) else str(ram), inline=True)
    embed.add_field(name="Uptime", value=str(uptime), inline=True)
    embed.add_field(name=player_field_title, value=player_list or "No players online.", inline=False)
    embed.add_field(name="Player Count", value=f"{users} / {max_users}", inline=True)
    embed.set_footer(text="Powered by VQuest - Casual Heroes Hosting")

    return embed


# -------------------------------------------------------------------------
# WarEventGameSettings JSON writer - mirrors vquest_scheduler.py 1:1
# -------------------------------------------------------------------------

def _write_war_event_json(day: str, settings: dict):
    war_settings = settings.get('WarEventGameSettings')
    if not war_settings:
        logger.warning(f"VQuestCog: no WarEventGameSettings for {day}")
        return
    try:
        if WAR_EVENT_JSON_PATH.exists():
            with WAR_EVENT_JSON_PATH.open('r') as f:
                current = json.load(f)
        else:
            current = {}
        current['WarEventGameSettings'] = war_settings
        with WAR_EVENT_JSON_PATH.open('w') as f:
            json.dump(current, f, indent=4)
        logger.info(f"VQuestCog: WarEventGameSettings updated for {day}")
    except Exception as e:
        logger.warning(f"VQuestCog: failed to write WarEventGameSettings JSON: {e}")


# -------------------------------------------------------------------------
# Schedule helpers
# -------------------------------------------------------------------------

def _load_day_presets(guild_id: str) -> dict | None:
    cfg = _load_guild_config(guild_id)
    if not cfg:
        return None

    overrides = cfg.get('schedule_overrides')
    if overrides:
        try:
            return json.loads(overrides)
        except Exception:
            pass

    preset_path = '/mnt/gamestoreage2/DiscordBots/vquestbot-gh/vquests_event_presets.py'
    try:
        spec = importlib.util.spec_from_file_location('_vquest_presets', preset_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, 'DAY_EVENT_PRESETS', None)
    except Exception as e:
        logger.warning(f"VQuestCog: failed to load preset file: {e}")
        return None


def _get_scheduler_time(guild_id: str) -> tuple[int, int]:
    cfg = _load_guild_config(guild_id)
    if not cfg:
        return 3, 27
    return (cfg.get('scheduler_hour') or 3), (cfg.get('scheduler_minute') or 27)


# -------------------------------------------------------------------------
# Main Cog
# -------------------------------------------------------------------------

class VQuestCog(Cog):
    """V Rising server management cog for QuestLogFluxer."""

    def __init__(self, bot):
        super().__init__(bot)
        self._monitor_task: asyncio.Task | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._last_run_date: str | None = None

    @Cog.listener()
    async def on_ready(self):
        logger.info("VQuestCog ready - starting monitor and scheduler loops")
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.ensure_future(self._status_monitor_loop())
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.ensure_future(self._daily_scheduler_loop())

    # ------------------------------------------------------------------
    # Status monitor - polls AMP every 30s, refreshes live serverinfo panel
    # Mirrors VQuestBot refresh_serverinfo (30s loop) + keep_amp_alive 1:1
    # ------------------------------------------------------------------

    async def _status_monitor_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                configs = _load_all_configs()
                for cfg in configs:
                    if not cfg.get('amp_instance_name'):
                        continue
                    guild_id = cfg['guild_id']

                    # Refresh the live serverinfo panel - mirrors Discord refresh_serverinfo 1:1
                    channel_id = cfg.get('serverchannel_message_id')
                    old_msg_id = cfg.get('serverinfo_id')
                    if channel_id:
                        try:
                            embed = await _build_serverinfo_embed(cfg, guild_id)
                            embed_dict = embed.to_dict() if hasattr(embed, 'to_dict') else embed
                            if old_msg_id:
                                try:
                                    await self.bot._http.edit_message(str(channel_id), str(old_msg_id), embeds=[embed_dict])
                                    continue
                                except Exception as edit_err:
                                    logger.debug(f"VQuestCog: edit_message failed for {old_msg_id}, will repost: {edit_err}")
                            resp = await self.bot._http.send_message(str(channel_id), embeds=[embed])
                            new_msg_id = resp.get('id') if isinstance(resp, resp.__class__) and hasattr(resp, 'get') else None
                            if new_msg_id and new_msg_id != old_msg_id:
                                with db_session_scope() as db:
                                    db.execute(
                                        text("UPDATE gamebot_configs SET serverinfo_message_id = :mid WHERE guild_id = :g AND game_type = 'V Rising'"),
                                        {'mid': str(new_msg_id), 'g': guild_id},
                                    )
                        except Exception as e:
                            logger.warning(f"VQuestCog: failed to refresh serverinfo for guild {guild_id}: {e}")

            except Exception as e:
                logger.warning(f"VQuestCog: status monitor error: {e}")
            await asyncio.sleep(30)

    # ------------------------------------------------------------------
    # Daily scheduler - mirrors vquest_scheduler.py daily_mode_check 1:1
    # ------------------------------------------------------------------

    async def _daily_scheduler_loop(self):
        await asyncio.sleep(30)
        while True:
            try:
                now = datetime.datetime.now()
                today = now.strftime('%Y-%m-%d')

                configs = _load_all_configs()
                for cfg in configs:
                    guild_id = cfg.get('guild_id')
                    if not guild_id or not cfg.get('amp_instance_name'):
                        continue
                    if not cfg.get('schedule_enabled', 1):
                        continue

                    sched_hour, sched_min = _get_scheduler_time(guild_id)
                    if now.hour == sched_hour and now.minute == sched_min and self._last_run_date != today:
                        self._last_run_date = today
                        logger.info(f"VQuestCog: triggering daily settings for guild {guild_id}")
                        asyncio.ensure_future(self._apply_daily_settings(cfg))

            except Exception as e:
                logger.warning(f"VQuestCog: scheduler loop error: {e}")

            await asyncio.sleep(60)

    async def _apply_daily_settings(self, cfg: dict):
        """Apply daily preset settings to AMP and notify Fluxer channel.
        Mirrors vquest_scheduler.py apply_vrising_settings() 1:1."""
        guild_id = cfg['guild_id']
        instance_name = cfg.get('amp_instance_name', '')

        try:
            presets = _load_day_presets(guild_id)
            if not presets:
                logger.warning(f"VQuestCog: no presets for guild {guild_id}")
                return

            day = datetime.datetime.now().strftime('%A')
            settings = presets.get(day)
            if not settings:
                logger.warning(f"VQuestCog: no settings for {day} in guild {guild_id}")
                return

            instance = await _get_amp_instance(instance_name)
            if not instance:
                logger.warning(f"VQuestCog: instance {instance_name} not found for guild {guild_id}")
                return

            logger.info(f"VQuestCog: applying {day} settings to {instance_name}")

            # Apply AMP settings - mirrors vquest_scheduler.py 1:1
            for key, value in settings.items():
                if key == 'WarEventGameSettings':
                    continue  # handled separately via JSON file
                if isinstance(value, dict):
                    for subkey, subvalue in value.items():
                        full_key = f"Meta.GenericModule.{key}.{subkey}"
                        await instance.set_config(full_key, str(subvalue))
                        logger.debug(f"VQuestCog: set {full_key} = {subvalue}")
                else:
                    await instance.set_config(f"Meta.GenericModule.{key}", str(value))
                    logger.debug(f"VQuestCog: set Meta.GenericModule.{key} = {value}")

            # Write WarEventGameSettings to JSON file - mirrors vquest_scheduler.py 1:1
            _write_war_event_json(day, settings)

            # Set server description
            blurb_map = {
                'Monday':    'Newblood Welcome - Boosted yields, lower risk.',
                'Tuesday':   'Arcane Surge - Stronger magic, Blood Moons.',
                'Wednesday': 'Ashen Sun - PvP skirmishes, brutal daylight.',
                'Thursday':  'Loot hunters. Harvest yields are lower, but drop tables are maxed.',
                'Friday':    'Twilight Chaos - PvP. Tougher enemies, long nights.',
                'Saturday':  'Trial of the Clans - Full PvP. Sieging.',
                'Sunday':    'Day of Rest - All modifiers off. Pure PvE.',
            }
            blurb = blurb_map.get(day, "Welcome to today's challenge!")
            description = (
                "Casual Heroes Hosting Services\n"
                f"- {blurb}\n"
                "- Clean, fair, welcoming. No -ism or toxicity (Ban)\n"
                "- Mods are active!\n"
                "- Active Admins | No Wipe Policy\n\n"
                "Join our Discord to report bugs or suggest future ideas!\n"
                "https://discord.gg/QBaxdqNDQH"
            )
            await instance.set_config("Meta.GenericModule.Description", description)

            # Backup cycle - mirrors vquest_scheduler.py 1:1
            logger.info(f"VQuestCog: stopping {instance_name} for backup")
            await instance.stop_application()
            await asyncio.sleep(10)

            try:
                await instance.take_backup(
                    name=f"{day}_AutoBackup",
                    description="Daily V Rising Backups.",
                    sticky=False,
                )
                await asyncio.sleep(60)
                logger.info(f"VQuestCog: backup complete for {instance_name}")
            except Exception as e:
                logger.warning(f"VQuestCog: backup failed for {instance_name}: {e}")

            await instance.start_application()
            logger.info(f"VQuestCog: {instance_name} restarted after backup")

            # Post update embed to server_update_channel_id
            notify_channel = cfg.get('server_update_channel_id')
            if not notify_channel:
                return

            war = settings.get('WarEventGameSettings', {})
            interval_name = INTERVAL_ENUM.get(war.get('Interval'), f"Unknown ({war.get('Interval')})")
            major_name    = DURATION_ENUM.get(war.get('MajorDuration'), "Unknown")
            minor_name    = DURATION_ENUM.get(war.get('MinorDuration'), "Unknown")
            weekday       = war.get('WeekdayTime', {})
            scale1        = war.get('ScalingPlayers1', {})
            scale4        = war.get('ScalingPlayers4', {})

            embed = fluxer.Embed(
                title=f"{day} Server Settings Activated!",
                description=blurb,
                color=VRISING_COLOR,
            )
            embed.add_field(name="Day",               value=day, inline=False)
            embed.add_field(name="Game Mode",          value=settings.get('GameModeType', '--'), inline=True)
            embed.add_field(name="Castle Raiding",     value="On" if settings.get('CastleDamageMode') == 'Always' else "Off", inline=True)
            embed.add_field(name="Player Damage",      value="On" if settings.get('PlayerDamageMode') == 'Always' else "Off", inline=True)
            embed.add_field(name="Sun Damage",         value=str(settings.get('SunDamageModifier', '--')), inline=True)
            embed.add_field(name="Yield Modifier",     value=str(settings.get('MaterialYieldModifier_Global', '--')), inline=True)
            embed.add_field(name="VBlood Power",       value=str(settings.get('UnitStatModifiers_VBlood', {}).get('PowerModifier', '--')), inline=True)
            embed.add_field(name="War Interval",       value=interval_name, inline=True)
            embed.add_field(name="Major Duration",     value=major_name, inline=True)
            embed.add_field(name="Minor Duration",     value=minor_name, inline=True)
            if weekday:
                embed.add_field(
                    name="Weekday Time",
                    value=f"{weekday.get('StartHour',0):02}:{weekday.get('StartMinute',0):02} - {weekday.get('EndHour',22):02}:{weekday.get('EndMinute',0):02}",
                    inline=False,
                )
            if scale1 and scale4:
                embed.add_field(name="1 Player Scaling",  value=f"Pts x{scale1.get('PointsModifier')} / Drop x{scale1.get('DropModifier')}", inline=True)
                embed.add_field(name="4+ Player Scaling", value=f"Pts x{scale4.get('PointsModifier')} / Drop x{scale4.get('DropModifier')}", inline=True)
            embed.set_footer(text="VQuest - Casual Heroes Hosting")

            try:
                resp = await self.bot._http.send_message(str(notify_channel), embeds=[embed])
                old_msg_id = cfg.get('pvec_messageid')
                if old_msg_id:
                    try:
                        await self.bot._http.delete_message(str(notify_channel), str(old_msg_id))
                    except Exception:
                        pass
                new_msg_id = resp.get('id') if isinstance(resp, dict) else None
                if new_msg_id:
                    with db_session_scope() as db:
                        db.execute(
                            text("UPDATE gamebot_configs SET serverchannel_message_id = :mid WHERE guild_id = :g AND game_type = 'V Rising'"),
                            {'mid': str(new_msg_id), 'g': guild_id},
                        )
            except Exception as e:
                logger.warning(f"VQuestCog: failed to send daily update embed for guild {guild_id}: {e}")

        except Exception as e:
            logger.error(f"VQuestCog: _apply_daily_settings failed for guild {guild_id}: {e}")

    # ------------------------------------------------------------------
    # Commands - mirrors /vrising slash group from Discord 1:1
    # ------------------------------------------------------------------

    @Cog.command(name="vquest_serverinfo")
    async def vquest_serverinfo(self, ctx):
        """Post live V Rising server info panel. Admin only.
        Mirrors /vrising serverinfo from Discord VQuestBot 1:1."""
        if not await is_bot_manager(ctx):
            await self.bot._http.send_message(str(ctx.channel.id), content="You don't have permission to do that.")
            return

        cfg = _load_guild_config(guild_id)
        if not cfg or not cfg.get('amp_instance_name'):
            await self.bot._http.send_message(str(ctx.channel.id), content="V Rising is not configured for this server.")
            return

        embed = await _build_serverinfo_embed(cfg, guild_id)
        resp = await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
        new_msg_id = resp.get('id') if isinstance(resp, dict) else None

        # Save channel + message ID - mirrors Discord serverinfo command 1:1
        if new_msg_id:
            # Delete previous serverinfo panel if exists
            old_msg_id = cfg.get('serverinfo_id')
            old_channel_id = cfg.get('serverchannel_message_id')
            if old_msg_id and old_channel_id:
                try:
                    await self.bot._http.delete_message(str(old_channel_id), str(old_msg_id))
                except Exception:
                    pass
            with db_session_scope() as db:
                db.execute(
                    text("UPDATE gamebot_configs SET serverchannel_message_id = :ch, serverinfo_message_id = :mid WHERE guild_id = :g AND game_type = 'V Rising'"),
                    {'ch': str(ctx.channel.id), 'mid': str(new_msg_id), 'g': guild_id},
                )

    @Cog.command(name="vquest_status")
    async def vquest_status(self, ctx):
        """Show live V Rising server status (one-off, not pinned)."""
        guild_id = str(ctx.guild.id) if hasattr(ctx, 'guild') and ctx.guild else None
        if not guild_id:
            return

        cfg = _load_guild_config(guild_id)
        if not cfg or not cfg.get('amp_instance_name'):
            await self.bot._http.send_message(
                str(ctx.channel.id),
                content="V Rising is not configured for this server.",
            )
            return

        embed = await _build_serverinfo_embed(cfg, guild_id)
        await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])

    @Cog.command(name="vquest_start")
    async def vquest_start(self, ctx):
        """Start the V Rising server. Admin only."""
        if not await is_bot_manager(ctx):
            await self.bot._http.send_message(str(ctx.channel.id), content="You don't have permission to do that.")
            return

        cfg = _load_guild_config(guild_id)
        if not cfg or not cfg.get('amp_instance_name'):
            await self.bot._http.send_message(str(ctx.channel.id), content="V Rising instance not configured.")
            return

        await self.bot._http.send_message(str(ctx.channel.id), content="Starting V Rising server...")
        try:
            instance = await _get_amp_instance(cfg['amp_instance_name'])
            if instance:
                await instance.start_application()
                await self.bot._http.send_message(str(ctx.channel.id), content="Server is starting.")
            else:
                await self.bot._http.send_message(str(ctx.channel.id), content="Could not find AMP instance.")
        except Exception as e:
            await self.bot._http.send_message(str(ctx.channel.id), content=f"Error: {e}")

    @Cog.command(name="vquest_stop")
    async def vquest_stop(self, ctx):
        """Stop the V Rising server. Admin only."""
        if not await is_bot_manager(ctx):
            await self.bot._http.send_message(str(ctx.channel.id), content="You don't have permission to do that.")
            return

        cfg = _load_guild_config(guild_id)
        if not cfg or not cfg.get('amp_instance_name'):
            await self.bot._http.send_message(str(ctx.channel.id), content="V Rising instance not configured.")
            return

        await self.bot._http.send_message(str(ctx.channel.id), content="Stopping V Rising server...")
        try:
            instance = await _get_amp_instance(cfg['amp_instance_name'])
            if instance:
                await instance.stop_application()
                await self.bot._http.send_message(str(ctx.channel.id), content="Server is stopping.")
            else:
                await self.bot._http.send_message(str(ctx.channel.id), content="Could not find AMP instance.")
        except Exception as e:
            await self.bot._http.send_message(str(ctx.channel.id), content=f"Error: {e}")

    @Cog.command(name="vquest_restart")
    async def vquest_restart(self, ctx):
        """Restart the V Rising server. Admin only."""
        if not await is_bot_manager(ctx):
            await self.bot._http.send_message(str(ctx.channel.id), content="You don't have permission to do that.")
            return

        cfg = _load_guild_config(guild_id)
        if not cfg or not cfg.get('amp_instance_name'):
            await self.bot._http.send_message(str(ctx.channel.id), content="V Rising instance not configured.")
            return

        await self.bot._http.send_message(str(ctx.channel.id), content="Restarting V Rising server...")
        try:
            instance = await _get_amp_instance(cfg['amp_instance_name'])
            if instance:
                await instance.restart_application()
                await self.bot._http.send_message(str(ctx.channel.id), content="Server is restarting.")
            else:
                await self.bot._http.send_message(str(ctx.channel.id), content="Could not find AMP instance.")
        except Exception as e:
            await self.bot._http.send_message(str(ctx.channel.id), content=f"Error: {e}")

    @Cog.command(name="vquest_players")
    async def vquest_players(self, ctx):
        """Show online V Rising players."""
        guild_id = str(ctx.guild.id) if hasattr(ctx, 'guild') and ctx.guild else None
        if not guild_id:
            return

        try:
            with db_session_scope() as db:
                rows = db.execute(
                    text("SELECT character_name FROM vrising_players WHERE guild_id = :g"),
                    {'g': guild_id},
                ).fetchall()

            embed = fluxer.Embed(title="V Rising - Online Players", color=VRISING_COLOR)
            if rows:
                embed.description = '\n'.join(f"- {r.character_name}" for r in rows)
            else:
                embed.description = "No players currently online."
            embed.set_footer(text="VQuest - Casual Heroes Hosting")
            await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
        except Exception as e:
            await self.bot._http.send_message(str(ctx.channel.id), content=f"Error fetching players: {e}")

    @Cog.command(name="vquest_setservername")
    async def vquest_setservername(self, ctx):
        """Set display server name. Usage: !vquest_setservername My Server Name
        Mirrors /vrising setservername from Discord 1:1."""
        if not await is_bot_manager(ctx):
            await self.bot._http.send_message(str(ctx.channel.id), content="You don't have permission to do that.")
            return

        # Args after command name
        name = ' '.join(ctx.content.split()[1:]).strip() if ctx.content else ''
        if not name:
            await self.bot._http.send_message(str(ctx.channel.id), content="Usage: !vquest_setservername <name>")
            return
        if len(name) > 100:
            await self.bot._http.send_message(str(ctx.channel.id), content="Server name too long. Keep it under 100 characters.")
            return

        with db_session_scope() as db:
            db.execute(
                text("UPDATE gamebot_configs SET server_display_name = :n WHERE guild_id = :g AND game_type = 'V Rising'"),
                {'n': name, 'g': guild_id},
            )
        await self.bot._http.send_message(str(ctx.channel.id), content=f"Server name updated to: **{name}**")

    @Cog.command(name="vquest_clearservername")
    async def vquest_clearservername(self, ctx):
        """Clear display server name. Mirrors /vrising clearservername from Discord 1:1."""
        if not await is_bot_manager(ctx):
            await self.bot._http.send_message(str(ctx.channel.id), content="You don't have permission to do that.")
            return

        with db_session_scope() as db:
            db.execute(
                text("UPDATE gamebot_configs SET server_display_name = NULL WHERE guild_id = :g AND game_type = 'V Rising'"),
                {'g': guild_id},
            )
        await self.bot._http.send_message(str(ctx.channel.id), content="Server name cleared.")

    @Cog.command(name="vquest_setpassword")
    async def vquest_setpassword(self, ctx):
        """Set server password shown in serverinfo. Usage: !vquest_setpassword mypass
        Mirrors /vrising setserverpassword from Discord 1:1."""
        if not await is_bot_manager(ctx):
            await self.bot._http.send_message(str(ctx.channel.id), content="You don't have permission to do that.")
            return

        password = ' '.join(ctx.content.split()[1:]).strip() if ctx.content else ''
        if not password:
            await self.bot._http.send_message(str(ctx.channel.id), content="Usage: !vquest_setpassword <password>")
            return
        if len(password) > 100:
            await self.bot._http.send_message(str(ctx.channel.id), content="Password too long. Keep it under 100 characters.")
            return

        with db_session_scope() as db:
            db.execute(
                text("UPDATE gamebot_configs SET server_password = :p, show_password = 1 WHERE guild_id = :g AND game_type = 'V Rising'"),
                {'p': password, 'g': guild_id},
            )
        await self.bot._http.send_message(str(ctx.channel.id), content="Server password updated.")

    @Cog.command(name="vquest_clearpassword")
    async def vquest_clearpassword(self, ctx):
        """Clear server password. Mirrors /vrising clearserverpassword from Discord 1:1."""
        if not await is_bot_manager(ctx):
            await self.bot._http.send_message(str(ctx.channel.id), content="You don't have permission to do that.")
            return

        with db_session_scope() as db:
            db.execute(
                text("UPDATE gamebot_configs SET server_password = NULL, show_password = 0 WHERE guild_id = :g AND game_type = 'V Rising'"),
                {'g': guild_id},
            )
        await self.bot._http.send_message(str(ctx.channel.id), content="Server password cleared.")

    # ------------------------------------------------------------------
    # RCON commands - BloodyBoss, BloodyEncounters, announcements
    # All admin-gated. ScarletRCON bridges VCF commands over Source RCON.
    # ------------------------------------------------------------------

    async def _rcon_guard(self, ctx) -> tuple[bool, str | None]:
        """Check admin permission and RCON config. Returns (ok, error_msg)."""
        if not await is_bot_manager(ctx):
            return False, "You don't have permission to do that."
        if not VRISING_RCON_HOST or not VRISING_RCON_PASSWORD:
            return False, "RCON is not configured for this server."
        return True, None

    @Cog.command(name="vr_announce")
    async def vr_announce(self, ctx):
        """Send a server-wide announcement via RCON. Usage: !vr_announce <message>"""
        ok, err = await self._rcon_guard(ctx)
        if not ok:
            await self.bot._http.send_message(str(ctx.channel.id), content=err)
            return

        msg = ' '.join(ctx.content.split()[1:]).strip() if ctx.content else ''
        if not msg:
            await self.bot._http.send_message(str(ctx.channel.id), content="Usage: `!vr_announce <message>`")
            return

        resp = await _send_rcon(f"announce {msg}")
        result = resp.strip() if resp else "Announcement sent."
        await self.bot._http.send_message(str(ctx.channel.id), content=f"Announced. ```{result}```")
