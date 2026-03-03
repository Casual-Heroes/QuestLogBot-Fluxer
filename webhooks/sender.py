# webhooks/sender.py - QuestLog -> Fluxer webhook sender
#
# Sends activity from the QuestLog web platform into Fluxer channels.
# Uses Fluxer's standard webhook format (identical to Discord webhooks).
#
# Called by:
#   - QuestLog web platform (via HTTP POST to this bot's internal API, or direct DB triggers)
#   - Could also be called from crons / event-driven background tasks
#
# Webhook types:
#   - new_post: User posted on QuestLog
#   - new_member: New user joined QuestLog
#   - lfg_post: LFG posted on QuestLog
#   - giveaway_start: Giveaway started
#   - giveaway_winner: Giveaway winner announced
#
# TODO: hook this into QuestLog web platform events (views_social.py, views_pages.py)

import aiohttp
from config import logger, FLUXER_API_URL, get_bot_token


async def send_webhook(webhook_url: str, payload: dict) -> bool:
    """POST a webhook payload to a Fluxer webhook URL."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status in (200, 204):
                    return True
                text = await resp.text()
                logger.warning(f"Webhook returned {resp.status}: {text[:200]}")
                return False
    except Exception as e:
        logger.error(f"Webhook send failed: {e}", exc_info=True)
        return False


def build_new_post_payload(username: str, game: str, content: str, post_url: str) -> dict:
    """Build webhook payload for a new QuestLog post."""
    preview = content[:200] + "..." if len(content) > 200 else content
    return {
        "username": "QuestLog",
        "embeds": [
            {
                "title": f"{username} posted about {game}",
                "description": preview,
                "url": post_url,
                "color": 0x5865F2,
            }
        ],
    }


def build_new_member_payload(username: str, profile_url: str) -> dict:
    """Build webhook payload for a new QuestLog member."""
    return {
        "username": "QuestLog",
        "embeds": [
            {
                "title": "New member joined QuestLog!",
                "description": f"Welcome **{username}** to the community!",
                "url": profile_url,
                "color": 0x57F287,
            }
        ],
    }


def build_lfg_payload(username: str, game: str, description: str, post_id: int) -> dict:
    """Build webhook payload for a new LFG post."""
    lfg_url = "https://casual-heroes.com/ql/lfg/"
    desc_text = f"\n{description}" if description else ""
    return {
        "username": "QuestLog LFG",
        "embeds": [
            {
                "title": f"{username} is looking for group - {game}",
                "description": f"Post ID: `#{post_id}`{desc_text}\n\nJoin with `!lfgjoin {post_id}`",
                "url": lfg_url,
                "color": 0xFEE75C,
            }
        ],
    }


def build_giveaway_start_payload(title: str, description: str, giveaway_url: str) -> dict:
    """Build webhook payload for a giveaway starting."""
    return {
        "username": "QuestLog Giveaways",
        "embeds": [
            {
                "title": f"Giveaway: {title}",
                "description": description,
                "url": giveaway_url,
                "color": 0xEB459E,
                "footer": {"text": "Visit the link to enter!"},
            }
        ],
    }


def build_giveaway_winner_payload(title: str, winners: list[str], giveaway_url: str) -> dict:
    """Build webhook payload for giveaway winner announcement."""
    if len(winners) == 1:
        winner_text = f"**{winners[0]}** won!"
    else:
        winner_list = ", ".join(f"**{w}**" for w in winners)
        winner_text = f"Winners: {winner_list}"
    return {
        "username": "QuestLog Giveaways",
        "embeds": [
            {
                "title": f"Giveaway ended: {title}",
                "description": winner_text,
                "url": giveaway_url,
                "color": 0xEB459E,
            }
        ],
    }


# TODO: Integrate with QuestLog web platform
# - Register webhook URLs per-guild in DB (table: fluxer_webhook_configs)
# - Call send_webhook() from:
#     views_social.py after post creation
#     views_pages.py after LFG post creation
#     views_pages.py after giveaway draw
#     A cron or signal after new user registration
# - Or expose a lightweight internal HTTP API endpoint the web platform POSTs to
