"""一起看电视直播：出站请求层（防 SSRF）。

直播中继和探测要请求第三方列表里的任意地址，所以所有出站请求都走这里：
- is_public_ip：只放行公网地址（内网、回环、链路本地、组播、保留地址一律拒绝）。
- FilteringResolver：DNS 解析结果里不通过 ip_allowed 的记录直接丢掉，覆盖所有主机名，
  包括重定向之后才出现的主机名。
- open_url：自己手动跟随重定向，每一跳都检查 scheme；aiohttp 对字面 IP 主机不走解析器，
  所以字面 IP 由这里在每一跳连接之前检查；检查的是 yarl 规范化后的主机，
  主机带非 ASCII 字符（全角数字、中文句号等）直接拒绝。
- read_capped：按上限读取响应体，超出就报 TooLarge。

ip_allowed 参数只给测试连本地 127.0.0.1 用，生产调用方不要传。
"""
import ipaddress
import logging
import urllib.parse
from typing import Callable

import aiohttp
import aiohttp.abc
import yarl

from live_watch import USER_AGENT

log = logging.getLogger("mrmeeseeks.watch")

REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class Blocked(Exception):
    """不允许的 scheme、地址，或重定向次数过多。"""


class TooLarge(Exception):
    """响应体超过上限。"""


def is_public_ip(ip: str) -> bool:
    """公网地址才返回 True；IPv4-mapped IPv6 按 IPv4 判断；解析不了的一律 False。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def quote_url(url: str) -> str:
    """把路径和查询里的非 ASCII 字符、空格做百分号编码，已有的 %xx 不动。"""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=~-._")
    query = urllib.parse.quote(parts.query, safe="/%:@!$&'()*+,;=~-._?")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


class FilteringResolver(aiohttp.abc.AbstractResolver):
    """包一层解析器，丢掉不允许的地址；一条都不剩就当解析失败。"""

    def __init__(self, ip_allowed: Callable[[str], bool],
                 inner: aiohttp.abc.AbstractResolver | None = None):
        self._ip_allowed = ip_allowed
        self._inner = inner or aiohttp.DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        kwargs = {"family": family} if family else {}
        records = await self._inner.resolve(host, port, **kwargs)
        ok = [r for r in records if self._ip_allowed(r["host"])]
        if not ok:
            raise OSError("blocked address")
        return ok

    async def close(self):
        await self._inner.close()


def make_session(ip_allowed: Callable[[str], bool] = is_public_ip) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(resolver=FilteringResolver(ip_allowed), ttl_dns_cache=60)
    return aiohttp.ClientSession(connector=connector)


def _check_url(url: str, ip_allowed: Callable[[str], bool]) -> yarl.URL:
    """检查一跳 URL，返回之后真正交给 aiohttp 的 yarl.URL。

    检查的是 yarl 规范化之后的主机（aiohttp 连接用的正是它）：全角数字、中文句号
    这类 Unicode 写法会被 NFKC/IDNA 规范成字面 IP，只看 urlsplit 的结果会漏掉。
    另外主机里带非 ASCII 字符的 URL 直接拒绝，多一道保险。
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise Blocked(f"scheme not allowed: {parts.scheme!r}")
    if not parts.hostname:
        raise Blocked("no host")
    if not parts.hostname.isascii():
        raise Blocked(f"non-ascii host: {parts.hostname!r}")
    try:
        u = yarl.URL(url)
    except ValueError as e:
        raise Blocked(f"bad url: {e}") from None
    if u.scheme not in ("http", "https"):
        raise Blocked(f"scheme not allowed: {u.scheme!r}")
    host = u.raw_host
    if not host:
        raise Blocked("no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return u  # 主机名：交给 FilteringResolver
    if not ip_allowed(host):
        raise Blocked(f"address not allowed: {host}")
    return u


async def open_url(session: aiohttp.ClientSession, url: str, ua: str | None, *,
                   ip_allowed: Callable[[str], bool] = is_public_ip,
                   timeout: float = 8.0, max_redirects: int = 5) -> aiohttp.ClientResponse:
    """GET 并手动跟随重定向，返回最终响应（调用方负责 release）。"""
    headers = {"User-Agent": ua or USER_AGENT}
    cur = quote_url(url)
    for _ in range(max_redirects + 1):
        target = _check_url(cur, ip_allowed)
        resp = await session.get(target, headers=headers, allow_redirects=False,
                                 timeout=aiohttp.ClientTimeout(total=timeout))
        if resp.status not in REDIRECT_STATUSES:
            return resp
        loc = resp.headers.get("Location")
        resp.release()
        if not loc:
            raise Blocked("redirect without Location")
        cur = quote_url(urllib.parse.urljoin(cur, loc))
    raise Blocked("too many redirects")


async def read_capped(resp: aiohttp.ClientResponse, cap: int) -> bytes:
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        buf += chunk
        if len(buf) > cap:
            raise TooLarge(f"body over {cap} bytes")
    return bytes(buf)
