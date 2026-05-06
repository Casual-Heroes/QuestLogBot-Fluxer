# cogs/nominations.py - Community Spotlight: Monthly Most Helpful nominations
#
# Monthly schedule (UTC):
#   1st  - posts nomination-open reminder in spotlight channel
#   20th - posts reminder with current top nominees
#   26th - closes nominations, posts voting poll (top 5 per category, reaction votes)
#   Last day of month - tallies votes, calls web /api/internal/close-nominations/,
#                       announces winners
#
# Bot commands:
#   !nominate @user reason  - submit or update your nomination (community category)
#   !nominations            - list current nominees
#
# Channel: spotlight_channel_id from web_fluxer_guild_settings

import asyncio
import calendar
import datetime
import json
import time

import requests

from fluxer import Cog
from sqlalchemy import text

from config import (
    logger,
    db_session_scope,
    QUESTLOG_INTERNAL_API_URL,
    QUESTLOG_BOT_SECRET,
)

CHECK_INTERVAL = 3600  # check once per hour

CATEGORIES = [
    {'key': 'community', 'label': 'Most Helpful',        'points': 15},
    {'key': 'lfg_host',  'label': 'Best LFG Host',       'points': 12},
    {'key': 'build',     'label': 'Most Creative Build',  'points': 12},
    {'key': '7dtd',      'label': '7DTD MVP',             'points': 10},
    {'key': 'valheim',   'label': 'Valheim Wanderer',     'points': 10},
    {'key': 'minecraft', 'label': 'Minecraft Builder',    'points': 10},
    {'key': 'dayz',      'label': 'DayZ Survivor',        'points': 10},
    {'key': 'palworld',  'label': 'Palworld Tamer',       'points': 10},
]


def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _get_guilds_with_spotlight() -> list[dict]:
    """Return list of {guild_id, spotlight_channel_id} for all configured guilds."""
    try:
        with db_session_scope() as db:
            rows = db.execute(
                text(
                    "SELECT guild_id, spotlight_channel_id "
                    "FROM web_fluxer_guild_settings "
                    "WHERE spotlight_channel_id IS NOT NULL AND spotlight_channel_id != ''"
                )
            ).fetchall()
            return [{'guild_id': r[0], 'channel_id': r[1]} for r in rows]
    except Exception as e:
        logger.error(f"NominationsCog: failed to load spotlight guilds: {e}")
        return []


def _top_nominees(month_year: str, category: str, limit: int = 5) -> list[dict]:
    """Return top nominees for a category this month, sorted by nomination count."""
    try:
        with db_session_scope() as db:
            rows = db.execute(
                text(
                    "SELECT n.nominated_user_id, u.username, COUNT(*) as cnt "
                    "FROM web_legacy_nominations n "
                    "JOIN web_users u ON u.id = n.nominated_user_id "
                    "WHERE n.month_year = :my AND n.category = :cat "
                    "GROUP BY n.nominated_user_id, u.username "
                    "ORDER BY cnt DESC LIMIT :lim"
                ),
                {'my': month_year, 'cat': category, 'lim': limit}
            ).fetchall()
            return [{'user_id': r[0], 'username': r[1], 'count': r[2]} for r in rows]
    except Exception as e:
        logger.error(f"NominationsCog: top_nominees failed: {e}")
        return []


def _resolve_web_user_id(fluxer_user_id: str) -> int | None:
    """Map Fluxer user ID to web_users.id via fluxer_id column."""
    try:
        with db_session_scope() as db:
            row = db.execute(
                text("SELECT id FROM web_users WHERE fluxer_id = :fid LIMIT 1"),
                {'fid': fluxer_user_id}
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _resolve_web_user_by_username(username: str) -> int | None:
    """Map QuestLog username to web_users.id."""
    try:
        with db_session_scope() as db:
            row = db.execute(
                text("SELECT id FROM web_users WHERE username = :u AND is_banned = 0 LIMIT 1"),
                {'u': username}
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _save_nomination(month_year: str, category: str,
                     nominated_user_id: int, nominated_by_fluxer_id: str,
                     guild_id: str, reason: str) -> bool:
    """Upsert a nomination from Fluxer. Returns True on success."""
    now = int(time.time())
    try:
        with db_session_scope() as db:
            existing = db.execute(
                text(
                    "SELECT id FROM web_legacy_nominations "
                    "WHERE month_year = :my AND category = :cat "
                    "AND nominated_by_fluxer_id = :fid LIMIT 1"
                ),
                {'my': month_year, 'cat': category, 'fid': nominated_by_fluxer_id}
            ).fetchone()

            if existing:
                db.execute(
                    text(
                        "UPDATE web_legacy_nominations SET nominated_user_id = :uid, "
                        "reason = :reason, updated_at = :now "
                        "WHERE id = :id"
                    ),
                    {'uid': nominated_user_id, 'reason': reason[:500], 'now': now, 'id': existing[0]}
                )
            else:
                db.execute(
                    text(
                        "INSERT INTO web_legacy_nominations "
                        "(month_year, category, nominated_user_id, nominated_by_fluxer_id, "
                        "guild_id, platform, reason, created_at, updated_at) "
                        "VALUES (:my, :cat, :uid, :fid, :gid, 'fluxer', :reason, :now, :now)"
                    ),
                    {
                        'my': month_year, 'cat': category, 'uid': nominated_user_id,
                        'fid': nominated_by_fluxer_id, 'gid': guild_id,
                        'reason': reason[:500], 'now': now,
                    }
                )
            db.commit()
        return True
    except Exception as e:
        logger.error(f"NominationsCog: save_nomination failed: {e}")
        return False


def _call_close_nominations(month_year: str) -> dict:
    """POST to web API to tally votes and award winners."""
    try:
        url = f"{QUESTLOG_INTERNAL_API_URL}/api/internal/close-nominations/"
        resp = requests.post(
            url,
            json={'month_year': month_year},
            headers={'X-Bot-Secret': QUESTLOG_BOT_SECRET, 'Content-Type': 'application/json'},
            timeout=15,
        )
        return resp.json() if resp.ok else {'error': resp.text[:200]}
    except Exception as e:
        logger.error(f"NominationsCog: close_nominations API failed: {e}")
        return {'error': str(e)}


class NominationsCog(Cog):
    """Monthly Most Helpful nominations and awards."""

    def __init__(self, bot):
        super().__init__(bot)
        self._task: asyncio.Task | None = None
        self._last_fired: dict[str, int] = {}  # guild_id -> last day fired

    @Cog.listener()
    async def on_ready(self):
        logger.info("NominationsCog ready - starting monthly check loop")
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._monthly_loop())

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _monthly_loop(self):
        await asyncio.sleep(30)  # brief startup delay
        while True:
            try:
                await self._check_monthly_events()
            except Exception as e:
                logger.error(f"NominationsCog: loop error: {e}")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _check_monthly_events(self):
        now = datetime.datetime.utcnow()
        day = now.day
        month_year = now.strftime('%Y-%m')
        last_day = _last_day_of_month(now.year, now.month)

        guilds = _get_guilds_with_spotlight()
        if not guilds:
            return

        for g in guilds:
            channel_id = g['channel_id']
            guild_id = g['guild_id']
            fire_key = f"{guild_id}:{month_year}:{day}"

            if self._last_fired.get(fire_key):
                continue

            if day == 1:
                await self._post_nominations_open(channel_id, month_year)
                self._last_fired[fire_key] = 1

            elif day == 20:
                await self._post_nominations_reminder(channel_id, month_year)
                self._last_fired[fire_key] = 1

            elif day == 26:
                await self._post_voting_poll(channel_id, month_year)
                self._last_fired[fire_key] = 1

            elif day == last_day:
                await self._close_and_announce(channel_id, guild_id, month_year)
                self._last_fired[fire_key] = 1

        # Prune old keys to avoid memory growth
        if len(self._last_fired) > 500:
            self._last_fired = {}

    # ------------------------------------------------------------------
    # Monthly event posts
    # ------------------------------------------------------------------

    async def _post_nominations_open(self, channel_id: str, month_year: str):
        embed = {
            'title': f'Community Spotlight - Nominations Open!',
            'description': (
                f'It\'s the start of a new month - time to recognize your community heroes!\n\n'
                f'**Nominate someone** who helped you, built something awesome, or made the server better.\n\n'
                f'- Fluxer: `!nominate @username reason`\n'
                f'- Website: https://casual-heroes.com/ql/legacy/nominate/\n\n'
                f'Nominations close on the **25th**. Voting begins on the **26th**.'
            ),
            'color': 0xEAB308,
            'footer': {'text': f'Month: {month_year}'},
        }
        try:
            await self.bot._http.send_message(str(channel_id), embeds=[embed])
        except Exception as e:
            logger.error(f"NominationsCog: post_nominations_open failed: {e}")

    async def _post_nominations_reminder(self, channel_id: str, month_year: str):
        lines = []
        for cat in CATEGORIES:
            nominees = _top_nominees(month_year, cat['key'], limit=3)
            if nominees:
                names = ', '.join(n['username'] for n in nominees)
                lines.append(f"**{cat['label']}:** {names}")
            else:
                lines.append(f"**{cat['label']}:** No nominations yet")

        embed = {
            'title': f'Nominations Reminder - 5 Days Left!',
            'description': (
                'Nominations close in 5 days. Current standings:\n\n'
                + '\n'.join(lines)
                + '\n\nNominate via `!nominate @username reason` or https://casual-heroes.com/ql/legacy/nominate/'
            ),
            'color': 0xF97316,
            'footer': {'text': f'Month: {month_year}'},
        }
        try:
            await self.bot._http.send_message(str(channel_id), embeds=[embed])
        except Exception as e:
            logger.error(f"NominationsCog: post_reminder failed: {e}")

    async def _post_voting_poll(self, channel_id: str, month_year: str):
        lines = ['Nominations are closed! React to vote for your favorites:\n']
        emoji_nums = ['1\u20e3', '2\u20e3', '3\u20e3', '4\u20e3', '5\u20e3']

        for cat in CATEGORIES:
            nominees = _top_nominees(month_year, cat['key'], limit=5)
            if not nominees:
                continue
            lines.append(f"**{cat['label']}**")
            for i, n in enumerate(nominees):
                lines.append(f"{emoji_nums[i]} {n['username']} ({n['count']} nominations)")
            lines.append('')

        if len(lines) == 1:
            lines.append('No nominations received this month.')

        embed = {
            'title': f'Community Spotlight - Voting Open!',
            'description': '\n'.join(lines),
            'color': 0x8B5CF6,
            'footer': {'text': f'Voting closes end of month | {month_year}'},
        }
        try:
            await self.bot._http.send_message(str(channel_id), embeds=[embed])
        except Exception as e:
            logger.error(f"NominationsCog: post_voting_poll failed: {e}")

    async def _close_and_announce(self, channel_id: str, guild_id: str, month_year: str):
        result = _call_close_nominations(month_year)
        if 'error' in result:
            logger.error(f"NominationsCog: close_nominations error for {guild_id}: {result['error']}")
            return

        winners = [r for r in result.get('results', []) if r.get('winner_id')]
        if not winners:
            embed = {
                'title': f'Community Spotlight - {month_year}',
                'description': 'No nominations were received this month. Nominate someone next month!',
                'color': 0x6B7280,
            }
        else:
            lines = []
            for w in winners:
                cat_label = next((c['label'] for c in CATEGORIES if c['key'] == w['category']), w['category'])
                username = w.get('username') or f"User #{w['winner_id']}"
                lines.append(f"**{cat_label}:** {username}")

            embed = {
                'title': f'Community Spotlight Winners - {month_year}',
                'description': (
                    'Congratulations to this month\'s Community Spotlight winners! '
                    'Legacy points and permanent flair trophies have been awarded.\n\n'
                    + '\n'.join(lines)
                    + '\n\nSee their Legacy profiles at https://casual-heroes.com/ql/'
                ),
                'color': 0xEAB308,
                'footer': {'text': 'Nominations for next month open on the 1st'},
            }
        try:
            await self.bot._http.send_message(str(channel_id), embeds=[embed])
        except Exception as e:
            logger.error(f"NominationsCog: close_and_announce failed: {e}")

    # ------------------------------------------------------------------
    # Bot commands
    # ------------------------------------------------------------------

    @Cog.command(name='nominate')
    async def cmd_nominate(self, ctx, *args):
        """!nominate @username reason - nominate someone for Community Most Helpful."""
        now = datetime.datetime.utcnow()
        if now.day > 25:
            await self.bot._http.send_message(
                str(ctx.channel.id),
                content='Nominations are closed for this month. Voting is now open!'
            )
            return

        if not args:
            await self.bot._http.send_message(
                str(ctx.channel.id),
                content='Usage: `!nominate @username reason`'
            )
            return

        # Parse: first arg is the mention or username, rest is reason
        raw_target = args[0].strip('<@!>') if args else ''
        reason = ' '.join(args[1:]).strip() if len(args) > 1 else ''

        nominated_user_id = None
        # Try as Fluxer user ID first
        if raw_target.isdigit():
            nominated_user_id = _resolve_web_user_id(raw_target)
        # Try as QuestLog username
        if not nominated_user_id:
            nominated_user_id = _resolve_web_user_by_username(raw_target)

        if not nominated_user_id:
            await self.bot._http.send_message(
                str(ctx.channel.id),
                content=f'Could not find a QuestLog account for `{raw_target}`. They must have a linked account.'
            )
            return

        # Check self-nomination
        nominator_web_id = _resolve_web_user_id(str(ctx.author.id))
        if nominator_web_id and nominator_web_id == nominated_user_id:
            await self.bot._http.send_message(
                str(ctx.channel.id),
                content="You can't nominate yourself!"
            )
            return

        month_year = now.strftime('%Y-%m')
        ok = _save_nomination(
            month_year=month_year,
            category='community',
            nominated_user_id=nominated_user_id,
            nominated_by_fluxer_id=str(ctx.author.id),
            guild_id=str(ctx.guild.id) if hasattr(ctx, 'guild') and ctx.guild else '',
            reason=reason,
        )

        if ok:
            await self.bot._http.send_message(
                str(ctx.channel.id),
                content=f'Nomination submitted for **{raw_target}**! You can update it anytime before the 25th.'
            )
        else:
            await self.bot._http.send_message(
                str(ctx.channel.id),
                content='Failed to save nomination. Please try again.'
            )

    @Cog.command(name='nominations')
    async def cmd_nominations(self, ctx):
        """!nominations - show current month's top nominees."""
        month_year = datetime.datetime.utcnow().strftime('%Y-%m')
        lines = []
        for cat in CATEGORIES:
            nominees = _top_nominees(month_year, cat['key'], limit=3)
            if nominees:
                names = ', '.join(f"{n['username']} ({n['count']})" for n in nominees)
                lines.append(f"**{cat['label']}:** {names}")
            else:
                lines.append(f"**{cat['label']}:** No nominations yet")

        embed = {
            'title': f'Current Nominees - {month_year}',
            'description': '\n'.join(lines) + '\n\nNominate: `!nominate @username reason`',
            'color': 0xEAB308,
        }
        try:
            await self.bot._http.send_message(str(ctx.channel.id), embeds=[embed])
        except Exception as e:
            logger.error(f"NominationsCog: cmd_nominations failed: {e}")
