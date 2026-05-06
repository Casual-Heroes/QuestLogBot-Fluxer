# cogs/permissions.py - Shared permission helpers for QuestLogFluxer
#
# Three tiers:
#   is_administrator(ctx)  - guild owner OR Administrator permission
#   is_moderator(ctx)      - above OR Manage Messages permission
#   is_bot_manager(ctx, cfg) - above OR has the admin_role_id set in gamebot_configs

import asyncio
from fluxer.enums import Permissions
from config import logger


async def _fetch_guild_context(ctx):
    """Fetch guild, member, and roles data in parallel. Returns (guild_data, member_data, roles_data)."""
    if not ctx.guild_id or not ctx._http:
        return None, None, None
    try:
        guild_data, member_data, roles_data = await asyncio.gather(
            ctx._http.get_guild(ctx.guild_id),
            ctx._http.get_guild_member(ctx.guild_id, ctx.author.id),
            ctx._http.get_guild_roles(ctx.guild_id),
        )
        return guild_data, member_data, roles_data
    except Exception as e:
        logger.warning(f'_fetch_guild_context failed: {e}')
        return None, None, None


def _compute_permissions(member_data, roles_data, guild_id) -> int:
    """Compute bitfield of all permissions the member has via their roles."""
    member_role_ids = {int(r) for r in member_data.get('roles', [])}
    guild_id_int = int(guild_id)
    computed = 0
    for role in (roles_data or []):
        role_id = int(role['id'])
        if role_id == guild_id_int or role_id in member_role_ids:
            computed |= int(role['permissions'])
    return computed


async def is_administrator(ctx) -> bool:
    """Guild owner or Administrator permission."""
    guild_data, member_data, roles_data = await _fetch_guild_context(ctx)
    if guild_data is None:
        return False
    if ctx.author.id == int(guild_data.get('owner_id', -1)):
        return True
    computed = _compute_permissions(member_data, roles_data, ctx.guild_id)
    return bool(computed & Permissions.ADMINISTRATOR)


async def is_moderator(ctx) -> bool:
    """Guild owner, Administrator, or Manage Messages permission."""
    guild_data, member_data, roles_data = await _fetch_guild_context(ctx)
    if guild_data is None:
        return False
    if ctx.author.id == int(guild_data.get('owner_id', -1)):
        return True
    computed = _compute_permissions(member_data, roles_data, ctx.guild_id)
    return bool(computed & (Permissions.ADMINISTRATOR | Permissions.MANAGE_MESSAGES))


async def is_bot_manager(ctx, cfg: dict = None) -> bool:
    """Guild owner, Administrator, Manage Messages, OR has the Bot Manager role configured in gamebot_configs."""
    guild_data, member_data, roles_data = await _fetch_guild_context(ctx)
    if guild_data is None:
        return False
    if ctx.author.id == int(guild_data.get('owner_id', -1)):
        return True
    computed = _compute_permissions(member_data, roles_data, ctx.guild_id)
    if computed & (Permissions.ADMINISTRATOR | Permissions.MANAGE_MESSAGES):
        return True
    # Check Bot Manager role from gamebot_configs
    if cfg and cfg.get('admin_role_id'):
        allowed = {r.strip() for r in str(cfg['admin_role_id']).split(',') if r.strip()}
        member_role_ids = {str(r) for r in member_data.get('roles', [])}
        if allowed & member_role_ids:
            return True
    return False
