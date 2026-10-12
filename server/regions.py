"""赛区 -> Discord 频道的映射，赛事推送（esports_feed）和开播通知（live_watch）共用。

用 `@mrmeeseeks region cn` 把当前频道绑定成 cn 赛区的频道。赛区名是自定义的小写单词，
但 Liquipedia 赛事路径会按 REGION_ALIASES 自动归到 cn / asia / korea / japan / pacific / emea / na / world，
所以想让比赛自动分流，就用这些名字。存在 STATE_DIR/regions.json。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile

log = logging.getLogger("mrmeeseeks.regions")

# Liquipedia 赛事路径里的段（小写）-> 赛区名。例如
#   Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season  -> korea, asia
#   Overwatch Champions Series/2026/China/Stage 3/Regular Season       -> cn
#   Overwatch Champions Series/2026/North America/Stage 3              -> na
REGION_ALIASES = {
    "china": "cn",
    "asia": "asia",
    "korea": "korea",
    "japan": "japan",
    "pacific": "pacific",
    "emea": "emea",
    "europe": "emea",
    "north america": "na",
    "na": "na",
}
WORLD = "world"          # 世界总决赛、季中赛等没有赛区的赛事
WORLD_CUP = "owwc"       # Overwatch World Cup
KNOWN_REGIONS = ("cn", "asia", "korea", "japan", "pacific", "emea", "na", WORLD, WORLD_CUP)

_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")


class RegionError(Exception):
    """赛区名不合法，消息可直接展示给用户。"""


def normalize_region(text: str) -> str:
    region = text.strip().lower().lstrip("#")
    if not _REGION_RE.match(region):
        raise RegionError(f"Invalid region name `{text}`. Use letters, digits, `-` or `_` (e.g. cn, asia, emea, na).")
    return region


def region_candidates(tournament: str) -> list[str]:
    """赛事路径 -> 可能的赛区名，具体的在前：韩国联赛是 ['korea', 'asia']，绑定了哪个就发到哪个。

    没有赛区的赛事（世界总决赛、季中赛）是 ['world']；世界杯是 ['owwc', 'world']。
    """
    path = tournament.split("#", 1)[0]
    found: list[str] = []
    for seg in path.split("/"):
        key = REGION_ALIASES.get(seg.strip().lower())
        if key and key not in found:
            found.append(key)
    if found:
        return found[::-1]
    if path.lower().startswith("overwatch world cup"):
        return [WORLD_CUP, WORLD]
    return [WORLD]


class RegionStore:
    """path 为 None 时只存在内存里（测试用）。写文件失败只记日志，内存里照常生效。"""

    def __init__(self, path: str | os.PathLike | None):
        self.path = str(path) if path is not None else None
        self.channels: dict[str, int] = {}

    def load(self) -> None:
        if self.path is None:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.channels = {normalize_region(k): int(v) for k, v in (data.get("channels") or {}).items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, RegionError) as e:
            log.warning("No usable region map at %s (%r)", self.path, e)

    def save(self) -> None:
        if self.path is None:
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"channels": self.channels}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write region map to %s", self.path)

    def set(self, region: str, channel_id: int) -> None:
        self.channels[region] = int(channel_id)
        self.save()

    def remove(self, region: str) -> bool:
        if region not in self.channels:
            return False
        del self.channels[region]
        self.save()
        return True

    def get(self, region: str) -> int | None:
        return self.channels.get(region)

    def resolve(self, candidates: list[str]) -> int | None:
        """按顺序找第一个绑定了频道的赛区；都没绑返回 None（由调用方决定用默认频道还是不发）。"""
        return resolve(self.channels, candidates)


def resolve(channels: dict[str, int], candidates: list[str]) -> int | None:
    for region in candidates:
        if region in channels:
            return channels[region]
    return None
