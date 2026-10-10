"""技能默认键位（PC）：补丁摘要里在技能名后面标出 `Shift`、`E` 这样的键位，方便认出是哪个技能。

查找顺序（先找到先用）：
  1. keybinds.json 手动标注（其他来源都没有、或者有错时用）
  2. 补丁原文写明的新技能键位（"New ability assigned to Secondary Fire by default."）
  3. 官方技能名自带的（"Scrap Gun – Primary Fire"）
  4. Wiki 英雄数据里的键位；Wiki 没写时，武器按左键、终极技能按 Q、被动写 Passive
都找不到就不标，不猜。威能没有键位。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

import patchnotes
from herodata import _norm

MANUAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keybinds.json")

# 各种写法 -> 显示的键位
LABELS = {
    "primary fire": "LMB", "hip fire": "LMB", "lmb": "LMB",
    "secondary fire": "RMB", "rmb": "RMB",
    "ability 1": "Shift", "shift": "Shift",
    "ability 2": "E", "e": "E",
    "ultimate": "Q", "q": "Q",
    "passive": "Passive",
    "jump": "Space", "space": "Space",
    "reload": "R", "r": "R",
    "crouch": "Ctrl", "ctrl": "Ctrl",
}
_FIRE_SUFFIX = re.compile(r"(?:–|-|——)\s*(Primary Fire|Secondary Fire)\s*$", re.I)


def label(key: str | None) -> str | None:
    return LABELS.get(key.strip().lower()) if key else None


def name_key(name: str) -> str:
    """忽略大小写、标点和末尾括号：'Reconstruction (Passive)' 和 'reconstruction' 相同。"""
    return _norm(re.sub(r"\s*\([^)]*\)\s*$", "", name))


def load_manual(path: str = MANUAL_PATH) -> dict[str, dict[str, str]]:
    """keybinds.json：{"英雄": {"技能": "Shift"}}；以 _ 开头的键是注释。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {hero: {k: v for k, v in abilities.items() if not k.startswith("_")}
            for hero, abilities in data.items() if not hero.startswith("_") and isinstance(abilities, dict)}


class KeyBinds:
    def __init__(self, store=None, manual: dict[str, dict[str, str]] | None = None):
        self.store = store            # HeroStore（Wiki 数据），可为 None
        self.manual = manual or {}

    def _hero_name(self, hero: str) -> str:
        if self.store is not None:
            found = self.store.resolve(hero)
            if isinstance(found, dict):
                return found["name"]
        return hero

    def _wiki(self, hero: str) -> dict[str, str]:
        """{名称键: 键位}，来自 Wiki 的技能列表。"""
        if self.store is None:
            return {}
        found = self.store.resolve(hero)
        if not isinstance(found, dict):
            return {}
        keys = {}
        for ability in found.get("abilities", []):
            key = label(ability.get("key")) or {"weapon": "LMB", "ultimate": "Q", "passive": "Passive"}.get(
                ability.get("type"))
            if key:
                keys.setdefault(name_key(ability["name"]), key)
        return keys

    def for_patch(self, patch: dict[str, Any]) -> Callable[[str, str], str | None]:
        """返回 key(英雄, 技能名) -> 键位或 None。"""
        stated = patchnotes.stated_keys(patch)
        manual = {name_key(h): {name_key(a): k for a, k in abilities.items()} for h, abilities in self.manual.items()}
        cache: dict[str, dict[str, str]] = {}

        def key(hero: str, name: str) -> str | None:
            k = name_key(name)
            canonical = self._hero_name(hero)
            for h in dict.fromkeys((hero, canonical)):
                found = label(manual.get(name_key(h), {}).get(k))
                if found:
                    return found
                found = label(next((v for a, v in stated.get(h, {}).items() if name_key(a) == k), None))
                if found:
                    return found
            m = _FIRE_SUFFIX.search(name)
            if m:
                return label(m.group(1))
            if canonical not in cache:
                cache[canonical] = self._wiki(canonical)
            return cache[canonical].get(k)

        return key

    def missing(self) -> list[tuple[str, str]]:
        """Wiki 里没写键位、按类型也推不出来、也没有手动标注的技能：需要人来标。"""
        if self.store is None:
            return []
        manual = {name_key(h): {name_key(a) for a in abilities} for h, abilities in self.manual.items()}
        out = []
        for hero in self.store.heroes.values():
            for ability in hero.get("abilities", []):
                if label(ability.get("key")) or ability.get("type") in ("weapon", "ultimate", "passive"):
                    continue
                if name_key(ability["name"]) not in manual.get(name_key(hero["name"]), set()):
                    out.append((hero["name"], ability["name"]))
        return out
