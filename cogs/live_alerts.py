# cogs/live_alerts.py - Twitch & YouTube Live Stream Alerts
#
# Polls web_fluxer_streamer_subs every 60 seconds.
# When a subscribed streamer goes live, sends an embed to the configured channel.
# Deduplication: is_currently_live flag prevents re-notifying the same stream session.
#
# Twitch:  uses app (client-credentials) token - no user OAuth required.
# YouTube: uses Data API v3 with API key - checks for active live broadcasts.

import asyncio
import time
import requests

import fluxer
from fluxer import Cog
from sqlalchemy import text
from config import logger, db_session_scope

# ---------------------------------------------------------------------------
# Twitch app-token helpers (client credentials - no user OAuth needed)
# ---------------------------------------------------------------------------

_twitch_app_token: str = ''
_twitch_token_expires_at: int = 0


def _twitch_get_app_token(client_id: str, client_secret: str) -> str:
    """Fetch or refresh Twitch app access token via client credentials grant."""
    global _twitch_app_token, _twitch_token_expires_at
    now = int(time.time())
    if _twitch_app_token and now < _twitch_token_expires_at - 60:
        return _twitch_app_token
    resp = requests.post(
        'https://id.twitch.tv/oauth2/token',
        params={
            'client_id': client_id,
            'client_secret': client_secret,
            'grant_type': 'client_credentials',
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    _twitch_app_token = data['access_token']
    _twitch_token_expires_at = now + data.get('expires_in', 3600)
    logger.debug("LiveAlerts: Twitch app token refreshed")
    return _twitch_app_token


def _twitch_check_live(handle: str, client_id: str, client_secret: str) -> dict | None:
    """
    Return stream info dict if `handle` is currently live on Twitch, else None.
    dict keys: title, viewer_count, game_name, thumbnail_url, started_at
    """
    token = _twitch_get_app_token(client_id, client_secret)
    resp = requests.get(
        'https://api.twitch.tv/helix/streams',
        params={'user_login': handle},
        headers={'Client-ID': client_id, 'Authorization': f'Bearer {token}'},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json().get('data', [])
    if not data:
        return None
    s = data[0]
    if s.get('type') != 'live':
        return None
    return {
        'title': s.get('title', ''),
        'viewer_count': s.get('viewer_count', 0),
        'game_name': s.get('game_name', ''),
        'thumbnail_url': s.get('thumbnail_url', '').replace('{width}', '320').replace('{height}', '180'),
        'stream_url': f"https://twitch.tv/{handle}",
        'avatar_url': '',  # fetched separately if needed
    }


def _twitch_get_avatar(handle: str, client_id: str, client_secret: str) -> str:
    """Return the Twitch channel profile image URL (best-effort)."""
    try:
        token = _twitch_get_app_token(client_id, client_secret)
        resp = requests.get(
            'https://api.twitch.tv/helix/users',
            params={'login': handle},
            headers={'Client-ID': client_id, 'Authorization': f'Bearer {token}'},
            timeout=8,
        )
        resp.raise_for_status()
        users = resp.json().get('data', [])
        return users[0]['profile_image_url'] if users else ''
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# YouTube helpers (API key only - checks for active live broadcasts)
# ---------------------------------------------------------------------------

def _youtube_check_live(handle: str, api_key: str) -> dict | None:
    """
    Return stream info dict if the YouTube channel `handle` has an active live broadcast.
    `handle` can be a channel ID (UC...) or a @handle / plain username.
    Returns None if offline or not found.
    """
    # Resolve channel ID if needed
    channel_id = _youtube_resolve_channel(handle, api_key)
    if not channel_id:
        return None

    # Search for active live broadcasts on this channel
    resp = requests.get(
        'https://www.googleapis.com/youtube/v3/search',
        params={
            'part': 'snippet',
            'channelId': channel_id,
            'eventType': 'live',
            'type': 'video',
            'key': api_key,
        },
        timeout=10,
    )
    resp.raise_for_status()
    items = resp.json().get('items', [])
    if not items:
        return None
    item = items[0]
    snippet = item.get('snippet', {})
    video_id = item.get('id', {}).get('videoId', '')
    thumbnails = snippet.get('thumbnails', {})
    thumb = (thumbnails.get('medium') or thumbnails.get('default') or {}).get('url', '')
    return {
        'title': snippet.get('title', ''),
        'viewer_count': 0,  # requires videos.list liveBroadcasts part
        'game_name': '',
        'thumbnail_url': thumb,
        'stream_url': f"https://youtube.com/watch?v={video_id}" if video_id else f"https://youtube.com/channel/{channel_id}",
        'channel_id': channel_id,
        'avatar_url': '',
    }


def _youtube_resolve_channel(handle: str, api_key: str) -> str:
    """Resolve a YouTube handle/@handle/channel ID to a channel ID string."""
    # Already a channel ID
    if handle.startswith('UC') and len(handle) > 20:
        return handle
    # @handle - use forHandle param
    search_handle = handle.lstrip('@')
    try:
        resp = requests.get(
            'https://www.googleapis.com/youtube/v3/channels',
            params={'part': 'id', 'forHandle': search_handle, 'key': api_key},
            timeout=8,
        )
        resp.raise_for_status()
        items = resp.json().get('items', [])
        if items:
            return items[0]['id']
    except Exception:
        pass
    # Fall back to search
    try:
        resp = requests.get(
            'https://www.googleapis.com/youtube/v3/search',
            params={'part': 'snippet', 'q': search_handle, 'type': 'channel',
                    'maxResults': 1, 'key': api_key},
            timeout=8,
        )
        resp.raise_for_status()
        items = resp.json().get('items', [])
        if items:
            return items[0]['id']['channelId']
    except Exception:
        pass
    return ''


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class LiveAlertsCog(Cog):
    """Polls Twitch and YouTube subscriptions and sends live alerts."""

    def __init__(self, bot):
        super().__init__(bot)
        self._task = None

        # Load API credentials from env (set in Fluxer bot's run environment)
        import os
        self._twitch_client_id = os.getenv('TWITCH_CLIENT_ID', '')
        self._twitch_client_secret = os.getenv('TWITCH_CLIENT_SECRET', '')
        self._youtube_api_key = os.getenv('YOUTUBE_API_KEY', '')

        if not self._twitch_client_id or not self._twitch_client_secret:
            logger.warning("LiveAlerts: TWITCH_CLIENT_ID/SECRET not set - Twitch alerts disabled")
        if not self._youtube_api_key:
            logger.warning("LiveAlerts: YOUTUBE_API_KEY not set - YouTube alerts disabled")

    @Cog.listener()
    async def on_ready(self):
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._poll_loop())
        logger.info("LiveAlerts: poll loop started (60s interval)")

    async def _poll_loop(self):
        await asyncio.sleep(15)  # Short delay after startup before first poll
        while True:
            try:
                await self._check_all_subs()
            except Exception as e:
                logger.error(f"LiveAlerts: poll loop error: {e}", exc_info=True)
            await asyncio.sleep(60)

    async def _check_all_subs(self):
        """Load all active subs and check each one."""
        try:
            with db_session_scope() as db:
                rows = db.execute(text(
                    "SELECT id, guild_id, streamer_platform, streamer_handle, "
                    "streamer_display_name, notify_channel_id, custom_message, "
                    "is_currently_live, last_notified_at "
                    "FROM web_fluxer_streamer_subs WHERE is_active = 1"
                )).fetchall()
        except Exception as e:
            logger.error(f"LiveAlerts: DB read error: {e}")
            return

        for row in rows:
            try:
                await self._check_sub(row)
                await asyncio.sleep(1)  # Be polite to APIs
            except Exception as e:
                logger.warning(f"LiveAlerts: error checking sub {row[0]}: {e}")

    async def _check_sub(self, row):
        sub_id = row[0]
        guild_id = row[1]
        platform = row[2]
        handle = row[3]
        display_name = row[4] or handle
        notify_channel_id = row[5]
        custom_message = row[6]
        was_live = bool(row[7])
        last_notified_at = row[8] or 0

        # Poll the platform
        loop = asyncio.get_event_loop()
        stream_info = None

        if platform == 'twitch' and self._twitch_client_id:
            try:
                stream_info = await loop.run_in_executor(
                    None, _twitch_check_live, handle,
                    self._twitch_client_id, self._twitch_client_secret
                )
            except Exception as e:
                logger.warning(f"LiveAlerts: Twitch check failed for {handle}: {e}")
                return

        elif platform == 'youtube' and self._youtube_api_key:
            try:
                stream_info = await loop.run_in_executor(
                    None, _youtube_check_live, handle, self._youtube_api_key
                )
            except Exception as e:
                logger.warning(f"LiveAlerts: YouTube check failed for {handle}: {e}")
                return

        is_live_now = stream_info is not None

        if is_live_now and not was_live:
            # Just went live - send notification
            await self._send_alert(
                guild_id, notify_channel_id, platform, handle,
                display_name, stream_info, custom_message
            )
            now = int(time.time())
            with db_session_scope() as db:
                db.execute(text(
                    "UPDATE web_fluxer_streamer_subs "
                    "SET is_currently_live = 1, last_notified_at = :now, updated_at = :now "
                    "WHERE id = :id"
                ), {'now': now, 'id': sub_id})
                db.commit()
            logger.info(f"LiveAlerts: [{platform}] {handle} went live - notified guild {guild_id}")

        elif not is_live_now and was_live:
            # Stream ended - clear live flag
            now = int(time.time())
            with db_session_scope() as db:
                db.execute(text(
                    "UPDATE web_fluxer_streamer_subs "
                    "SET is_currently_live = 0, updated_at = :now WHERE id = :id"
                ), {'now': now, 'id': sub_id})
                db.commit()
            logger.debug(f"LiveAlerts: [{platform}] {handle} went offline in guild {guild_id}")

    async def _send_alert(self, guild_id, channel_id, platform, handle,
                          display_name, stream_info, custom_message):
        title = stream_info.get('title', 'Now Live!')
        stream_url = stream_info.get('stream_url', '')
        thumbnail_url = stream_info.get('thumbnail_url', '')
        game_name = stream_info.get('game_name', '')
        viewer_count = stream_info.get('viewer_count', 0)

        if platform == 'twitch':
            color = 0x9146FF  # Twitch purple
            platform_label = 'Twitch'
            platform_icon = 'https://cdn.casual-heroes.com/static/icons/twitch_icon.png'
        else:
            color = 0xFF0000  # YouTube red
            platform_label = 'YouTube'
            platform_icon = 'https://cdn.casual-heroes.com/static/icons/youtube_icon.png'

        embed = fluxer.Embed(
            title=title,
            url=stream_url,
            color=color,
        )
        embed.set_author(name=f"{display_name} is now live on {platform_label}!")
        if thumbnail_url:
            embed.set_image(url=thumbnail_url)
        if game_name:
            embed.add_field(name='Playing', value=game_name, inline=True)
        if viewer_count:
            embed.add_field(name='Viewers', value=str(viewer_count), inline=True)
        embed.add_field(name='Watch Now', value=stream_url, inline=False)

        content = None
        if custom_message:
            content = (
                custom_message
                .replace('{streamer}', display_name)
                .replace('{title}', title)
                .replace('{url}', stream_url)
            )

        try:
            await self.bot._http.send_message(
                str(channel_id),
                content=content,
                embed=embed,
            )
        except Exception as e:
            logger.warning(
                f"LiveAlerts: failed to send alert for {handle} "
                f"to channel {channel_id} in guild {guild_id}: {e}"
            )
