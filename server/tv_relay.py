"""一起看电视直播：直播中继（每个 (房间, 版本) 一个 LiveStream）。

浏览器只拿到 /tv/<room>/<version>/s/<n> 这种路径，上游地址一个都不暴露：
- playlist：取上游媒体列表（1 秒内合并成一次请求），把分片行和 KEY/MAP/MEDIA 的 URI
  改写成按编号的本站路径；同一个绝对地址永远是同一个编号，编号表只留最近 200 个左右。
- segment：按编号转发分片，带小 LRU 缓存；同一编号的并发请求共用一次上游请求；
  没发过的编号直接 KeyError，不碰上游。
- dead_reason：列表连续失败 10 秒（playlist）、序号 10 秒不动（stalled）、
  分片连续失败 3 次（segments），一旦设上就不再清掉，由调用方决定换源。
所有请求都走 tv_net.open_url，每一跳都过 ip_allowed；要请求的地址只来自候选源和它的列表内容。
模块常量在用的时候才读（测试会 monkeypatch）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict

import aiohttp

from tv_channels import Candidate
from tv_net import Blocked, TooLarge, is_public_ip, open_url, read_capped
from tv_stream import StreamError, _fetch_playlist, absolutize, media_sequence, resolve_media

log = logging.getLogger("mrmeeseeks.watch")

SEGMENT_CACHE = 30
MAX_SEGMENT_BYTES = 20 * 1024 * 1024
COALESCE_SECONDS = 1.0
DEAD_SECONDS = 10.0
MAX_SEGMENT_FAILURES = 3
ID_WINDOW = 200

NET_ERRORS = (Blocked, TooLarge, aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError)
URI_TAGS = ("#EXT-X-KEY:", "#EXT-X-MAP:", "#EXT-X-MEDIA:")
DROP_TAGS = ("#EXT-X-STREAM-INF", "#EXT-X-I-FRAME-STREAM-INF")
URI_RE = re.compile(r'URI="([^"]*)"')


class LiveStream:
    def __init__(self, session, cand: Candidate, prefix: str, *,
                 ip_allowed=is_public_ip, clock=time.monotonic):
        self._session = session
        self._cand = cand
        self._prefix = prefix.rstrip("/")
        self._ip_allowed = ip_allowed
        self._clock = clock
        self.dead_reason: str | None = None

        self._lock = asyncio.Lock()
        self._media_url: str | None = None   # resolve_media 的结果；失败后清空，下次重新解析
        self._text: str | None = None        # 上次改写好的列表
        self._last_ok: float | None = None
        self._fail_since: float | None = None
        self._progress = None                # 上次看到的序号（没有序号时用最后一个分片地址）
        self._progress_since: float | None = None

        self._ids: dict[str, int] = {}       # 绝对地址 -> 编号
        self._urls: dict[int, str] = {}      # 编号 -> 绝对地址
        self._next_id = 0
        self._cache: OrderedDict[int, tuple[bytes, str]] = OrderedDict()
        self._inflight: dict[int, asyncio.Task] = {}
        self._seg_failures = 0

    def _mark_dead(self, reason: str) -> None:
        if self.dead_reason is None:
            self.dead_reason = reason
            log.info("tv stream %s dead: %s (%s)", self._prefix, reason, self._cand.url)

    # ---------- 媒体列表 ----------

    async def playlist(self) -> str:
        async with self._lock:
            if (self._text is not None and self._last_ok is not None
                    and self._clock() - self._last_ok < COALESCE_SECONDS):
                return self._text
            try:
                final, text = await self._fetch_media()
            except StreamError:
                now = self._clock()
                if self._fail_since is None:
                    self._fail_since = now
                if now - self._fail_since >= DEAD_SECONDS:
                    self._mark_dead("playlist")
                raise
            now = self._clock()
            self._fail_since = None
            out, last_url = self._rewrite(text, final)
            seq = media_sequence(text)
            progress = seq if seq is not None else last_url
            if progress != self._progress or self._progress_since is None:
                self._progress = progress
                self._progress_since = now
            elif now - self._progress_since >= DEAD_SECONDS:
                self._mark_dead("stalled")
            self._text = out
            self._last_ok = now
            return out

    async def _fetch_media(self) -> tuple[str, str]:
        """返回 (最终地址, 列表内容)；第一次或上次失败之后重新从候选地址解析（顺便刷新带 IP 的令牌）。"""
        cand = self._cand
        try:
            if self._media_url is None:
                final, text = await resolve_media(self._session, cand.url, cand.ua,
                                                  ip_allowed=self._ip_allowed)
                self._media_url = final
                return final, text
            return await _fetch_playlist(self._session, self._media_url, cand.ua, self._ip_allowed)
        except StreamError:
            self._media_url = None
            raise
        except NET_ERRORS as e:
            self._media_url = None
            raise StreamError(f"{type(e).__name__}: {e}") from e

    def _id_for(self, url: str) -> int:
        n = self._ids.get(url)
        if n is None:
            n = self._next_id
            self._next_id += 1
            self._ids[url] = n
            self._urls[n] = url
        return n

    def _rewrite(self, text: str, base: str) -> tuple[str, str | None]:
        """改写成只含本站路径的列表；顺带返回最后一个分片的绝对地址。"""
        out: list[str] = []
        used: set[int] = set()
        last_url = None

        def local(url: str) -> str:
            n = self._id_for(url)
            used.add(n)
            return f"{self._prefix}/s/{n}"

        for raw in text.lstrip("﻿").splitlines():
            ln = raw.strip()
            if not ln:
                continue
            if not ln.startswith("#"):
                last_url = absolutize(ln, base)
                out.append(local(last_url))
                continue
            if not ln.startswith("#EXT") or ln.startswith(DROP_TAGS):
                continue  # 注释行可能带上游地址；主列表的标签不该出现在媒体列表里
            if ln.startswith(URI_TAGS):
                ln = URI_RE.sub(lambda m: f'URI="{local(absolutize(m.group(1), base))}"', ln)
            elif "URI=" in ln:
                continue  # 低延迟 PART/PRELOAD-HINT 等：不支持，丢掉免得漏出上游地址
            out.append(ln)

        # 编号表有上限：比最新编号小 ID_WINDOW 以上、且这次没用到的忘掉
        floor = self._next_id - 1 - ID_WINDOW
        if floor > 0:
            for n in [n for n in self._urls if n < floor and n not in used]:
                del self._ids[self._urls.pop(n)]
                self._cache.pop(n, None)
        return "\n".join(out) + "\n", last_url

    # ---------- 分片 ----------

    async def segment(self, n: int) -> tuple[bytes, str]:
        url = self._urls[n]  # 没发过的编号：KeyError，不碰上游
        hit = self._cache.get(n)
        if hit is not None:
            self._cache.move_to_end(n)
            return hit
        task = self._inflight.get(n)
        if task is None:
            task = asyncio.ensure_future(self._fetch_segment(n, url))
            self._inflight[n] = task
            task.add_done_callback(lambda t, n=n: self._segment_done(n, t))
        # shield：某个等待者被取消不影响共用的请求
        return await asyncio.shield(task)

    def _segment_done(self, n: int, task: asyncio.Task) -> None:
        if self._inflight.get(n) is task:
            del self._inflight[n]
        if not task.cancelled():
            task.exception()  # 取走异常，免得没人等时报 "exception was never retrieved"

    async def _fetch_segment(self, n: int, url: str) -> tuple[bytes, str]:
        try:
            resp = await open_url(self._session, url, self._cand.ua, ip_allowed=self._ip_allowed)
            try:
                if resp.status != 200:
                    raise StreamError(f"segment http {resp.status}")
                ctype = resp.headers.get("Content-Type") or "video/mp2t"
                body = await read_capped(resp, MAX_SEGMENT_BYTES)
            finally:
                resp.release()
        except (StreamError, *NET_ERRORS) as e:
            self._seg_failures += 1
            log.debug("tv segment %s failed (%d in a row): %s: %s",
                      url, self._seg_failures, type(e).__name__, e)
            if self._seg_failures >= MAX_SEGMENT_FAILURES:
                self._mark_dead("segments")
            if isinstance(e, StreamError):
                raise
            raise StreamError(f"{type(e).__name__}: {e}") from e
        self._seg_failures = 0
        result = (body, ctype)
        if n in self._urls:
            self._cache[n] = result
            self._cache.move_to_end(n)
            while len(self._cache) > SEGMENT_CACHE:
                self._cache.popitem(last=False)
        return result
