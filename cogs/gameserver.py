# cogs/gameserver.py - Unified game server management for QuestLogFluxer
#
# Replaces vquest.py, sdtd.py, shroudquest.py, valquest.py with a single
# data-driven cog. Game servers are auto-discovered from AMP on startup.
#
# Architecture:
#   - On ready: polls AMP for all instances, reads GenericModule.kvp for
#     log regexes, inserts unknown instances into gamebot_configs as unconfigured
#   - Quest Control is the authenticated AMP boundary for status and lifecycle actions
#   - Per configured instance: starts a LogWatcher, status monitor, daily scheduler
#   - Commands are generic: !gs_status, !gs_start, !gs_stop, !gs_restart,
#     !gs_players, !gs_backup -- work for ANY game
#   - 7DTD-specific: auto zombie spawn, telnet position tracker (preserved)
#   - No per-game env vars needed - single AMP_USER/AMP_PASSWORD

import asyncio
import datetime
import json
import logging
import os
import random
import re
import time
from glob import glob
from pathlib import Path

import aiohttp
import fluxer
from fluxer import Cog
from sqlalchemy import text, bindparam

from config import (
    QUEST_CONTROL_BOT_SECRET,
    QUESTLOG_INTERNAL_API_URL,
    db_session_scope,
    logger,
)

# Suppress noisy AMP library connection error logs - we handle them gracefully
logging.getLogger('ampapi').setLevel(logging.CRITICAL)

# ---- AMP credentials (single account) ----
AMP_URL      = os.getenv('AMP_URL', '')
AMP_USER     = os.getenv('AMP_USER', '')
AMP_PASSWORD = os.getenv('AMP_PASSWORD', '')
AMP_SERVER_PASSWORD_NODE = 'Meta.GenericModule.ServerPassword'

# ---- AMP instance base paths (local + remote NFS mounts) ----
# Primary local path. Additional paths can be added via AMP_INSTANCES_EXTRA
# as a colon-separated list, e.g. /mnt/remote/serverc/gamestorage1:/mnt/remote/serverb/gamestorage1
AMP_INSTANCES_BASE = Path(os.getenv('AMP_INSTANCES_BASE', '/mnt/gamestoreage2/ampinstances'))
_extra_raw = os.getenv('AMP_INSTANCES_EXTRA', '')
AMP_INSTANCES_PATHS: list[Path] = [AMP_INSTANCES_BASE] + [
    Path(p.strip()) for p in _extra_raw.split(':') if p.strip()
]
# A narrowly scoped compatibility mapping for the one AMP instance hosted on
# the first game-storage volume. Do not add the whole volume as a search root.
EXPLICIT_INSTANCE_ROOTS = {
    'ShroudQuest01': Path('/mnt/gamestoreage1/ShroudQuest01'),
}
# Server B stores AMP instances beneath ampdata rather than gamestorage. Keep
# this compatibility root in code so a stale environment list cannot make a
# real remote backup look missing after AMP has successfully created it.
_SERVER_B_AMP_ROOT = Path('/mnt/remote/serverb/ampdata/instances')
if _SERVER_B_AMP_ROOT.exists() and _SERVER_B_AMP_ROOT not in AMP_INSTANCES_PATHS:
    AMP_INSTANCES_PATHS.append(_SERVER_B_AMP_ROOT)

# ---- 7DTD-specific RCON (preserved from sdtd.py) ----
RCON_HOST     = os.getenv('RCON_HOST', '')
RCON_PORT     = int(os.getenv('RCON_PORT', '8081'))
RCON_PASSWORD = os.getenv('RCON_PASSWORD', '')

# ---- 7DTD position data dir (shared with 7questbot) ----
_DATA_DIR             = Path(os.getenv('Q7_DATA_DIR', '/mnt/gamestoreage2/DiscordBots/7questbot'))
PLAYER_POSITIONS_FILE = _DATA_DIR / 'player_positions.json'
ONLINE_PLAYERS_FILE   = _DATA_DIR / 'online_players.json'

# ---- Game color map ----
GAME_COLORS = {
    'V Rising':              0x8B0000,
    'Seven Days To Die':     0xE8890C,
    'Enshrouded':            0x7B4EA0,
    'Valheim':               0x3A7BD5,
    'Icarus':                0x2E8B57,
    'Palworld':              0xF4A261,
    'Palworld (Modded)':     0xF4A261,
}
DEFAULT_COLOR = 0x008080

GAME_EMOJIS = {
    'V Rising':              '🩸',
    'Seven Days To Die':     '🧟',
    'Enshrouded':            '🌫️',
    'Valheim':               '⚔️',
    'Icarus':                '🪐',
    'Palworld':              '🐾',
    'Palworld (Modded)':     '🐾',
}
DEFAULT_EMOJI = '🎮'

SDTD_SPAWN_RADIUS = 15

# ---------------------------------------------------------------------------
# Log Watcher - reads AMP_Logs/*.log files per instance (no env var needed)
# ---------------------------------------------------------------------------

class LogWatcher:
    """Tails the latest AMPLOG_*.log file in the given directory."""

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        # Real filenames are "AMPLOG_2026-07-12 22-42-00.log" - underscore after
        # AMPLOG, space before the time. A stray space-instead-of-underscore here
        # silently matched zero files, so no join/leave events were ever processed
        # for ANY instance - "Currently Online"/"Top Players" always showed empty.
        self.glob_pattern = os.path.join(log_dir, 'AMPLOG_*.log')
        self.current_file: str | None = None
        self.file_handle = None
        self.file_position: int = 0
        self._update_latest()

    def _update_latest(self):
        files = sorted(glob(self.glob_pattern))
        if not files:
            return
        latest = files[-1]
        if latest != self.current_file:
            if self.file_handle:
                self.file_handle.close()
            self.current_file = latest
            self.file_handle = open(latest, 'rb')
            # Seek to end on first open so we only read NEW lines
            self.file_handle.seek(0, 2)
            self.file_position = self.file_handle.tell()

    def read_new_lines(self) -> list[str]:
        self._update_latest()
        if not self.file_handle:
            return []
        self.file_handle.seek(self.file_position)
        raw = self.file_handle.readlines()
        self.file_position = self.file_handle.tell()
        lines = []
        for l in raw:
            try:
                lines.append(l.decode('utf-8', errors='replace').strip())
            except Exception:
                pass
        return [l for l in lines if l]

    def close(self):
        if self.file_handle:
            self.file_handle.close()
            self.file_handle = None


# ---------------------------------------------------------------------------
# KVP reader - extracts join/leave regex from AMP GenericModule.kvp
# ---------------------------------------------------------------------------

# Lines to suppress from live log - AMP internal noise generated by our own polling
_LIVE_LOG_BLOCKLIST = [
    'Authentication attempt for user SVC-AMP-SITEOPS',
    'Authentication success',
    'Authentication attempt for user SVC-AMP',
]

def _filter_live_log_lines(lines: list[str]) -> list[str]:
    """Filter out AMP internal noise, return only meaningful game server output."""
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if any(blocked in line for blocked in _LIVE_LOG_BLOCKLIST):
            continue
        out.append(line)
    return out


def _chunk_log_lines(lines: list[str], max_chars: int = 1900) -> list[str]:
    """Batch filtered log lines into chunks under Discord/Fluxer's message length
    limit. Shared by both platforms' live-log sends so a given batch of lines is
    grouped identically regardless of which channel(s) it's forwarded to."""
    chunks = []
    chunk = ''
    for line in lines:
        if len(chunk) + len(line) + 1 > max_chars:
            chunks.append(chunk)
            chunk = ''
        chunk += line + '\n'
    if chunk:
        chunks.append(chunk)
    return chunks


def _fix_regex(pattern: str) -> str:
    """Convert .NET/PCRE named groups (?<name>...) to Python syntax (?P<name>...).
    Duplicate named groups in alternatives receive numbered suffixes because
    Python does not allow a group name to be defined more than once.
    """
    import re as _re
    converted = _re.sub(r'\(\?<([^>]+)>', r'(?P<\1>', pattern)
    seen: dict[str, int] = {}

    def _dedup(m):
        name = m.group(1)
        seen[name] = seen.get(name, 0) + 1
        if seen[name] == 1:
            return m.group(0)
        return f'(?P<{name}_{seen[name]}>'

    return _re.sub(r'\(\?P<([^>]+)>', _dedup, converted)


def _capture(match: re.Match, name: str) -> str:
    """Return a named capture, including a renamed alternative capture."""
    groups = match.groupdict()
    value = groups.get(name)
    if value:
        return str(value).strip()
    for key, candidate in groups.items():
        if key.startswith(f'{name}_') and candidate:
            return str(candidate).strip()
    return ''


def _normalize_player_id(value: str | None) -> str | None:
    """Return the stable account ID portion emitted by game-server logs.

    Newer Palworld builds append a separate player ID to the account ID. The
    combined value is not the user ID and exceeds gamebot_players.userid.
    """
    if not value:
        return None
    normalized = re.split(
        r',\s*(?:Player\s+id|IP)\s*[:=]',
        str(value).strip(),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    return normalized[:64] or None


def _default_log_regexes(game_type: str) -> tuple[str | None, str | None]:
    """Return parsers only for AMP modules that do not publish log regexes."""
    normalized = (game_type or '').lower()
    if 'minecraft' in normalized:
        return (
            r'^\[[^\]]+\] \[[^\]]+/INFO\]: '
            r'(?P<username>[A-Za-z0-9_]{1,16})\[[^\]]+\]: '
            r'logged in with entity id\b',
            r'^\[[^\]]+\] \[[^\]]+/INFO\]: '
            r'(?P<username>[A-Za-z0-9_]{1,16}) lost connection:',
        )
    if 'palworld' in normalized:
        return (
            r'^(?:\[[\d-]+ [\d:]+\] \[LOG\] )?'
            r'(?P<username>.+) joined the server\. '
            r'\(User id: (?P<userid>[^,)]+)'
            r'(?:, Player id: [^)]+)?\)|'
            r"(?:\[[\d:]+\]\s?\[info\] )?'(?P<username_2>.+)' "
            r'\(UserId=(?P<userid_2>[^,)]+)'
            r'(, IP=(?P<endpoint>.+?))?\) has logged in\.$',
            r'^(?:\[[\d-]+ [\d:]+\] \[LOG\] )?'
            r'(?P<username>.+) left the server\. '
            r'\(User id: (?P<userid>[^,)]+)'
            r'(?:, Player id: [^)]+)?\)|'
            r"(?:\[[\d:]+\]\s?\[info\] )?'(?P<username_2>.+)' "
            r'\(UserId=(?P<userid_2>[^,)]+)'
            r'(, IP=(?P<endpoint>.+?))?\) has logged out\.$',
        )
    return None, None


def read_kvp(instance_name: str) -> dict:
    """Read AMP module KVP data for an instance.

    GenericModule is preferred when present, but module-specific files such as
    MinecraftModule are also scanned so this works for every AMP module.
    """
    explicit_root = EXPLICIT_INSTANCE_ROOTS.get(instance_name)
    instance_base = explicit_root if explicit_root and explicit_root.exists() else next(
        (p / instance_name for p in AMP_INSTANCES_PATHS if (p / instance_name).exists()),
        AMP_INSTANCES_BASE / instance_name
    )
    result = {}
    try:
        generic_path = instance_base / 'GenericModule.kvp'
        module_paths = sorted(instance_base.glob('*Module.kvp'))
        kvp_paths = (
            ([generic_path] if generic_path in module_paths else [])
            + [path for path in module_paths if path != generic_path]
        )
        if not kvp_paths:
            return result
    except PermissionError:
        logger.warning(
            f'read_kvp: permission denied on {instance_base}'
        )
        return result

    for kvp_path in kvp_paths:
        try:
            kvp_text = kvp_path.read_text(errors='replace')
        except PermissionError:
            logger.warning(f'read_kvp: permission denied reading {kvp_path}')
            continue
        for line in kvp_text.splitlines():
            if '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip()
            if key in ('Console.UserJoinRegex', 'Console.UserLeaveRegex'):
                value = _fix_regex(value)
            # GenericModule was loaded first and remains authoritative. Other
            # module files fill only fields GenericModule did not publish.
            result.setdefault(key, value)
    # Extract server password from App.AppSettings JSON
    app_settings_raw = result.get('App.AppSettings', '')
    if app_settings_raw:
        try:
            app_settings = json.loads(app_settings_raw)
            # Different games use different key names. Only publish the key
            # when the source actually contains it: missing and explicitly
            # empty have different meanings during the discovery upsert.
            if 'Password' in app_settings:
                pw = app_settings.get('Password')
                result['server_password'] = str(pw or '').strip()
            elif 'ServerPassword' in app_settings:
                pw = app_settings.get('ServerPassword')
                result['server_password'] = str(pw or '').strip()
        except (json.JSONDecodeError, AttributeError):
            pass

    # Enshrouded 1.0 keeps its join password in the Default user group, not in
    # AMP's generic ServerPassword field. Mark that configuration as externally
    # authoritative so an empty generic setting cannot erase Quest Control's
    # separately synchronized value. No role password is read or returned here.
    enshrouded_candidates = (
        instance_base / 'enshrouded' / '2278520' / 'enshrouded_server.json',
        instance_base / 'enshrouded_server.json',
    )
    enshrouded_path = next(
        (path for path in enshrouded_candidates if path.is_file()),
        None,
    )
    if enshrouded_path is not None:
        try:
            if enshrouded_path.stat().st_size <= 256 * 1024:
                payload = json.loads(
                    enshrouded_path.read_text(encoding='utf-8-sig')
                )
                groups = payload.get('userGroups') if isinstance(payload, dict) else None
                if isinstance(groups, list) and any(
                    isinstance(group, dict)
                    and str(group.get('name') or '').strip() == 'Default'
                    for group in groups
                ):
                    result['enshrouded_default_role_password'] = True
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.warning(
                'read_kvp: could not parse Enshrouded server roles for %s',
                instance_name,
            )
    return result


# ---------------------------------------------------------------------------
# Game-specific config readers
# ---------------------------------------------------------------------------

def read_ingame_server_name(instance_name: str, game_type: str) -> str | None:
    """Auto-discover and read the in-game server name from the game's config file.

    Tries common config file patterns and property/key names used across games.
    No per-game registration needed - works for any game AMP supports.

    Supported formats:
      - XML:  <property name="ServerName" value="..." />  (7DTD, Valheim-style)
              <ServerName>...</ServerName>                (direct element)
      - INI:  ServerName=...  or  server_name=...        (many survival games)
      - JSON: {"ServerName": "...", "Name": "..."}       (some modern games)
    """
    import xml.etree.ElementTree as ET
    import configparser

    instance_dir = next(
        (p / instance_name for p in AMP_INSTANCES_PATHS if (p / instance_name).exists()),
        None
    )
    if not instance_dir:
        return None

    # Common config file names to search for, in priority order
    config_globs = [
        '**/serverconfig.xml',
        '**/server_config.xml',
        '**/GameUserSettings.ini',
        '**/serverconfig.ini',
        '**/server.cfg',
        '**/server.properties',
        '**/settings.json',
        '**/config.json',
    ]

    # Common property names for server name across games, in priority order
    name_keys = ['ServerName', 'server_name', 'Name', 'hostname', 'ServerHostName', 'DisplayName']

    for glob_pattern in config_globs:
        matches = sorted(instance_dir.glob(glob_pattern))
        if not matches:
            continue
        config_file = matches[0]
        ext = config_file.suffix.lower()
        try:
            if ext == '.xml':
                tree = ET.parse(config_file)
                root = tree.getroot()
                # Try <property name="ServerName" value="..." />
                for key in name_keys:
                    for elem in root.iter('property'):
                        if elem.get('name', '').lower() == key.lower():
                            val = elem.get('value', '').strip()
                            if val:
                                return val
                # Try <ServerName>value</ServerName>
                for key in name_keys:
                    elem = root.find(f'.//{key}')
                    if elem is not None and elem.text and elem.text.strip():
                        return elem.text.strip()

            elif ext in ('.ini', '.cfg', '.properties'):
                content = config_file.read_text(errors='replace')
                # Try configparser first
                parser = configparser.RawConfigParser()
                try:
                    parser.read_string('[root]\n' + content)
                    for key in name_keys:
                        try:
                            val = parser.get('root', key).strip().strip('"\'')
                            if val:
                                return val
                        except configparser.NoOptionError:
                            pass
                except Exception:
                    pass
                # Fallback: line-by-line key=value scan
                for line in content.splitlines():
                    for key in name_keys:
                        if line.strip().lower().startswith(key.lower() + '='):
                            val = line.split('=', 1)[1].strip().strip('"\'')
                            if val:
                                return val

            elif ext == '.json':
                data = json.loads(config_file.read_text(errors='replace'))
                if isinstance(data, dict):
                    for key in name_keys:
                        val = data.get(key, '')
                        if val and isinstance(val, str):
                            return val.strip()

        except Exception:
            continue

    return None


# ---------------------------------------------------------------------------
# AMP helpers
# ---------------------------------------------------------------------------

async def _get_amp_instance(instance_name: str):
    """Connect to AMP and return the named instance (single account)."""
    if not AMP_URL or not AMP_USER or not AMP_PASSWORD:
        logger.warning('AMP credentials not configured')
        return None
    try:
        from ampapi.dataclass import APIParams
        from ampapi.bridge import Bridge
        from ampapi.controller import AMPControllerInstance as _AMPCtrl

        params = APIParams(url=AMP_URL, user=AMP_USER, password=AMP_PASSWORD)
        Bridge(api_params=params)
        ctrl = _AMPCtrl()
        await ctrl.get_instances()
        instance = next(
            (i for i in ctrl.instances if i.instance_name == instance_name),
            None
        )
        return instance
    except Exception as e:
        logger.error(f'AMP connect failed for {instance_name}: {e}')
        return None


async def _get_all_amp_instances() -> list:
    """Return all AMP instances visible to the single admin account."""
    if not AMP_URL or not AMP_USER or not AMP_PASSWORD:
        return []
    try:
        from ampapi.dataclass import APIParams
        from ampapi.bridge import Bridge
        from ampapi.controller import AMPControllerInstance as _AMPCtrl

        params = APIParams(url=AMP_URL, user=AMP_USER, password=AMP_PASSWORD)
        Bridge(api_params=params)
        ctrl = _AMPCtrl()
        await ctrl.get_instances()
        # Filter out ADS controller itself
        return [i for i in ctrl.instances if getattr(i, 'module', '') != 'ADS']
    except Exception as e:
        logger.error(f'AMP get_instances failed: {e}')
        return []


def _extract_amp_server_password(setting) -> tuple[bool, str]:
    """Extract AMP's authoritative player password.

    Returns (False, '') when the setting is unavailable, so callers preserve
    the last known DB value. Returns (True, '') when AMP explicitly has no
    password, so an intentional clear still propagates.
    """
    if not isinstance(setting, dict):
        return False, ''
    node = setting.get('node', setting.get('Node'))
    if node != AMP_SERVER_PASSWORD_NODE:
        return False, ''
    missing = object()
    value = setting.get(
        'current_value',
        setting.get('CurrentValue', missing),
    )
    if value is missing:
        return False, ''
    return True, '' if value is None else str(value)


async def _get_amp_server_password(instance, kvp: dict) -> str | None:
    """Read the password from AMP, falling back to an explicit KVP value."""
    if kvp.get('enshrouded_default_role_password'):
        return None
    try:
        setting = await asyncio.wait_for(
            instance.get_config(
                AMP_SERVER_PASSWORD_NODE,
                format_data=False,
            ),
            timeout=5,
        )
        found, password = _extract_amp_server_password(setting)
        if found:
            return password
    except Exception as e:
        logger.warning(
            'AMP discovery: password lookup failed for %s: %s',
            getattr(instance, 'instance_name', 'unknown'),
            e,
        )

    # None means this source did not contain the setting. The discovery SQL
    # must preserve the DB value in that case instead of replacing it with ''.
    return kvp.get('server_password')


async def _get_server_status(
    instance_name: str,
    public_ip: str | None = None,
    fluxer_guild_id: str | None = None,
) -> dict:
    """Return status from Quest Control, which is the sole AMP API boundary."""
    result = {
        'state': 'Unknown', 'is_running': False, 'uptime': None,
        'cpu': None, 'ram_mb': None, 'ram_max_mb': None,
        'player_count': 0, 'player_max': 0, 'ip': None, 'port': None,
    }
    guild_id = str(fluxer_guild_id or '').strip()
    if not guild_id or not QUEST_CONTROL_BOT_SECRET:
        logger.error(
            'Quest Control status disabled for %s: missing Fluxer guild or bot secret',
            instance_name,
        )
        return result
    url = (
        f"{QUESTLOG_INTERNAL_API_URL.rstrip('/')}"
        '/api/internal/quest-control/server/'
    )
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                params={
                    'instance': instance_name,
                    'guild_id': guild_id,
                    'platform': 'fluxer',
                },
                headers={'X-Bot-Secret': QUEST_CONTROL_BOT_SECRET},
                allow_redirects=False,
            ) as response:
                payload = await response.json(content_type=None)
                if response.status != 200 or not isinstance(payload, dict) or not payload.get('ok'):
                    logger.warning(
                        'Quest Control status rejected for %s guild=%s: HTTP %s',
                        instance_name,
                        guild_id,
                        response.status,
                    )
                    return result
        status = payload.get('status')
        if not isinstance(status, dict):
            return result
        for key in result:
            if key in status:
                result[key] = status[key]
        if isinstance(status.get('top_players'), list):
            result['top_players'] = status['top_players'][:10]
        if isinstance(status.get('server_password'), str):
            result['server_password'] = status['server_password']
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning(
            'Quest Control status unavailable for %s: %s',
            instance_name,
            type(exc).__name__,
        )
    return result


async def _quest_control_server_action(
    instance_name: str,
    fluxer_guild_id: str,
    action: str,
) -> tuple[bool, str | None]:
    """Request a tenant-bound lifecycle action through Quest Control."""
    if not QUEST_CONTROL_BOT_SECRET:
        return False, 'Quest Control bot authentication is not configured.'
    url = (
        f"{QUESTLOG_INTERNAL_API_URL.rstrip('/')}"
        '/api/internal/quest-control/server/'
    )
    try:
        timeout = aiohttp.ClientTimeout(total=100)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                json={
                    'instance': instance_name,
                    'guild_id': str(fluxer_guild_id),
                    'action': action,
                },
                headers={'X-Bot-Secret': QUEST_CONTROL_BOT_SECRET},
                allow_redirects=False,
            ) as response:
                payload = await response.json(content_type=None)
                if not isinstance(payload, dict):
                    return False, 'Quest Control returned an invalid response.'
                if response.status == 200 and payload.get('ok'):
                    return True, None
                return False, str(payload.get('error') or f'HTTP {response.status}')
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning(
            'Quest Control action unavailable for %s action=%s: %s',
            instance_name,
            action,
            type(exc).__name__,
        )
        return False, 'Quest Control is temporarily unavailable.'


# ---------------------------------------------------------------------------
# DB helpers (unified table)
# ---------------------------------------------------------------------------

_DEFAULT_BACKUP_WARNING_MINUTES = [15, 10, 5]


def _parse_backup_warning_minutes(raw) -> list[int]:
    """backup_warning_minutes is an admin-editable JSON array of integers, e.g.
    '[15,10,5]' or '[30,20,10,5,1]' - falls back to the documented default
    (15/10/5) only when genuinely unset, never hardcoded into the cycle logic."""
    if not raw:
        return list(_DEFAULT_BACKUP_WARNING_MINUTES)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and parsed:
            vals = sorted({int(v) for v in parsed if int(v) > 0}, reverse=True)
            if vals:
                return vals
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return list(_DEFAULT_BACKUP_WARNING_MINUTES)


def _load_all_configs() -> list[dict]:
    try:
        with db_session_scope() as db:
            rows = db.execute(text(
                "SELECT * FROM gamebot_configs "
                "WHERE configured = 1 "
                "AND (discord_guild_id IS NOT NULL OR fluxer_guild_id IS NOT NULL)"
            )).fetchall()
            configs = [dict(r._mapping) for r in rows]
            for cfg in configs:
                default_join, default_leave = _default_log_regexes(
                    cfg.get('game_type', '')
                )
                updates = {}
                if not cfg.get('join_regex') and default_join:
                    updates['join_regex'] = default_join
                    cfg['join_regex'] = default_join
                if not cfg.get('leave_regex') and default_leave:
                    updates['leave_regex'] = default_leave
                    cfg['leave_regex'] = default_leave
                if updates:
                    set_clause = ', '.join(
                        f'{field} = :{field}' for field in updates
                    )
                    updates['instance_name'] = cfg['instance_name']
                    db.execute(text(
                        f'UPDATE gamebot_configs SET {set_clause} '
                        'WHERE instance_name = :instance_name'
                    ), updates)
                    logger.info(
                        'Stored fallback join/leave parsers for %s',
                        cfg['instance_name'],
                    )
            return configs
    except Exception as e:
        logger.error(f'_load_all_configs: {e}')
        return []


def _load_config(instance_name: str) -> dict | None:
    try:
        with db_session_scope() as db:
            row = db.execute(text(
                "SELECT * FROM gamebot_configs WHERE instance_name = :n LIMIT 1"
            ), {'n': instance_name}).fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error(f'_load_config {instance_name}: {e}')
        return None


async def _has_permission(cfg: dict, ctx) -> bool:
    """Check if ctx.author can run admin game server commands.
    Delegates to is_bot_manager: owner > Administrator > Manage Messages > Bot Manager role.
    """
    from cogs.permissions import is_bot_manager
    return await is_bot_manager(ctx, cfg)


def _upsert_player(
    instance_name: str,
    guild_id: str,
    userid: str | None,
    username: str,
) -> bool:
    # gamebot_players.guild_id is a legacy denormalized ownership key. Use either
    # linked platform so Discord-only instances never write an empty guild id.
    storage_userid = _normalize_player_id(userid) or f'name:{username}'[:64]
    try:
        with db_session_scope() as db:
            db.execute(text("""
                INSERT INTO gamebot_players (instance_name, guild_id, userid, username)
                VALUES (:inst, :g, :uid, :name)
                ON DUPLICATE KEY UPDATE username=VALUES(username), last_seen=NOW()
            """), {
                'inst': instance_name,
                'g': guild_id,
                'uid': storage_userid,
                'name': username,
            })
        return True
    except Exception as e:
        logger.error(f'_upsert_player: {e}')
        return False


def _remove_player(
    instance_name: str,
    userid: str | None,
    username: str | None,
) -> bool:
    """Remove player by userid OR username - tries both so stale IDs don't block removal."""
    normalized_userid = _normalize_player_id(userid)
    if not normalized_userid and not username:
        return False
    try:
        with db_session_scope() as db:
            result = db.execute(text("""
                DELETE FROM gamebot_players
                WHERE instance_name=:inst
                  AND (
                    (:uid IS NOT NULL AND userid=:uid)
                    OR (:name IS NOT NULL AND username=:name)
                  )
            """), {
                'inst': instance_name,
                'uid': normalized_userid,
                'name': username,
            })
            return bool(result.rowcount)
    except Exception as e:
        logger.error(f'_remove_player: {e}')
        return False


def _lookup_username_by_userid(instance_name: str, userid: str) -> str | None:
    """Look up a character name by Steam/user ID - used when leave log only gives userid."""
    normalized_userid = _normalize_player_id(userid)
    if not normalized_userid:
        return None
    try:
        with db_session_scope() as db:
            row = db.execute(text(
                "SELECT username FROM gamebot_players WHERE instance_name=:inst AND userid=:uid LIMIT 1"
            ), {
                'inst': instance_name,
                'uid': normalized_userid,
            }).fetchone()
            return row.username if row else None
    except Exception:
        return None


def _get_online_players(instance_name: str) -> list[str]:
    try:
        with db_session_scope() as db:
            rows = db.execute(text(
                "SELECT username FROM gamebot_players WHERE instance_name=:inst ORDER BY joined_at"
            ), {'inst': instance_name}).fetchall()
            return [r.username for r in rows]
    except Exception:
        return []


def _update_serverinfo_id(instance_name: str, msg_id: str | None):
    try:
        with db_session_scope() as db:
            db.execute(text(
                "UPDATE gamebot_configs SET serverinfo_message_id=:mid WHERE instance_name=:n"
            ), {'mid': msg_id, 'n': instance_name})
    except Exception as e:
        logger.error(f'_update_serverinfo_id: {e}')


# ---------------------------------------------------------------------------
# Auto-discovery: poll AMP, register unknown instances in DB
# ---------------------------------------------------------------------------

async def discover_instances():
    """Poll AMP and upsert any new instances into gamebot_configs as unconfigured."""
    instances = await _get_all_amp_instances()
    if not instances:
        logger.warning('AMP discovery: no instances returned (check AMP credentials)')
        return

    for inst in instances:
        name = getattr(inst, 'instance_name', None)
        if not name:
            continue

        # Read from KVP
        kvp = read_kvp(name)
        join_regex   = kvp.get('Console.UserJoinRegex')
        leave_regex  = kvp.get('Console.UserLeaveRegex')
        display_name = getattr(inst, 'friendly_name', None) or kvp.get('Meta.DisplayName') or name
        explicit_root = EXPLICIT_INSTANCE_ROOTS.get(name)
        instance_base = explicit_root if explicit_root and explicit_root.exists() else next(
            (p / name for p in AMP_INSTANCES_PATHS if (p / name).exists()),
            AMP_INSTANCES_BASE / name
        )
        amp_log_dir  = str(instance_base / 'AMP_Logs')
        server_pw    = await _get_amp_server_password(inst, kvp)

        # game_type from AMP module_display_name or Meta.DisplayName
        game_type = (
            getattr(inst, 'module_display_name', None)
            or display_name
            or 'Unknown'
        )
        default_join_regex, default_leave_regex = _default_log_regexes(game_type)
        join_regex = join_regex or default_join_regex
        leave_regex = leave_regex or default_leave_regex

        try:
            with db_session_scope() as db:
                db.execute(text("""
                    INSERT INTO gamebot_configs
                        (instance_name, instance_id, game_type, amp_log_dir,
                         join_regex, leave_regex, server_display_name, server_password, configured)
                    VALUES
                        (:name, :iid, :gtype, :logdir,
                         :jrx, :lrx, :dname, :pw, 0)
                    ON DUPLICATE KEY UPDATE
                        instance_id          = COALESCE(instance_id, VALUES(instance_id)),
                        game_type            = VALUES(game_type),
                        amp_log_dir          = VALUES(amp_log_dir),
                        join_regex           = COALESCE(
                            VALUES(join_regex),
                            join_regex
                        ),
                        leave_regex          = COALESCE(
                            VALUES(leave_regex),
                            leave_regex
                        ),
                        server_display_name  = VALUES(server_display_name),
                        server_password      = COALESCE(
                            VALUES(server_password),
                            server_password
                        )
                """), {
                    'name':   name,
                    'iid':    getattr(inst, 'instance_id', None),
                    'gtype':  game_type,
                    'logdir': amp_log_dir,
                    'jrx':    join_regex,
                    'lrx':    leave_regex,
                    'dname':  display_name,
                    'pw':     server_pw,
                })
            password_state = (
                'unchanged' if server_pw is None
                else ('set' if server_pw else 'none')
            )
            logger.info(
                f'AMP discovery: registered {name} ({game_type}), '
                f'password={password_state}'
            )
        except Exception as e:
            logger.error(f'AMP discovery upsert failed for {name}: {e}')

    # Remove DB records for instances that no longer exist in AMP
    amp_names = [getattr(i, 'instance_name', None) for i in instances]
    amp_names = [n for n in amp_names if n]
    if amp_names:
        try:
            placeholders = ','.join([':n' + str(i) for i in range(len(amp_names))])
            params = {'n' + str(i): n for i, n in enumerate(amp_names)}
            with db_session_scope() as db:
                result = db.execute(text(f"""
                    DELETE FROM gamebot_configs
                    WHERE instance_name NOT IN ({placeholders})
                """), params)
                if result.rowcount:
                    logger.info(f'AMP discovery: removed {result.rowcount} stale instance(s) from DB')
        except Exception as e:
            logger.error(f'AMP discovery cleanup failed: {e}')


# ---------------------------------------------------------------------------
# Embed builder (generic, works for any game)
# ---------------------------------------------------------------------------

async def build_serverinfo_embed(cfg: dict) -> fluxer.Embed:
    instance_name = cfg['instance_name']
    game_type     = cfg.get('game_type', 'Game Server')
    display_name  = cfg.get('server_display_name') or game_type
    color         = GAME_COLORS.get(game_type, DEFAULT_COLOR)
    game_emoji    = GAME_EMOJIS.get(game_type, DEFAULT_EMOJI)

    status = await _get_server_status(
        instance_name,
        public_ip=cfg.get('public_ip') or None,
        fluxer_guild_id=cfg.get('fluxer_guild_id'),
    )
    players = _get_online_players(instance_name)

    is_running = status['is_running']
    raw_state = str(status.get('state') or 'Unknown').strip()
    if is_running:
        state_emoji, state_label = '🟢', 'Online'
    elif raw_state.lower() in {'stopped', 'offline', 'failed', 'suspended'}:
        state_emoji, state_label = '🔴', 'Offline'
    elif raw_state.lower() == 'unknown':
        state_emoji, state_label = '⚪', 'Status unavailable'
    else:
        state_emoji, state_label = '🟡', raw_state

    embed = fluxer.Embed(
        title=f'{game_emoji} {display_name} Server Info',
        color=color,
    )

    # Row 1: Status + Players (player count respects toggle)
    embed.add_field(name='Server Status', value=f'{state_emoji} {state_label}', inline=True)
    if cfg.get('show_player_count', True):
        player_count = status['player_count'] or len(players)
        player_max   = status['player_max'] or 0
        pc_str = f"{player_count}/{player_max}" if player_max else str(player_count)
        embed.add_field(name='Players', value=pc_str, inline=True)

    # Row 2: In-game server name (from game config file) or fallback to display name
    ingame_name = read_ingame_server_name(instance_name, game_type)
    embed.add_field(name='Server Name', value=f"```{ingame_name or display_name}```", inline=False)

    # Connect info - respects show_ip_port toggle
    if cfg.get('show_ip_port', True) and status.get('ip'):
        connect = f"{status['ip']}:{status['port']}" if status.get('port') else status['ip']
        embed.add_field(name='IP Address', value=f"```{connect}```", inline=False)

    # Quest Control includes the cached password only when show_password is enabled.
    server_password = status.get('server_password')
    if server_password:
        embed.add_field(name='Server Password', value=f"```{server_password}```", inline=False)

    # Stats: CPU / RAM / Uptime
    if status['cpu'] is not None:
        embed.add_field(name='CPU Usage', value=f"{status['cpu']}%", inline=True)
    if status['ram_mb'] is not None:
        embed.add_field(name='Memory Usage', value=f"{int(status['ram_mb'])} MB", inline=True)
    if status['uptime']:
        embed.add_field(name='Uptime', value=str(status['uptime']), inline=True)

    # Online players / top players by playtime - respects show_top_5_players toggle
    if cfg.get('show_top_5_players', True):
        if players:
            lines_out = []
            char_count = 0
            for i, name in enumerate(players, 1):
                line = f"{i}. {name}"
                if char_count + len(line) + 1 > 1000:
                    lines_out.append(f'... and {len(players) - i + 1} more')
                    break
                lines_out.append(line)
                char_count += len(line) + 1
            embed.add_field(name=f'Currently Online ({len(players)})', value='\n'.join(lines_out), inline=False)
        else:
            # No one online - show top players by playtime from AMP analytics
            top_players = status.get('top_players') or []
            if top_players:
                tp_lines = '\n'.join(
                    f"{i}. {player.get('username')}  {player.get('session_time', '')}".rstrip()
                    for i, player in enumerate(top_players[:10], 1)
                    if isinstance(player, dict) and player.get('username')
                )
                if tp_lines:
                    embed.add_field(name='Top Players by Playtime (30d)', value=tp_lines, inline=False)
            elif is_running:
                embed.add_field(name='Currently Online', value='No players online.', inline=False)

    embed.set_footer(text=f'Powered by QuestLog - Casual Heroes')
    embed.timestamp = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
    return embed


# ---------------------------------------------------------------------------
# 7DTD-specific: auto zombie spawner (preserved from sdtd.py)
# ---------------------------------------------------------------------------

COMMON_ZOMBIES = [
    'zombieBiker', 'zombieNurse', 'zombieBurnt', 'zombieSpiderFeral',
    'zombieJanitorFeral', 'zombieMarlene', 'zombieArlene',
]
FACTIONS = {
    'feral_strike':    ['zombieSpiderFeral', 'zombieJanitorFeral', 'zombieMarlene'],
    'radiated_wave':   ['zombieYoRadiated', 'zombieNurseRadiated', 'zombieSkateboarderFeral'],
    'hazmat_response': ['zombieSoldier', 'zombieHazmat', 'zombieLab'],
}

def _calculate_spawn_count(player_count: int) -> int:
    if player_count <= 1:  return random.randint(1, 2)
    if player_count <= 3:  return random.randint(2, 4)
    if player_count <= 5:  return random.randint(3, 5)
    if player_count <= 10: return random.randint(5, 8)
    if player_count <= 20: return random.randint(8, 12)
    return random.randint(12, 20)

def _get_spawn_delay(player_count: int) -> int:
    if player_count <= 1:  return random.randint(10800, 14400)
    if player_count <= 3:  return random.randint(5400, 7200)
    if player_count <= 5:  return random.randint(3600, 5400)
    if player_count <= 10: return random.randint(1800, 3600)
    if player_count <= 20: return random.randint(900, 1800)
    return random.randint(600, 900)

def _load_pos() -> dict:
    try:
        if PLAYER_POSITIONS_FILE.exists():
            return json.loads(PLAYER_POSITIONS_FILE.read_text())
    except Exception:
        pass
    return {}

def _player_online(username: str) -> bool:
    try:
        if ONLINE_PLAYERS_FILE.exists():
            data = json.loads(ONLINE_PLAYERS_FILE.read_text())
            return any(p.get('name') == username for p in data.get('players', []))
    except Exception:
        pass
    return False

async def _is_blood_moon(instance_name: str) -> bool:
    # Blood-moon detection requires game time, which AMP status does not expose.
    # Preserve the existing disabled behavior without performing a useless status call.
    return False


# ---------------------------------------------------------------------------
# Main Cog
# ---------------------------------------------------------------------------

class GameServerCog(Cog):

    def __init__(self, bot):
        super().__init__(bot)
        self._log_watchers: dict[str, LogWatcher] = {}
        self._spawn_delay_tasks: dict[str, asyncio.Task] = {}
        self._known_players: dict[str, set] = {}
        self._background_tasks: dict[str, asyncio.Task] = {}
        self._amp_console_instances: dict[str, object] = {}
        self._amp_console_primed: set[str] = set()
        self._amp_console_seen: dict[
            str,
            dict[tuple[str, str, str], float],
        ] = {}
        self._recent_player_events: dict[
            tuple[str, str, str],
            float,
        ] = {}

    def _claim_player_event(
        self,
        instance_name: str,
        username: str,
        userid: str | None,
        joined: bool,
    ) -> bool:
        """Claim one real player transition and reject immediate replays."""
        now = time.monotonic()
        event = 'joined' if joined else 'left'
        opposite = 'left' if joined else 'joined'
        identity = _normalize_player_id(userid) or username.casefold()
        key = (instance_name, event, identity)
        opposite_key = (instance_name, opposite, identity)

        # A real opposite transition makes the next matching event valid.
        self._recent_player_events.pop(opposite_key, None)

        previous = self._recent_player_events.get(key)
        if previous is not None and now - previous < 300:
            logger.warning(
                'Suppressed replayed %s event for %s on %s',
                event,
                username,
                instance_name,
            )
            return False

        self._recent_player_events[key] = now
        cutoff = now - 1800
        self._recent_player_events = {
            event_key: seen_at
            for event_key, seen_at in self._recent_player_events.items()
            if seen_at >= cutoff
        }
        return True

    # ---- Lifecycle ----

    @Cog.listener()
    async def on_ready(self):
        """Start each long-running worker once.

        Fluxer can emit on_ready again after a reconnect. Starting fresh loops on
        every event caused duplicate backup cycles and duplicate AMP polling.
        """
        workers = {
            'discovery': self._discovery_loop,
            'status_monitor': self._status_monitor_loop,
            'log_watcher': self._log_watcher_loop,
            'daily_scheduler': self._daily_scheduler_loop,
        }
        started = []
        for name, worker in workers.items():
            task = self._background_tasks.get(name)
            if task is None or task.done():
                self._background_tasks[name] = asyncio.create_task(
                    worker(),
                    name=f'gameserver:{name}',
                )
                started.append(name)

        if started:
            logger.info(
                'GameServerCog: started background workers: %s',
                ', '.join(started),
            )
        else:
            logger.info('GameServerCog: background workers already running')

    async def cog_unload(self):
        """Cancel workers when this cog is unloaded or reloaded."""
        for task in self._background_tasks.values():
            if task and not task.done():
                task.cancel()
        self._background_tasks.clear()

    # ---- Discovery loop ----

    async def _discovery_loop(self):
        """Poll AMP every 5 minutes and register new instances."""
        while True:
            try:
                await discover_instances()
            except Exception as e:
                logger.error(f'discovery_loop error: {e}')
            await asyncio.sleep(300)

    # ---- Status monitor: rebuilds pinned server info embed every 60s ----

    async def _status_monitor_loop(self):
        await asyncio.sleep(15)
        while True:
            try:
                configs = _load_all_configs()
                for cfg in configs:
                    if not cfg.get('stats_channel_id'):
                        continue
                    try:
                        await self._refresh_serverinfo(cfg)
                    except Exception as e:
                        logger.error(f'status_monitor {cfg["instance_name"]}: {e}')
            except Exception as e:
                logger.error(f'status_monitor_loop crashed: {e}')
            await asyncio.sleep(30)

    async def _refresh_serverinfo(self, cfg: dict):
        instance_name = cfg['instance_name']
        channel_id    = cfg.get('stats_channel_id')
        if not channel_id:
            return
        embed = await build_serverinfo_embed(cfg)
        old_msg_id = cfg.get('serverinfo_message_id')

        def _is_channel_dead(err_str: str) -> bool:
            """True when the channel/guild itself is gone or bot has no access - don't post new."""
            return any(k in err_str for k in ('unknown_guild', 'unknown channel', 'access_denied', '403', 'forbidden'))

        def _is_message_gone(err_str: str) -> bool:
            """True when only the specific message is missing - safe to post a fresh one."""
            return 'unknown message' in err_str and not _is_channel_dead(err_str)

        # Try to edit existing message so it stays pinned in place
        if old_msg_id:
            try:
                await self.bot._http.edit_message(str(channel_id), str(old_msg_id), embeds=[embed.to_dict()])
                return
            except Exception as e:
                err_str = str(e).lower()
                logger.warning(f'[monitor] edit_message failed for {instance_name} msg={old_msg_id}: {e!r}')
                if _is_channel_dead(err_str):
                    # Channel/guild gone or no access - skip entirely, don't spam new messages
                    logger.debug(f'[monitor] Channel dead for {instance_name} - skipping')
                    return
                if _is_message_gone(err_str):
                    # Only the message was deleted - clear ID and post fresh below
                    _update_serverinfo_id(instance_name, None)
                    old_msg_id = None
                else:
                    # Rate limit or transient error - skip this cycle
                    logger.debug(f'[monitor] Transient error for {instance_name} - skipping cycle')
                    return

        # No existing message (or it was deleted) - post a new one
        try:
            resp = await self.bot._http.send_message(str(channel_id), embeds=[embed])
            new_id = resp.get('id') if isinstance(resp, dict) else None
            _update_serverinfo_id(instance_name, new_id)
        except Exception as e:
            logger.error(f'[monitor] send_message failed for {instance_name}: {e}')

    # ---- Log watcher loop: one watcher per configured instance ----

    async def _read_amp_console_lines(self, cfg: dict) -> list[str]:
        """Read new console entries when an instance's log path is unavailable."""
        instance_name = cfg['instance_name']
        instance = self._amp_console_instances.get(instance_name)
        if instance is None:
            instance = await _get_amp_instance(instance_name)
            if instance is None:
                return []
            self._amp_console_instances[instance_name] = instance

        try:
            updates = await asyncio.wait_for(
                instance.get_updates(),
                timeout=5,
            )
        except Exception as e:
            logger.warning(
                f'AMP console watcher failed for {instance_name}: {e}'
            )
            self._amp_console_instances.pop(instance_name, None)
            self._amp_console_primed.discard(instance_name)
            return []

        entries = (
            updates.get('console_entries', [])
            if isinstance(updates, dict)
            else getattr(updates, 'console_entries', [])
        )

        parsed_entries = []
        is_minecraft = 'minecraft' in (
            cfg.get('game_type', '').lower()
        )
        for entry in entries:
            if isinstance(entry, dict):
                contents = str(entry.get('contents') or '').strip()
                source = str(entry.get('source') or '').strip()
                timestamp = str(entry.get('timestamp') or '')
            else:
                contents = str(getattr(entry, 'contents', '') or '').strip()
                source = str(getattr(entry, 'source', '') or '').strip()
                timestamp = str(getattr(entry, 'timestamp', '') or '')
            if not contents:
                continue
            signature = (timestamp, source, contents)
            if is_minecraft:
                time_match = re.search(r'T(\d{2}:\d{2}:\d{2})', timestamp)
                display_time = time_match.group(1) if time_match else '00:00:00'
                if source:
                    contents = f'[{display_time}] [{source}]: {contents}'
            parsed_entries.append((signature, contents))

        seen = self._amp_console_seen.setdefault(instance_name, {})
        now = time.monotonic()

        # AMP can return recent history on the first call or after reconnecting.
        # Remember those entries before priming so the next poll cannot replay
        # the same join/leave lines.
        if instance_name not in self._amp_console_primed:
            for signature, _ in parsed_entries:
                seen[signature] = now
            self._amp_console_primed.add(instance_name)
            logger.info(
                f'AMP console watcher started for {instance_name}'
            )
            return []

        lines = []
        for signature, contents in parsed_entries:
            timestamp = signature[0]
            previous = seen.get(signature)
            # Timestamped entries are stable AMP events. Some modules omit a
            # timestamp, so only suppress those identical lines briefly.
            if previous is not None and (
                bool(timestamp) or now - previous < 60
            ):
                continue
            seen[signature] = now
            lines.append(contents)

        if len(seen) > 4096:
            newest = sorted(
                seen.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:2048]
            self._amp_console_seen[instance_name] = dict(newest)
        return lines

    async def _log_watcher_loop(self):
        await asyncio.sleep(20)
        while True:
            configs = _load_all_configs()
            for cfg in configs:
                instance_name = cfg['instance_name']
                log_dir = cfg.get('amp_log_dir', '')
                try:
                    log_dir_exists = bool(log_dir) and Path(log_dir).exists()
                except PermissionError:
                    logger.warning(
                        f'LogWatcher: permission denied on {log_dir}; '
                        f'using AMP console feed for {instance_name}'
                    )
                    log_dir_exists = False
                if not log_dir_exists:
                    if instance_name not in self._known_players:
                        self._known_players[instance_name] = set(
                            _get_online_players(instance_name)
                        )
                    lines = await self._read_amp_console_lines(cfg)
                    if lines:
                        await self._process_log_lines(lines, cfg)
                    continue
                if instance_name not in self._log_watchers:
                    try:
                        self._log_watchers[instance_name] = LogWatcher(log_dir)
                        self._known_players[instance_name] = set(
                            _get_online_players(instance_name)
                        )
                        logger.info(f'LogWatcher started for {instance_name}')
                    except Exception as e:
                        logger.error(f'LogWatcher init {instance_name}: {e}')
                        continue
                watcher = self._log_watchers[instance_name]
                try:
                    lines = watcher.read_new_lines()
                    if lines:
                        await self._process_log_lines(lines, cfg)
                except Exception as e:
                    logger.error(f'log_watcher_loop {instance_name}: {e}')
            await asyncio.sleep(3)

    async def _send_join_leave_notice(
        self,
        cfg: dict,
        username: str,
        joined: bool,
    ):
        """Send one player event to every configured platform destination."""
        if not cfg.get('alert_join_leave'):
            return

        instance_name = cfg['instance_name']
        server_name = cfg.get('server_display_name') or instance_name
        event = 'joined' if joined else 'left'
        title = 'Player Joined' if joined else 'Player Left'
        description = f'**{username}** {event} **{server_name}**'
        color = GAME_COLORS.get(cfg.get('game_type', ''), DEFAULT_COLOR)

        fluxer_channel = cfg.get('join_leave_fluxer_channel_id')
        if cfg.get('fluxer_guild_id') and fluxer_channel:
            try:
                embed = fluxer.Embed(
                    title=title,
                    description=description,
                    color=color,
                )
                embed.set_footer(
                    text='Hosted by CasualHeroes - Powered by QuestLog'
                )
                await self.bot._http.send_message(
                    str(fluxer_channel),
                    embeds=[embed],
                )
            except Exception as e:
                logger.error(
                    f'join_leave fluxer send {instance_name}: {e}'
                )

        discord_channel = cfg.get('join_leave_discord_channel_id')
        if cfg.get('discord_guild_id') and discord_channel:
            try:
                with db_session_scope() as db:
                    db.execute(text(
                        "INSERT INTO discord_pending_broadcasts "
                        "(guild_id, channel_id, payload, created_at) "
                        "VALUES (:g, :c, :p, :t)"
                    ), {
                        'g': int(cfg['discord_guild_id']),
                        'c': int(discord_channel),
                        'p': json.dumps({
                            'action': 'direct',
                            'title': title,
                            'description': description,
                            'color': color,
                            'footer': (
                                'Hosted by CasualHeroes - Powered by QuestLog'
                            ),
                        }),
                        't': int(time.time()),
                    })
            except Exception as e:
                logger.error(
                    f'join_leave discord send {instance_name}: {e}'
                )

    async def _process_log_lines(self, lines: list[str], cfg: dict):
        instance_name = cfg['instance_name']
        known_players = self._known_players.setdefault(
            instance_name,
            set(),
        )
        default_join, default_leave = _default_log_regexes(
            cfg.get('game_type', '')
        )
        join_pattern = cfg.get('join_regex') or default_join
        leave_pattern = cfg.get('leave_regex') or default_leave
        player_guild_id = (
            cfg.get('discord_guild_id')
            or cfg.get('fluxer_guild_id')
            or ''
        )

        try:
            join_re = re.compile(join_pattern) if join_pattern else None
        except re.error as e:
            logger.error(
                f'Invalid join regex for {instance_name}: {e}'
            )
            join_re = None
        try:
            leave_re = re.compile(leave_pattern) if leave_pattern else None
        except re.error as e:
            logger.error(
                f'Invalid leave regex for {instance_name}: {e}'
            )
            leave_re = None

        for line in lines:
            # --- Join ---
            if join_re:
                m = join_re.search(line)
                if m:
                    username = _capture(m, 'username')
                    userid = _normalize_player_id(
                        _capture(m, 'userid') or None
                    )
                    if username and username not in known_players:
                        known_players.add(username)
                        _upsert_player(
                            instance_name,
                            player_guild_id,
                            userid,
                            username,
                        )
                        if self._claim_player_event(
                            instance_name,
                            username,
                            userid,
                            joined=True,
                        ):
                            await self._send_join_leave_notice(
                                cfg,
                                username,
                                joined=True,
                            )
                        # 7DTD: cancel spawn delay to trigger sooner when player joins
                        if '7dtd' in instance_name.lower() or 'sdtd' in instance_name.lower():
                            t = self._spawn_delay_tasks.get(instance_name)
                            if t and not t.done():
                                t.cancel()
                    continue

            # --- Leave ---
            if leave_re:
                m = leave_re.search(line)
                if m:
                    username = _capture(m, 'username')
                    userid = _normalize_player_id(
                        _capture(m, 'userid') or None
                    )
                    # If regex has no username group (e.g. V Rising only gives Steam ID on disconnect)
                    # look up the character name from DB using userid
                    if not username and userid:
                        username = _lookup_username_by_userid(instance_name, userid) or ''
                    if username:
                        known_players.discard(username)
                        _remove_player(instance_name, userid, username)
                        if self._claim_player_event(
                            instance_name,
                            username,
                            userid,
                            joined=False,
                        ):
                            await self._send_join_leave_notice(
                                cfg,
                                username,
                                joined=False,
                            )

        # --- Live log forwarding ---
        # Platform-specific channels, routed independently - a Discord channel picked
        # here has no meaning to this bot's own send path (Fluxer only), so it goes
        # through the same discord_pending_broadcasts outbox _send_backup_notice uses.
        if cfg.get('alert_live_logs'):
            filtered = _filter_live_log_lines(lines)
            if filtered:
                for chunk in _chunk_log_lines(filtered):
                    fluxer_channel = cfg.get('live_log_fluxer_channel_id')
                    if fluxer_channel and cfg.get('fluxer_guild_id'):
                        try:
                            await self.bot._http.send_message(str(fluxer_channel), content=f'```\n{chunk}\n```')
                        except Exception as e:
                            logger.error(f'live_log fluxer send {instance_name}: {e}')

                    discord_channel = cfg.get('live_log_discord_channel_id')
                    if discord_channel and cfg.get('discord_guild_id'):
                        try:
                            with db_session_scope() as db:
                                db.execute(text(
                                    "INSERT INTO discord_pending_broadcasts (guild_id, channel_id, payload, created_at) "
                                    "VALUES (:g, :c, :p, :t)"
                                ), {
                                    'g': int(cfg['discord_guild_id']), 'c': int(discord_channel),
                                    'p': json.dumps({'action': 'direct', 'description': f'```\n{chunk}\n```', 'color': DEFAULT_COLOR}),
                                    't': int(time.time()),
                                })
                        except Exception as e:
                            logger.error(f'live_log discord send {instance_name}: {e}')

    # ---- 7DTD auto zombie spawn ----

    async def _spawn_loop_for_instance(self, cfg: dict):
        """Auto zombie spawner for 7DTD instances."""
        instance_name = cfg['instance_name']
        while True:
            players = _get_online_players(instance_name)
            if not players:
                await asyncio.sleep(60)
                continue
            player_count = len(players)
            try:
                await self._spawn_zombies(cfg, player_count)
            except Exception as e:
                logger.error(f'spawn_loop {instance_name}: {e}')
            delay = _get_spawn_delay(player_count)
            try:
                task = asyncio.ensure_future(asyncio.sleep(delay))
                self._spawn_delay_tasks[instance_name] = task
                await task
            except asyncio.CancelledError:
                await asyncio.sleep(60)

    async def _spawn_zombies(self, cfg: dict, player_count: int):
        instance_name = cfg['instance_name']
        if await _is_blood_moon(instance_name):
            return
        positions = _load_pos()
        if not positions:
            return
        players = _get_online_players(instance_name)
        if not players:
            return
        target = random.choice(players)
        pos_list = positions.get(target, [])
        if not pos_list:
            return
        pos = pos_list[-1]
        count = _calculate_spawn_count(player_count)
        hour = datetime.datetime.utcnow().hour
        is_night = hour >= 20 or hour < 5
        if is_night:
            faction = random.choice(list(FACTIONS.keys()))
            zombie_pool = FACTIONS[faction]
        else:
            zombie_pool = COMMON_ZOMBIES
        instance = await _get_amp_instance(instance_name)
        if not instance:
            return
        for _ in range(count):
            zombie = random.choice(zombie_pool)
            ox = random.randint(-SDTD_SPAWN_RADIUS, SDTD_SPAWN_RADIUS)
            oz = random.randint(-SDTD_SPAWN_RADIUS, SDTD_SPAWN_RADIUS)
            x = int(pos.get('x', 0)) + ox
            y = int(pos.get('y', 0))
            z = int(pos.get('z', 0)) + oz
            cmd = f'spawnentityat {zombie} {x} {y} {z}'
            try:
                await instance.send_console_message(cmd)
            except Exception as e:
                logger.error(f'spawn cmd {instance_name}: {e}')

    # ---- Daily scheduler ----

    async def _poll_manual_backup_requests(self, configs: list[dict]):
        """Admin-triggered 'Run Backup Cycle Now' button in Quest Control - inserts a row
        into gamebot_backup_run_requests, we consume it here and kick off the same warned
        cycle the scheduler would, entirely independent of backup_hour/backup_minute. This
        exists for testing and for catching a missed scheduler window without waiting a day."""
        try:
            with db_session_scope() as db:
                rows = db.execute(text(
                    "SELECT id, instance_name FROM gamebot_backup_run_requests WHERE consumed_at IS NULL"
                )).fetchall()
                if not rows:
                    return
                ids = [r[0] for r in rows]
                db.execute(text(
                    "UPDATE gamebot_backup_run_requests SET consumed_at = :t WHERE id IN :ids"
                ).bindparams(bindparam('ids', expanding=True)), {'t': int(time.time()), 'ids': ids})
                db.commit()
        except Exception as e:
            logger.error(f'_poll_manual_backup_requests fetch: {e}')
            return

        for row_id, instance_name in rows:
            cfg = next((c for c in configs if c['instance_name'] == instance_name), None)
            if not cfg:
                logger.warning(f'Manual backup run requested for unknown instance {instance_name}')
                continue
            logger.info(f'Manual backup run requested for {instance_name}')
            asyncio.ensure_future(self._run_warned_backup_cycle(cfg, backup_days=None))

    async def _daily_scheduler_loop(self):
        await asyncio.sleep(30)
        _last_settings_run: dict[str, str] = {}
        _last_backup_run: dict[str, str] = {}
        while True:
            configs = _load_all_configs()
            now = datetime.datetime.now()
            today_key = now.strftime('%Y-%m-%d')

            await self._poll_manual_backup_requests(configs)

            for cfg in configs:
                instance_name = cfg['instance_name']
                sched_h = cfg.get('scheduler_hour', 3)
                sched_m = cfg.get('scheduler_minute', 27)
                backup_h = cfg.get('backup_hour', 23)
                backup_m = cfg.get('backup_minute', 30)

                # Settings run - schedule_enabled ONLY gates this (per-day settings
                # presets). It must NOT also gate the backup run below - those are
                # two distinct features that happen to share this loop, and an
                # instance with no settings presets built yet (so its admin turns
                # this off) should still get its nightly backups.
                if cfg.get('schedule_enabled', 1):
                    settings_key = f'settings:{instance_name}:{today_key}'
                    if now.hour == sched_h and now.minute == sched_m:
                        if _last_settings_run.get(settings_key) != today_key:
                            _last_settings_run[settings_key] = today_key
                            try:
                                await self._apply_daily_settings(cfg)
                            except Exception as e:
                                logger.error(f'daily_scheduler settings {instance_name}: {e}')

                # Backup run - backups_enabled=1 uses the warned stop/backup/start
                # cycle instead of the live in-place backup, fully replacing it for
                # that instance. It's triggered (max warning minutes) BEFORE the
                # configured backup_hour:backup_minute, not at it, so the LAST warning
                # (and the actual stop/backup) lands exactly at the admin-configured
                # time - the lead time is per-instance, admin-editable, not hardcoded.
                backup_key = f'backup:{instance_name}:{today_key}'
                scheduled_dt = now.replace(hour=backup_h, minute=backup_m, second=0, microsecond=0)
                lead_minutes = max(_parse_backup_warning_minutes(cfg.get('backup_warning_minutes')), default=0)
                trigger_dt = scheduled_dt - datetime.timedelta(minutes=lead_minutes)
                is_trigger_time = now.hour == trigger_dt.hour and now.minute == trigger_dt.minute
                if is_trigger_time:
                    if _last_backup_run.get(backup_key) != today_key:
                        _last_backup_run[backup_key] = today_key
                        backup_days = None
                        raw_bd = cfg.get('backup_days')
                        if raw_bd:
                            try:
                                import json as _json
                                backup_days = _json.loads(raw_bd)
                            except Exception:
                                pass
                        try:
                            if cfg.get('backups_enabled'):
                                asyncio.ensure_future(self._run_warned_backup_cycle(cfg, backup_days=backup_days))
                            else:
                                await self._run_backup(cfg, backup_days=backup_days)
                        except Exception as e:
                            logger.error(f'daily_scheduler backup {instance_name}: {e}')

            await asyncio.sleep(60)

    async def _apply_daily_settings(self, cfg: dict):
        """Apply per-day preset overrides and restart the server."""
        instance_name = cfg['instance_name']
        instance = await _get_amp_instance(instance_name)
        if not instance:
            return
        day_name = datetime.datetime.now().strftime('%A')
        logger.info(f'Daily settings scheduler: {instance_name} - {day_name}')

        overrides = {}
        raw = cfg.get('schedule_overrides')
        if raw:
            try:
                all_overrides = json.loads(raw)
                overrides = all_overrides.get(day_name, {})
            except Exception:
                pass

        try:
            await instance.stop_application()
            await asyncio.sleep(10)
            for key, value in overrides.items():
                try:
                    await instance.set_config(key, value)
                except Exception as e:
                    logger.warning(f'set_config {key}: {e}')
            await asyncio.sleep(5)
            await instance.start_application()
        except Exception as e:
            logger.error(f'_apply_daily_settings {instance_name}: {e}')

    async def _run_backup(self, cfg: dict, backup_days: list = None):
        """Take an AMP backup, optionally skipping based on backup_days."""
        instance_name = cfg['instance_name']
        instance = await _get_amp_instance(instance_name)
        if not instance:
            return
        day_name = datetime.datetime.now().strftime('%A')
        if backup_days is not None and day_name not in backup_days:
            logger.info(f'Backup scheduler: skipping {instance_name} on {day_name} (not in backup_days)')
            return
        logger.info(f'Backup scheduler: taking backup for {instance_name} on {day_name}')

        backup_ok = await self._take_backup_and_wait(
            cfg,
            instance,
            f'Auto-{day_name}',
            f'Scheduled daily backup ({day_name})',
        )
        if not backup_ok:
            logger.error(f'_run_backup: no completed backup found for {instance_name}')
            return

        try:
            await self._mirror_latest_backup(cfg)
        except Exception as e:
            logger.error(f'_run_backup mirror {instance_name}: {e}')

        try:
            await self._prune_amp_backups(instance)
        except Exception as e:
            logger.error(f'_run_backup prune {instance_name}: {e}')

        # Post update to channel - platform-specific, routed independently (this
        # legacy non-warned path has no per-day override data available, unlike
        # _apply_daily_settings, so there's no "Settings Applied" field here).
        color = GAME_COLORS.get(cfg.get('game_type', ''), DEFAULT_COLOR)
        display = cfg.get('server_display_name') or cfg['instance_name']
        description = f'Scheduled backup taken for {day_name}.'

        fluxer_channel = cfg.get('server_update_fluxer_channel_id')
        if fluxer_channel and cfg.get('fluxer_guild_id'):
            try:
                embed = fluxer.Embed(title=f'Daily Restart - {display}', description=description, color=color)
                embed.set_footer(text='Hosted by CasualHeroes - Powered by QuestLog')
                await self.bot._http.send_message(str(fluxer_channel), embeds=[embed])
            except Exception as e:
                logger.error(f'daily update fluxer post {instance_name}: {e}')

        discord_channel = cfg.get('server_update_discord_channel_id')
        if discord_channel and cfg.get('discord_guild_id'):
            try:
                with db_session_scope() as db:
                    db.execute(text(
                        "INSERT INTO discord_pending_broadcasts (guild_id, channel_id, payload, created_at) "
                        "VALUES (:g, :c, :p, :t)"
                    ), {
                        'g': int(cfg['discord_guild_id']), 'c': int(discord_channel),
                        'p': json.dumps({
                            'action': 'direct',
                            'title': f'Daily Restart - {display}', 'description': description,
                            'color': color, 'footer': 'Hosted by CasualHeroes - Powered by QuestLog',
                        }),
                        't': int(time.time()),
                    })
            except Exception as e:
                logger.error(f'daily update discord post {instance_name}: {e}')

    # ---- Warned backup cycle (backups_enabled=1) ----
    # Full stop/backup/start cycle with player-facing warnings, replacing the
    # live-backup-while-running path (_run_backup) for any instance with
    # backups_enabled=1. Fires at T-15/10/5 minutes before backup_hour:backup_minute,
    # then at T-0 stops the server, backs it up, restarts it, and confirms online.
    # Each stage posts to whichever of Discord/Fluxer this instance is linked to
    # (discord_guild_id/fluxer_guild_id + their respective channel fields), and
    # optionally sends an in-game broadcast via AMP console if rcon_broadcast_enabled.

    # Defaults used only when the admin hasn't customized a given embed's title/description
    # via Quest Control's Backups tab - so out-of-the-box behavior is unchanged, but every
    # word of it is one save away from being fully admin-controlled, never hardcoded here.
    _BACKUP_EMBED_DEFAULTS = {
        'warn':   {'title': '{server}', 'description': '{server} will go down in {minutes} minutes for nightly backups. ({time})', 'color': None},
        'down':   {'title': '{server}', 'description': '{server} going down now for nightly backups...', 'color': None},
        'online': {'title': '{server}', 'description': '{server} is now online - have fun!', 'color': None},
    }

    def _resolve_backup_embed(self, cfg: dict, kind: str, minutes_left: int = 0, backup_epoch: int = 0) -> dict:
        """Build the {title, description, color} for one of the 3 backup-cycle embeds
        (warn/down/online), starting from this instance's admin-configured text and
        substituting {server}/{minutes}/{time} placeholders. Falls back to the stock
        message for any field left blank."""
        instance_name = cfg['instance_name']
        display = cfg.get('server_display_name') or instance_name
        defaults = self._BACKUP_EMBED_DEFAULTS[kind]

        title = (cfg.get(f'backup_{kind}_embed_title') or '').strip() or defaults['title']
        description = (cfg.get(f'backup_{kind}_embed_description') or '').strip() or defaults['description']
        color_hex = (cfg.get(f'backup_{kind}_embed_color') or '').strip()
        try:
            color = int(color_hex.lstrip('#'), 16) if color_hex else GAME_COLORS.get(cfg.get('game_type', ''), DEFAULT_COLOR)
        except ValueError:
            color = GAME_COLORS.get(cfg.get('game_type', ''), DEFAULT_COLOR)

        # {time} is a Discord-native <t:unix:t> timestamp - Discord renders it in each
        # viewer's own local timezone client-side. Fluxer has no equivalent markup, so
        # it gets a plain UTC fallback instead.
        discord_time = f'<t:{backup_epoch}:t>' if backup_epoch else ''
        fluxer_time = datetime.datetime.utcfromtimestamp(backup_epoch).strftime('%H:%M UTC') if backup_epoch else ''

        def _fill(text_, time_str):
            return (text_.replace('{server}', display)
                          .replace('{minutes}', str(minutes_left))
                          .replace('{time}', time_str))

        return {
            'title_discord': _fill(title, discord_time), 'description_discord': _fill(description, discord_time),
            'title_fluxer': _fill(title, fluxer_time), 'description_fluxer': _fill(description, fluxer_time),
            'color': color,
        }

    async def _send_backup_notice(self, cfg: dict, kind: str, minutes_left: int = 0,
                                   backup_epoch: int = 0, include_rcon: bool = True, rcon_text: str = ''):
        """Post the admin-configured `kind` embed ('warn'/'down'/'online') to this
        instance's linked Discord channel, Fluxer channel, and in-game broadcast (if
        enabled) - whichever are configured. Best-effort: a failure on one destination
        doesn't block the others."""
        instance_name = cfg['instance_name']
        resolved = self._resolve_backup_embed(cfg, kind, minutes_left, backup_epoch)

        # Fluxer channel (this bot's own send path) - dedicated field, distinct from
        # server_update_channel_id/notif_channel_id which are used by other features.
        fluxer_channel = cfg.get('backup_notice_fluxer_channel_id')
        if cfg.get('fluxer_guild_id') and fluxer_channel:
            try:
                embed = fluxer.Embed(title=resolved['title_fluxer'], description=resolved['description_fluxer'], color=resolved['color'])
                embed.set_footer(text='Hosted by CasualHeroes - Powered by QuestLog')
                await self.bot._http.send_message(str(fluxer_channel), embeds=[embed])
            except Exception as e:
                logger.error(f'_send_backup_notice fluxer {instance_name}: {e}')

        # Discord channel - queued via discord_pending_broadcasts, same outbox
        # pattern the QuestLog admin site already uses for cross-bot messaging,
        # since this cog has no direct Discord connection of its own.
        discord_channel = cfg.get('backup_notice_discord_channel_id')
        if cfg.get('discord_guild_id') and discord_channel:
            try:
                with db_session_scope() as db:
                    db.execute(text(
                        "INSERT INTO discord_pending_broadcasts (guild_id, channel_id, payload, created_at) "
                        "VALUES (:g, :c, :p, :t)"
                    ), {
                        'g': int(cfg['discord_guild_id']), 'c': int(discord_channel),
                        # network_broadcasts.py on WardenBot reads embed fields directly off
                        # the top-level payload (title/description/color) - no 'embed' wrapper.
                        # action='direct' posts a plain embed to the channel - without it,
                        # the consumer's default behavior creates a new thread per message
                        # (its LFG-group posting pattern), which is wrong for a status notice.
                        'p': json.dumps({
                            'action': 'direct',
                            'title': resolved['title_discord'], 'description': resolved['description_discord'],
                            'color': resolved['color'], 'footer': 'Hosted by CasualHeroes - Powered by QuestLog',
                        }),
                        't': int(time.time()),
                    })
            except Exception as e:
                logger.error(f'_send_backup_notice discord {instance_name}: {e}')

        # In-game broadcast via AMP console - admin-authored message/command,
        # since broadcast syntax differs per game and we don't hardcode it here.
        if include_rcon and cfg.get('rcon_broadcast_enabled') and cfg.get('rcon_broadcast_message'):
            try:
                instance = await _get_amp_instance(instance_name)
                if instance:
                    rcon_msg = cfg['rcon_broadcast_message'].replace('{message}', rcon_text or resolved['description_fluxer'])
                    await instance.send_console_message(rcon_msg)
            except Exception as e:
                logger.error(f'_send_backup_notice rcon {instance_name}: {e}')

    @staticmethod
    def _latest_amp_backup_snapshot(cfg: dict):
        """Return a stable identity for the newest local AMP backup archive."""
        amp_log_dir = cfg.get('amp_log_dir')
        if not amp_log_dir:
            return None
        backups_dir = Path(amp_log_dir).parent / 'Backups'
        try:
            zips = sorted(backups_dir.glob('*.zip'), key=lambda path: path.stat().st_mtime_ns)
            if not zips:
                return None
            latest = zips[-1]
            stat = latest.stat()
            return (str(latest), stat.st_mtime_ns, stat.st_size)
        except OSError as e:
            logger.error(
                '_latest_amp_backup_snapshot: unable to inspect %s: %s',
                backups_dir,
                e,
            )
            return None

    async def _take_backup_and_wait(
        self,
        cfg: dict,
        instance,
        name: str,
        description: str,
    ) -> bool:
        """Make quota room, submit an AMP backup, and confirm a new archive.

        AMP accepts backup requests asynchronously. The configured wait belongs
        before mirroring, otherwise the mirror can copy yesterday's archive
        while the new one is still being written.
        """
        instance_name = cfg['instance_name']

        # Retain at most two ordinary backups before asking AMP to create the
        # third. This prevents a full AMP quota from rejecting the new archive.
        prune_ok = await self._prune_amp_backups(
            instance,
            max_kept=max(0, self.MAX_KEPT_BACKUPS - 1),
        )
        if not prune_ok:
            logger.warning(
                '_take_backup_and_wait: pre-backup retention failed for %s; '
                'continuing so AMP can report the authoritative result',
                instance_name,
            )

        before = self._latest_amp_backup_snapshot(cfg)
        try:
            result = await instance.take_backup(name, description, False)
        except Exception as e:
            logger.error(f'_take_backup_and_wait {instance_name}: {e}')
            return False

        if not bool(getattr(result, 'status', False)):
            logger.error(
                '_take_backup_and_wait: AMP rejected %s: %s',
                instance_name,
                getattr(result, 'reason', 'unknown'),
            )
            return False

        wait_after_backup = int(cfg.get('backup_wait_after_backup_sec') or 300)
        await asyncio.sleep(max(0, wait_after_backup))

        after = self._latest_amp_backup_snapshot(cfg)
        if cfg.get('amp_log_dir') and (after is None or after == before):
            logger.error(
                '_take_backup_and_wait: AMP accepted %s but no new completed '
                'backup archive appeared after %s seconds',
                instance_name,
                wait_after_backup,
            )
            return False

        return True

    async def _mirror_latest_backup(self, cfg: dict):
        """Copy the AMP backup that was just taken to both backup drives. AMP's
        Backups/ folder is a sibling of AMP_Logs (derived from amp_log_dir, which
        is already known-correct per-instance - no new config field needed). Runs
        the actual file copy in a thread since it's blocking I/O, potentially over
        NFS for remote-hosted instances.

        Safety: copies to a .partial temp name first, verifies the byte count
        matches the source, then atomically renames into place - a crash or network
        blip mid-copy never leaves a half-written file at the real destination path.
        Copies to each of the two drives independently: a failure on one (e.g. one
        drive is full or offline) never blocks the other from succeeding. The
        original AMP backup file is only ever read, never modified or deleted.
        """
        instance_name = cfg['instance_name']
        amp_log_dir = cfg.get('amp_log_dir')
        if not amp_log_dir:
            logger.warning(f'_mirror_latest_backup: no amp_log_dir configured for {instance_name}, skipping mirror')
            return

        backups_dir = Path(amp_log_dir).parent / 'Backups'

        def _find_latest_and_copy():
            if not backups_dir.is_dir():
                logger.warning(f'_mirror_latest_backup: {backups_dir} does not exist for {instance_name}, skipping mirror')
                return
            zips = sorted(backups_dir.glob('*.zip'), key=lambda p: p.stat().st_mtime)
            if not zips:
                logger.warning(f'_mirror_latest_backup: no backup zips found in {backups_dir} for {instance_name}')
                return
            latest = zips[-1]
            src_size = latest.stat().st_size

            # One drive at a time, with a short pause and up to 2 retries per
            # drive - a transient "Read-only file system" blip (seen once on
            # both /backup drives simultaneously, cause unconfirmed - possibly
            # a hypervisor-level hiccup since these are virtio disks) shouldn't
            # silently skip a mirror if the drive recovers a moment later.
            for drive_root in ('/backup/primary2tb', '/backup/secondary4tb'):
                dest_dir = Path(drive_root) / instance_name
                dest_final = dest_dir / latest.name
                dest_partial = dest_dir / (latest.name + '.partial')

                if dest_final.exists() and dest_final.stat().st_size == src_size:
                    logger.info(f'_mirror_latest_backup: {latest.name} already mirrored to {drive_root}, skipping')
                    continue

                max_attempts = 3
                for attempt in range(1, max_attempts + 1):
                    try:
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        with open(latest, 'rb') as src_f, open(dest_partial, 'wb') as dst_f:
                            while True:
                                chunk = src_f.read(8 * 1024 * 1024)
                                if not chunk:
                                    break
                                dst_f.write(chunk)
                        copied_size = dest_partial.stat().st_size
                        if copied_size != src_size:
                            logger.error(
                                f'_mirror_latest_backup: size mismatch copying {latest.name} to {drive_root} '
                                f'(src={src_size} copied={copied_size}) - leaving .partial file, NOT renaming'
                            )
                            break
                        dest_partial.rename(dest_final)
                        logger.info(f'_mirror_latest_backup: mirrored {latest.name} ({src_size} bytes) to {drive_root} for {instance_name}')
                        break
                    except Exception as e:
                        if attempt < max_attempts:
                            logger.warning(
                                f'_mirror_latest_backup: attempt {attempt}/{max_attempts} failed copying '
                                f'{latest.name} to {drive_root} for {instance_name}: {e} - retrying in 10s'
                            )
                            time.sleep(10)
                        else:
                            logger.error(
                                f'_mirror_latest_backup: all {max_attempts} attempts failed copying '
                                f'{latest.name} to {drive_root} for {instance_name}: {e}'
                            )

                # Brief pause before moving to the next drive, in case writing
                # to both back-to-back with no gap is what triggered the blip.
                time.sleep(3)

        try:
            await asyncio.get_event_loop().run_in_executor(None, _find_latest_and_copy)
        except Exception as e:
            logger.error(f'_mirror_latest_backup: unexpected error for {instance_name}: {e}')

        try:
            await self._prune_mirrored_backups(instance_name)
        except Exception as e:
            logger.error(f'_mirror_latest_backup: prune mirrored failed for {instance_name}: {e}')

    MAX_KEPT_BACKUPS = 3

    async def _prune_amp_backups(self, instance, max_kept: int | None = None):
        """Delete AMP's own stored backups down to the MAX_KEPT_BACKUPS newest,
        oldest-first. Sticky backups are never counted or deleted - an admin
        marked those as keep-forever, so they're excluded from both the "how
        many do we have" count and the deletion candidates entirely.

        AMP added fields such as parent_manifest to its backup response before
        the installed ampapi package added them to its Backup dataclass. Request
        raw dictionaries here so a new optional AMP field cannot disable
        retention and eventually fill the instance's backup quota.
        """
        try:
            backups = await instance.get_backups(format_data=False)
        except Exception as e:
            logger.error(f'_prune_amp_backups: get_backups failed: {e}')
            return False

        if not isinstance(backups, list):
            logger.error(
                '_prune_amp_backups: unexpected response type %s',
                type(backups).__name__,
            )
            return False

        def _field(backup, *names, default=None):
            if isinstance(backup, dict):
                for name in names:
                    if name in backup:
                        return backup[name]
                return default
            for name in names:
                if hasattr(backup, name):
                    return getattr(backup, name)
            return default

        keep_count = self.MAX_KEPT_BACKUPS if max_kept is None else max(0, int(max_kept))
        non_sticky = [
            backup for backup in backups
            if not bool(_field(backup, 'sticky', 'Sticky', default=False))
        ]
        non_sticky.sort(
            key=lambda backup: str(_field(
                backup,
                'timestamp',
                'Timestamp',
                default='',
            ))
        )
        to_delete = non_sticky[:-keep_count] if keep_count and len(non_sticky) > keep_count else (
            non_sticky if keep_count == 0 else []
        )

        for backup in to_delete:
            backup_id = _field(backup, 'id', 'ID', 'Id')
            backup_name = _field(backup, 'name', 'Name', default=backup_id)
            backup_timestamp = _field(backup, 'timestamp', 'Timestamp', default='unknown')
            if not backup_id:
                logger.error('_prune_amp_backups: backup response is missing an id')
                continue
            try:
                await instance.delete_local_backup(str(backup_id))
                logger.info(
                    '_prune_amp_backups: deleted old AMP backup %s (%s)',
                    backup_name,
                    backup_timestamp,
                )
            except Exception as e:
                logger.error(f'_prune_amp_backups: failed deleting backup {backup_id}: {e}')
                return False
        return True

    async def _prune_mirrored_backups(self, instance_name: str):
        """Delete mirrored backup zips on both drives down to the
        MAX_KEPT_BACKUPS newest per instance, oldest-first by mtime. Runs the
        directory scan/delete in a thread since it's blocking file I/O."""
        def _prune_dir(drive_root: str):
            dest_dir = Path(drive_root) / instance_name
            if not dest_dir.is_dir():
                return
            zips = sorted(
                (p for p in dest_dir.glob('*.zip') if not p.name.endswith('.partial')),
                key=lambda p: p.stat().st_mtime,
            )
            if len(zips) <= self.MAX_KEPT_BACKUPS:
                return
            for old in zips[:-self.MAX_KEPT_BACKUPS]:
                try:
                    old.unlink()
                    logger.info(f'_prune_mirrored_backups: deleted {old} for {instance_name}')
                except Exception as e:
                    logger.error(f'_prune_mirrored_backups: failed deleting {old}: {e}')

        for drive_root in ('/backup/primary2tb', '/backup/secondary4tb'):
            await asyncio.get_event_loop().run_in_executor(None, _prune_dir, drive_root)

    async def _restart_after_backup_cycle(
        self,
        cfg: dict,
        instance,
        wait_after_start: int,
    ) -> bool:
        """Restart a stopped server and publish its configured online notice.

        Restarting is required whether backup verification succeeds or fails.
        Keeping the online notification in the same recovery path prevents a
        backup bookkeeping problem from leaving players with a stale down alert.
        """
        instance_name = cfg['instance_name']
        try:
            await instance.start_application()
        except Exception as e:
            logger.error(f'Warned backup cycle start {instance_name}: {e}')
            return False

        await asyncio.sleep(max(0, wait_after_start))

        # Pick up any message/channel edits made while the cycle was running.
        fresh = _load_all_configs()
        current_cfg = next(
            (item for item in fresh if item['instance_name'] == instance_name),
            cfg,
        )
        await self._send_backup_notice(
            current_cfg,
            'online',
            include_rcon=False,
        )
        logger.info(
            'Warned backup cycle: %s restarted; online notice sent',
            instance_name,
        )
        return True

    async def _run_warned_backup_cycle(self, cfg: dict, backup_days: list = None):
        """Full warn -> stop -> backup -> start -> confirm cycle for one instance.
        Every timing value (warning lead times, wait durations) is admin-configurable
        per instance via Quest Control's Schedule tab - nothing here is hardcoded.
        The scheduler triggers this at (backup_hour:backup_minute - max(warning_minutes)),
        so the LAST warning always lands exactly at the configured backup time."""
        instance_name = cfg['instance_name']
        display = cfg.get('server_display_name') or instance_name
        day_name = datetime.datetime.now().strftime('%A')
        if backup_days is not None and day_name not in backup_days:
            logger.info(f'Warned backup cycle: skipping {instance_name} on {day_name} (not in backup_days)')
            return

        warning_minutes = _parse_backup_warning_minutes(cfg.get('backup_warning_minutes'))
        wait_after_stop = int(cfg.get('backup_wait_after_stop_sec') or 300)
        wait_after_start = int(cfg.get('backup_wait_after_start_sec') or 120)

        logger.info(f'Warned backup cycle starting for {instance_name} (warnings={warning_minutes})')

        # Absolute time the server actually goes down, for the {time} placeholder in
        # admin-authored embed text - computed once so it stays consistent across every
        # warning even as the cycle progresses.
        backup_epoch = int(time.time()) + max(warning_minutes, default=0) * 60

        # Warnings count down to 0 - the gap between consecutive entries (and from the
        # last entry to 0) is how long we sleep after sending each one. e.g. [15,10,5]
        # sleeps 5 minutes after each notice, landing the stop exactly at T-0.
        checkpoints = warning_minutes + [0]
        for i, minutes_left in enumerate(checkpoints):
            if minutes_left > 0:
                await self._send_backup_notice(cfg, 'warn', minutes_left=minutes_left, backup_epoch=backup_epoch)
            else:
                await self._send_backup_notice(cfg, 'down', backup_epoch=backup_epoch)
                break
            gap_minutes = minutes_left - checkpoints[i + 1]
            await asyncio.sleep(max(0, gap_minutes) * 60)
            # Re-check backups_enabled hasn't been turned off mid-warning-sequence.
            fresh = _load_all_configs()
            cfg = next((c for c in fresh if c['instance_name'] == instance_name), cfg)
            if not cfg.get('backups_enabled'):
                logger.info(f'Warned backup cycle: {instance_name} backups_enabled turned off, aborting')
                return

        instance = await _get_amp_instance(instance_name)
        if not instance:
            logger.error(f'Warned backup cycle: {instance_name} AMP instance not found, aborting')
            return

        try:
            await instance.stop_application()
        except Exception as e:
            logger.error(f'Warned backup cycle stop {instance_name}: {e}')
            return

        await asyncio.sleep(wait_after_stop)

        backup_ok = await self._take_backup_and_wait(
            cfg,
            instance,
            f'Auto-{day_name}',
            f'Scheduled nightly backup ({day_name})',
        )

        if not backup_ok:
            # The instance is already stopped at this point. Restart it now
            # rather than leaving it down after a failed backup. Players still
            # need the online notice once that recovery restart has completed.
            restarted = await self._restart_after_backup_cycle(
                cfg,
                instance,
                wait_after_start,
            )
            logger.error(
                'Warned backup cycle: backup verification failed for %s; '
                'recovery restart=%s',
                instance_name,
                'complete' if restarted else 'failed',
            )
            return

        try:
            await self._mirror_latest_backup(cfg)
        except Exception as e:
            logger.error(f'Warned backup cycle mirror {instance_name}: {e}')

        try:
            await self._prune_amp_backups(instance)
        except Exception as e:
            logger.error(f'Warned backup cycle prune {instance_name}: {e}')

        restarted = await self._restart_after_backup_cycle(
            cfg,
            instance,
            wait_after_start,
        )
        if not restarted:
            return
        logger.info(f'Warned backup cycle complete for {instance_name}')

    # ---- Generic commands (work for any game) ----

    def _get_cfg_for_guild(self, guild_id: str) -> list[dict]:
        configs = _load_all_configs()
        return [c for c in configs if str(c.get('fluxer_guild_id')) == str(guild_id)]

    @Cog.command(name='gs_status')
    async def gs_status(self, ctx):
        """Show status of all configured game servers for this guild."""
        guild_id = str(ctx.guild.id) if ctx.guild else None
        if not guild_id:
            return
        configs = self._get_cfg_for_guild(guild_id)
        if not configs:
            embed = fluxer.Embed(description='No game servers configured for this server.', color=0xFF4444)
            await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
            return
        for cfg in configs:
            embed = await build_serverinfo_embed(cfg)
            await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])

    @Cog.command(name='gs_start')
    async def gs_start(self, ctx, instance: str = None):
        """Start a game server. Usage: !gs_start <instance_name>"""
        guild_id = str(ctx.guild.id) if ctx.guild else None
        if not guild_id:
            return
        cfg = self._resolve_instance(guild_id, instance, ctx)
        if not cfg:
            return
        if not await _has_permission(cfg, ctx):
            embed = fluxer.Embed(description='You do not have permission.', color=0xFF4444)
            await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
            return
        ok, error = await _quest_control_server_action(
            cfg['instance_name'],
            guild_id,
            'start',
        )
        if not ok:
            embed = fluxer.Embed(description=error or 'Start failed.', color=0xFF4444)
            await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
            return
        embed = fluxer.Embed(
            description=f'Start command sent to **{cfg.get("server_display_name") or cfg["instance_name"]}**.',
            color=0x00FF88,
        )
        await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])

    @Cog.command(name='gs_stop')
    async def gs_stop(self, ctx, instance: str = None):
        """Stop a game server. Usage: !gs_stop <instance_name>"""
        guild_id = str(ctx.guild.id) if ctx.guild else None
        if not guild_id:
            return
        cfg = self._resolve_instance(guild_id, instance, ctx)
        if not cfg:
            return
        if not await _has_permission(cfg, ctx):
            embed = fluxer.Embed(description='You do not have permission.', color=0xFF4444)
            await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
            return
        ok, error = await _quest_control_server_action(
            cfg['instance_name'],
            guild_id,
            'stop',
        )
        if not ok:
            embed = fluxer.Embed(description=error or 'Stop failed.', color=0xFF4444)
            await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
            return
        embed = fluxer.Embed(
            description=f'Stop command sent to **{cfg.get("server_display_name") or cfg["instance_name"]}**.',
            color=0xFF8C00,
        )
        await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])

    @Cog.command(name='gs_restart')
    async def gs_restart(self, ctx, instance: str = None):
        """Restart a game server. Usage: !gs_restart <instance_name>"""
        guild_id = str(ctx.guild.id) if ctx.guild else None
        if not guild_id:
            return
        cfg = self._resolve_instance(guild_id, instance, ctx)
        if not cfg:
            return
        if not await _has_permission(cfg, ctx):
            embed = fluxer.Embed(description='You do not have permission.', color=0xFF4444)
            await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
            return
        ok, error = await _quest_control_server_action(
            cfg['instance_name'],
            guild_id,
            'restart',
        )
        if not ok:
            embed = fluxer.Embed(description=error or 'Restart failed.', color=0xFF4444)
            await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
            return
        embed = fluxer.Embed(
            description=f'Restart command sent to **{cfg.get("server_display_name") or cfg["instance_name"]}**.',
            color=0x00BFFF,
        )
        await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])

    @Cog.command(name='gs_backup')
    async def gs_backup(self, ctx, instance: str = None):
        """Take a backup. Usage: !gs_backup <instance_name>"""
        guild_id = str(ctx.guild.id) if ctx.guild else None
        if not guild_id:
            return
        cfg = self._resolve_instance(guild_id, instance, ctx)
        if not cfg:
            return
        if not await _has_permission(cfg, ctx):
            embed = fluxer.Embed(description='You do not have permission.', color=0xFF4444)
            await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
            return
        ok, error = await _quest_control_server_action(
            cfg['instance_name'],
            guild_id,
            'backup',
        )
        if not ok:
            embed = fluxer.Embed(description=error or 'Backup request failed.', color=0xFF4444)
            await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
            return
        embed = fluxer.Embed(
            description=f'Backup queued through Quest Control for **{cfg.get("server_display_name") or cfg["instance_name"]}**.',
            color=0x00FF88,
        )
        await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])

    @Cog.command(name='gs_players')
    async def gs_players(self, ctx, instance: str = None):
        """List online players. Usage: !gs_players <instance_name>"""
        guild_id = str(ctx.guild.id) if ctx.guild else None
        if not guild_id:
            return
        cfg = self._resolve_instance(guild_id, instance, ctx)
        if not cfg:
            return
        players = _get_online_players(cfg['instance_name'])
        color = GAME_COLORS.get(cfg.get('game_type', ''), DEFAULT_COLOR)
        display = cfg.get('server_display_name') or cfg['instance_name']
        if players:
            embed = fluxer.Embed(
                title=f'{display} - Online Players ({len(players)})',
                description='\n'.join(players),
                color=color,
            )
        else:
            embed = fluxer.Embed(
                title=f'{display} - Online Players',
                description='No players currently online.',
                color=color,
            )
        await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])

    @Cog.command(name='gs_serverinfo')
    async def gs_serverinfo(self, ctx, instance: str = None):
        """Post a live server info panel. Usage: !gs_serverinfo <instance_name>"""
        guild_id = str(ctx.guild.id) if ctx.guild else None
        if not guild_id:
            return
        cfg = self._resolve_instance(guild_id, instance, ctx)
        if not cfg:
            return
        if not await _has_permission(cfg, ctx):
            embed = fluxer.Embed(description='You do not have permission.', color=0xFF4444)
            await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
            return
        # Post to configured stats channel if set, otherwise fall back to command channel
        target_channel = cfg.get('stats_channel_id') or str(ctx.channel.id)
        embed = await build_serverinfo_embed(cfg)
        try:
            resp = await self.bot._http.send_message(str(target_channel), embeds=[embed])
            new_id = resp.get('id') if isinstance(resp, dict) else None
            _update_serverinfo_id(cfg['instance_name'], new_id)
            if target_channel != str(ctx.channel.id):
                confirm = fluxer.Embed(description=f'Server panel posted to <#{target_channel}>.', color=0x00CC66)
                await self.bot._http.send_message(str(ctx.channel.id), embeds=[confirm])
        except Exception as e:
            logger.error(f'gs_serverinfo send: {e}')

    def _resolve_instance(self, guild_id: str, instance_arg: str | None, ctx) -> dict | None:
        """Find the right config for this guild. If one server, use it. If multiple, require name arg."""
        configs = self._get_cfg_for_guild(guild_id)
        if not configs:
            embed = fluxer.Embed(description='No game servers configured for this guild.', color=0xFF4444)
            asyncio.ensure_future(
                self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
            )
            return None
        if len(configs) == 1:
            return configs[0]
        if not instance_arg:
            names = ', '.join(f'`{c["instance_name"]}`' for c in configs)
            embed = fluxer.Embed(
                description=f'Multiple servers configured. Specify one: {names}',
                color=0xFF8C00,
            )
            asyncio.ensure_future(
                self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
            )
            return None
        match = next((c for c in configs if c['instance_name'].lower() == instance_arg.lower()), None)
        if not match:
            embed = fluxer.Embed(description=f'Instance `{instance_arg}` not found.', color=0xFF4444)
            asyncio.ensure_future(
                self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
            )
        return match
