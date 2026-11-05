# cogs/ffxiv_timers.py - FFXIV Timer Alerts for QuestLogFluxer
#
# Posts three types of time-based alerts to configured Fluxer channels:
#   1. Gathering nodes  - warning X minutes before unspoiled/legendary nodes spawn in Eorzea time
#   2. Ocean Fishing    - registration-open alert 45 min before each 2-hour departure window
#   3. Daily/Weekly resets - reminder when daily (15:00 UTC) and weekly (Tue 08:00 UTC) resets hit
#
# Config stored in web_ffxiv_timer_channels (one row per guild).
# Node data loaded from ffxiv_gathering_nodes.json (same file the website uses).

import asyncio
import json
import math
import time
import os
import logging
from datetime import datetime, timezone, timedelta

import fluxer
from fluxer import Cog
from sqlalchemy import text

from config import logger, db_session_scope

# ---------------------------------------------------------------------------
# Eorzea time constants
# ---------------------------------------------------------------------------
# 1 real second = 20/1440 Eorzea minutes (Eorzea day = 70 real minutes = 4200 real seconds)
ET_RATIO = 20 / 1440  # Eorzea minutes per real second... actually: 1440 ET min / 4200 real sec
EORZEA_DAY_SECONDS = 4200  # 70 real minutes = 1 Eorzea day

def _eorzea_time(unix_ts=None):
    """Return (eorzea_hour, eorzea_minute) for a given unix timestamp (default: now)."""
    if unix_ts is None:
        unix_ts = time.time()
    et_minutes_total = (unix_ts / EORZEA_DAY_SECONDS) * 1440
    et_minutes_today = et_minutes_total % 1440
    h = int(et_minutes_today // 60)
    m = int(et_minutes_today % 60)
    return h, m

def _et_minutes_since_midnight(unix_ts=None):
    """Total Eorzea minutes elapsed since last Eorzea midnight."""
    if unix_ts is None:
        unix_ts = time.time()
    et_minutes_total = (unix_ts / EORZEA_DAY_SECONDS) * 1440
    return et_minutes_total % 1440

def _real_seconds_until_et_minute(target_et_minute, unix_ts=None):
    """Seconds of real time until a given Eorzea minute-of-day arrives (0-1439)."""
    if unix_ts is None:
        unix_ts = time.time()
    current_et = _et_minutes_since_midnight(unix_ts)
    if target_et_minute > current_et:
        et_remaining = target_et_minute - current_et
    else:
        et_remaining = (1440 - current_et) + target_et_minute
    return et_remaining * (EORZEA_DAY_SECONDS / 1440)

# ---------------------------------------------------------------------------
# Ocean Fishing constants (matches views_ffxiv.py exactly)
# ---------------------------------------------------------------------------
_OF_EPOCH_ANCHOR     = 1593302400  # 2020-06-28 00:00:00 UTC - known window 0 start
_OF_WINDOW_DURATION  = 7200        # 2 hours per window
_OF_REG_OFFSET       = 2700        # registration opens 45 min before departure

_OF_ROUTES = [
    {'name': 'Indigo Route',    'color': 'indigo', 'stops': ['Bloodbrine Sea',  'Rothlyt Sound',       'Northern Strait of Merlthor'], 'times': ['Day',    'Sunset',  'Night'],   'blue_fish': ['Hafgufa', 'Seafaring Toad'], 'achievement_fish': ['Hafgufa', 'Seafaring Toad', 'Coral Manta']},
    {'name': 'Ruby Route',      'color': 'red',    'stops': ['Rhotano Sea',     'Cieldalaes Margin',   'Galadion Bay'],                 'times': ['Night',  'Day',     'Evening'], 'blue_fish': ['Stonescale'],               'achievement_fish': ['Stonescale', 'Gilled Topknot']},
    {'name': 'Indigo Route',    'color': 'indigo', 'stops': ['Bloodbrine Sea',  'Rothlyt Sound',       'Northern Strait of Merlthor'], 'times': ['Night',  'Day',     'Sunset'],  'blue_fish': ['Hafgufa', 'Seafaring Toad'], 'achievement_fish': ['Hafgufa', 'Seafaring Toad', 'Coral Manta']},
    {'name': 'Ruby Route',      'color': 'red',    'stops': ['Rhotano Sea',     'Cieldalaes Margin',   'Galadion Bay'],                 'times': ['Evening','Night',   'Day'],     'blue_fish': ['Stonescale'],               'achievement_fish': ['Stonescale', 'Gilled Topknot']},
    {'name': 'Indigo Route',    'color': 'indigo', 'stops': ['Bloodbrine Sea',  'Rothlyt Sound',       'Northern Strait of Merlthor'], 'times': ['Sunset', 'Night',   'Day'],     'blue_fish': ['Hafgufa', 'Seafaring Toad'], 'achievement_fish': ['Hafgufa', 'Seafaring Toad', 'Coral Manta']},
    {'name': 'Ruby Route',      'color': 'red',    'stops': ['Rhotano Sea',     'Cieldalaes Margin',   'Galadion Bay'],                 'times': ['Day',    'Evening', 'Night'],   'blue_fish': ['Stonescale'],               'achievement_fish': ['Stonescale', 'Gilled Topknot']},
    {'name': 'Indigo Route',    'color': 'indigo', 'stops': ['Bloodbrine Sea',  'Rothlyt Sound',       'Northern Strait of Merlthor'], 'times': ['Day',    'Night',   'Sunset'],  'blue_fish': ['Hafgufa', 'Seafaring Toad'], 'achievement_fish': ['Hafgufa', 'Seafaring Toad', 'Coral Manta']},
    {'name': 'Ruby Route',      'color': 'red',    'stops': ['Rhotano Sea',     'Cieldalaes Margin',   'Galadion Bay'],                 'times': ['Sunset', 'Day',     'Evening'], 'blue_fish': ['Stonescale'],               'achievement_fish': ['Stonescale', 'Gilled Topknot']},
]

def _of_window_start(window_num):
    return _OF_EPOCH_ANCHOR + window_num * _OF_WINDOW_DURATION

def _current_of_window_num(ts=None):
    ts = ts or time.time()
    return int((ts - _OF_EPOCH_ANCHOR) // _OF_WINDOW_DURATION)

# ---------------------------------------------------------------------------
# Gathering node data
# ---------------------------------------------------------------------------
_NODES_PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'ch-webserver', 'app', 'static', 'data', 'ffxiv_gathering_nodes.json')
_NODES_PATH_ALT = '/srv/ch-webserver/app/static/data/ffxiv_gathering_nodes.json'

def _load_nodes():
    for path in (_NODES_PATH, _NODES_PATH_ALT):
        try:
            with open(os.path.abspath(path)) as f:
                return json.load(f)
        except Exception:
            pass
    logger.warning("FFXIVTimers: could not load ffxiv_gathering_nodes.json")
    return []

# Only include timed nodes (Unspoiled, Legendary, Ephemeral have spawn times)
TIMED_TYPES = {'Unspoiled', 'Legendary', 'Ephemeral'}

def _timed_nodes():
    return [n for n in _load_nodes() if n.get('limitType') in TIMED_TYPES and n.get('times')]

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
COLOR_GATHERING = 0x57C75E   # green
COLOR_OCEAN     = 0x5865F2   # indigo/blue
COLOR_RESETS    = 0xAB8BFF   # purple

JOB_COLORS = {'BTN': 0x57C75E, 'MIN': 0xA8A8A8, 'FSH': 0x5B9BD5}
JOB_ICONS  = {'BTN': '🌿', 'MIN': '⛏️', 'FSH': '🎣'}

# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class FFXIVTimersCog(Cog):
    """Posts FFXIV gathering, ocean fishing, and reset alerts to configured channels."""

    def __init__(self, bot):
        super().__init__(bot)
        self._task_gathering = None
        self._task_ocean     = None
        self._task_resets    = None

    @Cog.listener()
    async def on_ready(self):
        if self._task_gathering is None or self._task_gathering.done():
            self._task_gathering = asyncio.ensure_future(self._loop_gathering())
        if self._task_ocean is None or self._task_ocean.done():
            self._task_ocean = asyncio.ensure_future(self._loop_ocean())
        if self._task_resets is None or self._task_resets.done():
            self._task_resets = asyncio.ensure_future(self._loop_resets())
        logger.info("FFXIVTimers: all timer loops started")

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _get_config(self, event_type):
        """Return the webhook config row for a given event_type, or None."""
        try:
            with db_session_scope() as db:
                row = db.execute(text(
                    "SELECT id, guild_id, channel_id, is_enabled, embed_footer, embed_title, discord_webhook_url "
                    "FROM web_fluxer_webhook_configs WHERE event_type = :e LIMIT 1"
                ), {'e': event_type}).fetchone()
                if row:
                    return dict(row._mapping)
                return None
        except Exception as e:
            logger.error(f"FFXIVTimers: error loading config for {event_type}: {e}")
            return None

    async def _send_discord_webhook(self, webhook_url, embed):
        """POST embed to a Discord webhook URL."""
        import urllib.request as _urllib_req
        import urllib.error as _urllib_err
        try:
            payload = json.dumps({'embeds': [embed]}).encode()
            req = _urllib_req.Request(webhook_url, data=payload,
                headers={'Content-Type': 'application/json', 'User-Agent': 'QuestLog-Timers/1.0'})
            with _urllib_req.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            logger.warning(f"FFXIVTimers: Discord webhook send failed: {e}")

    async def _send(self, channel_id, embed):
        try:
            await self.bot._http.send_message(str(channel_id), embeds=[embed])
        except Exception as e:
            logger.warning(f"FFXIVTimers: send to {channel_id} failed: {e}")

    # ------------------------------------------------------------------
    # Gathering loop
    # ------------------------------------------------------------------

    async def _loop_gathering(self):
        await asyncio.sleep(10)
        nodes = _timed_nodes()
        logger.info(f"FFXIVTimers: gathering loop started with {len(nodes)} timed nodes")

        while True:
            try:
                await self._check_gathering(nodes)
            except Exception as e:
                logger.error(f"FFXIVTimers: gathering loop error: {e}", exc_info=True)
            # Check every 30 real seconds - Eorzea time moves fast
            await asyncio.sleep(30)

    async def _check_gathering(self, nodes):
        cfg = self._get_config('ffxiv_gathering')
        if not cfg or not cfg.get('is_enabled') or not cfg.get('channel_id'):
            return

        channel_id = cfg['channel_id']
        warn_min = int(cfg.get('embed_footer') or 5)

        now = time.time()
        et_h, et_m = _eorzea_time(now)
        et_min = et_h * 60 + et_m

        for node in nodes:
            for spawn_et_hour in node['times']:
                spawn_et_min = spawn_et_hour * 60
                uptime = node.get('uptime', 180)

                active = (spawn_et_min <= et_min < spawn_et_min + uptime) or \
                         (spawn_et_min + uptime > 1440 and et_min < (spawn_et_min + uptime) % 1440)

                warn_et_min = (spawn_et_min - warn_min) % 1440

                if warn_et_min <= et_min < warn_et_min + 1 and not active:
                    embed = self._gathering_warning_embed(node, spawn_et_hour, warn_min)
                    await self._send(channel_id, embed)
                    if cfg.get('discord_webhook_url'):
                        await self._send_discord_webhook(cfg['discord_webhook_url'], embed)

                if et_min == spawn_et_min:
                    embed = self._gathering_active_embed(node, spawn_et_hour, uptime)
                    await self._send(channel_id, embed)
                    if cfg.get('discord_webhook_url'):
                        await self._send_discord_webhook(cfg['discord_webhook_url'], embed)

    def _gathering_warning_embed(self, node, spawn_et_hour, warn_min):
        job = node.get('job', '')
        color = JOB_COLORS.get(job, COLOR_GATHERING)
        icon = JOB_ICONS.get(job, '⏰')
        items = node.get('items', [])
        items_str = '\n'.join(f'- {i}' for i in items) if items else 'Unknown'
        coords = node.get('coords', [])
        coords_str = f"({coords[0]}, {coords[1]})" if len(coords) == 2 else ''
        return {
            'title': f'{icon} Node Spawning Soon - {node["name"]}',
            'url': 'https://questlog.casual-heroes.com/ffxiv/tools/gathering/',
            'description': f'**{warn_min} minutes** until this node spawns in Eorzea time.\n\nSpawns at **ET {spawn_et_hour:02d}:00**',
            'color': color,
            'fields': [
                {'name': 'Zone', 'value': node.get('zone', 'Unknown'), 'inline': True},
                {'name': 'Job / Level', 'value': f'{job} Lv{node.get("lvl", "?")}', 'inline': True},
                {'name': 'Coordinates', 'value': coords_str or 'N/A', 'inline': True},
                {'name': 'Items', 'value': items_str, 'inline': False},
            ],
            'footer': {'text': f'{node.get("limitType", "")} Node'},
        }

    def _gathering_active_embed(self, node, spawn_et_hour, uptime_min):
        job = node.get('job', '')
        color = JOB_COLORS.get(job, COLOR_GATHERING)
        icon = JOB_ICONS.get(job, '🌿')
        items = node.get('items', [])
        items_str = '\n'.join(f'- {i}' for i in items) if items else 'Unknown'
        coords = node.get('coords', [])
        coords_str = f"({coords[0]}, {coords[1]})" if len(coords) == 2 else ''
        uptime_real_min = int(uptime_min * EORZEA_DAY_SECONDS / 1440 / 60)
        return {
            'title': f'{icon} Node Now Active - {node["name"]}',
            'url': 'https://questlog.casual-heroes.com/ffxiv/tools/gathering/',
            'description': f'This node is **active now!** You have ~{uptime_real_min} real minutes.',
            'color': color,
            'fields': [
                {'name': 'Zone', 'value': node.get('zone', 'Unknown'), 'inline': True},
                {'name': 'Job / Level', 'value': f'{job} Lv{node.get("lvl", "?")}', 'inline': True},
                {'name': 'Coordinates', 'value': coords_str or 'N/A', 'inline': True},
                {'name': 'Items', 'value': items_str, 'inline': False},
            ],
            'footer': {'text': f'{node.get("limitType", "")} Node - ET {spawn_et_hour:02d}:00 for {uptime_min} ET minutes'},
        }

    # ------------------------------------------------------------------
    # Ocean Fishing loop
    # ------------------------------------------------------------------

    async def _loop_ocean(self):
        await asyncio.sleep(20)
        logger.info("FFXIVTimers: ocean fishing loop started")
        while True:
            try:
                await self._check_ocean()
            except Exception as e:
                logger.error(f"FFXIVTimers: ocean loop error: {e}", exc_info=True)
            await asyncio.sleep(60)

    async def _check_ocean(self):
        cfg = self._get_config('ffxiv_ocean')
        if not cfg or not cfg.get('is_enabled') or not cfg.get('channel_id'):
            return

        now = time.time()
        current_wnum = _current_of_window_num(now)
        next_wnum = current_wnum + 1
        next_start = _of_window_start(next_wnum)
        reg_opens = next_start - _OF_REG_OFFSET

        if reg_opens <= now < reg_opens + 60:
            route = _OF_ROUTES[next_wnum % 8]
            embed = self._ocean_embed(route, next_start)
            await self._send(cfg['channel_id'], embed)
            if cfg.get('discord_webhook_url'):
                await self._send_discord_webhook(cfg['discord_webhook_url'], embed)

    def _ocean_embed(self, route, departure_ts):
        dep_dt = datetime.fromtimestamp(departure_ts, tz=timezone.utc)
        dep_str = dep_dt.strftime('%b %d at %H:%M UTC')
        color = 0x5865F2 if route['color'] == 'indigo' else 0xED4245

        stops_val = '\n'.join(
            f'{stop} ({time_of_day})'
            for stop, time_of_day in zip(route['stops'], route['times'])
        )
        blue_val = ', '.join(route.get('blue_fish', [])) or 'None this run'

        return {
            'title': f'🎣 Ocean Fishing - Registration Now Open!',
            'url': 'https://questlog.casual-heroes.com/ffxiv/tools/ocean-fishing/',
            'description': f'**{route["name"]}** departing **{dep_str}**\nRegistration closes at departure. Board at the Limsa Lominsa Lower Decks (hawker\'s stall).',
            'color': color,
            'fields': [
                {'name': 'Stops', 'value': stops_val, 'inline': False},
                {'name': 'Blue / Achievement Fish', 'value': blue_val, 'inline': False},
            ],
            'footer': {'text': 'questlog.casual-heroes.com/ffxiv/tools/ocean-fishing/'},
        }

    # ------------------------------------------------------------------
    # Resets loop
    # ------------------------------------------------------------------

    async def _loop_resets(self):
        await asyncio.sleep(30)
        logger.info("FFXIVTimers: resets loop started")
        while True:
            try:
                await self._check_resets()
            except Exception as e:
                logger.error(f"FFXIVTimers: resets loop error: {e}", exc_info=True)
            await asyncio.sleep(60)

    async def _check_resets(self):
        cfg = self._get_config('ffxiv_resets')
        if not cfg or not cfg.get('is_enabled') or not cfg.get('channel_id'):
            return

        # daily/weekly flags stored as "daily,weekly" csv in embed_title
        flags = (cfg.get('embed_title') or 'daily,weekly').split(',')
        want_daily  = 'daily'  in flags
        want_weekly = 'weekly' in flags

        now = datetime.now(tz=timezone.utc)
        is_daily_reset  = (now.hour == 15 and now.minute == 0 and now.second < 60)
        is_weekly_reset = (now.weekday() == 1 and now.hour == 8 and now.minute == 0 and now.second < 60)

        if is_daily_reset and want_daily:
            embed = self._daily_reset_embed(now)
            await self._send(cfg['channel_id'], embed)
            if cfg.get('discord_webhook_url'):
                await self._send_discord_webhook(cfg['discord_webhook_url'], embed)
        if is_weekly_reset and want_weekly:
            embed = self._weekly_reset_embed(now)
            await self._send(cfg['channel_id'], embed)
            if cfg.get('discord_webhook_url'):
                await self._send_discord_webhook(cfg['discord_webhook_url'], embed)

    def _daily_reset_embed(self, dt):
        date_str = dt.strftime('%A, %B %d')
        return {
            'title': '🔄 Daily Reset',
            'url': 'https://questlog.casual-heroes.com/ffxiv/tools/resets/',
            'description': f'**{date_str}** - Daily tasks have reset.',
            'color': COLOR_RESETS,
            'fields': [
                {'name': 'Resets Now', 'value': (
                    '- Duty Roulettes\n'
                    '- Beast Tribe Quests (allied society)\n'
                    '- Wondrous Tails stamps\n'
                    '- Custom Deliveries\n'
                    '- Cosmic Exploration (09:00 UTC)\n'
                    '- GC Daily Supply & Provisioning (20:00 UTC)'
                ), 'inline': False},
            ],
            'footer': {'text': 'questlog.casual-heroes.com/ffxiv/tools/resets/'},
        }

    def _weekly_reset_embed(self, dt):
        date_str = dt.strftime('%A, %B %d')
        return {
            'title': '📅 Weekly Reset',
            'url': 'https://questlog.casual-heroes.com/ffxiv/tools/resets/',
            'description': f'**{date_str}** - Weekly tasks have reset.',
            'color': COLOR_RESETS,
            'fields': [
                {'name': 'Resets Now', 'value': (
                    '- Normal Raids (4 turns)\n'
                    '- Alliance Raid\n'
                    '- Trials (Extreme)\n'
                    '- Savage Raids\n'
                    '- Ultimate Raids\n'
                    '- Weekly Challenge Log\n'
                    '- Hunt Elite Mark Bills\n'
                    '- Wondrous Tails book exchange\n'
                    '- Tomestone weekly cap reset'
                ), 'inline': False},
            ],
            'footer': {'text': 'questlog.casual-heroes.com/ffxiv/tools/resets/'},
        }
