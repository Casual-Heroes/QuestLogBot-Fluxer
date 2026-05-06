# cogs/bridge.py - Discord <-> Fluxer message bridge
#
# Relays messages bidirectionally between a Discord channel and a Fluxer channel
# via the QuestLog internal relay API.
#
# Flow (Fluxer -> Discord):
#   1. User sends message in a bridged Fluxer channel
#   2. on_message fires -> POST to /ql/internal/bridge/relay/ with source=fluxer
#   3. QuestLog hub queues it for the Discord side
#   4. WardenBot polls /ql/internal/bridge/pending/discord/ and posts to Discord channel
#
# Flow (Discord -> Fluxer):
#   1. WardenBot posts received Discord message to /ql/internal/bridge/relay/ source=discord
#   2. This cog polls /ql/internal/bridge/pending/fluxer/ every 3s
#   3. Posts formatted message to the Fluxer target channel
#   4. Records sent message ID in /ql/internal/bridge/message-map/
#
# Reactions:
#   - on_raw_reaction_add fires for unicode emojis only
#   - POSTs to /ql/internal/bridge/reaction/ with platform + message_id + emoji
#   - Hub looks up cross-platform message map and queues the reaction
#   - Polls /ql/internal/bridge/pending-reactions/fluxer/ every 6s and calls add_reaction()
#
# Replies:
#   - message.referenced_message provides the quoted message
#   - Sent as "> quote\n**[D] Author:** reply"
#
# Anti-loop:
#   - Never relay messages from this bot's own user.id
#   - Never relay messages starting with "**[D]" or "**[F]"

import asyncio
import re

import aiohttp
from fluxer import Cog, File

from config import logger, QUESTLOG_INTERNAL_API_URL, QUESTLOG_BOT_SECRET, DISCORD_BOT_TOKEN

_BASE = QUESTLOG_INTERNAL_API_URL.rstrip('/')
_TYPING_URL             = _BASE + '/api/internal/bridge/typing/'
_RELAY_URL              = _BASE + '/api/internal/bridge/relay/'
_PENDING_URL            = _BASE + '/api/internal/bridge/pending/fluxer/'
_MSG_MAP_URL            = _BASE + '/api/internal/bridge/message-map/'
_REACTION_URL           = _BASE + '/api/internal/bridge/reaction/'
_PENDING_REACTIONS_URL  = _BASE + '/api/internal/bridge/pending-reactions/fluxer/'
_DELETE_URL             = _BASE + '/api/internal/bridge/delete/'
_PENDING_DELETIONS_URL  = _BASE + '/api/internal/bridge/pending-deletions/fluxer/'
_EDIT_URL               = _BASE + '/api/internal/bridge/edit/'
_PENDING_EDITS_URL      = _BASE + '/api/internal/bridge/pending-edits/fluxer/'

_HEADERS = {'X-Bot-Secret': QUESTLOG_BOT_SECRET, 'Content-Type': 'application/json'}

# Prefixes that indicate a relayed message - skip to prevent loops
_RELAY_PREFIXES = ('**[D]', '**[F]', '**[M]', '[D]', '[F]', '[M]')

_CUSTOM_EMOJI_RE = re.compile(r'<a?:(\w+):\d+>')

# Matches a string that is purely one or more URLs (possibly separated by whitespace)
_URL_ONLY_RE = re.compile(r'^(https?://\S+\s*)+$')


def _format_bridged(tag: str, author: str, content: str, reply_quote: str = '') -> str:
    """
    Format a bridged message for delivery.
    If content is a bare URL (or URLs), put them on a new line so the platform
    can unfurl the link preview. Mixed text+URL stays on one line.
    """
    header = f"**[{tag}] {author}:**"
    body = content.rstrip() if content else ''
    if reply_quote:
        prefix = f"> {reply_quote}\n"
    else:
        prefix = ''
    if body and _URL_ONLY_RE.match(body):
        return f"{prefix}{header}\n{body}"
    return f"{prefix}{header} {body}".rstrip()


def _resolve_fluxer_content(message) -> tuple:
    """
    Resolve Fluxer mention markup and custom emoji for relay.
    Returns (content, mentions) where:
    - content keeps raw <@userid> tokens for user mentions (hub will resolve cross-platform)
    - mentions is a list of {id, display_name} for each mentioned user
    - custom emoji are resolved to :name: text
    """
    content = message.content or ''
    mentions = []

    # Build mentions list from SDK's mentions list
    if hasattr(message, 'mentions') and message.mentions:
        for user in message.mentions:
            uid = str(user.id)
            name = getattr(user, 'display_name', None) or getattr(user, 'username', uid)
            mentions.append({'id': uid, 'display_name': name})
            # Normalise <@!uid> -> <@uid> for consistent hub matching
            content = content.replace(f'<@!{uid}>', f'<@{uid}>')

    # Normalise any remaining <@!id> patterns not in the mentions list
    content = re.sub(r'<@!(\d+)>', r'<@\1>', content)

    # <:name:id> and <a:name:id> (custom / animated emoji) -> :name:
    content = _CUSTOM_EMOJI_RE.sub(r':\1:', content)

    return content.strip(), mentions


def _format_reply_quote(content: str, max_len: int = 120) -> str:
    """Strip relay prefix formatting and truncate quoted reply content."""
    text = (content or '').strip()
    # Strip formatting like "**[D] Name:** " or "**[F] Name:** " or "**[M] Name:** "
    for marker in ('**[D] ', '**[F] ', '**[M] '):
        if text.startswith(marker):
            idx = text.find(':** ')
            if idx != -1:
                text = text[idx + 4:]
            break
    if len(text) > max_len:
        text = text[:max_len] + '...'
    return text


class BridgeCog(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        self._session: aiohttp.ClientSession | None = None
        self._poll_task: asyncio.Task | None = None

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("BridgeCog: started relay polling")

    async def cog_unload(self):
        if self._poll_task:
            self._poll_task.cancel()
        if self._session:
            await self._session.close()

    @Cog.listener()
    async def on_ready(self):
        """Ensure session and poll task are running after reconnect."""
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info("BridgeCog: restarted relay polling on reconnect")

    @Cog.listener()
    async def on_message(self, message):
        """Relay Fluxer messages to Discord via hub queue."""
        # Ignore own messages
        if message.author.id == self.bot.user.id:
            return
        # Ignore relayed messages (anti-loop)
        if message.content and message.content.startswith(_RELAY_PREFIXES):
            return
        # Ignore bot commands - keep them native to this platform
        if message.content and message.content.lstrip().startswith(('!', '/')):
            return
        # Resolve mentions and custom emoji - keeps raw <@id> tokens for hub cross-platform resolution
        content, mentions = _resolve_fluxer_content(message)

        # Convert @everyone/@here to @room for Matrix
        content = content.replace('@everyone', '@room').replace('@here', '@room')

        # Extract attachments (images, GIFs, videos, files)
        attachments = []
        for att in (message.attachments or []):
            url = str(getattr(att, 'url', '') or '').strip()
            if url.startswith('https://'):
                attachments.append({
                    'url': url,
                    'filename': str(getattr(att, 'filename', '') or ''),
                    'content_type': str(getattr(att, 'content_type', '') or ''),
                })

        # Extract image/GIF URLs from embeds (Tenor, Giphy, direct image embeds)
        for emb in (message.embeds or []):
            img_url = None
            if getattr(emb, 'type', None) == 'gifv':
                video = getattr(emb, 'video', None)
                thumb = getattr(emb, 'thumbnail', None)
                if thumb:
                    img_url = str(getattr(thumb, 'proxy_url', None) or getattr(thumb, 'url', None) or '')
                elif video:
                    img_url = str(getattr(video, 'url', '') or '')
            elif getattr(emb, 'type', None) == 'image' and getattr(emb, 'url', None):
                img_url = str(emb.url)
            elif getattr(emb, 'image', None) and getattr(emb.image, 'url', None):
                img_url = str(emb.image.url)
            elif getattr(emb, 'thumbnail', None) and getattr(emb.thumbnail, 'url', None) and getattr(emb, 'type', None) in ('rich', 'link'):
                img_url = str(emb.thumbnail.url)
            if img_url and img_url.startswith('https://'):
                lower = img_url.split('?')[0].lower()
                if lower.endswith('.gif') or 'tenor.com' in img_url or 'giphy.com' in img_url:
                    fname, ctype = 'image.gif', 'image/gif'
                elif lower.endswith('.png'):
                    fname, ctype = 'image.png', 'image/png'
                elif lower.endswith(('.jpg', '.jpeg')):
                    fname, ctype = 'image.jpg', 'image/jpeg'
                elif lower.endswith('.webp'):
                    fname, ctype = 'image.webp', 'image/webp'
                else:
                    fname, ctype = 'image.gif', 'image/gif'
                attachments.append({'url': img_url, 'filename': fname, 'content_type': ctype})

        # Skip if no content and no attachments
        if not content and not attachments:
            return

        # Reply vs forward detection.
        # Fluxer sends referenced_message for both replies and forwards.
        # A forward has a referenced message from a DIFFERENT channel.
        reply_quote = None
        reply_to_message_id = None
        is_forward = False
        forward_from_author = None
        forward_content = None

        if message.referenced_message:
            ref = message.referenced_message
            # Use channel_id int attribute directly (Message dataclass field)
            ref_channel_id = ref.channel_id
            current_channel_id = message.channel_id
            if ref_channel_id and ref_channel_id != current_channel_id:
                # Different channel = forward
                is_forward = True
                ref_author = getattr(ref, 'author', None)
                forward_from_author = (
                    getattr(ref_author, 'display_name', None) or
                    getattr(ref_author, 'username', None) or 'Unknown'
                ) if ref_author else 'Unknown'
                forward_content = (ref.content or '').strip()
            else:
                ref_content = (ref.content or '').strip()
                if ref_content:
                    reply_quote = _format_reply_quote(ref_content)
                reply_to_message_id = str(ref.id)

        # If this is a forward, prepend the forwarded content
        if is_forward and forward_content:
            content = f"[forwarded from {forward_from_author}]\n> {forward_content}" + (f"\n{content}" if content else '')

        channel_id = str(message.channel_id)
        avatar_url = message.author.avatar_url  # Fluxer User.avatar_url property
        payload = {
            'source_platform': 'fluxer',
            'fluxer_channel_id': channel_id,
            'source_message_id': str(message.id),
            'author_name': message.author.display_name or message.author.username,
            'author_avatar': avatar_url,
            'content': content,
            'reply_quote': reply_quote,
            'reply_to_message_id': reply_to_message_id,
            'attachments': attachments,
            'mentions': mentions,
        }
        try:
            if self._session and not self._session.closed:
                async with self._session.post(
                    _RELAY_URL, json=payload, headers=_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('queued', 0) == 0:
                            # Channel not in any bridge - ignore silently
                            return
                    elif resp.status not in (200, 201):
                        logger.warning(f"BridgeCog: relay POST returned {resp.status} to {_RELAY_URL}")
        except Exception as e:
            logger.debug(f"BridgeCog: relay POST error: {e}")

    @Cog.listener()
    async def on_message_delete(self, data):
        """Relay message deletions to Discord via hub."""
        # data is the raw gateway dict: {'id': message_id, 'channel_id': ...}
        message_id = str(data.get('id', '') or '')
        channel_id = str(data.get('channel_id', '') or '')
        if not message_id or not channel_id:
            return
        try:
            if self._session and not self._session.closed:
                async with self._session.post(
                    _DELETE_URL,
                    json={'platform': 'fluxer', 'message_id': message_id, 'channel_id': channel_id},
                    headers=_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    pass  # best-effort
        except Exception as e:
            logger.debug(f"BridgeCog: delete relay error: {e}")

    @Cog.listener()
    async def on_message_edit(self, message):
        """Relay Fluxer message edits to Discord via hub."""
        message_id = str(message.id)
        channel_id = str(message.channel_id)
        new_content = (message.content or '').strip()

        if not message_id or not channel_id or not new_content:
            return
        if message.author.id == self.bot.user.id:
            return
        if new_content.startswith(_RELAY_PREFIXES):
            return
        try:
            if self._session and not self._session.closed:
                async with self._session.post(
                    _EDIT_URL,
                    json={
                        'platform': 'fluxer',
                        'message_id': message_id,
                        'channel_id': channel_id,
                        'new_content': new_content,
                    },
                    headers=_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    pass
        except Exception as e:
            logger.debug(f"BridgeCog: edit relay error: {e}")

    @Cog.listener()
    async def on_raw_reaction_add(self, payload):
        """Relay unicode emoji reactions to Discord via hub."""
        # Ignore bot's own reactions
        if payload.user_id == self.bot.user.id:
            return
        # Only relay unicode emojis - skip custom emojis (have an integer id)
        if payload.emoji.id is not None:
            return
        emoji_str = str(payload.emoji)
        if not emoji_str:
            return

        try:
            if self._session and not self._session.closed:
                async with self._session.post(
                    _REACTION_URL,
                    json={
                        'platform': 'fluxer',
                        'message_id': str(payload.message_id),
                        'channel_id': str(payload.channel_id),
                        'emoji': emoji_str,
                    },
                    headers=_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    pass  # best-effort, hub will 200 or 400 silently
        except Exception as e:
            logger.debug(f"BridgeCog: reaction relay error: {e}")

    @Cog.listener()
    async def on_typing_start(self, data):
        """Relay Fluxer typing indicators to Discord via Discord API."""
        # data = raw gateway TYPING_START payload: {channel_id, user_id, ...}
        channel_id = str(data.get('channel_id', '') or '')
        user_id = str(data.get('user_id', '') or '')
        if not channel_id or not user_id:
            return
        # Ignore own bot typing
        if self.bot.user and str(self.bot.user.id) == user_id:
            return
        if not self._session or self._session.closed:
            return
        try:
            async with self._session.post(
                _TYPING_URL,
                json={'platform': 'fluxer', 'channel_id': channel_id},
                headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                if resp.status != 200:
                    return
                result = await resp.json()
        except Exception:
            return

        for target in result.get('targets', []):
            if target.get('platform') != 'discord':
                continue
            discord_channel_id = str(target.get('channel_id', ''))
            if not discord_channel_id or not DISCORD_BOT_TOKEN:
                continue
            try:
                async with self._session.post(
                    f'https://discord.com/api/v10/channels/{discord_channel_id}/typing',
                    headers={'Authorization': f'Bot {DISCORD_BOT_TOKEN}', 'Content-Length': '0'},
                    timeout=aiohttp.ClientTimeout(total=3)
                ) as _:
                    pass  # best-effort, 204 No Content
            except Exception as e:
                logger.debug(f"BridgeCog (Fluxer): Discord typing POST error: {e}")

    async def _poll_loop(self):
        """Poll the hub every 3s for messages; every 6s for reactions/edits/deletions."""
        await asyncio.sleep(5)  # Wait for bot to be ready
        tick = 0
        while True:
            try:
                await self._deliver_pending()
            except Exception as e:
                logger.warning(f"BridgeCog poll error: {e}")

            # Reactions, deletions, and edits every other tick (every 6s)
            if tick % 2 == 0:
                try:
                    await self._deliver_pending_reactions()
                except Exception as e:
                    logger.warning(f"BridgeCog reaction poll error: {e}")
                try:
                    await self._deliver_pending_deletions()
                except Exception as e:
                    logger.warning(f"BridgeCog deletion poll error: {e}")
                try:
                    await self._deliver_pending_edits()
                except Exception as e:
                    logger.warning(f"BridgeCog edit poll error: {e}")

            tick += 1
            await asyncio.sleep(3)

    async def _deliver_pending(self):
        """Fetch pending messages from hub and post to Fluxer channels."""
        if not self._session or self._session.closed:
            return
        try:
            async with self._session.get(
                _PENDING_URL, headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"BridgeCog: pending fetch returned {resp.status} from {_PENDING_URL}")
                    return
                data = await resp.json()
        except Exception as e:
            logger.warning(f"BridgeCog: pending fetch error: {e}")
            return

        messages = data.get('messages', [])
        for msg in messages:
            channel_id_str = str(msg.get('target_channel_id', ''))
            content = msg.get('content', '')
            author = msg.get('author_name', 'Unknown')
            reply_quote = msg.get('reply_quote', '')
            reply_to_event_id = msg.get('reply_to_event_id')  # Fluxer message ID to reply to
            attachments = msg.get('attachments', []) or []
            relay_id = msg.get('id')
            source = msg.get('source_platform', 'discord')
            _TAG_MAP = {'discord': 'D', 'fluxer': 'F', 'matrix': 'M'}
            tag = _TAG_MAP.get(source, 'D')

            if not channel_id_str or (not content and not attachments):
                continue

            # Only include the blockquote if we can't do a native reply
            rq = reply_quote if (reply_quote and not reply_to_event_id) else ''
            formatted = _format_bridged(tag, author, content, rq)

            # Collect attachment files to upload
            attach_files = []
            attach_text_urls = []
            if attachments:
                for a in attachments:
                    # Prefer discord_url (direct public Matrix URL) over the proxy url
                    url = a.get('discord_url') or a.get('url', '')
                    if not url:
                        continue
                    filename = a.get('filename') or 'attachment'
                    content_type = a.get('content_type', '')
                    # Try to download and re-upload image/gif/video attachments
                    if content_type.startswith(('image/', 'video/')) or filename.lower().endswith(('.gif', '.png', '.jpg', '.jpeg', '.webp', '.mp4', '.webm')):
                        try:
                            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                                if r.status == 200:
                                    data_bytes = await r.read()
                                    attach_files.append((data_bytes, filename, content_type))
                                else:
                                    attach_text_urls.append(url)
                        except Exception:
                            attach_text_urls.append(url)
                    else:
                        attach_text_urls.append(url)

            if attach_text_urls:
                # Don't append a URL that is already present in the formatted text
                # (Discord unfurls link embeds as thumbnails even when the URL is the message content)
                extra_urls = [u for u in attach_text_urls if u not in formatted]
                if extra_urls:
                    formatted = (formatted + '\n' + '\n'.join(extra_urls)).strip()

            try:
                channel = await self.bot.fetch_channel(int(channel_id_str))

                # Use native Fluxer reply if we have the target message ID
                message_reference = None
                if reply_to_event_id:
                    message_reference = {'message_id': reply_to_event_id, 'channel_id': channel_id_str}

                if attach_files:
                    # Send text label + first file together; remaining files separately
                    first_bytes, first_fname, first_ctype = attach_files[0]
                    first_file = File(first_bytes, filename=first_fname)
                    sent = await channel.send(formatted.rstrip(':').strip() if not content else formatted, file=first_file, message_reference=message_reference)
                    if relay_id and sent:
                        await self._store_message_map(relay_id, str(sent.id), channel_id_str)
                    for file_bytes, fname, ctype in attach_files[1:]:
                        fluxer_file = File(file_bytes, filename=fname)
                        await channel.send(file=fluxer_file)
                else:
                    sent = await channel.send(formatted, message_reference=message_reference)
                    # Record the sent message ID for reaction mapping
                    if relay_id and sent:
                        await self._store_message_map(relay_id, str(sent.id), channel_id_str)
            except Exception as e:
                logger.warning(f"BridgeCog: send to channel {channel_id_str} failed: {e}")

    async def _store_message_map(self, relay_id: int, message_id: str, channel_id: str):
        """Best-effort: record sent message ID so reactions can be mapped cross-platform."""
        try:
            if self._session and not self._session.closed:
                async with self._session.post(
                    _MSG_MAP_URL,
                    json={
                        'relay_queue_id': relay_id,
                        'platform': 'fluxer',
                        'message_id': message_id,
                        'channel_id': channel_id,
                    },
                    headers=_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    pass
        except Exception as e:
            logger.debug(f"BridgeCog: message-map store error: {e}")

    async def _deliver_pending_reactions(self):
        """Fetch pending reactions from hub and add to Fluxer messages."""
        if not self._session or self._session.closed:
            return
        try:
            async with self._session.get(
                _PENDING_REACTIONS_URL, headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        except Exception as e:
            logger.debug(f"BridgeCog: pending reactions fetch error: {e}")
            return

        for item in data.get('reactions', []):
            message_id = str(item.get('target_message_id', ''))
            channel_id = str(item.get('target_channel_id', ''))
            emoji = item.get('emoji', '')
            if not message_id or not channel_id or not emoji:
                continue
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
                message = await channel.fetch_message(int(message_id))
                await message.add_reaction(emoji)
            except Exception as e:
                logger.debug(f"BridgeCog: add reaction to {message_id} failed: {e}")


    async def _deliver_pending_deletions(self):
        """Fetch pending deletions from hub and delete messages from Fluxer channels."""
        if not self._session or self._session.closed:
            return
        try:
            async with self._session.get(
                _PENDING_DELETIONS_URL, headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        except Exception as e:
            logger.debug(f"BridgeCog: pending deletions fetch error: {e}")
            return

        for item in data.get('deletions', []):
            message_id = str(item.get('target_message_id', ''))
            channel_id = str(item.get('target_channel_id', ''))
            if not message_id or not channel_id:
                continue
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
                message = await channel.fetch_message(int(message_id))
                await message.delete()
            except Exception as e:
                logger.debug(f"BridgeCog: delete message {message_id} failed: {e}")


    async def _deliver_pending_edits(self):
        """Fetch pending edits from hub and edit messages in Fluxer channels."""
        if not self._session or self._session.closed:
            return
        try:
            async with self._session.get(
                _PENDING_EDITS_URL, headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        except Exception as e:
            logger.debug(f"BridgeCog: pending edits fetch error: {e}")
            return

        for item in data.get('edits', []):
            message_id = str(item.get('target_message_id', ''))
            channel_id = str(item.get('target_channel_id', ''))
            new_content = item.get('new_content', '')
            if not message_id or not channel_id or not new_content:
                continue
            try:
                await self.bot._http.edit_message(channel_id, message_id, content=new_content)
            except Exception as e:
                logger.debug(f"BridgeCog: edit message {message_id} failed: {e}")


def setup(bot):
    bot.add_cog(BridgeCog(bot))
