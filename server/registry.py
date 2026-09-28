"""客户端注册表与配对逻辑（与 Discord、网络层无关，便于单独测试）。"""
from __future__ import annotations

import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

CODE_TTL_SECONDS = 300      # 配对码有效期
MAX_CODE_ATTEMPTS = 5       # 同一个配对码最多尝试次数
_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混淆的 0/O/1/I


class PairingError(Exception):
    """无法发起配对时抛出，消息可直接展示给用户。"""


@dataclass
class PendingPair:
    code: str
    user_id: int
    user_name: str
    channel_id: int
    expires_at: float
    attempts: int = 0


@dataclass
class ClientConn:
    client_id: str
    hostname: str
    ws: Any
    connected_at: float
    pending: PendingPair | None = None
    user_id: int | None = None
    user_name: str | None = None
    channel_id: int | None = None
    busy: bool = False
    last_snapshot_at: float = 0.0

    @property
    def paired(self) -> bool:
        return self.channel_id is not None

    async def send(self, **msg: Any) -> None:
        await self.ws.send(json.dumps(msg, ensure_ascii=False))


class Registry:
    def __init__(self) -> None:
        self.clients: dict[str, ClientConn] = {}

    # ---------- 连接管理 ----------
    def _new_id(self) -> str:
        while True:
            cid = "MEE-" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(4))
            if cid not in self.clients:
                return cid

    def add(self, hostname: str, ws: Any) -> ClientConn:
        client = ClientConn(self._new_id(), hostname, ws, time.time())
        self.clients[client.client_id] = client
        return client

    def remove(self, client_id: str) -> ClientConn | None:
        return self.clients.pop(client_id, None)

    def get(self, client_id: str) -> ClientConn | None:
        return self.clients.get(client_id)

    def available(self) -> list[ClientConn]:
        """已握手但尚未绑定的客户端，按连接时间排序。"""
        return sorted((c for c in self.clients.values() if not c.paired),
                      key=lambda c: c.connected_at)

    def in_channel(self, channel_id: int) -> list[ClientConn]:
        return [c for c in self.clients.values() if c.channel_id == channel_id]

    # ---------- 配对 ----------
    def start_pairing(self, client_id: str, user_id: int, user_name: str,
                      channel_id: int) -> PendingPair:
        client = self.clients.get(client_id)
        if client is None:
            raise PairingError(f"客户端 {client_id} 已离线。")
        if client.paired:
            raise PairingError(f"客户端 {client_id} 已经被绑定了。")
        code = f"{secrets.randbelow(10**8):08d}"
        client.pending = PendingPair(code, user_id, user_name, channel_id,
                                     time.time() + CODE_TTL_SECONDS)
        return client.pending

    def cancel_pairing(self, client_id: str) -> None:
        client = self.clients.get(client_id)
        if client:
            client.pending = None

    def verify(self, client_id: str, code: str) -> tuple[bool, str, int]:
        """返回 (是否成功, 原因, 剩余次数)。"""
        client = self.clients.get(client_id)
        if client is None or client.pending is None:
            return False, "当前没有待处理的配对请求", 0
        pending = client.pending
        if time.time() > pending.expires_at:
            client.pending = None
            return False, "配对码已过期，请在 Discord 里重新 @mrmeeseeks", 0

        pending.attempts += 1
        if hmac.compare_digest(pending.code.encode(), str(code).strip().encode()):
            client.pending = None
            client.user_id = pending.user_id
            client.user_name = pending.user_name
            client.channel_id = pending.channel_id
            return True, "配对成功", 0

        left = MAX_CODE_ATTEMPTS - pending.attempts
        if left <= 0:
            client.pending = None
            return False, "错误次数过多，本次配对已作废，请重新 @mrmeeseeks", 0
        return False, "配对码错误", left

    def unpair(self, client_id: str) -> None:
        client = self.clients.get(client_id)
        if client:
            client.user_id = client.user_name = client.channel_id = None
            client.pending = None
