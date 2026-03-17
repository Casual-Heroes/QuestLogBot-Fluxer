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
# YouTube helpers (no API key required - scrapes /live page)
# ---------------------------------------------------------------------------

_YT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}


def _youtube_resolve_channel_url(handle: str) -> str:
    """
    Convert a handle/channel-ID to the canonical /channel/UC... URL path.
    Returns empty string if unresolvable.
    No API key required.
    """
    import re
    clean = handle.lstrip('@')
    # Already a channel ID
    if clean.startswith('UC') and len(clean) > 20:
        return f'/channel/{clean}'
    # It's a @handle - scrape the handle page to get the channel ID
    try:
        resp = requests.get(
            f'https://www.youtube.com/@{clean}',
            headers=_YT_HEADERS,
            timeout=12,
        )
        if resp.status_code != 200:
            return ''
        # Extract channel ID from page source
        m = re.search(r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"', resp.text)
        if m:
            return f'/channel/{m.group(1)}'
        # Fallback: look for canonical link
        m2 = re.search(r'channel/(UC[A-Za-z0-9_-]{22})', resp.text)
        if m2:
            return f'/channel/{m2.group(1)}'
    except Exception:
        pass
    return ''


def _youtube_check_live(handle: str, api_key: str) -> dict | None:
    """
    Check if a YouTube channel is currently live by fetching its /live page.
    No API key or billing required - uses public page scraping.
    Returns stream info dict if live, None otherwise.
    """
    import re, json as _json

    channel_path = _youtube_resolve_channel_url(handle)
    if not channel_path:
        logger.warning(f"LiveAlerts: YouTube could not resolve channel for handle '{handle}'")
        return None

    live_url = f'https://www.youtube.com{channel_path}/live'
    try:
        resp = requests.get(live_url, headers=_YT_HEADERS, timeout=12)
        if resp.status_code != 200:
            logger.debug(f"LiveAlerts: YouTube /live page returned {resp.status_code} for {handle}")
            return None
        html = resp.text
    except Exception as e:
        logger.warning(f"LiveAlerts: YouTube /live fetch failed for {handle}: {e}")
        return None

    # Extract ytInitialData JSON blob
    m = re.search(r'var ytInitialData\s*=\s*(\{.*?\});\s*</script>', html, re.DOTALL)
    if not m:
        # Channel is not live (YouTube redirects or shows "no live stream" page)
        return None

    try:
        yt_data = _json.loads(m.group(1))
    except Exception:
        return None

    # Walk the data to find videoDetails with isLive or isLiveContent
    raw = _json.dumps(yt_data)

    # Quick check: if "isLive":true not present anywhere, not live
    if '"isLive":true' not in raw and '"isLiveContent":true' not in raw:
        return None

    # Extract video ID from the page
    video_id = ''
    vid_m = re.search(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"', raw)
    if vid_m:
        video_id = vid_m.group(1)

    # Extract title
    title = ''
    title_m = re.search(r'"title"\s*:\s*\{"runs":\[\{"text"\s*:\s*"([^"]+)"', raw)
    if title_m:
        title = title_m.group(1)
    if not title:
        title_m2 = re.search(r'"title"\s*:\s*"([^"]+)"', raw)
        if title_m2:
            title = title_m2.group(1)

    # Extract viewer count
    viewer_count = 0
    views_m = re.search(r'"concurrentViewers"\s*:\s*"(\d+)"', raw)
    if views_m:
        viewer_count = int(views_m.group(1))

    # Thumbnail from video ID
    thumbnail_url = f'https://img.youtube.com/vi/{video_id}/maxresdefault.jpg' if video_id else ''
    stream_url = f'https://youtube.com/watch?v={video_id}' if video_id else f'https://youtube.com{channel_path}/live'

    logger.info(f"LiveAlerts: YouTube {handle} is LIVE - '{title}' ({viewer_count} viewers)")
    return {
        'title': title or 'Live Stream',
        'viewer_count': viewer_count,
        'game_name': '',
        'thumbnail_url': thumbnail_url,
        'stream_url': stream_url,
        'channel_id': channel_path.replace('/channel/', ''),
        'avatar_url': '',
    }


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
            logger.info("LiveAlerts: YOUTUBE_API_KEY not set - YouTube live detection uses page scraping (no API key needed)")

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

        elif platform == 'youtube':
            try:
                stream_info = await loop.run_in_executor(
                    None, _youtube_check_live, handle, ''
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
