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

from config import logger, QUESTLOG_INTERNAL_API_URL, QUESTLOG_BOT_SECRET

_BASE = QUESTLOG_INTERNAL_API_URL.rstrip('/')
_RELAY_URL              = _BASE + '/api/internal/bridge/relay/'
_PENDING_URL            = _BASE + '/api/internal/bridge/pending/fluxer/'
_MSG_MAP_URL            = _BASE + '/api/internal/bridge/message-map/'
_REACTION_URL           = _BASE + '/api/internal/bridge/reaction/'
_PENDING_REACTIONS_URL  = _BASE + '/api/internal/bridge/pending-reactions/fluxer/'
_DELETE_URL             = _BASE + '/api/internal/bridge/delete/'
_PENDING_DELETIONS_URL  = _BASE + '/api/internal/bridge/pending-deletions/fluxer/'

_HEADERS = {'X-Bot-Secret': QUESTLOG_BOT_SECRET, 'Content-Type': 'application/json'}

# Prefixes that indicate a relayed message - skip to prevent loops
_RELAY_PREFIXES = ('**[D]', '**[F]', '**[M]', '[D]', '[F]', '[M]')

_CUSTOM_EMOJI_RE = re.compile(r'<a?:(\w+):\d+>')


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

        # Skip if no content and no attachments
        if not content and not attachments:
            return

        # Reply context: include a quote if this is a reply
        reply_quote = None
        reply_to_message_id = None
        if message.referenced_message:
            ref_content = (message.referenced_message.content or '').strip()
            if ref_content:
                reply_quote = _format_reply_quote(ref_content)
            reply_to_message_id = str(message.referenced_message.id)

        channel_id = str(message.channel.id)
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
                        logger.debug(f"BridgeCog: relay non-200: {resp.status}")
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

    async def _poll_loop(self):
        """Poll the hub every 3s for messages; every 6s for reactions."""
        await asyncio.sleep(5)  # Wait for bot to be ready
        tick = 0
        while True:
            try:
                await self._deliver_pending()
            except Exception as e:
                logger.warning(f"BridgeCog poll error: {e}")

            # Reactions and deletions every other tick (every 6s)
            if tick % 2 == 0:
                try:
                    await self._deliver_pending_reactions()
                except Exception as e:
                    logger.warning(f"BridgeCog reaction poll error: {e}")
                try:
                    await self._deliver_pending_deletions()
                except Exception as e:
                    logger.warning(f"BridgeCog deletion poll error: {e}")

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
                    return
                data = await resp.json()
        except Exception as e:
            logger.debug(f"BridgeCog: pending fetch error: {e}")
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
            if reply_quote and not reply_to_event_id:
                formatted = f"> {reply_quote}\n**[{tag}] {author}:** {content}"
            else:
                formatted = f"**[{tag}] {author}:** {content}".rstrip()

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
                formatted = (formatted + '\n' + '\n'.join(attach_text_urls)).strip()

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


def setup(bot):
    bot.add_cog(BridgeCog(bot))
