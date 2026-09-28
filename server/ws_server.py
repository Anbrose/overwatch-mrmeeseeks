"""WebSocket 服务：本地客户端通过它握手、配对、上传截图。

协议（JSON 文本帧）：
  客户端 -> 服务器
    {"type": "hello", "hostname": "...", "version": 1}
    {"type": "pair_code", "code": "12345678"}
    {"type": "snapshot", "scoreboard": "<base64 jpeg>", "hud": "<base64 jpeg>"}
  服务器 -> 客户端
    {"type": "welcome", "client_id": "MEE-XXXX"}
    {"type": "pair_request", "user": "...", "channel": "...", "expires_in": 300}
    {"type": "pair_result", "ok": bool, "reason": "...", "attempts_left": n}
    {"type": "unpaired", "reason": "..."}
    {"type": "info" | "error", "message": "..."}
    {"type": "analysis", "summary": "..."}
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from typing import Protocol

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from registry import ClientConn, Registry

log = logging.getLogger("mrmeeseeks.ws")


class Events(Protocol):
    """服务器把事件交给 bot 处理（bot 负责往 Discord 频道发消息）。"""
    async def client_paired(self, client: ClientConn) -> None: ...
    async def pair_failed(self, client: ClientConn, channel_id: int, reason: str) -> None: ...
    async def client_disconnected(self, client: ClientConn) -> None: ...
    async def snapshot_received(self, client: ClientConn, scoreboard: bytes, hud: bytes) -> str: ...


class WSServer:
    def __init__(self, registry: Registry, events: Events, host: str, port: int,
                 min_snapshot_interval: float = 5.0, max_image_bytes: int = 8 * 1024 * 1024):
        self.registry = registry
        self.events = events
        self.host = host
        self.port = port
        self.min_snapshot_interval = min_snapshot_interval
        self.max_image_bytes = max_image_bytes
        self._server = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        # 两张 jpeg 的 base64 + JSON 包装，留足余量
        max_msg = int(self.max_image_bytes * 2 * 1.4) + 4096
        self._server = await serve(self._handler, self.host, self.port,
                                   max_size=max_msg, ping_interval=20, ping_timeout=20)
        log.info("WebSocket server listening on %s:%s", self.host, self.port)

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ---------- 连接处理 ----------
    async def _handler(self, ws: ServerConnection) -> None:
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if hello.get("type") != "hello":
                raise ValueError("first message must be hello")
        except Exception:
            await ws.close(code=1008, reason="handshake required")
            return

        hostname = str(hello.get("hostname") or "unknown")[:64]
        client = self.registry.add(hostname, ws)
        log.info("Client handshake: %s (%s) from %s", client.client_id, hostname, ws.remote_address)
        await client.send(type="welcome", client_id=client.client_id)

        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await client.send(type="error", message="Could not parse message")
                    continue
                kind = msg.get("type")
                if kind == "pair_code":
                    await self._on_pair_code(client, msg)
                elif kind == "snapshot":
                    await self._on_snapshot(client, msg)
                else:
                    await client.send(type="error", message=f"Unknown message type: {kind}")
        except ConnectionClosed:
            pass
        finally:
            self.registry.remove(client.client_id)
            log.info("Client disconnected: %s", client.client_id)
            try:
                await self.events.client_disconnected(client)
            except Exception:
                log.exception("Failed to handle disconnect event")

    async def _on_pair_code(self, client: ClientConn, msg: dict) -> None:
        pending = client.pending
        channel_id = pending.channel_id if pending else None
        ok, reason, left = self.registry.verify(client.client_id, str(msg.get("code", "")))
        await client.send(type="pair_result", ok=ok, reason=reason, attempts_left=left)
        if ok:
            log.info("Paired: %s -> channel %s", client.client_id, client.channel_id)
            await self.events.client_paired(client)
        elif left == 0 and channel_id is not None:
            await self.events.pair_failed(client, channel_id, reason)

    async def _on_snapshot(self, client: ClientConn, msg: dict) -> None:
        if not client.paired:
            await client.send(type="error", message="Not paired yet; screenshots ignored")
            return
        if client.busy:
            await client.send(type="info", message="Previous screenshots are still being analyzed; skipped")
            return
        now = time.time()
        if now - client.last_snapshot_at < self.min_snapshot_interval:
            await client.send(type="info", message="Screenshots sent too often; skipped")
            return
        try:
            scoreboard = base64.b64decode(msg["scoreboard"], validate=True)
            hud = base64.b64decode(msg["hud"], validate=True)
        except (KeyError, binascii.Error, TypeError):
            await client.send(type="error", message="Invalid screenshot data")
            return
        if max(len(scoreboard), len(hud)) > self.max_image_bytes:
            await client.send(type="error", message="Screenshot too large")
            return

        client.busy = True
        client.last_snapshot_at = now
        await client.send(type="info", message="Screenshots received, analyzing…")
        task = asyncio.create_task(self._analyze(client, scoreboard, hud))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _analyze(self, client: ClientConn, scoreboard: bytes, hud: bytes) -> None:
        try:
            summary = await self.events.snapshot_received(client, scoreboard, hud)
            if client.client_id in self.registry.clients:
                await client.send(type="analysis", summary=summary)
        except ConnectionClosed:
            pass
        except Exception:
            log.exception("Screenshot analysis failed")
            try:
                await client.send(type="error", message="Analysis failed; see server logs for details")
            except ConnectionClosed:
                pass
        finally:
            client.busy = False
