# cogs/moderation.py - Moderation commands
#
# !ban, !tempban, !kick, !timeout
# Uses fluxer.checks for permission checking (owner + ADMINISTRATOR bypass built in).
# Uses Fluxer-specific fields: ban_duration_seconds, timeout_reason
# All responses use embeds.
#
# TODO slash commands: /ban, /kick, /timeout, /tempban

import datetime
import fluxer
from fluxer import Cog
from fluxer.checks import has_permission
from fluxer.enums import Permissions
from config import logger

RED_COLOR = 0xED4245
ORANGE_COLOR = 0xFEE75C
GREEN_COLOR = 0x57F287


def _parse_mention(text: str) -> str | None:
    """Extract user ID from <@123> or plain ID."""
    text = text.strip()
    if text.startswith("<@") and text.endswith(">"):
        return text[2:-1].lstrip("!")
    if text.isdigit():
        return text
    return None


class ModerationCog(Cog):
    def __init__(self, bot):
        super().__init__(bot)

    @Cog.command()
    @has_permission(Permissions.BAN_MEMBERS)
    async def ban(self, ctx, *args):
        """!ban @user [reason] - Permanently ban a member."""
        if not args:
            embed = fluxer.Embed(
                title="Ban Member",
                description="**Usage:** `!ban @user [reason]`",
                color=RED_COLOR,
            )
            await ctx.reply(embed=embed)
            return

        user_id = _parse_mention(args[0])
        if not user_id:
            embed = fluxer.Embed(title="Invalid User", description="Please mention a valid user.", color=RED_COLOR)
            await ctx.reply(embed=embed)
            return

        reason = " ".join(args[1:]) or "No reason provided"
        guild_id = str(ctx.guild.id)

        try:
            await self.bot.http.ban_guild_member(guild_id, user_id, reason=reason, delete_message_days=1)
            embed = fluxer.Embed(
                title="Member Banned",
                description=f"<@{user_id}> has been permanently banned.",
                color=RED_COLOR,
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Banned by", value=ctx.author.username, inline=True)
            embed.set_footer(text="QuestLog Moderation")
            await ctx.reply(embed=embed)
            logger.info(f"Banned {user_id} from {guild_id} - {reason}")
        except Exception as e:
            logger.error(f"Ban failed: {e}", exc_info=True)
            embed = fluxer.Embed(title="Error", description="Failed to ban user. Check bot permissions.", color=RED_COLOR)
            await ctx.reply(embed=embed)

    @Cog.command()
    @has_permission(Permissions.BAN_MEMBERS)
    async def tempban(self, ctx, *args):
        """!tempban @user <hours> [reason] - Temporarily ban a member."""
        if len(args) < 2:
            embed = fluxer.Embed(
                title="Temp Ban Member",
                description="**Usage:** `!tempban @user <hours> [reason]`\n**Example:** `!tempban @user 24 spamming`",
                color=ORANGE_COLOR,
            )
            await ctx.reply(embed=embed)
            return

        user_id = _parse_mention(args[0])
        if not user_id:
            embed = fluxer.Embed(title="Invalid User", description="Please mention a valid user.", color=RED_COLOR)
            await ctx.reply(embed=embed)
            return

        try:
            hours = int(args[1])
        except ValueError:
            embed = fluxer.Embed(title="Invalid Duration", description="Hours must be a number.", color=RED_COLOR)
            await ctx.reply(embed=embed)
            return

        reason = " ".join(args[2:]) or "No reason provided"
        guild_id = str(ctx.guild.id)

        try:
            await self.bot.http.ban_guild_member(
                guild_id, user_id,
                reason=reason,
                delete_message_days=0,
                ban_duration_seconds=hours * 3600,
            )
            embed = fluxer.Embed(
                title="Member Temp Banned",
                description=f"<@{user_id}> has been temporarily banned.",
                color=ORANGE_COLOR,
            )
            embed.add_field(name="Duration", value=f"{hours} hour(s)", inline=True)
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Banned by", value=ctx.author.username, inline=True)
            embed.set_footer(text="QuestLog Moderation")
            await ctx.reply(embed=embed)
            logger.info(f"Temp-banned {user_id} from {guild_id} for {hours}h - {reason}")
        except Exception as e:
            logger.error(f"Tempban failed: {e}", exc_info=True)
            embed = fluxer.Embed(title="Error", description="Failed to temp-ban user. Check bot permissions.", color=RED_COLOR)
            await ctx.reply(embed=embed)

    @Cog.command()
    @has_permission(Permissions.KICK_MEMBERS)
    async def kick(self, ctx, *args):
        """!kick @user [reason] - Kick a member."""
        if not args:
            embed = fluxer.Embed(
                title="Kick Member",
                description="**Usage:** `!kick @user [reason]`",
                color=ORANGE_COLOR,
            )
            await ctx.reply(embed=embed)
            return

        user_id = _parse_mention(args[0])
        if not user_id:
            embed = fluxer.Embed(title="Invalid User", description="Please mention a valid user.", color=RED_COLOR)
            await ctx.reply(embed=embed)
            return

        reason = " ".join(args[1:]) or "No reason provided"
        guild_id = str(ctx.guild.id)

        try:
            await self.bot.http.kick_guild_member(guild_id, user_id, reason=reason)
            embed = fluxer.Embed(
                title="Member Kicked",
                description=f"<@{user_id}> has been kicked from the server.",
                color=ORANGE_COLOR,
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Kicked by", value=ctx.author.username, inline=True)
            embed.set_footer(text="QuestLog Moderation")
            await ctx.reply(embed=embed)
            logger.info(f"Kicked {user_id} from {guild_id} - {reason}")
        except Exception as e:
            logger.error(f"Kick failed: {e}", exc_info=True)
            embed = fluxer.Embed(title="Error", description="Failed to kick user. Check bot permissions.", color=RED_COLOR)
            await ctx.reply(embed=embed)

    @Cog.command()
    @has_permission(Permissions.MODERATE_MEMBERS)
    async def timeout(self, ctx, *args):
        """!timeout @user <minutes> [reason] - Timeout a member."""
        if len(args) < 2:
            embed = fluxer.Embed(
                title="Timeout Member",
                description="**Usage:** `!timeout @user <minutes> [reason]`\n**Example:** `!timeout @user 10 spam`",
                color=ORANGE_COLOR,
            )
            await ctx.reply(embed=embed)
            return

        user_id = _parse_mention(args[0])
        if not user_id:
            embed = fluxer.Embed(title="Invalid User", description="Please mention a valid user.", color=RED_COLOR)
            await ctx.reply(embed=embed)
            return

        try:
            minutes = int(args[1])
        except ValueError:
            embed = fluxer.Embed(title="Invalid Duration", description="Minutes must be a number.", color=RED_COLOR)
            await ctx.reply(embed=embed)
            return

        reason = " ".join(args[2:]) or "No reason provided"
        guild_id = str(ctx.guild.id)
        until = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        try:
            await self.bot.http.edit_guild_member(
                guild_id, user_id,
                communication_disabled_until=until,
                reason=reason,
            )
            embed = fluxer.Embed(
                title="Member Timed Out",
                description=f"<@{user_id}> has been timed out.",
                color=ORANGE_COLOR,
            )
            embed.add_field(name="Duration", value=f"{minutes} minute(s)", inline=True)
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Timed out by", value=ctx.author.username, inline=True)
            embed.set_footer(text="QuestLog Moderation")
            await ctx.reply(embed=embed)
        except Exception as e:
            logger.error(f"Timeout failed: {e}", exc_info=True)
            embed = fluxer.Embed(title="Error", description="Failed to timeout user. Check bot permissions.", color=RED_COLOR)
            await ctx.reply(embed=embed)

    # TODO: /ban, /kick, /timeout, /tempban slash commands
    # TODO: audit log integration
