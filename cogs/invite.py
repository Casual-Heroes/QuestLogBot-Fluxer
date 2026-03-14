# cogs/invite.py - Early access invite code generator
#
# !invite - Get a personal early-access invite code for QuestLog (DM'd to you)
#
# Only works in guilds listed in EARLY_ACCESS_GUILD_IDS env var.
# Each Fluxer user gets one code (reuses existing unused code if they already have one).
# Requires EARLY_ACCESS_ENABLED=1 on the website to gate registration.

import os
import time
import secrets

# Per-user cooldown: one invite lookup per hour per user
_invite_cooldowns: dict[str, float] = {}
_INVITE_COOLDOWN = 3600.0  # 1 hour

import fluxer
from fluxer import Cog
from sqlalchemy import text
from config import logger, db_session_scope

# Guilds where !invite is allowed (comma-separated guild IDs in env)
_raw_guild_ids = os.getenv('EARLY_ACCESS_GUILD_IDS', '').strip()
EARLY_ACCESS_GUILD_IDS: set[str] = {g.strip() for g in _raw_guild_ids.split(',') if g.strip()}

_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # no O/0 or I/1


def _gen_code() -> str:
    return ''.join(secrets.choice(_ALPHABET) for _ in range(10))


class InviteCog(Cog):
    def __init__(self, bot):
        super().__init__(bot)

    @Cog.command()
    async def invite(self, ctx):
        """!invite - Get your personal QuestLog early-access invite code."""
        user_id = str(ctx.author.id)
        now = time.time()
        if now - _invite_cooldowns.get(user_id, 0) < _INVITE_COOLDOWN:
            await ctx.reply("You already requested an invite code recently. Check your DMs.")
            return
        _invite_cooldowns[user_id] = now
        guild_id = str(ctx.guild.id) if ctx.guild else None

        # Only work in authorized guilds
        if EARLY_ACCESS_GUILD_IDS and guild_id not in EARLY_ACCESS_GUILD_IDS:
            await ctx.reply("This command is only available in the official Casual Heroes community.")
            return

        user_id = str(ctx.author.id)
        notes = f'fluxer:{user_id}'

        with db_session_scope() as db:
            # Check if they already have an unused, unrevoked code
            existing = db.execute(text(
                "SELECT code FROM web_early_access_codes "
                "WHERE notes = :notes AND used_by_user_id IS NULL AND is_revoked = 0 "
                "LIMIT 1"
            ), {'notes': notes}).fetchone()

            if existing:
                code_str = existing[0]
                action = 'existing'
            else:
                # Generate a unique code
                code_str = None
                for _ in range(10):
                    candidate = _gen_code()
                    clash = db.execute(text(
                        "SELECT 1 FROM web_early_access_codes WHERE code = :code"
                    ), {'code': candidate}).fetchone()
                    if not clash:
                        code_str = candidate
                        break

                if not code_str:
                    await ctx.reply("Could not generate a code right now - please try again.")
                    return

                db.execute(text(
                    "INSERT INTO web_early_access_codes (code, platform, notes, created_at) "
                    "VALUES (:code, 'fluxer', :notes, :now)"
                ), {'code': code_str, 'notes': notes, 'now': int(time.time())})
                db.commit()
                action = 'new'

        logger.info(f"InviteCog: {action} code {code_str} sent to Fluxer user {user_id}")

        embed = fluxer.Embed(
            title="Your QuestLog Invite Code",
            description=(
                f"Here's your personal early-access invite code:\n\n"
                f"**`{code_str}`**\n\n"
                f"Head to [casual-heroes.com/ql/register/](https://casual-heroes.com/ql/register/) "
                f"and enter this code when creating your account.\n\n"
                f"*One-time use - keep it to yourself!*"
            ),
            color=0x6366F1,
            url="https://casual-heroes.com/ql/register/",
        )
        embed.set_footer(text="QuestLog Early Access | casual-heroes.com/ql/")

        try:
            dm = await ctx.author.create_dm()
            await dm.send(embed=embed)
            await ctx.reply("Check your DMs! I sent you your invite code.")
        except Exception as e:
            logger.warning(f"InviteCog: could not DM user {user_id}: {e}")
            # Fall back to replying in-channel (ephemeral if supported, else plain)
            await ctx.reply(
                f"Couldn't DM you - make sure your DMs are open. Here's your code: **`{code_str}`** - "
                f"register at https://casual-heroes.com/ql/register/",
            )


def setup(bot):
    bot.add_cog(InviteCog(bot))
