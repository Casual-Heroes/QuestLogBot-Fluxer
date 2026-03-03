# client.py - Fluxer Gateway + REST Client
#
# Fluxer's gateway protocol is intentionally identical to Discord's.
# REST API base URL: https://api.fluxer.app/v1
# Gateway: wss://gateway.fluxer.app
#
# When a dedicated fluxer.py Python library exists, replace this client.
# Until then, this uses aiohttp directly for full control over base URLs.

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

import aiohttp

from config import FLUXER_API_URL, FLUXER_GATEWAY_URL, logger

# Gateway opcodes (identical to Discord)
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_PRESENCE_UPDATE = 3
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


class FluxerClient:
    """
    Minimal Fluxer client - gateway WebSocket + REST.
    Handles IDENTIFY, HEARTBEAT, RESUME, and DISPATCH.
    Prefix command dispatch is handled by the bot layer above this.
    """

    def __init__(self, token: str, command_prefix: str = "!"):
        self.token = token
        self.command_prefix = command_prefix
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._heartbeat_interval: float = 41250
        self._last_sequence: Optional[int] = None
        self._session_id: Optional[str] = None
        self._resume_gateway_url: Optional[str] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._event_handlers: dict[str, list[Callable]] = {}
        self._command_handlers: dict[str, Callable] = {}
        self._cogs: list[Any] = []
        self.user: Optional[dict] = None
        self.guilds: dict[str, dict] = {}
        self._running = False
        self._headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        }

    # ====== Cog / Event Registration ======

    def add_cog(self, cog: Any):
        """Register a cog. Cogs define on_* methods and @command methods."""
        self._cogs.append(cog)
        cog._client = self
        logger.info(f"Loaded cog: {cog.__class__.__name__}")

    def event(self, event_name: str):
        """Decorator to register an event handler."""
        def decorator(func: Callable):
            self._event_handlers.setdefault(event_name, []).append(func)
            return func
        return decorator

    def command(self, name: str):
        """Decorator to register a prefix command."""
        def decorator(func: Callable):
            self._command_handlers[name.lower()] = func
            return func
        return decorator

    async def _dispatch_event(self, event_name: str, data: Any):
        """Fire all registered handlers for an event."""
        handlers = self._event_handlers.get(event_name, [])
        for handler in handlers:
            try:
                await handler(data)
            except Exception as e:
                logger.error(f"Error in handler for {event_name}: {e}", exc_info=True)

        # Also check cogs for on_<event> methods
        method_name = f"on_{event_name.lower()}"
        for cog in self._cogs:
            method = getattr(cog, method_name, None)
            if method:
                try:
                    await method(data)
                except Exception as e:
                    logger.error(f"Error in {cog.__class__.__name__}.{method_name}: {e}", exc_info=True)

    async def _dispatch_command(self, message: dict):
        """Parse and dispatch a prefix command from a MESSAGE_CREATE event."""
        content = message.get("content", "")
        if not content.startswith(self.command_prefix):
            return

        parts = content[len(self.command_prefix):].strip().split()
        if not parts:
            return

        cmd_name = parts[0].lower()
        args = parts[1:]

        # Check top-level commands
        handler = self._command_handlers.get(cmd_name)
        if handler:
            try:
                await handler(message, args)
            except Exception as e:
                logger.error(f"Error in command !{cmd_name}: {e}", exc_info=True)
            return

        # Check cog commands
        for cog in self._cogs:
            method = getattr(cog, f"cmd_{cmd_name}", None)
            if method:
                try:
                    await method(message, args)
                except Exception as e:
                    logger.error(f"Error in {cog.__class__.__name__}.cmd_{cmd_name}: {e}", exc_info=True)
                return

    # ====== REST Helpers ======

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers)
        return self._session

    async def get(self, path: str) -> Any:
        session = await self._get_session()
        async with session.get(f"{FLUXER_API_URL}{path}") as resp:
            resp.raise_for_status()
            return await resp.json()

    async def post(self, path: str, data: dict) -> Any:
        session = await self._get_session()
        async with session.post(f"{FLUXER_API_URL}{path}", json=data) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def patch(self, path: str, data: dict) -> Any:
        session = await self._get_session()
        async with session.patch(f"{FLUXER_API_URL}{path}", json=data) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def delete(self, path: str) -> Any:
        session = await self._get_session()
        async with session.delete(f"{FLUXER_API_URL}{path}") as resp:
            resp.raise_for_status()
            if resp.content_length:
                return await resp.json()
            return {}

    async def send_message(self, channel_id: str, content: str = "", embed: dict = None) -> dict:
        payload = {}
        if content:
            payload["content"] = content
        if embed:
            payload["embeds"] = [embed]
        return await self.post(f"/channels/{channel_id}/messages", payload)

    async def send_reply(self, message: dict, content: str) -> dict:
        return await self.post(f"/channels/{message['channel_id']}/messages", {
            "content": content,
            "message_reference": {"message_id": message["id"]},
        })

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str):
        session = await self._get_session()
        async with session.put(
            f"{FLUXER_API_URL}/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me"
        ) as resp:
            resp.raise_for_status()

    async def ban_member(self, guild_id: str, user_id: str, reason: str = None,
                         delete_message_days: int = 0, duration_seconds: int = 0):
        payload = {"delete_message_days": delete_message_days}
        if reason:
            payload["reason"] = reason
        if duration_seconds:
            payload["ban_duration_seconds"] = duration_seconds
        return await self.post(f"/guilds/{guild_id}/bans/{user_id}", payload)

    async def kick_member(self, guild_id: str, user_id: str):
        return await self.delete(f"/guilds/{guild_id}/members/{user_id}")

    async def timeout_member(self, guild_id: str, user_id: str,
                              until: str, reason: str = None):
        payload = {"communication_disabled_until": until}
        if reason:
            payload["timeout_reason"] = reason
        return await self.patch(f"/guilds/{guild_id}/members/{user_id}", payload)

    # ====== Gateway ======

    async def _send(self, op: int, data: Any):
        if self._ws:
            await self._ws.send_json({"op": op, "d": data})

    async def _heartbeat_loop(self):
        while self._running:
            await asyncio.sleep(self._heartbeat_interval / 1000)
            await self._send(OP_HEARTBEAT, self._last_sequence)
            logger.debug("Heartbeat sent")

    async def _identify(self):
        await self._send(OP_IDENTIFY, {
            "token": self.token,
            "intents": (
                1 << 0  |  # GUILDS
                1 << 1  |  # GUILD_MEMBERS
                1 << 2  |  # GUILD_BANS
                1 << 9  |  # GUILD_MESSAGES
                1 << 10 |  # GUILD_MESSAGE_REACTIONS
                1 << 15    # MESSAGE_CONTENT
            ),
            "properties": {
                "os": "linux",
                "browser": "questlogfluxer",
                "device": "questlogfluxer",
            },
        })

    async def _resume(self):
        await self._send(OP_RESUME, {
            "token": self.token,
            "session_id": self._session_id,
            "seq": self._last_sequence,
        })

    async def _connect(self, gateway_url: str):
        session = await self._get_session()
        async with session.ws_connect(gateway_url) as ws:
            self._ws = ws
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(msg.data)
                    await self._handle_payload(payload)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning(f"WebSocket closed: {msg}")
                    break

    async def _handle_payload(self, payload: dict):
        op = payload.get("op")
        data = payload.get("d")
        seq = payload.get("s")
        event_name = payload.get("t")

        if seq is not None:
            self._last_sequence = seq

        if op == OP_HELLO:
            self._heartbeat_interval = data["heartbeat_interval"]
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            if self._session_id and self._resume_gateway_url:
                await self._resume()
            else:
                await self._identify()

        elif op == OP_DISPATCH:
            await self._handle_dispatch(event_name, data)

        elif op == OP_RECONNECT:
            logger.info("Gateway requested reconnect")
            if self._ws:
                await self._ws.close()

        elif op == OP_INVALID_SESSION:
            logger.warning("Invalid session - re-identifying")
            self._session_id = None
            await asyncio.sleep(5)
            await self._identify()

        elif op == OP_HEARTBEAT:
            await self._send(OP_HEARTBEAT, self._last_sequence)

    async def _handle_dispatch(self, event_name: str, data: Any):
        if event_name == "READY":
            self.user = data["user"]
            self._session_id = data["session_id"]
            logger.info(f"Logged in as @{self.user['username']}#{self.user['discriminator']}")
            await self._dispatch_event("ready", data)

        elif event_name == "RESUMED":
            logger.info("Session resumed")

        elif event_name == "GUILD_CREATE":
            self.guilds[data["id"]] = data

        elif event_name == "GUILD_DELETE":
            self.guilds.pop(data["id"], None)

        elif event_name == "MESSAGE_CREATE":
            if not data.get("author", {}).get("bot"):
                await self._dispatch_command(data)
            await self._dispatch_event("message_create", data)

        else:
            await self._dispatch_event(event_name.lower(), data)

    async def start(self):
        """Connect to Fluxer gateway and start processing events."""
        self._running = True
        gateway_url = self._resume_gateway_url or FLUXER_GATEWAY_URL

        while self._running:
            try:
                logger.info(f"Connecting to gateway: {gateway_url}")
                await self._connect(gateway_url)
            except Exception as e:
                logger.error(f"Gateway error: {e}", exc_info=True)

            if not self._running:
                break

            logger.info("Reconnecting in 5s...")
            await asyncio.sleep(5)
            gateway_url = self._resume_gateway_url or FLUXER_GATEWAY_URL

    async def close(self):
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
