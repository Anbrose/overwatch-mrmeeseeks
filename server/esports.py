"""Overwatch 赛事数据：Liquipedia 赛程汇总页（比赛）和 esports.overwatch.com 首页（新闻）。

Liquipedia API 条款（liquipedia.net/api-terms-of-use）：必须 gzip、自定义 User-Agent 带联系方式、
action=parse 每 30 秒最多 1 次、结果尽量缓存、注明来源（CC-BY-SA）。这里每 10 分钟才抓一次。

Liquipedia:Matches 的 HTML 里每场比赛是一个 <div class="match-info">：
  timer-object 的 data-timestamp（Unix 秒），已结束的带 data-finished="finished"
  两个 <span class="name">：有 <a title="全名"> 就取 title，否则是纯文本（TBD）
  scoreholder：未开始是 "vs"，已结束是两个 match-info-header-scoreholder-score；下面是 "(BoN)"
  match-info-tournament-name 里的 <a href="/overwatch/<赛事路径>#<轮次>"><span>显示名</span></a>
"""
from __future__ import annotations

import html as htmllib
import re
from typing import Any
from urllib.parse import unquote

import aiohttp

LIQUIPEDIA = "https://liquipedia.net"
LIQUIPEDIA_API = LIQUIPEDIA + "/overwatch/api.php"
OW_ESPORTS_HOME = "https://esports.overwatch.com/en-us"
USER_AGENT = "mrmeeseeks-discord-bot/1.0 (https://github.com/Anbrose/overwatch-mrmeeseeks)"
OFFICIAL_PREFIXES = ("Overwatch Champions Series/", "Overwatch World Cup/")

_BLOCK = '<div class="match-info">'
_TIMESTAMP = re.compile(r'data-timestamp="(\d+)"')
_NAME = re.compile(r'<span class="name"[^>]*>(.*?)</span>', re.S)
_TITLE = re.compile(r'<a [^>]*title="([^"]+)"')
_SCORE = re.compile(r'match-info-header-scoreholder-score[^"]*">(\d+)<')
_BEST_OF = re.compile(r'\(Bo(\d+)\)')
_TOURNAMENT = re.compile(
    r'class="match-info-tournament-name"><a href="(/overwatch/[^"]+)"[^>]*>\s*<span>(.*?)</span>', re.S)
_NEWS = re.compile(r'<a[^>]*href="(https://esports\.overwatch\.com/en-us/news/[a-z0-9-]+)"[^>]*>(.*?)</a>', re.S)


class EsportsUnavailable(Exception):
    pass


def _team(span: str) -> str:
    m = _TITLE.search(span)
    name = m.group(1) if m else re.sub(r"<[^>]+>", "", span)
    name = htmllib.unescape(name).replace(" (page does not exist)", "").strip()
    return name or "TBD"


def parse_matches(page_html: str) -> list[dict[str, Any]]:
    """Liquipedia:Matches 解析出的全部比赛（不过滤赛事）。解析不全的块直接跳过。"""
    matches = []
    for block in page_html.split(_BLOCK)[1:]:
        ts, tour = _TIMESTAMP.search(block), _TOURNAMENT.search(block)
        names = _NAME.findall(block)
        if not ts or not tour or len(names) < 2:
            continue
        href = htmllib.unescape(tour.group(1))
        path = unquote(href[len("/overwatch/"):]).replace("_", " ")
        team1, team2 = _team(names[0]), _team(names[1])
        scores = [int(s) for s in _SCORE.findall(block)]
        finished = 'data-finished="finished"' in block and len(scores) == 2
        best_of = _BEST_OF.search(block)
        start = int(ts.group(1))
        matches.append({
            "id": f"{path}|{start}|{team1}|{team2}",
            "start": start,
            "tournament": path,
            "label": htmllib.unescape(re.sub(r"<[^>]+>", "", tour.group(2))).strip(),
            "team1": team1,
            "team2": team2,
            "best_of": int(best_of.group(1)) if best_of else None,
            "finished": finished,
            "score": scores if finished else None,
            "url": LIQUIPEDIA + href,
        })
    return matches


def official(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in matches if m["tournament"].startswith(OFFICIAL_PREFIXES)]


def parse_news(page_html: str) -> list[dict[str, Any]]:
    """官方首页上的新闻链接（去重保序）；取不到标题或日期时为 None。"""
    news, seen = [], set()
    for url, body in _NEWS.findall(page_html):
        if url in seen:
            continue
        seen.add(url)
        title = re.search(r"<h2[^>]*>(.*?)</h2>", body, re.S)
        date = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", body)
        news.append({"url": url,
                     "title": htmllib.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip() if title else None,
                     "date": date.group(1) if date else None})
    return news


def _session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
                                 timeout=aiohttp.ClientTimeout(total=30))


async def fetch_matches() -> list[dict[str, Any]]:
    """官方赛事的比赛。页面有内容却一场都解析不出来时视为改版，抛 EsportsUnavailable。"""
    params = {"action": "parse", "page": "Liquipedia:Matches", "prop": "text",
              "format": "json", "formatversion": "2"}
    async with _session() as session, session.get(LIQUIPEDIA_API, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    page_html = (data.get("parse") or {}).get("text") or ""
    matches = parse_matches(page_html)
    if not matches:
        raise EsportsUnavailable(f"no matches parsed from Liquipedia:Matches ({len(page_html)} chars)")
    return official(matches)


async def fetch_news() -> list[dict[str, Any]]:
    async with _session() as session, session.get(OW_ESPORTS_HOME) as resp:
        resp.raise_for_status()
        return parse_news(await resp.text())
