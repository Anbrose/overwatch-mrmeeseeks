"""一起看电视直播：选清晰度、识别 H.265、探测直播源能不能播。

浏览器基本只能解 H.264，所以：
- choose_variant：丢掉 H.265 的清晰度，在 H.264 里挑不超过 8 Mbps 的最高一档。
- ts_has_hevc：解析 MPEG-TS 的 PAT/PMT，看有没有 stream_type 0x24（HEVC）的流。
- resolve_media：顺着主列表找到真正的媒体列表（最多两层主列表）。
- probe：媒体列表 -> 最后一个分片 -> 至少 1 KB 且不是 H.265，才算可播。
所有请求都走 tv_net.open_url，每一跳都过 ip_allowed。
本模块自带一套 m3u8 小工具，不依赖 vod_playlist（它强制 https）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from dataclasses import dataclass

import aiohttp

from tv_channels import Candidate
from tv_net import Blocked, TooLarge, is_public_ip, open_url, read_capped

log = logging.getLogger("mrmeeseeks.watch")

MAX_VIDEO_BPS = 8_000_000
MIN_SEGMENT_BYTES = 1024
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
SEGMENT_PEEK_BYTES = 64 * 1024
MAX_MASTER_LEVELS = 2
TS_PACKET = 188


@dataclass
class Variant:
    url: str
    bandwidth: int | None
    codecs: str | None


class StreamError(Exception):
    """直播源不能用：不是 m3u8、没有可选的清晰度，或网络出错。"""


def absolutize(ref: str, base: str) -> str:
    """相对地址按 base 补全；"//host/x" 沿用 base 的 scheme（urljoin 的行为）。"""
    return urllib.parse.urljoin(base, ref.strip())


def _attrs(line: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line):
        out[m.group(1)] = m.group(2).strip('"')
    return out


def parse_master(text: str, base: str) -> list[Variant] | None:
    """主列表 -> 清晰度列表；没有 #EXT-X-STREAM-INF 说明是媒体列表，返回 None。"""
    lines = [ln.strip() for ln in text.lstrip("﻿").splitlines()]
    variants: list[Variant] = []
    seen = False
    for i, ln in enumerate(lines):
        if not ln.startswith("#EXT-X-STREAM-INF"):
            continue
        seen = True
        attrs = _attrs(ln.split(":", 1)[1] if ":" in ln else "")
        uri = next((x for x in lines[i + 1:] if x and not x.startswith("#")), None)
        if uri is None:
            continue
        bw = attrs.get("BANDWIDTH")
        variants.append(Variant(absolutize(uri, base),
                                int(bw) if bw and bw.isdigit() else None,
                                attrs.get("CODECS")))
    return variants if seen else None


def choose_variant(variants: list[Variant]) -> Variant | None:
    """只要 H.264；有带宽的取不超过 MAX_VIDEO_BPS 的最高一档，都没有则取第一个没标带宽的。"""
    ok = [v for v in variants if not (v.codecs and re.search(r"hvc1|hev1", v.codecs))]
    fit = [v for v in ok if v.bandwidth is not None and v.bandwidth <= MAX_VIDEO_BPS]
    if fit:
        return max(fit, key=lambda v: v.bandwidth)
    return next((v for v in ok if v.bandwidth is None), None)


def media_sequence(text: str) -> int | None:
    m = re.search(r"#EXT-X-MEDIA-SEQUENCE:\s*(\d+)", text)
    return int(m.group(1)) if m else None


def segment_urls(text: str, base: str) -> list[str]:
    return [absolutize(ln.strip(), base) for ln in text.lstrip("﻿").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _section(data: bytes, pid: int) -> bytes | None:
    """从 PUSI 包开始拼出 pid 上的第一个完整 PSI 段（含 table_id，不含 pointer）。"""
    buf = None
    need = 0
    for off in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
        pkt = data[off:off + TS_PACKET]
        if pkt[0] != 0x47:
            return None
        if ((pkt[1] & 0x1F) << 8 | pkt[2]) != pid:
            continue
        pusi = bool(pkt[1] & 0x40)
        afc = (pkt[3] >> 4) & 3
        if not afc & 1:
            continue  # 没有负载
        i = 4
        if afc & 2:
            i += 1 + pkt[4]
        if i >= TS_PACKET:
            continue
        payload = pkt[i:]
        if pusi:
            ptr = payload[0]
            buf = bytearray(payload[1 + ptr:])
        elif buf is not None:
            buf += payload
        else:
            continue
        if len(buf) >= 3:
            need = 3 + ((buf[1] & 0x0F) << 8 | buf[2])
            if len(buf) >= need:
                return bytes(buf[:need])
    return None


def ts_has_hevc(data: bytes) -> bool:
    """data 是 MPEG-TS 且 PMT 里有 stream_type 0x24 就返回 True；其它情况（含不是 TS）False。"""
    if len(data) < TS_PACKET or data[0] != 0x47:
        return False
    if len(data) >= 2 * TS_PACKET and data[TS_PACKET] != 0x47:
        return False
    pat = _section(data, 0)
    if pat is None or len(pat) < 12:
        return False
    pmt_pids = []
    for i in range(8, len(pat) - 4, 4):
        prog = pat[i] << 8 | pat[i + 1]
        if prog != 0:
            pmt_pids.append((pat[i + 2] & 0x1F) << 8 | pat[i + 3])
    for pid in pmt_pids:
        pmt = _section(data, pid)
        if pmt is None or len(pmt) < 16:
            continue
        i = 12 + ((pmt[10] & 0x0F) << 8 | pmt[11])
        end = len(pmt) - 4
        while i + 5 <= end:
            if pmt[i] == 0x24:
                return True
            i += 5 + ((pmt[i + 3] & 0x0F) << 8 | pmt[i + 4])
    return False


async def _fetch_playlist(session, url, ua, ip_allowed) -> tuple[str, str]:
    resp = await open_url(session, url, ua, ip_allowed=ip_allowed)
    try:
        if resp.status != 200:
            raise StreamError(f"http {resp.status}")
        final = str(resp.url)
        body = await read_capped(resp, MAX_PLAYLIST_BYTES)
    finally:
        resp.release()
    text = body.decode("utf-8-sig", errors="replace")
    if not text.lstrip().startswith("#EXTM3U"):
        raise StreamError("not an m3u8 playlist")
    return final, text


async def resolve_media(session, url: str, ua: str | None, *,
                        ip_allowed=is_public_ip) -> tuple[str, str]:
    """返回 (最终媒体列表地址, 内容)。最多跟两层主列表。"""
    cur = url
    try:
        for _ in range(MAX_MASTER_LEVELS + 1):
            final, text = await _fetch_playlist(session, cur, ua, ip_allowed)
            variants = parse_master(text, final)
            if variants is None:
                return final, text
            best = choose_variant(variants)
            if best is None:
                raise StreamError("no playable variant")
            cur = best.url
    except StreamError:
        raise
    except (Blocked, TooLarge, aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
        raise StreamError(f"{type(e).__name__}: {e}") from e
    raise StreamError("too many master playlist levels")


async def _probe(session, cand: Candidate, ip_allowed) -> bool:
    final, text = await resolve_media(session, cand.url, cand.ua, ip_allowed=ip_allowed)
    segs = segment_urls(text, final)
    if not segs:
        return False
    resp = await open_url(session, segs[-1], cand.ua, ip_allowed=ip_allowed)
    try:
        if resp.status != 200:
            return False
        buf = bytearray()
        async for chunk in resp.content.iter_chunked(16 * 1024):
            buf += chunk
            if len(buf) >= SEGMENT_PEEK_BYTES:
                break
    finally:
        resp.release()
    data = bytes(buf[:SEGMENT_PEEK_BYTES])
    return len(data) >= MIN_SEGMENT_BYTES and not ts_has_hevc(data)


async def probe(session, cand: Candidate, *, ip_allowed=is_public_ip, timeout: float = 8.0) -> bool:
    """这个候选源现在能不能播；任何异常都当作不能（取消除外）。timeout 管整个探测。"""
    try:
        return await asyncio.wait_for(_probe(session, cand, ip_allowed), timeout)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.debug("probe %s failed: %s: %s", cand.url, type(e).__name__, e)
        return False
