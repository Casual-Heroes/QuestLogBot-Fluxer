# cogs/rss.py - RSS Feed Monitor for QuestLogFluxer
#
# Mirrors WardenBot's rss_feeds.py design for Fluxer guilds:
# - Polls web_fluxer_rss_feeds every 5 minutes (respects per-feed poll_interval_minutes)
# - Posts new articles to the configured Fluxer channel
# - Saves articles to web_fluxer_rss_articles for the member portal viewer
# - Handles rss_force_send guild actions from the dashboard
# - !checkrss - force-run all feeds for this guild (owner/admin only)

import asyncio
import html as _html_module
import json
import re
import time as time_lib
from typing import Optional, Tuple, List, Any

import fluxer
from fluxer import Cog
from sqlalchemy import text

from config import logger, db_session_scope

RSS_POLL_INTERVAL = 5 * 60       # Check every 5 minutes; per-feed interval enforced by last_checked_at
RSS_FETCH_TIMEOUT = 30
RSS_MAX_SIZE = 5 * 1024 * 1024   # 5 MB
RSS_MAX_REDIRECTS = 5
PORTAL_BASE_URL = "https://casual-heroes.com/ql/fluxer"

ALLOWED_LINK_SCHEMES = {'http', 'https'}

try:
    import requests as _requests
except ImportError:
    _requests = None

try:
    import feedparser as _feedparser
except ImportError:
    _feedparser = None


# ---------------------------------------------------------------------------
# Helpers (module-level, mirrored from WardenBot's rss_feeds.py)
# ---------------------------------------------------------------------------

def _sanitize_link(url: str) -> str:
    """Allow only http/https links."""
    if not url:
        return ''
    url = url.strip()
    from urllib.parse import urlparse
    try:
        scheme = urlparse(url).scheme.lower()
    except Exception:
        return ''
    return url if scheme in ALLOWED_LINK_SCHEMES else ''


def _strip_html(text: str) -> str:
    """Remove HTML tags and unescape entities."""
    if not text:
        return ''
    text = re.sub(r'<[^>]+>', '', text)
    return _html_module.unescape(text).strip()


def _truncate(s: Optional[str], limit: int) -> Optional[str]:
    if not s:
        return s
    return s[:limit] if len(s) <= limit else s[:limit - 3] + '...'


def _secure_fetch_sync(url: str) -> Tuple[Optional[Any], Optional[str]]:
    """Synchronous SSRF-safe RSS fetch. Returns (parsed, error)."""
    from urllib.parse import urlparse
    import ipaddress
    import socket

    if not url:
        return None, 'URL is required'
    url = url.strip()
    try:
        parsed_url = urlparse(url)
    except Exception:
        return None, 'Invalid URL'
    if parsed_url.scheme not in ('http', 'https'):
        return None, 'URL must be http or https'
    hostname = parsed_url.hostname
    if not hostname:
        return None, 'No hostname'

    blocked_hosts = {'localhost', '127.0.0.1', '::1', '0.0.0.0'}
    if hostname.lower() in blocked_hosts:
        return None, 'Localhost not allowed'

    blocked_suffixes = ['.local', '.internal', '.private', '.corp', '.lan', '.intranet', '.localdomain']
    for suf in blocked_suffixes:
        if hostname.lower().endswith(suf):
            return None, f'Internal domains not allowed'

    try:
        for _fam, _t, _p, _cn, sockaddr in socket.getaddrinfo(hostname, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return None, f'Private/internal IP not allowed: {sockaddr[0]}'
    except socket.gaierror:
        pass  # DNS fail - let fetch handle it

    if _requests is None:
        return None, 'requests library not available'
    if _feedparser is None:
        return None, 'feedparser library not available'

    try:
        current_url = url
        for _ in range(RSS_MAX_REDIRECTS + 1):
            resp = _requests.get(
                current_url,
                timeout=RSS_FETCH_TIMEOUT,
                stream=True,
                allow_redirects=False,
                headers={'User-Agent': 'QuestLog RSS Bot/1.0 (+https://casual-heroes.com/)'},
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get('Location', '')
                if not loc:
                    return None, 'Redirect with no Location header'
                # Re-validate redirect target - prevents SSRF via open redirects
                _redir, _err = _fetch_rss_feed(loc)
                # We only need the SSRF check, not a full fetch - validate URL structure
                try:
                    redir_parsed = urlparse(loc)
                    if redir_parsed.scheme not in ('http', 'https'):
                        return None, 'Redirect to non-HTTP URL blocked'
                    redir_host = redir_parsed.hostname or ''
                    if redir_host.lower() in blocked_hosts:
                        return None, 'Redirect to localhost blocked'
                    for suf in blocked_suffixes:
                        if redir_host.lower().endswith(suf):
                            return None, 'Redirect to internal domain blocked'
                    for _fam, _t, _p, _cn, sockaddr in socket.getaddrinfo(redir_host, None):
                        ip = ipaddress.ip_address(sockaddr[0])
                        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                            return None, f'Redirect to private IP blocked: {sockaddr[0]}'
                except socket.gaierror:
                    pass
                current_url = loc
                continue

            content = b''
            for chunk in resp.iter_content(chunk_size=65536):
                content += chunk
                if len(content) > RSS_MAX_SIZE:
                    return None, 'Feed too large (>5MB)'
            parsed = _feedparser.parse(content)
            return parsed, None

        return None, 'Too many redirects'
    except Exception as exc:
        return None, str(exc)


def _get_entry_guid(entry: Any) -> str:
    """Get a stable unique ID for an RSS entry."""
    return str(entry.get('id') or entry.get('guid') or entry.get('link') or entry.get('title') or '')


def _parse_published_time(entry: Any) -> Optional[int]:
    """Parse the published timestamp of an entry."""
    import email.utils
    pub = entry.get('published_parsed') or entry.get('updated_parsed')
    if pub:
        import calendar
        try:
            return int(calendar.timegm(pub))
        except Exception:
            pass
    raw = entry.get('published') or entry.get('updated')
    if raw:
        try:
            return int(email.utils.parsedate_to_datetime(raw).timestamp())
        except Exception:
            pass
    return None


def _extract_thumbnail(entry: Any) -> Optional[str]:
    """Extract a thumbnail URL from an RSS entry."""
    # media:thumbnail
    media_content = entry.get('media_content', [])
    if media_content:
        for mc in media_content:
            url = mc.get('url', '')
            if url and mc.get('medium', '').startswith('image'):
                return url
            if url:
                return url

    # enclosures
    enclosures = entry.get('enclosures', [])
    for enc in enclosures:
        enc_type = enc.get('type', '')
        if enc_type.startswith('image/'):
            return enc.get('href', enc.get('url', ''))

    # og:image from content
    summary = entry.get('summary', '') or ''
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary)
    if m:
        return m.group(1)

    return None


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class RssCog(Cog):
    """RSS feed polling and posting for Fluxer guilds."""

    def __init__(self, bot):
        super().__init__(bot)
        self._rss_poll_task = None

    @Cog.listener()
    async def on_ready(self):
        logger.info("RssCog ready - starting RSS poll loop")
        if self._rss_poll_task is None or self._rss_poll_task.done():
            self._rss_poll_task = asyncio.ensure_future(self._rss_poll_loop())

    # -------------------------------------------------------------------------
    # Background loop
    # -------------------------------------------------------------------------

    async def _rss_poll_loop(self):
        """Poll all active Fluxer RSS feeds every 5 minutes."""
        await asyncio.sleep(30)  # Startup delay
        while True:
            try:
                await self._poll_all_feeds()
            except Exception as exc:
                logger.error(f"RssCog: poll loop error: {exc}")
            await asyncio.sleep(RSS_POLL_INTERVAL)

    async def _poll_all_feeds(self):
        """Load all active feeds due for polling and process them."""
        now = int(time_lib.time())
        try:
            with db_session_scope() as db:
                rows = db.execute(
                    text(
                        "SELECT id, guild_id, url, label, channel_id, ping_role_id, "
                        "poll_interval_minutes, max_age_days, category_filter_mode, category_filters, "
                        "embed_config, last_checked_at, last_entry_id, consecutive_failures "
                        "FROM web_fluxer_rss_feeds WHERE is_active = 1 AND enabled = 1"
                    )
                ).fetchall()
        except Exception as exc:
            logger.error(f"RssCog: failed to load feeds: {exc}")
            return

        for row in rows:
            feed = dict(row._mapping)
            interval_seconds = (feed.get('poll_interval_minutes') or 15) * 60
            last = feed.get('last_checked_at') or 0
            if (now - last) < interval_seconds:
                continue
            try:
                await self._process_feed(feed)
            except Exception as exc:
                logger.error(f"RssCog: error processing feed {feed['id']}: {exc}")
            await asyncio.sleep(1)  # Rate-limit between feeds

    # -------------------------------------------------------------------------
    # Force-send (called from action dispatch)
    # -------------------------------------------------------------------------

    async def force_send_feed(self, guild_id: str, feed_id: int) -> Tuple[bool, str]:
        """Force-send the latest article from a specific feed now."""
        try:
            with db_session_scope() as db:
                row = db.execute(
                    text(
                        "SELECT id, guild_id, url, label, channel_id, ping_role_id, "
                        "poll_interval_minutes, max_age_days, category_filter_mode, category_filters, "
                        "embed_config, last_checked_at, last_entry_id, consecutive_failures "
                        "FROM web_fluxer_rss_feeds WHERE id = :fid AND guild_id = :gid AND is_active = 1"
                    ),
                    {'fid': feed_id, 'gid': guild_id},
                ).fetchone()
                if not row:
                    return False, 'Feed not found'
                feed = dict(row._mapping)

            count = await self._process_feed(feed, force=True)
            if count == 0:
                return True, 'Feed checked - no new articles found'
            return True, f'Posted {count} article(s)'
        except Exception as exc:
            logger.error(f"RssCog: force_send_feed error: {exc}")
            return False, str(exc)

    # -------------------------------------------------------------------------
    # Core processing
    # -------------------------------------------------------------------------

    async def _process_feed(self, feed: dict, force: bool = False) -> int:
        """
        Fetch feed, find new entries, save to web_fluxer_rss_articles, post to channel.
        Returns count of new entries saved.
        """
        feed_id = feed['id']
        guild_id = feed['guild_id']
        now = int(time_lib.time())

        # Mark as checked immediately to prevent concurrent re-processing
        try:
            with db_session_scope() as db:
                db.execute(
                    text("UPDATE web_fluxer_rss_feeds SET last_checked_at = :t WHERE id = :fid"),
                    {'t': now, 'fid': feed_id},
                )
                db.commit()
        except Exception as exc:
            logger.warning(f"RssCog: failed to update last_checked_at for feed {feed_id}: {exc}")

        # Fetch the feed
        loop = asyncio.get_event_loop()
        parsed, error = await loop.run_in_executor(None, _secure_fetch_sync, feed['url'])
        if error or not parsed or not getattr(parsed, 'entries', None):
            err_msg = error or 'Feed returned no entries'
            logger.warning(f"RssCog: feed {feed_id} ({feed['url']}) error: {err_msg}")
            try:
                with db_session_scope() as db:
                    db.execute(
                        text(
                            "UPDATE web_fluxer_rss_feeds SET "
                            "consecutive_failures = consecutive_failures + 1, "
                            "last_error = :err WHERE id = :fid"
                        ),
                        {'err': _truncate(err_msg, 500), 'fid': feed_id},
                    )
                    db.commit()
            except Exception:
                pass
            return 0

        # Clear failure counter on success
        try:
            with db_session_scope() as db:
                db.execute(
                    text(
                        "UPDATE web_fluxer_rss_feeds SET consecutive_failures = 0, last_error = NULL "
                        "WHERE id = :fid AND consecutive_failures > 0"
                    ),
                    {'fid': feed_id},
                )
                db.commit()
        except Exception:
            pass

        entries = parsed.entries

        # Get already-stored GUIDs to avoid duplicates
        try:
            with db_session_scope() as db:
                rows = db.execute(
                    text("SELECT entry_guid FROM web_fluxer_rss_articles WHERE feed_id = :fid"),
                    {'fid': feed_id},
                ).fetchall()
                existing_guids = {r.entry_guid for r in rows}
        except Exception as exc:
            logger.error(f"RssCog: failed to load existing GUIDs for feed {feed_id}: {exc}")
            existing_guids = set()

        # Find new entries (up to 50); force=True bypasses GUID dedup to re-post latest
        new_entries = []
        for entry in entries[:50]:
            guid = _get_entry_guid(entry)
            if guid and (force or guid not in existing_guids):
                new_entries.append((entry, guid))

        if not new_entries:
            return 0

        # On force send, only take the single most recent entry
        if force:
            new_entries = new_entries[:1]

        # Apply max_age_days filter - skip articles older than the configured limit (not on force)
        max_age_days = feed.get('max_age_days')
        if max_age_days and not force:
            cutoff = now - (max_age_days * 86400)
            age_filtered = []
            for entry, guid in new_entries:
                pub_at = _parse_published_time(entry)
                # If no publish time, use a conservative fallback (allow it through)
                if pub_at is None or pub_at >= cutoff:
                    age_filtered.append((entry, guid))
            skipped = len(new_entries) - len(age_filtered)
            if skipped > 0:
                logger.debug(f"RssCog: feed {feed_id} skipped {skipped} articles older than {max_age_days} days")
            new_entries = age_filtered

        if not new_entries:
            return 0

        # Apply category filter
        embed_cfg = {}
        if feed.get('embed_config'):
            try:
                embed_cfg = json.loads(feed['embed_config'])
            except Exception:
                pass

        filter_mode = feed.get('category_filter_mode') or 'none'
        if filter_mode != 'none':
            cat_filters = []
            if feed.get('category_filters'):
                try:
                    cat_filters = json.loads(feed['category_filters'])
                except Exception:
                    pass
            if cat_filters:
                filter_set = {f.lower() for f in cat_filters}
                filtered = []
                for entry, guid in new_entries:
                    cats = {
                        (tag.get('term') or tag.get('label') or '').lower()
                        for tag in entry.get('tags', [])
                        if tag.get('term') or tag.get('label')
                    }
                    has_match = bool(cats & filter_set)
                    if (filter_mode == 'include' and has_match) or (filter_mode == 'exclude' and not has_match):
                        filtered.append((entry, guid))
                new_entries = filtered

        if not new_entries:
            return 0

        # Oldest-first
        new_entries.reverse()
        total_new = len(new_entries)
        feed_label = feed.get('label') or feed.get('url', '')

        # Save all new articles to DB
        saved_count = 0
        for entry, guid in new_entries:
            try:
                thumbnail = _extract_thumbnail(entry)
                summary = _strip_html(entry.get('summary', entry.get('description', '')))
                author = entry.get('author')
                if not author and hasattr(entry, 'author_detail'):
                    author = entry.author_detail.get('name')
                cats = [
                    (tag.get('term') or tag.get('label') or '')
                    for tag in entry.get('tags', [])[:10]
                    if tag.get('term') or tag.get('label')
                ]
                cats_json = json.dumps(cats) if cats else None
                pub_at = _parse_published_time(entry)
                with db_session_scope() as db:
                    db.execute(
                        text(
                            "INSERT IGNORE INTO web_fluxer_rss_articles "
                            "(feed_id, guild_id, entry_guid, entry_link, entry_title, "
                            "entry_summary, entry_author, entry_thumbnail, entry_categories, "
                            "feed_label, published_at, posted_at) "
                            "VALUES (:fid, :gid, :guid, :link, :title, "
                            ":summary, :author, :thumb, :cats, :label, :pub, :now)"
                        ),
                        {
                            'fid': feed_id,
                            'gid': guild_id,
                            'guid': guid[:500],
                            'link': _sanitize_link(entry.get('link', ''))[:500] or None,
                            'title': _truncate(entry.get('title', ''), 500),
                            'summary': _truncate(summary, 1000),
                            'author': _truncate(author, 256),
                            'thumb': _truncate(thumbnail, 500),
                            'cats': cats_json,
                            'label': _truncate(feed_label, 200),
                            'pub': pub_at,
                            'now': now,
                        }
                    )
                    db.execute(
                        text(
                            "UPDATE web_fluxer_rss_feeds SET last_entry_id = :guid "
                            "WHERE id = :fid"
                        ),
                        {'guid': guid[:200], 'fid': feed_id},
                    )
                    db.commit()
                saved_count += 1
            except Exception as exc:
                logger.error(f"RssCog: failed to save article for feed {feed_id}: {exc}")

        # Post to channel
        channel_id_str = feed['channel_id']
        if not channel_id_str:
            logger.warning(f"RssCog: no channel_id configured for feed {feed_id}")
            return saved_count

        ping_content = f"<@&{feed['ping_role_id']}>" if feed.get('ping_role_id') else None

        # Embed color
        embed_color = 0xea580c
        color_str = embed_cfg.get('color', '#ea580c') or '#ea580c'
        try:
            embed_color = int(color_str.lstrip('#'), 16)
        except Exception:
            pass

        emoji_prefix = embed_cfg.get('custom_emoji_prefix', '') or ''
        title_prefix = embed_cfg.get('title_prefix', '') or ''
        title_suffix = embed_cfg.get('title_suffix', '') or ''
        custom_desc = embed_cfg.get('custom_description', '') or ''
        footer_text = embed_cfg.get('footer_text', '') or 'Powered by QuestLog Network'
        max_individual = embed_cfg.get('max_individual_posts', 5) or 5
        always_summary = embed_cfg.get('always_use_summary', False)
        thumbnail_mode = embed_cfg.get('thumbnail_mode', 'rss') or 'rss'
        custom_thumb_url = embed_cfg.get('custom_thumbnail_url', '') or ''
        show_author = embed_cfg.get('show_author', True)
        show_date = embed_cfg.get('show_publish_date', True)
        show_cats = embed_cfg.get('show_categories', False)

        portal_url = f"{PORTAL_BASE_URL}/{guild_id}/rss/"
        use_summary = always_summary or (max_individual == 0) or (total_new > max_individual)

        try:
            if use_summary:
                # Summary embed
                s_title = f"{total_new} New Article{'s' if total_new > 1 else ''} from {feed_label}"
                if title_prefix:
                    s_title = f"{title_prefix} {s_title}"
                if title_suffix:
                    s_title = f"{s_title} {title_suffix}"
                s_title = f"{emoji_prefix} {s_title}" if emoji_prefix else f"📰 {s_title}"

                s_desc = (f"{custom_desc}\n\n" if custom_desc else '') + f"**[Read articles in member portal]({portal_url})**"

                embed = fluxer.Embed(
                    title=s_title[:256],
                    description=s_desc[:4096],
                    color=embed_color,
                )
                embed.set_footer(text=footer_text[:256])

                await self.bot._http.send_message(
                    channel_id_str,
                    content=ping_content,
                    embed=embed,
                )
            else:
                # Individual embeds
                for entry, guid in new_entries:
                    title = entry.get('title', 'No Title') or 'No Title'
                    if title_prefix:
                        title = f"{title_prefix} {title}"
                    if title_suffix:
                        title = f"{title} {title_suffix}"
                    if emoji_prefix:
                        title = f"{emoji_prefix} {title}"

                    link = _sanitize_link(entry.get('link', ''))
                    summary = _strip_html(entry.get('summary', entry.get('description', '')))
                    if custom_desc:
                        summary = f"{custom_desc}\n\n{summary}"

                    embed = fluxer.Embed(
                        title=title[:256],
                        description=summary[:1000] if summary else None,
                        url=link or None,
                        color=embed_color,
                    )

                    # Thumbnail
                    if thumbnail_mode == 'custom' and custom_thumb_url:
                        embed.set_thumbnail(url=custom_thumb_url)
                    elif thumbnail_mode == 'rss':
                        thumb = _extract_thumbnail(entry)
                        if thumb:
                            embed.set_thumbnail(url=thumb)

                    if show_author:
                        author = entry.get('author')
                        if not author and hasattr(entry, 'author_detail'):
                            author = entry.author_detail.get('name')
                        if author:
                            embed.set_author(name=author[:256])

                    if show_date:
                        pub_at = _parse_published_time(entry)
                        if pub_at:
                            from datetime import datetime, timezone
                            pub_str = datetime.fromtimestamp(pub_at, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
                            embed.add_field(name='Published', value=pub_str, inline=True)

                    if show_cats:
                        cats = [
                            (tag.get('term') or tag.get('label') or '')
                            for tag in entry.get('tags', [])[:5]
                            if tag.get('term') or tag.get('label')
                        ]
                        if cats:
                            embed.add_field(name='Categories', value=', '.join(cats), inline=True)

                    embed.set_footer(text=footer_text[:256])

                    await self.bot._http.send_message(
                        channel_id_str,
                        content=ping_content,
                        embed=embed,
                    )
                    ping_content = None  # Only ping on first post
                    await asyncio.sleep(2)  # Rate-limit between individual posts
        except Exception as exc:
            logger.error(f"RssCog: error posting to channel for feed {feed_id}: {exc}")

        return saved_count

    # -------------------------------------------------------------------------
    # Command: !checkrss
    # -------------------------------------------------------------------------

    @Cog.command(name='checkrss')
    async def cmd_checkrss(self, ctx):
        """Force-check all RSS feeds for this guild right now (owner/admin only)."""
        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)

        # Owner/admin check
        try:
            with db_session_scope() as db:
                row = db.execute(
                    text("SELECT owner_id FROM web_fluxer_guild_settings WHERE guild_id = :g"),
                    {'g': guild_id},
                ).fetchone()
                if not row or str(row.owner_id) != user_id:
                    await ctx.send("You must be the server owner to use this command.")
                    return
        except Exception as exc:
            logger.error(f"RssCog: checkrss admin check failed: {exc}")
            await ctx.send("Error checking permissions.")
            return

        msg = await ctx.send("Checking RSS feeds...")
        try:
            with db_session_scope() as db:
                feeds = db.execute(
                    text(
                        "SELECT id, guild_id, url, label, channel_id, ping_role_id, "
                        "poll_interval_minutes, max_age_days, category_filter_mode, category_filters, "
                        "embed_config, last_checked_at, last_entry_id, consecutive_failures "
                        "FROM web_fluxer_rss_feeds WHERE guild_id = :g AND is_active = 1"
                    ),
                    {'g': guild_id},
                ).fetchall()
                feed_list = [dict(f._mapping) for f in feeds]
        except Exception as exc:
            await msg.edit(content=f"Error loading feeds: {exc}")
            return

        if not feed_list:
            await msg.edit(content="No active RSS feeds configured for this server.")
            return

        total_new = 0
        for feed in feed_list:
            count = await self._process_feed(feed, force=True)
            total_new += count

        await msg.edit(content=f"Done. Checked {len(feed_list)} feed(s) - {total_new} new article(s) posted.")
