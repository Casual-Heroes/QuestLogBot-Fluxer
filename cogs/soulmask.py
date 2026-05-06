# cogs/soulmask.py - Soulmask cluster server management for QuestLogFluxer
#
# Manages two Soulmask instances via AMP RCON passthrough:
#   Server B (192.168.2.154) - SoulQuest-SunkenThrone01  - Child / end-game zone
#   Server C (192.168.3.154) - SoulQuest01               - Parent / Verdant Wilds (login server)
#
# Key features:
#   - Scheduled mode rotations on SunkenThrone via Set_Coefficient RCON commands
#   - Coefficients apply immediately without restart (runtime only, reset on backup restart)
#   - Backup is nightly off-peak — coefficients reset to JSON baseline then bot re-applies schedule
#   - Config-driven: data/soulmask/schedule.json defines modes, days, times, coefficients
#   - Fluxer announcements before and after mode switches
#   - Commands: !sm_status, !sm_mode, !sm_coefficients, !sm_reload

import asyncio
import datetime
import json
import logging
import os
from pathlib import Path

import fluxer
from fluxer import Cog

from config import logger

# ---- AMP credentials (shared with gameserver.py) ----
AMP_URL      = os.getenv('AMP_URL', '')
AMP_USER     = os.getenv('AMP_USER', '')
AMP_PASSWORD = os.getenv('AMP_PASSWORD', '')

# ---- Soulmask instance names (as they appear in AMP) ----
INSTANCE_SUNKEN  = os.getenv('SOULMASK_INSTANCE_B', 'SoulQuest-SunkenThrone01')
INSTANCE_VERDANT = os.getenv('SOULMASK_INSTANCE_C', 'SoulQuest01')


# ---- RCON (AMP Console Passthrough, Source RCON protocol) ----
# Used as fallback direct RCON if AMP API passthrough fails
SOULMASK_B_RCON_HOST     = os.getenv('SOULMASK_B_RCON_HOST', '')
SOULMASK_B_RCON_PORT     = int(os.getenv('SOULMASK_B_RCON_PORT', '19000'))
SOULMASK_B_RCON_PASSWORD = os.getenv('SOULMASK_B_RCON_PASSWORD', '')

SOULMASK_C_RCON_HOST     = os.getenv('SOULMASK_C_RCON_HOST', '')
SOULMASK_C_RCON_PORT     = int(os.getenv('SOULMASK_C_RCON_PORT', '19000'))
SOULMASK_C_RCON_PASSWORD = os.getenv('SOULMASK_C_RCON_PASSWORD', '')

# ---- Fluxer channel IDs ----
SOULMASK_STATUS_CHANNEL  = int(os.getenv('SOULMASK_STATUS_CHANNEL', '0'))
SOULMASK_ANNOUNCE_CHANNEL = int(os.getenv('SOULMASK_ANNOUNCE_CHANNEL', '0'))

# ---- Config / data paths ----
_DATA_DIR     = Path(os.getenv('SOULMASK_DATA_DIR', '/mnt/gamestoreage2/DiscordBots/questlogfluxer/data/soulmask'))
SCHEDULE_FILE = _DATA_DIR / 'schedule.json'

SOULMASK_COLOR = 0xB8860B  # dark goldenrod

# ---- Suppress noisy AMP library logs ----
logging.getLogger('ampapi').setLevel(logging.CRITICAL)


# ---------------------------------------------------------------------------
# Schedule config loader
# ---------------------------------------------------------------------------

def _load_schedule() -> dict:
    """Load schedule.json. Returns empty dict on missing/invalid file."""
    if not SCHEDULE_FILE.exists():
        return {}
    try:
        return json.loads(SCHEDULE_FILE.read_text())
    except Exception as e:
        logger.error(f'SoulmaskCog: failed to load schedule.json: {e}')
        return {}


def _default_schedule() -> dict:
    """Return a default schedule.json structure as a starting point."""
    return {
        "instances": {
            "SoulQuest-SunkenThrone01": {
                "description": "End-game zone - rotating modes",
                "announce_channel": 0,
                "modes": {
                    "pve": {
                        "label": "PvE",
                        "description": "Standard PvE — default baseline",
                        "coefficients": {}
                    },
                    "pvec": {
                        "label": "PvE-C",
                        "description": "PvE with conflict — building damage and limited PvP enabled",
                        "coefficients": {
                            "YouFangShangHaiKaiGuan": 1,
                            "WanJiaHitJianZhuShangHaiRatio": 1,
                            "PVP_ShangHaiRatio_JinZhan": 0.4,
                            "PVP_ShangHaiRatio_YuanCheng": 0.4,
                            "RuQinBeginHour": 18,
                            "RuQinEndHour": 22
                        }
                    },
                    "loot_frenzy": {
                        "label": "Loot Frenzy",
                        "description": "Boosted drops and boss loot",
                        "coefficients": {
                            "BossRenDiaoLuoRatio": 2,
                            "JingYingRenDiaoLuoRatio": 1.5,
                            "BaoXiangDropRatio": 2
                        }
                    },
                    "build_day": {
                        "label": "Build Day",
                        "description": "Raids off, fast crafting",
                        "coefficients": {
                            "RuQinKaiGuan": 0,
                            "ZhiZuoTimeRatio": 0.5,
                            "ConverPropsSpeedRatio": 10
                        }
                    }
                },
                "schedule": [
                    {"days": ["Tuesday", "Friday"], "start_hour": 0, "mode": "pvec"},
                    {"days": ["Saturday"], "start_hour": 0, "mode": "loot_frenzy"}
                ],
                "default_mode": "pve"
            }
        }
    }


# ---------------------------------------------------------------------------
# AMP instance helper
# ---------------------------------------------------------------------------

async def _get_amp_instance(instance_name: str):
    """Connect to AMP via ADS account, find the Soulmask instance, re-auth with scoped account."""
    try:
        from ampapi.dataclass import APIParams
        from ampapi.bridge import Bridge
        from ampapi.controller import AMPControllerInstance as _AMPCtrl

        params = APIParams(url=AMP_URL, user=AMP_USER, password=AMP_PASSWORD)
        Bridge(api_params=params)
        ctrl = _AMPCtrl()
        await ctrl.get_instances()
        return next((i for i in ctrl.instances if i.instance_name == instance_name), None)
    except Exception as e:
        logger.warning(f'SoulmaskCog: _get_amp_instance({instance_name}) failed: {e}')
        return None


# ---------------------------------------------------------------------------
# RCON helpers
# ---------------------------------------------------------------------------

async def _send_rcon(instance_name: str, command: str) -> bool:
    """Send a console command to a Soulmask instance via AMP passthrough."""
    try:
        instance = await _get_amp_instance(instance_name)
        if instance:
            await instance.send_console_message(command)
            logger.info(f'SoulmaskCog: RCON [{instance_name}] {command}')
            return True
        logger.warning(f'SoulmaskCog: RCON - instance {instance_name} not found or not running')
        return False
    except Exception as e:
        logger.warning(f'SoulmaskCog: RCON command failed [{instance_name}] {command}: {e}')
        return False


async def _set_coefficient(instance_name: str, key: str, value) -> bool:
    """Send a single Set_Coefficient command."""
    return await _send_rcon(instance_name, f'Set_Coefficient {key} {value}')


async def _apply_mode(instance_name: str, mode: dict) -> tuple[int, int]:
    """
    Apply all coefficients for a mode via RCON.
    Returns (success_count, fail_count).
    """
    coefficients = mode.get('coefficients', {})
    if not coefficients:
        return 0, 0

    ok = fail = 0
    for key, value in coefficients.items():
        if await _set_coefficient(instance_name, key, value):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.25)  # small delay between commands

    return ok, fail


async def _reset_to_baseline(instance_name: str, schedule_cfg: dict) -> tuple[int, int]:
    """
    Reset all coefficients defined in any mode back to 1 (baseline).
    Called after backup restart to ensure clean state before re-applying schedule.
    Returns (success_count, fail_count).
    """
    # Collect all unique coefficient keys across all modes
    all_keys = set()
    for mode in schedule_cfg.get('modes', {}).values():
        all_keys.update(mode.get('coefficients', {}).keys())

    ok = fail = 0
    for key in all_keys:
        # Most coefficients baseline at 1; KaiGuan (switch) fields also default to their
        # standard value — we reset everything to 1 and let the schedule re-apply on top
        if await _set_coefficient(instance_name, key, 1):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.25)

    return ok, fail


# ---------------------------------------------------------------------------
# Schedule logic
# ---------------------------------------------------------------------------

def _active_mode_for_day(instance_cfg: dict, day_name: str) -> str:
    """Return the mode name that should be active today, or the default."""
    schedule = instance_cfg.get('schedule', [])
    for entry in schedule:
        if day_name in entry.get('days', []):
            return entry['mode']
    return instance_cfg.get('default_mode', 'pve')


def _next_mode_change(instance_cfg: dict, now: datetime.datetime) -> tuple[str, datetime.datetime] | tuple[None, None]:
    """Return (mode_name, datetime) of the next scheduled mode change, or (None, None)."""
    schedule = instance_cfg.get('schedule', [])
    candidates = []
    for entry in schedule:
        mode = entry['mode']
        start_hour = entry.get('start_hour', 0)
        for i in range(7):
            candidate_day = now + datetime.timedelta(days=i)
            if candidate_day.strftime('%A') in entry.get('days', []):
                candidate_dt = candidate_day.replace(
                    hour=start_hour, minute=0, second=0, microsecond=0
                )
                if candidate_dt > now:
                    candidates.append((mode, candidate_dt))

    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[1])
    return candidates[0]


# ---------------------------------------------------------------------------
# Fluxer embed helpers
# ---------------------------------------------------------------------------

def _mode_embed(instance_name: str, mode_name: str, mode: dict, action: str = 'Active') -> dict:
    """Build a Fluxer embed for a mode change announcement."""
    label = mode.get('label', mode_name.title())
    description = mode.get('description', '')
    coefficients = mode.get('coefficients', {})

    coeff_lines = '\n'.join(f'`{k}` = {v}' for k, v in coefficients.items()) or 'No coefficient changes (baseline)'

    return {
        'title': f'SoulQuest — {action}: {label}',
        'description': f'**{instance_name}**\n{description}',
        'color': SOULMASK_COLOR,
        'fields': [
            {'name': 'Coefficients Applied', 'value': coeff_lines, 'inline': False}
        ]
    }


def _status_embed(instance_name: str, mode_name: str, mode: dict,
                  next_mode: str | None, next_dt: datetime.datetime | None) -> dict:
    """Build a status embed showing current mode and next change."""
    label = mode.get('label', mode_name.title())
    next_info = 'No scheduled changes'
    if next_mode and next_dt:
        next_info = f'{next_mode.title()} at {next_dt.strftime("%A %H:%M UTC")}'

    return {
        'title': f'SoulQuest — {instance_name}',
        'description': f'**Current Mode:** {label}\n**Next Change:** {next_info}',
        'color': SOULMASK_COLOR,
    }


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class SoulmaskCog(Cog):
    """Soulmask cluster management — scheduled mode rotations via RCON."""

    def __init__(self, bot):
        super().__init__(bot)
        self._scheduler_task: asyncio.Task | None = None
        self._active_modes: dict[str, str] = {}  # instance_name -> current mode name

    @Cog.listener()
    async def on_ready(self):
        _DATA_DIR.mkdir(parents=True, exist_ok=True)

        # Write default schedule if none exists
        if not SCHEDULE_FILE.exists():
            SCHEDULE_FILE.write_text(json.dumps(_default_schedule(), indent=2))
            logger.info('SoulmaskCog: wrote default schedule.json')

        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.ensure_future(self._scheduler_loop())
            logger.info('SoulmaskCog: scheduler started')

    # ---- Scheduler ----

    async def _scheduler_loop(self):
        """Check schedule every minute and apply mode changes."""
        await asyncio.sleep(30)
        _last_applied: dict[str, str] = {}  # instance -> 'day:mode' last applied

        while True:
            try:
                schedule = _load_schedule()
                instances = schedule.get('instances', {})
                now = datetime.datetime.utcnow()
                day_name = now.strftime('%A')

                for instance_name, instance_cfg in instances.items():
                    mode_name = _active_mode_for_day(instance_cfg, day_name)
                    run_key = f'{instance_name}:{day_name}:{mode_name}'

                    if _last_applied.get(instance_name) == run_key:
                        continue  # already applied today

                    # Check if we've passed the start_hour for this mode
                    schedule_entries = instance_cfg.get('schedule', [])
                    start_hour = 0
                    for entry in schedule_entries:
                        if day_name in entry.get('days', []) and entry['mode'] == mode_name:
                            start_hour = entry.get('start_hour', 0)
                            break

                    if now.hour < start_hour:
                        continue  # not time yet

                    modes = instance_cfg.get('modes', {})
                    mode = modes.get(mode_name, {})

                    logger.info(f'SoulmaskCog: applying mode [{mode_name}] to {instance_name}')

                    # Announce upcoming change
                    channel_id = instance_cfg.get('announce_channel', SOULMASK_ANNOUNCE_CHANNEL)
                    if channel_id:
                        try:
                            embed = _mode_embed(instance_name, mode_name, mode, action='Activating')
                            await self.bot._http.send_message(str(channel_id), embeds=[embed])
                        except Exception as e:
                            logger.warning(f'SoulmaskCog: announce failed: {e}')

                    ok, fail = await _apply_mode(instance_name, mode)
                    _last_applied[instance_name] = run_key
                    self._active_modes[instance_name] = mode_name

                    logger.info(f'SoulmaskCog: mode [{mode_name}] applied to {instance_name} — {ok} ok, {fail} failed')

                    # Confirm announcement
                    if channel_id:
                        try:
                            label = mode.get('label', mode_name.title())
                            result_text = f'{ok} coefficients applied'
                            if fail:
                                result_text += f', {fail} failed'
                            await self.bot._http.send_message(str(channel_id), content=f'**{label}** mode is now active on {instance_name}. {result_text}.')
                        except Exception as e:
                            logger.warning(f'SoulmaskCog: confirm announce failed: {e}')

            except Exception as e:
                logger.error(f'SoulmaskCog: scheduler loop error: {e}', exc_info=True)

            await asyncio.sleep(60)

    # ---- Commands ----

    @Cog.command(name='sm_status')
    async def cmd_status(self, ctx):
        """Show current mode and next scheduled change for all Soulmask instances."""
        schedule = _load_schedule()
        instances = schedule.get('instances', {})
        if not instances:
            await ctx.reply('No Soulmask instances configured in schedule.json.')
            return

        now = datetime.datetime.utcnow()
        day_name = now.strftime('%A')

        for instance_name, instance_cfg in instances.items():
            mode_name = self._active_modes.get(instance_name) or _active_mode_for_day(instance_cfg, day_name)
            modes = instance_cfg.get('modes', {})
            mode = modes.get(mode_name, {})
            next_mode, next_dt = _next_mode_change(instance_cfg, now)
            embed = _status_embed(instance_name, mode_name, mode, next_mode, next_dt)
            await ctx.reply(embeds=[embed])

    @Cog.command(name='sm_mode')
    async def cmd_mode(self, ctx, instance_short: str, mode_name: str):
        """Manually apply a mode to an instance. Usage: !sm_mode sunken pvec"""
        schedule = _load_schedule()
        instances = schedule.get('instances', {})

        # Allow short names: 'sunken' -> SoulQuest-SunkenThrone01, 'verdant' -> SoulQuest01
        _short_map = {
            'sunken':  INSTANCE_SUNKEN,
            'verdant': INSTANCE_VERDANT,
            'b':       INSTANCE_SUNKEN,
            'c':       INSTANCE_VERDANT,
        }
        instance_name = _short_map.get(instance_short.lower(), instance_short)
        instance_cfg = instances.get(instance_name)

        if not instance_cfg:
            await ctx.reply(f'Unknown instance `{instance_short}`. Use: sunken, verdant')
            return

        modes = instance_cfg.get('modes', {})
        mode = modes.get(mode_name.lower())
        if not mode:
            available = ', '.join(modes.keys())
            await ctx.reply(f'Unknown mode `{mode_name}`. Available: {available}')
            return

        await ctx.reply(f'Applying **{mode.get("label", mode_name)}** mode to `{instance_name}`...')
        ok, fail = await _apply_mode(instance_name, mode)
        self._active_modes[instance_name] = mode_name.lower()

        result = f'{ok} coefficients applied'
        if fail:
            result += f', {fail} failed'
        await ctx.reply(f'Done. {result}.')

    @Cog.command(name='sm_reset')
    async def cmd_reset(self, ctx, instance_short: str):
        """Reset all scheduled coefficients to baseline (1) on an instance. Usage: !sm_reset sunken"""
        schedule = _load_schedule()
        instances = schedule.get('instances', {})

        _short_map = {
            'sunken':  INSTANCE_SUNKEN,
            'verdant': INSTANCE_VERDANT,
            'b':       INSTANCE_SUNKEN,
            'c':       INSTANCE_VERDANT,
        }
        instance_name = _short_map.get(instance_short.lower(), instance_short)
        instance_cfg = instances.get(instance_name)

        if not instance_cfg:
            await ctx.reply(f'Unknown instance `{instance_short}`.')
            return

        await ctx.reply(f'Resetting all coefficients to baseline on `{instance_name}`...')
        ok, fail = await _reset_to_baseline(instance_name, instance_cfg)
        self._active_modes[instance_name] = instance_cfg.get('default_mode', 'pve')

        result = f'{ok} coefficients reset'
        if fail:
            result += f', {fail} failed'
        await ctx.reply(f'Done. {result}.')

    @Cog.command(name='sm_coefficients')
    async def cmd_coefficients(self, ctx, instance_short: str):
        """List current active coefficients for an instance. Usage: !sm_coefficients sunken"""
        schedule = _load_schedule()
        instances = schedule.get('instances', {})

        _short_map = {
            'sunken':  INSTANCE_SUNKEN,
            'verdant': INSTANCE_VERDANT,
            'b':       INSTANCE_SUNKEN,
            'c':       INSTANCE_VERDANT,
        }
        instance_name = _short_map.get(instance_short.lower(), instance_short)
        instance_cfg = instances.get(instance_name)

        if not instance_cfg:
            await ctx.reply(f'Unknown instance `{instance_short}`.')
            return

        mode_name = self._active_modes.get(instance_name, instance_cfg.get('default_mode', 'pve'))
        modes = instance_cfg.get('modes', {})
        mode = modes.get(mode_name, {})
        coefficients = mode.get('coefficients', {})

        if not coefficients:
            await ctx.reply(f'`{instance_name}` is in **{mode_name}** mode — no coefficient overrides (baseline).')
            return

        lines = '\n'.join(f'`{k}` = {v}' for k, v in coefficients.items())
        await ctx.reply(f'**{instance_name}** — {mode_name} mode:\n{lines}')

    @Cog.command(name='sm_reload')
    async def cmd_reload(self, ctx):
        """Reload schedule.json without restarting the bot."""
        schedule = _load_schedule()
        count = len(schedule.get('instances', {}))
        await ctx.reply(f'Schedule reloaded. {count} instance(s) configured.')

    @Cog.command(name='sm_schedule')
    async def cmd_schedule(self, ctx):
        """Show the full weekly schedule for all instances."""
        schedule = _load_schedule()
        instances = schedule.get('instances', {})
        if not instances:
            await ctx.reply('No instances configured.')
            return

        lines = []
        for instance_name, instance_cfg in instances.items():
            lines.append(f'**{instance_name}**')
            default = instance_cfg.get('default_mode', 'pve')
            lines.append(f'  Default: {default}')
            for entry in instance_cfg.get('schedule', []):
                days = ', '.join(entry.get('days', []))
                mode = entry.get('mode', '?')
                hour = entry.get('start_hour', 0)
                lines.append(f'  {days} @ {hour:02d}:00 UTC → {mode}')

        await ctx.reply('\n'.join(lines))
