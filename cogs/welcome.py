# cogs/welcome.py - Welcome & Goodbye Messages
#
# Reads config from web_fluxer_welcome_config.
# on_member_join:   channel message (text or embed), DM, auto-role, role restore
# on_member_remove: save roles for persistence, goodbye message
#
# Variables supported in message templates:
#   {user}             - mention (<@user_id>)
#   {username}         - display name
#   {server}           - guild name
#   {member_count}     - approximate member count (from web_fluxer_members)
#   {member_count_ord} - ordinal form (1st, 2nd, etc.)
#
# Role Persistence:
#   When role_persistence_enabled=1 in web_fluxer_guild_settings, roles are
#   saved to web_fluxer_members.saved_roles on member leave and restored on rejoin.
#   Roles with dangerous permissions are never saved (admin, ban, kick, etc.)

import time
import json

import fluxer
from fluxer import Cog
from fluxer.models.user import User
from sqlalchemy import text
from config import logger, db_session_scope


def _ordinal(n: int) -> str:
    """Convert integer to ordinal string: 1 -> '1st', etc."""
    if 11 <= (n % 100) <= 13:
        suffix = 'th'
    else:
        suffix = ['th', 'st', 'nd', 'rd', 'th'][min(n % 10, 4)]
    return f"{n}{suffix}"


def _parse_color(color_str, default: int = 0x5865F2) -> int:
    """Parse '#RRGGBB' hex string to int. Returns default on failure."""
    if not color_str:
        return default
    try:
        return int(str(color_str).lstrip('#'), 16)
    except (ValueError, AttributeError):
        return default


def _format(template: str, *, user_mention: str, username: str,
            server: str, member_count: int) -> str:
    """Substitute template variables."""
    return (
        template
        .replace('{user}', user_mention)
        .replace('{username}', username)
        .replace('{server}', server)
        .replace('{member_count}', str(member_count))
        .replace('{member_count_ord}', _ordinal(member_count))
    )


def _load_config(guild_id: str) -> dict | None:
    """Return welcome config dict for guild, or None if disabled/missing."""
    try:
        with db_session_scope() as db:
            row = db.execute(
                text(
                    "SELECT enabled, welcome_channel_id, welcome_message, "
                    "welcome_embed_enabled, welcome_embed_title, welcome_embed_color, "
                    "welcome_embed_footer, welcome_embed_thumbnail, "
                    "dm_enabled, dm_message, "
                    "goodbye_enabled, goodbye_channel_id, goodbye_message, auto_role_id "
                    "FROM web_fluxer_welcome_config WHERE guild_id = :g"
                ),
                {'g': guild_id},
            ).fetchone()
            if not row or not row.enabled:
                return None
            return dict(row._mapping)
    except Exception as e:
        logger.error(f"WelcomeCog: config load failed for guild {guild_id}: {e}")
        return None


def _guild_name(guild_id: str) -> str:
    """Best-effort guild name from channel cache."""
    try:
        with db_session_scope() as db:
            row = db.execute(
                text("SELECT guild_name FROM web_fluxer_guild_channels "
                     "WHERE guild_id = :g AND guild_name IS NOT NULL AND guild_name != '' "
                     "LIMIT 1"),
                {'g': guild_id},
            ).fetchone()
            return row[0] if row else ''
    except Exception:
        return ''


def _member_count(guild_id: str) -> int:
    """Approximate member count from web_fluxer_members (excludes members who left)."""
    try:
        with db_session_scope() as db:
            row = db.execute(
                text("SELECT COUNT(*) FROM web_fluxer_members "
                     "WHERE guild_id = :g AND left_at IS NULL"),
                {'g': guild_id},
            ).fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def _role_persistence_enabled(guild_id: str) -> bool:
    """Return True if role_persistence_enabled is set for this guild."""
    try:
        with db_session_scope() as db:
            row = db.execute(
                text("SELECT role_persistence_enabled FROM web_fluxer_guild_settings "
                     "WHERE guild_id = :g LIMIT 1"),
                {'g': guild_id},
            ).fetchone()
            return bool(row[0]) if row else False
    except Exception:
        return False


def _get_saved_roles(guild_id: str, user_id: str) -> list:
    """Return saved role IDs for a member, then clear them."""
    try:
        with db_session_scope() as db:
            row = db.execute(
                text("SELECT saved_roles FROM web_fluxer_members "
                     "WHERE guild_id = :g AND user_id = :u LIMIT 1"),
                {'g': guild_id, 'u': user_id},
            ).fetchone()
            if not row or not row[0]:
                return []
            try:
                roles = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                return []
            # Clear saved roles now that we're consuming them
            db.execute(
                text("UPDATE web_fluxer_members SET saved_roles = NULL "
                     "WHERE guild_id = :g AND user_id = :u"),
                {'g': guild_id, 'u': user_id},
            )
            db.commit()
            return roles if isinstance(roles, list) else []
    except Exception as e:
        logger.error(f"WelcomeCog: _get_saved_roles error: {e}")
        return []


def _save_roles(guild_id: str, user_id: str, role_ids: list) -> None:
    """Save role IDs for a member on leave."""
    try:
        with db_session_scope() as db:
            db.execute(
                text("UPDATE web_fluxer_members SET saved_roles = :roles, left_at = :now "
                     "WHERE guild_id = :g AND user_id = :u"),
                {'roles': json.dumps(role_ids), 'now': int(time.time()),
                 'g': guild_id, 'u': user_id},
            )
            db.commit()
    except Exception as e:
        logger.error(f"WelcomeCog: _save_roles error: {e}")




class WelcomeCog(Cog):
    """Welcome and goodbye messages for Fluxer guilds."""

    def __init__(self, bot):
        super().__init__(bot)

    # -------------------------------------------------------------------------
    # on_member_join
    # -------------------------------------------------------------------------

    @Cog.listener()
    async def on_member_join(self, data):
        try:
            guild_id = str(data.get('guild_id') or '')
            user_data = data.get('user') or {}
            user_id = str(user_data.get('id') or '')
            if not guild_id or not user_id:
                return
            if user_data.get('bot'):
                return

            username = user_data.get('global_name') or user_data.get('username') or 'Unknown'
            avatar_hash = user_data.get('avatar')

            cfg = _load_config(guild_id)
            if not cfg:
                return

            count = _member_count(guild_id)
            server = _guild_name(guild_id)
            user_mention = f"<@{user_id}>"

            # --- Channel welcome ---
            if cfg.get('welcome_channel_id') and cfg.get('welcome_message'):
                channel_id = cfg['welcome_channel_id']
                body = _format(
                    cfg['welcome_message'],
                    user_mention=user_mention, username=username,
                    server=server, member_count=count,
                )
                try:
                    if cfg.get('welcome_embed_enabled'):
                        color = _parse_color(cfg.get('welcome_embed_color'))
                        embed = fluxer.Embed(description=body, color=color)
                        if cfg.get('welcome_embed_title'):
                            embed.title = _format(
                                cfg['welcome_embed_title'],
                                user_mention=user_mention, username=username,
                                server=server, member_count=count,
                            )
                        if cfg.get('welcome_embed_thumbnail') and avatar_hash:
                            ext = 'gif' if avatar_hash.startswith('a_') else 'png'
                            embed.set_thumbnail(
                                url=f"https://fluxerusercontent.com/avatars/{user_id}/{avatar_hash}.{ext}"
                            )
                        if cfg.get('welcome_embed_footer'):
                            embed.set_footer(text=_format(
                                cfg['welcome_embed_footer'],
                                user_mention=user_mention, username=username,
                                server=server, member_count=count,
                            ))
                        await self.bot._http.send_message(channel_id, embed=embed)
                    else:
                        await self.bot._http.send_message(channel_id, content=body)
                except Exception as e:
                    logger.warning(f"WelcomeCog: channel welcome failed in {guild_id}: {e}")

            # --- DM welcome ---
            if cfg.get('dm_enabled') and cfg.get('dm_message'):
                try:
                    dm_body = _format(
                        cfg['dm_message'],
                        user_mention=user_mention, username=username,
                        server=server, member_count=count,
                    )
                    dm_embed = fluxer.Embed(description=dm_body, color=0x5865F2)
                    if server:
                        dm_embed.set_author(name=server)
                    user_obj = User(id=int(user_id), username=username, _http=self.bot._http)
                    await user_obj.send(embed=dm_embed)
                except Exception as e:
                    logger.debug(f"WelcomeCog: DM to {user_id} failed (DMs may be closed): {e}")

            # --- Auto-role ---
            if cfg.get('auto_role_id'):
                try:
                    await self.bot._http.add_guild_member_role(
                        guild_id, user_id, cfg['auto_role_id']
                    )
                    logger.debug(
                        f"WelcomeCog: assigned auto-role {cfg['auto_role_id']} "
                        f"to {user_id} in {guild_id}"
                    )
                except Exception as e:
                    logger.warning(f"WelcomeCog: auto-role failed in {guild_id}: {e}")

            # --- Role persistence: restore saved roles for returning members ---
            if _role_persistence_enabled(guild_id):
                saved_role_ids = _get_saved_roles(guild_id, user_id)
                if saved_role_ids:
                    restored = 0
                    for role_id in saved_role_ids:
                        try:
                            await self.bot._http.add_guild_member_role(
                                guild_id, user_id, str(role_id)
                            )
                            restored += 1
                        except Exception as e:
                            logger.warning(
                                f"WelcomeCog: could not restore role {role_id} "
                                f"for {user_id} in {guild_id}: {e}"
                            )
                    if restored:
                        logger.info(
                            f"WelcomeCog: [ROLE PERSIST] restored {restored} roles "
                            f"for {user_id} rejoining {guild_id}"
                        )

            logger.info(f"WelcomeCog: welcomed {username} ({user_id}) in guild {guild_id}")

        except Exception as e:
            logger.error(f"WelcomeCog: on_member_join error: {e}", exc_info=True)

    # -------------------------------------------------------------------------
    # on_member_remove
    # -------------------------------------------------------------------------

    @Cog.listener()
    async def on_member_remove(self, data):
        try:
            guild_id = str(data.get('guild_id') or '')
            user_data = data.get('user') or {}
            user_id = str(user_data.get('id') or '')
            if not guild_id or not user_id:
                return
            if user_data.get('bot'):
                return

            username = user_data.get('global_name') or user_data.get('username') or 'Unknown'

            # --- Role persistence: save roles before member record is gone ---
            if _role_persistence_enabled(guild_id):
                # Fluxer sends member roles in the GUILD_MEMBER_REMOVE payload
                member_roles = data.get('roles') or []
                if member_roles:
                    # Get is_managed flags for all roles in one query
                    try:
                        with db_session_scope() as db:
                            rows = db.execute(
                                text("SELECT role_id, is_managed FROM web_fluxer_guild_roles "
                                     "WHERE guild_id = :g"),
                                {'g': guild_id},
                            ).fetchall()
                        managed_ids = {str(r[0]) for r in rows if r[1]}
                    except Exception:
                        managed_ids = set()

                    # Skip managed (bot-assigned) roles - they can't be re-assigned manually
                    safe_role_ids = [
                        str(rid) for rid in member_roles
                        if str(rid) not in managed_ids
                    ]

                    if safe_role_ids:
                        _save_roles(guild_id, user_id, safe_role_ids)
                        logger.info(
                            f"WelcomeCog: [ROLE PERSIST] saved {len(safe_role_ids)} roles "
                            f"for {user_id} leaving {guild_id}"
                        )

            cfg = _load_config(guild_id)
            if not cfg:
                return
            if not cfg.get('goodbye_enabled') or not cfg.get('goodbye_channel_id'):
                return
            if not cfg.get('goodbye_message'):
                return

            count = _member_count(guild_id)
            server = _guild_name(guild_id)

            # For goodbye we can't mention (user left), use bold name instead
            goodbye_text = _format(
                cfg['goodbye_message'],
                user_mention=f"**{username}**",
                username=username,
                server=server,
                member_count=count,
            )
            goodbye_embed = fluxer.Embed(description=goodbye_text, color=0x747F8D)
            try:
                await self.bot._http.send_message(
                    cfg['goodbye_channel_id'], embed=goodbye_embed
                )
            except Exception as e:
                logger.warning(f"WelcomeCog: goodbye failed in {guild_id}: {e}")

        except Exception as e:
            logger.error(f"WelcomeCog: on_member_remove error: {e}", exc_info=True)


def setup(bot):
    bot.add_cog(WelcomeCog(bot))
