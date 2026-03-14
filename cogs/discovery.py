# cogs/discovery.py - Game Discovery for QuestLogFluxer
#
# Mirrors WardenBot's game discovery system exactly:
# - Scheduled task runs every 15 minutes, per-guild interval controlled by config
# - Queries WebFluxerGameSearchConfig for IGDB search params
# - Saves new games to WebFluxerFoundGame (used by member portal /games page)
# - Saves to WebFluxerAnnouncedGame to prevent re-announcing
# - Posts a summary embed to the configured discovery channel
# - !checkgames - force-run discovery for this guild (owner/admin only)

import json
import time
import asyncio
import logging
from datetime import datetime

import fluxer
from fluxer import Cog
from sqlalchemy import text

from config import logger, db_session_scope, IGDB_CLIENT_ID, IGDB_CLIENT_SECRET
from utils import igdb

DISCOVERY_CHECK_INTERVAL = 15 * 60   # 15 minutes between scheduled passes
DISCOVERY_COLOR_GREEN = 0x57F287
PORTAL_BASE_URL = "https://casual-heroes.com/ql/fluxer"


def _is_owner_or_admin(bot, guild_id: str, user_id: str) -> bool:
    """Check if user is guild owner or has admin role configured in guild settings."""
    try:
        with db_session_scope() as db:
            row = db.execute(
                text("SELECT owner_id, admin_roles FROM web_fluxer_guild_settings WHERE guild_id = :g"),
                {'g': guild_id},
            ).fetchone()
            if not row:
                return False
            if str(row.owner_id) == str(user_id):
                return True
            # Check admin_roles - these are role IDs the user must have
            # We can't check member roles here without a guild object, so just allow owner for now
            return False
    except Exception as e:
        logger.warning(f"DiscoveryCog: admin check failed for {user_id} in {guild_id}: {e}")
        return False


def _load_guild_config(guild_id: str) -> dict | None:
    """Load game discovery config from web_fluxer_guild_settings."""
    try:
        with db_session_scope() as db:
            row = db.execute(
                text(
                    "SELECT game_discovery_enabled, game_discovery_channel_id, "
                    "game_discovery_ping_role_id, game_check_interval_hours, last_game_check_at "
                    "FROM web_fluxer_guild_settings WHERE guild_id = :g"
                ),
                {'g': guild_id},
            ).fetchone()
            if not row:
                return None
            return dict(row._mapping)
    except Exception as e:
        logger.error(f"DiscoveryCog: config load failed for guild {guild_id}: {e}")
        return None


def _load_search_configs(guild_id: str) -> list:
    """Load all enabled game search configs for a guild."""
    try:
        with db_session_scope() as db:
            rows = db.execute(
                text(
                    "SELECT id, name, genres, themes, keywords, game_modes, platforms, "
                    "min_hype, min_rating, days_ahead, show_on_website "
                    "FROM web_fluxer_game_search_configs "
                    "WHERE guild_id = :g AND enabled = 1"
                ),
                {'g': guild_id},
            ).fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception as e:
        logger.error(f"DiscoveryCog: search config load failed for guild {guild_id}: {e}")
        return []


def _is_already_announced(guild_id: str, igdb_id: int) -> bool:
    """Check if this game was already announced in this guild."""
    try:
        with db_session_scope() as db:
            row = db.execute(
                text("SELECT id FROM web_fluxer_announced_games WHERE guild_id = :g AND igdb_id = :i"),
                {'g': guild_id, 'i': igdb_id},
            ).fetchone()
            return row is not None
    except Exception as e:
        logger.warning(f"DiscoveryCog: announced check failed: {e}")
        return False


def _is_already_found(guild_id: str, igdb_id: int) -> bool:
    """Check if this game is already in the found games table."""
    try:
        with db_session_scope() as db:
            row = db.execute(
                text("SELECT id FROM web_fluxer_found_games WHERE guild_id = :g AND igdb_id = :i"),
                {'g': guild_id, 'i': igdb_id},
            ).fetchone()
            return row is not None
    except Exception as e:
        logger.warning(f"DiscoveryCog: found-game check failed: {e}")
        return False


def _record_announced_game(guild_id: str, game, steam_app_id=None) -> None:
    """Save an AnnouncedGame record to prevent re-announcing."""
    try:
        with db_session_scope() as db:
            db.execute(
                text(
                    "INSERT INTO web_fluxer_announced_games "
                    "(guild_id, igdb_id, igdb_slug, steam_id, game_name, release_date, "
                    "genres, platforms, cover_url, announced_at) "
                    "VALUES (:guild_id, :igdb_id, :slug, :steam_id, :name, :release_date, "
                    ":genres, :platforms, :cover_url, :announced_at)"
                ),
                {
                    'guild_id': guild_id,
                    'igdb_id': game.id,
                    'slug': getattr(game, 'slug', None),
                    'steam_id': steam_app_id,
                    'name': game.name,
                    'release_date': game.release_date,
                    'genres': json.dumps(game.genres) if hasattr(game, 'genres') else None,
                    'platforms': json.dumps(game.platforms) if hasattr(game, 'platforms') else None,
                    'cover_url': game.cover_url,
                    'announced_at': int(time.time()),
                },
            )
            db.commit()
    except Exception as e:
        logger.warning(f"DiscoveryCog: failed to record announced game '{game.name}': {e}")


def _record_found_game(guild_id: str, game, search_config: dict, steam_url=None) -> None:
    """Save a FoundGame record for the member portal /games page."""
    try:
        with db_session_scope() as db:
            db.execute(
                text(
                    "INSERT INTO web_fluxer_found_games "
                    "(guild_id, igdb_id, igdb_slug, game_name, release_date, summary, "
                    "genres, themes, keywords, game_modes, platforms_json, cover_url, "
                    "igdb_url, steam_url, hypes, rating, search_config_id, search_config_name, found_at) "
                    "VALUES (:guild_id, :igdb_id, :slug, :name, :release_date, :summary, "
                    ":genres, :themes, :keywords, :game_modes, :platforms, :cover_url, "
                    ":igdb_url, :steam_url, :hypes, :rating, :sc_id, :sc_name, :found_at)"
                ),
                {
                    'guild_id': guild_id,
                    'igdb_id': game.id,
                    'slug': getattr(game, 'slug', None),
                    'name': game.name,
                    'release_date': game.release_date,
                    'summary': getattr(game, 'summary', None),
                    'genres': json.dumps(game.genres) if hasattr(game, 'genres') else None,
                    'themes': json.dumps(game.themes) if hasattr(game, 'themes') else None,
                    'keywords': json.dumps(game.keywords) if hasattr(game, 'keywords') and game.keywords else None,
                    'game_modes': json.dumps(game.game_modes) if hasattr(game, 'game_modes') else None,
                    'platforms': json.dumps(game.platforms) if hasattr(game, 'platforms') else None,
                    'cover_url': game.cover_url,
                    'igdb_url': f"https://www.igdb.com/games/{game.slug}" if getattr(game, 'slug', None) else None,
                    'steam_url': steam_url,
                    'hypes': getattr(game, 'hypes', None),
                    'rating': getattr(game, 'rating', None),
                    'sc_id': search_config.get('id'),
                    'sc_name': search_config.get('name'),
                    'found_at': int(time.time()),
                },
            )
            db.commit()
    except Exception as e:
        logger.warning(f"DiscoveryCog: failed to record found game '{game.name}': {e}")


def _update_last_check(guild_id: str, timestamp: int) -> None:
    """Update last_game_check_at for a guild."""
    try:
        with db_session_scope() as db:
            db.execute(
                text("UPDATE web_fluxer_guild_settings SET last_game_check_at = :t WHERE guild_id = :g"),
                {'t': timestamp, 'g': guild_id},
            )
            db.commit()
    except Exception as e:
        logger.error(f"DiscoveryCog: failed to update last_game_check_at for {guild_id}: {e}")


def _get_all_discovery_guilds() -> list:
    """Get all guilds with game discovery enabled and a channel configured."""
    try:
        with db_session_scope() as db:
            rows = db.execute(
                text(
                    "SELECT guild_id, game_discovery_channel_id, game_discovery_ping_role_id, "
                    "game_check_interval_hours, last_game_check_at "
                    "FROM web_fluxer_guild_settings "
                    "WHERE game_discovery_enabled = 1 AND game_discovery_channel_id IS NOT NULL"
                )
            ).fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception as e:
        logger.error(f"DiscoveryCog: failed to load discovery guilds: {e}")
        return []


def _build_release_window(games: list) -> str:
    """Build a human-readable release window string from a list of IGDBGame objects."""
    release_dates = [g.release_date for g in games if g.release_date]
    if not release_dates:
        return "TBA"
    min_date = min(release_dates)
    max_date = max(release_dates)
    min_dt = datetime.fromtimestamp(min_date)
    max_dt = datetime.fromtimestamp(max_date)
    if min_dt.year == max_dt.year and min_dt.month == max_dt.month:
        return min_dt.strftime("%b %Y")
    elif min_dt.year == max_dt.year:
        return f"{min_dt.strftime('%b')} - {max_dt.strftime('%b %Y')}"
    else:
        return f"{min_dt.strftime('%b %Y')} - {max_dt.strftime('%b %Y')}"


async def _run_discovery_for_guild(bot, guild_id: str, channel_id: str,
                                    ping_role_id: str | None,
                                    search_configs: list) -> int:
    """
    Run a full game discovery pass for one guild.
    Returns the number of newly announced games.
    """
    now = int(time.time())
    all_games_meta = {}   # igdb_id -> {game, is_public, search_config}

    for sc in search_configs:
        try:
            genres = json.loads(sc['genres']) if sc.get('genres') else None
            themes = json.loads(sc['themes']) if sc.get('themes') else None
            keywords = json.loads(sc['keywords']) if sc.get('keywords') else None
            modes = json.loads(sc['game_modes']) if sc.get('game_modes') else None
            platforms = json.loads(sc['platforms']) if sc.get('platforms') else None
            days_ahead = sc.get('days_ahead') or 30
            min_hype = sc.get('min_hype')
            min_rating = sc.get('min_rating')

            games = await igdb.search_upcoming_games(
                days_ahead=365,
                days_behind=0,
                genres=genres,
                themes=themes,
                keywords=keywords,
                game_modes=modes,
                platforms=platforms,
                min_hype=min_hype,
                min_rating=min_rating,
                limit=100,
            )

            logger.info(f"DiscoveryCog: guild {guild_id} search '{sc['name']}' returned {len(games)} games")

            # Filter to announcement window
            cutoff = now + (days_ahead * 24 * 60 * 60)
            games = [g for g in games if g.release_date and g.release_date <= cutoff]

            for game in games:
                if game.id not in all_games_meta:
                    all_games_meta[game.id] = {
                        'game': game,
                        'is_public': bool(sc.get('show_on_website', 1)),
                        'search_config': sc,
                    }
        except Exception as e:
            logger.error(f"DiscoveryCog: search '{sc['name']}' failed for guild {guild_id}: {e}")
            continue

    logger.info(f"DiscoveryCog: guild {guild_id} total unique games across searches: {len(all_games_meta)}")

    games_to_announce = []
    games_by_config: dict[str, list] = {}

    for game_id, meta in all_games_meta.items():
        game = meta['game']
        is_public = meta['is_public']
        sc = meta['search_config']

        if not is_public:
            continue

        if _is_already_announced(guild_id, game.id):
            continue

        # Extract Steam info from IGDB websites
        steam_app_id = None
        steam_url = None
        if hasattr(game, 'websites') and game.websites:
            for website in game.websites:
                if isinstance(website, dict) and website.get('category') == 13:
                    url = website.get('url', '')
                    steam_url = url
                    if '/app/' in url:
                        try:
                            steam_app_id = int(url.split('/app/')[1].split('/')[0].split('?')[0])
                        except (ValueError, IndexError):
                            pass
                    break

        _record_announced_game(guild_id, game, steam_app_id)
        games_to_announce.append(game)

        # Track by search config name for embed field
        config_name = sc.get('name') or 'Unnamed Search'
        if config_name not in games_by_config:
            games_by_config[config_name] = []
        games_by_config[config_name].append(game)

        if not _is_already_found(guild_id, game.id):
            _record_found_game(guild_id, game, sc, steam_url)

    announced_count = len(games_to_announce)

    if not games_to_announce:
        logger.info(f"DiscoveryCog: no new games to announce for guild {guild_id}")
        return 0

    # Build announcement embed (mirrors WardenBot exactly)
    try:
        most_anticipated = max(
            games_to_announce,
            key=lambda g: g.hypes if hasattr(g, 'hypes') and g.hypes else 0
        )
        most_anticipated_hypes = most_anticipated.hypes if hasattr(most_anticipated, 'hypes') and most_anticipated.hypes else 0

        all_platforms: set = set()
        all_genres: set = set()
        for game in games_to_announce:
            if hasattr(game, 'platforms') and game.platforms:
                all_platforms.update(game.platforms)
            if hasattr(game, 'genres') and game.genres:
                all_genres.update(game.genres)

        release_window = _build_release_window(games_to_announce)

        portal_url = f"{PORTAL_BASE_URL}/{guild_id}/games/"

        embed = fluxer.Embed(
            title="🎮 New Games Discovered!",
            description=f"Found **{announced_count}** new game{'s' if announced_count != 1 else ''} matching your searches!",
            color=DISCOVERY_COLOR_GREEN,
        )

        # By search config
        if games_by_config:
            config_lines = []
            for cfg_name, cfg_games in list(games_by_config.items())[:5]:
                config_lines.append(f"**{cfg_name}**: {len(cfg_games)} game{'s' if len(cfg_games) != 1 else ''}")
            embed.add_field(
                name="📋 By Search Configuration",
                value="\n".join(config_lines),
                inline=False,
            )

        # Most anticipated
        if most_anticipated_hypes > 0:
            embed.add_field(
                name="🔥 Most Anticipated",
                value=f"**{most_anticipated.name}**\n{most_anticipated_hypes:,} follows",
                inline=True,
            )
        else:
            embed.add_field(
                name="🔥 Most Anticipated",
                value=f"**{most_anticipated.name}**",
                inline=True,
            )

        # Quick stats
        platform_str = ", ".join(list(all_platforms)[:4]) if all_platforms else "Various"
        if len(all_platforms) > 4:
            platform_str += f" +{len(all_platforms) - 4}"
        genre_str = ", ".join(list(all_genres)[:3]) if all_genres else "Various"
        if len(all_genres) > 3:
            genre_str += f" +{len(all_genres) - 3}"
        embed.add_field(
            name="📊 Quick Stats",
            value=f"**Platforms:** {platform_str}\n**Genres:** {genre_str}",
            inline=True,
        )

        # Release window
        embed.add_field(name="📅 Release Window", value=release_window, inline=True)

        # Dashboard link
        embed.add_field(
            name="🔗 View Full Details",
            value=f"[Click here to view all games on the portal]({portal_url})",
            inline=False,
        )

        if most_anticipated.cover_url:
            embed.set_thumbnail(url=most_anticipated.cover_url)

        embed.set_footer(text=f"Based on {len(search_configs)} active search configuration{'s' if len(search_configs) != 1 else ''}")

        content = f"<@&{ping_role_id}>" if ping_role_id else None
        await bot._http.send_message(channel_id, content=content, embed=embed)
        logger.info(f"DiscoveryCog: sent game discovery embed for {announced_count} games in guild {guild_id}")

    except Exception as e:
        logger.warning(f"DiscoveryCog: failed to send embed for guild {guild_id}: {e}", exc_info=True)

    return announced_count


class DiscoveryCog(Cog):
    """
    Game Discovery for QuestLogFluxer.
    Mirrors WardenBot's discovery cog - same IGDB logic, same embed format.
    """

    def __init__(self, bot):
        super().__init__(bot)
        self._discovery_task: asyncio.Task | None = None
        self._boot_skipped = False

    @Cog.listener()
    async def on_ready(self):
        logger.info("DiscoveryCog ready - starting game discovery loop")
        if self._discovery_task is None or self._discovery_task.done():
            self._discovery_task = asyncio.ensure_future(self._game_discovery_loop())

    # ------------------------------------------------------------------
    # Scheduled discovery loop
    # ------------------------------------------------------------------

    async def _game_discovery_loop(self):
        """
        Main discovery loop. Runs every 15 minutes.
        Per-guild interval is controlled by game_check_interval_hours.
        First pass after boot is skipped (stamps timestamps to prevent
        re-announcing on every restart).
        """
        while True:
            try:
                await self._run_discovery_pass()
            except Exception as e:
                logger.error(f"DiscoveryCog: discovery loop error: {e}", exc_info=True)
            await asyncio.sleep(DISCOVERY_CHECK_INTERVAL)

    async def _run_discovery_pass(self):
        """One pass: iterate all enabled guilds and check if it's time to run."""
        if not igdb.is_configured():
            logger.debug("DiscoveryCog: IGDB not configured, skipping pass")
            return

        guilds = _get_all_discovery_guilds()
        if not guilds:
            return

        now = int(time.time())

        if not self._boot_skipped:
            # Stamp last_game_check_at to now for all enabled guilds so intervals
            # count from boot time, not some arbitrary past timestamp.
            for g in guilds:
                _update_last_check(g['guild_id'], now)
            self._boot_skipped = True
            logger.info(f"DiscoveryCog: first pass skipped, timestamps reset for {len(guilds)} guilds")
            return

        for guild_cfg in guilds:
            guild_id = guild_cfg['guild_id']
            channel_id = guild_cfg['game_discovery_channel_id']
            ping_role_id = guild_cfg.get('game_discovery_ping_role_id')
            interval_hours = guild_cfg.get('game_check_interval_hours') or 24
            last_check = guild_cfg.get('last_game_check_at') or 0
            interval_secs = interval_hours * 3600

            if (now - last_check) < interval_secs:
                hours_ago = (now - last_check) / 3600
                logger.debug(f"DiscoveryCog: skipping guild {guild_id} - checked {hours_ago:.1f}h ago (interval {interval_hours}h)")
                continue

            logger.info(f"DiscoveryCog: running discovery for guild {guild_id}")

            search_configs = _load_search_configs(guild_id)
            if not search_configs:
                logger.info(f"DiscoveryCog: no search configs for guild {guild_id}, skipping")
                _update_last_check(guild_id, now)
                continue

            try:
                count = await _run_discovery_for_guild(
                    self.bot, guild_id, channel_id, ping_role_id, search_configs
                )
                logger.info(f"DiscoveryCog: guild {guild_id} complete - {count} new games announced")
            except Exception as e:
                logger.error(f"DiscoveryCog: error running discovery for guild {guild_id}: {e}", exc_info=True)

            _update_last_check(guild_id, now)

    # ------------------------------------------------------------------
    # Public API: called by CoreCog action dispatcher
    # ------------------------------------------------------------------

    async def run_for_guild_now(self, guild_id: str) -> int:
        """Force-run discovery for one guild and reset its check timestamp. Returns new game count."""
        cfg = _load_guild_config(guild_id)
        if not cfg or not cfg.get('game_discovery_enabled'):
            raise ValueError("Game discovery not enabled for this guild")
        channel_id = cfg.get('game_discovery_channel_id')
        if not channel_id:
            raise ValueError("No announcement channel configured")
        search_configs = _load_search_configs(guild_id)
        if not search_configs:
            raise ValueError("No search configurations found")
        ping_role_id = cfg.get('game_discovery_ping_role_id')
        count = await _run_discovery_for_guild(self.bot, guild_id, channel_id, ping_role_id, search_configs)
        _update_last_check(guild_id, int(time.time()))
        return count

    # ------------------------------------------------------------------
    # !checkgames command - force-run for this guild (owner only)
    # ------------------------------------------------------------------

    @Cog.command(name="checkgames")
    async def checkgames(self, ctx):
        """Force-run game discovery for this guild right now. Owner only."""
        guild_id = str(ctx.guild.id) if hasattr(ctx, 'guild') and ctx.guild else None
        user_id = str(ctx.author.id) if hasattr(ctx, 'author') and ctx.author else None

        if not guild_id:
            await ctx.reply("This command can only be used inside a server.")
            return

        # Permission check - owner or admin only
        if not _is_owner_or_admin(self.bot, guild_id, user_id):
            await ctx.reply("Only the server owner can use this command.")
            return

        if not igdb.is_configured():
            await ctx.reply("IGDB is not configured. Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in the bot environment.")
            return

        cfg = _load_guild_config(guild_id)
        if not cfg or not cfg.get('game_discovery_enabled'):
            error_embed = fluxer.Embed(
                title="Game Discovery Not Enabled",
                description="Enable game discovery in the [QuestLog dashboard](https://casual-heroes.com/ql/dashboard/fluxer/{guild_id}/discovery/?tab=games) first.".replace('{guild_id}', guild_id),
                color=0xED4245,
            )
            await ctx.reply(embed=error_embed)
            return

        channel_id = cfg.get('game_discovery_channel_id')
        if not channel_id:
            error_embed = fluxer.Embed(
                title="No Announcement Channel",
                description="Set a game discovery announcement channel in the dashboard first.",
                color=0xED4245,
            )
            await ctx.reply(embed=error_embed)
            return

        search_configs = _load_search_configs(guild_id)
        if not search_configs:
            error_embed = fluxer.Embed(
                title="No Search Configurations",
                description="Create at least one game search config in the [QuestLog dashboard](https://casual-heroes.com/ql/dashboard/fluxer/{guild_id}/discovery/?tab=games).".replace('{guild_id}', guild_id),
                color=0xED4245,
            )
            await ctx.reply(embed=error_embed)
            return

        # Acknowledge the command
        working_embed = fluxer.Embed(
            title="Searching IGDB...",
            description=f"Running game discovery across {len(search_configs)} search configuration{'s' if len(search_configs) != 1 else ''}. This may take a moment.",
            color=0xFEE75C,
        )
        await ctx.reply(embed=working_embed)

        try:
            ping_role_id = cfg.get('game_discovery_ping_role_id')
            count = await _run_discovery_for_guild(
                self.bot, guild_id, channel_id, ping_role_id, search_configs
            )
            _update_last_check(guild_id, int(time.time()))

            if count > 0:
                result_embed = fluxer.Embed(
                    title="Discovery Complete",
                    description=f"Found and announced **{count}** new game{'s' if count != 1 else ''}! Check <#{channel_id}> for the summary.",
                    color=0x57F287,
                )
            else:
                result_embed = fluxer.Embed(
                    title="No New Games",
                    description="No new games matched your search configurations this time. Previously announced games are not re-announced.",
                    color=0x57F287,
                )
            result_embed.add_field(
                name="View Found Games",
                value=f"[Open Member Portal]({PORTAL_BASE_URL}/{guild_id}/games/)",
                inline=False,
            )
            await ctx.reply(embed=result_embed)

        except Exception as e:
            logger.error(f"DiscoveryCog: !checkgames error for guild {guild_id}: {e}", exc_info=True)
            err_embed = fluxer.Embed(
                title="Discovery Error",
                description=f"An error occurred during game discovery: {str(e)[:200]}",
                color=0xED4245,
            )
            await ctx.reply(embed=err_embed)
