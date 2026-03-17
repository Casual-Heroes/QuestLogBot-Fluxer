# cogs/creators.py - Creator commands for QuestLogFluxer
#
# !raffle           - Link to this server's raffle dashboard page
# !cotw             - Embed showing current Creator of the Week + next rotation
# !cotm             - Embed showing current Creator of the Month + next rotation

import time
import logging
from datetime import datetime, timezone

import fluxer
from fluxer import Cog
from sqlalchemy import text

from config import logger, db_session_scope

DASHBOARD_BASE = "https://dashboard.casual-heroes.com/ql/dashboard/fluxer"
PROFILE_BASE = "https://casual-heroes.com/ql/u"

GOLD_COLOR   = 0xFEE75C
PURPLE_COLOR = 0xA855F7


def _next_monday_ts() -> int:
    """Unix timestamp of next Monday 00:00 UTC."""
    now = datetime.now(timezone.utc)
    days_until_monday = (7 - now.weekday()) % 7 or 7
    from datetime import timedelta
    next_monday = (now + timedelta(days=days_until_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(next_monday.timestamp())


def _next_first_ts() -> int:
    """Unix timestamp of the 1st of next month 00:00 UTC."""
    now = datetime.now(timezone.utc)
    if now.month == 12:
        year, month = now.year + 1, 1
    else:
        year, month = now.year, now.month + 1
    from datetime import timedelta
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    return int(first.timestamp())


class CreatorsCog(Cog):
    def __init__(self, bot):
        super().__init__(bot)

    # -----------------------------------------------------------------------
    # !raffle
    # -----------------------------------------------------------------------

    @Cog.command(name='raffle')
    async def cmd_raffle(self, ctx):
        """!raffle - Link to this server's raffles page on the QuestLog dashboard."""
        guild_id = str(ctx.guild.id)
        url = f"{DASHBOARD_BASE}/{guild_id}/raffles/"
        embed = fluxer.Embed(
            title="🎟️ Raffles",
            description=(
                f"Browse active raffles, enter to win, and manage past draws for **{ctx.guild.name}**.\n\n"
                f"[**Open Raffles Dashboard**]({url})"
            ),
            color=GOLD_COLOR,
        )
        embed.set_footer(text="QuestLog - casual-heroes.com")
        await ctx.send(embed=embed)

    # -----------------------------------------------------------------------
    # !cotw
    # -----------------------------------------------------------------------

    @Cog.command(name='cotw')
    async def cmd_cotw(self, ctx):
        """!cotw - Show the current Creator of the Week."""
        try:
            with db_session_scope() as db:
                row = db.execute(text(
                    "SELECT cp.display_name, cp.bio, cp.avatar_url, cp.twitch_url, "
                    "cp.youtube_url, cp.kick_url, cp.twitter_url, cp.cotw_last_featured, "
                    "wu.username "
                    "FROM web_creator_profiles cp "
                    "JOIN web_users wu ON wu.id = cp.user_id "
                    "WHERE cp.is_current_cotw = 1 "
                    "LIMIT 1"
                )).fetchone()
        except Exception as e:
            logger.error(f"CreatorsCog cotw DB error: {e}")
            await ctx.send("Could not fetch Creator of the Week right now.")
            return

        next_ts = _next_monday_ts()
        next_str = f"<t:{next_ts}:R>"

        if not row:
            embed = fluxer.Embed(
                title="⭐ Creator of the Week",
                description="No Creator of the Week has been selected yet.\nCheck back soon!",
                color=GOLD_COLOR,
            )
            embed.add_field(name="Next Rotation", value=next_str, inline=False)
            embed.set_footer(text="QuestLog Creators - casual-heroes.com/ql/creators/")
            await ctx.send(embed=embed)
            return

        name = row.display_name or row.username
        profile_url = f"{PROFILE_BASE}/{row.username}/"

        links = []
        if row.twitch_url: links.append(f"[Twitch]({row.twitch_url})")
        if row.youtube_url: links.append(f"[YouTube]({row.youtube_url})")
        if row.kick_url: links.append(f"[Kick]({row.kick_url})")
        if row.twitter_url: links.append(f"[Twitter]({row.twitter_url})")

        desc_parts = []
        if row.bio:
            bio = row.bio[:200] + ("..." if len(row.bio) > 200 else "")
            desc_parts.append(bio)
        if links:
            desc_parts.append("**Platforms:** " + " | ".join(links))
        desc_parts.append(f"\n[View Full Profile]({profile_url})")

        embed = fluxer.Embed(
            title=f"⭐ Creator of the Week - {name}",
            description="\n".join(desc_parts),
            color=GOLD_COLOR,
            url=profile_url,
        )
        if row.avatar_url:
            embed.set_thumbnail(url=row.avatar_url)
        if row.cotw_last_featured:
            embed.add_field(
                name="Featured Since",
                value=f"<t:{row.cotw_last_featured}:D>",
                inline=True,
            )
        embed.add_field(name="Next Rotation", value=next_str, inline=True)
        embed.set_footer(text="QuestLog Creators - casual-heroes.com/ql/creators/")
        await ctx.send(embed=embed)

    # -----------------------------------------------------------------------
    # !cotm
    # -----------------------------------------------------------------------

    @Cog.command(name='cotm')
    async def cmd_cotm(self, ctx):
        """!cotm - Show the current Creator of the Month."""
        try:
            with db_session_scope() as db:
                row = db.execute(text(
                    "SELECT cp.display_name, cp.bio, cp.avatar_url, cp.twitch_url, "
                    "cp.youtube_url, cp.kick_url, cp.twitter_url, cp.cotm_last_featured, "
                    "wu.username "
                    "FROM web_creator_profiles cp "
                    "JOIN web_users wu ON wu.id = cp.user_id "
                    "WHERE cp.is_current_cotm = 1 "
                    "LIMIT 1"
                )).fetchone()
        except Exception as e:
            logger.error(f"CreatorsCog cotm DB error: {e}")
            await ctx.send("Could not fetch Creator of the Month right now.")
            return

        next_ts = _next_first_ts()
        next_str = f"<t:{next_ts}:R>"

        if not row:
            embed = fluxer.Embed(
                title="👑 Creator of the Month",
                description="No Creator of the Month has been selected yet.\nCheck back soon!",
                color=PURPLE_COLOR,
            )
            embed.add_field(name="Next Rotation", value=next_str, inline=False)
            embed.set_footer(text="QuestLog Creators - casual-heroes.com/ql/creators/")
            await ctx.send(embed=embed)
            return

        name = row.display_name or row.username
        profile_url = f"{PROFILE_BASE}/{row.username}/"

        links = []
        if row.twitch_url: links.append(f"[Twitch]({row.twitch_url})")
        if row.youtube_url: links.append(f"[YouTube]({row.youtube_url})")
        if row.kick_url: links.append(f"[Kick]({row.kick_url})")
        if row.twitter_url: links.append(f"[Twitter]({row.twitter_url})")

        desc_parts = []
        if row.bio:
            bio = row.bio[:200] + ("..." if len(row.bio) > 200 else "")
            desc_parts.append(bio)
        if links:
            desc_parts.append("**Platforms:** " + " | ".join(links))
        desc_parts.append(f"\n[View Full Profile]({profile_url})")

        embed = fluxer.Embed(
            title=f"👑 Creator of the Month - {name}",
            description="\n".join(desc_parts),
            color=PURPLE_COLOR,
            url=profile_url,
        )
        if row.avatar_url:
            embed.set_thumbnail(url=row.avatar_url)
        if row.cotm_last_featured:
            embed.add_field(
                name="Featured Since",
                value=f"<t:{row.cotm_last_featured}:D>",
                inline=True,
            )
        embed.add_field(name="Next Rotation", value=next_str, inline=True)
        embed.set_footer(text="QuestLog Creators - casual-heroes.com/ql/creators/")
        await ctx.send(embed=embed)
