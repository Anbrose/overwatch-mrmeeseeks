"""把 Overwatch fandom Wiki 的英雄页面 wikitext 解析成规范化 dict。纯函数，不联网。

数据来源格式（2026-10 核对）：
- Infobox：{{Infobox character | role = [[Tank]] | sub-role = [[Stalwart]] | health = 250 | armor = 300 | health6v6 = ...}}
- 武器：{{Ability_details | ability_type = Weapon... | shot_type | damage | damage_falloff_range | headshot | pellets | pradius}}
- 补丁：{{ChangelogsTabber | owpvp = {{PatchTableElement|YYYY-MM-DD| 文本 }} ... }}
"""
from __future__ import annotations

import re
from typing import Any

import mwparserfromhell

ROLE_QUEUE_TANK_BONUS = 150  # Wiki「Roles」页：All tanks have 150 more health in Role Queue
_NUM = r"\d+(?:\.\d+)?"
_RANGE = re.compile(rf"^\s*({_NUM})\s*(?:[-–—]\s*({_NUM}))?")


def _name(tpl) -> str:
    return str(tpl.name).strip().replace("_", " ").lower()


def _param(tpl, key: str) -> str | None:
    return str(tpl.get(key).value).strip() if tpl.has(key) else None


def parse_infobox(wikitext: str) -> dict[str, str]:
    """Infobox 用逐行扫描而不是 mwparserfromhell：部分页面（如 Moira）的 infobox 里有不规范标记，
    会让模板解析失败。只取 {{Infobox character 之后、第一个 == 标题之前，每个字段第一次出现的值。"""
    start = wikitext.find("{{Infobox character")
    if start < 0:
        return {}
    fields: dict[str, str] = {}
    for line in wikitext[start:].splitlines()[1:]:
        if line.startswith("=="):
            break
        m = re.match(r"^\|\s*([\w-]+)\s*=\s*(.*)$", line)
        if m and m.group(1) not in fields:
            fields[m.group(1)] = m.group(2).strip()
    return fields


def _resolve_vars(text: str, variables: dict[str, str]) -> str:
    """展开 {{#vardefineecho:name|value}} 为 value，{{#var:name}} 为已定义的值。value 里可能还有嵌套模板。"""
    code = mwparserfromhell.parse(text)
    for tpl in code.filter_templates(recursive=False):
        fn, _, key = str(tpl.name).strip().partition(":")
        fn, key = fn.strip().lower(), key.strip()
        if fn in ("#vardefineecho", "#vardefine"):
            value = str(tpl.params[0].value).strip() if tpl.params else ""
            variables[key] = value
            code.replace(tpl, value if fn == "#vardefineecho" else "")
        elif fn == "#var":
            code.replace(tpl, variables.get(key, ""))
    return str(code)


def _plain(text: str) -> str:
    """去掉注释，{{tt|显示|提示}} 取显示值，其他模板和链接取纯文本。"""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"\{\{tt\|([^|{}]*)\|[^{}]*\}\}", r"\1", text)
    text = re.sub(r"\{\{(?:al|proj)\|([^|{}]*)[^{}]*\}\}", r"\1", text)
    return mwparserfromhell.parse(text).strip_code().strip()


def _segments(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"<br\s*/?>", text) if s.strip()]


def _int(text: str | None) -> int | None:
    if text is None:
        return None
    m = re.match(rf"\s*({_NUM})\s*$", _plain(text))
    return int(float(m.group(1))) if m else None


def parse_damage(text: str) -> list[dict[str, Any]]:
    """'70 - 21' / '{{tt|45|125 total}} (direct hit)<br>80 - 10 (splash, enemy)' -> 变体列表。"""
    variants = []
    for seg in _segments(text):
        plain = _plain(seg)
        m = _RANGE.match(plain)
        if not m:
            continue
        hi = float(m.group(1))
        lo = float(m.group(2)) if m.group(2) else hi
        label = plain[m.end():].strip().strip("()").strip()
        variants.append({"label": label, "max": hi, "min": lo})
    return variants


def parse_falloff(text: str | None) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    segs = _segments(text)
    m = _RANGE.match(_plain(segs[0])) if segs else None
    if not m or not m.group(2):
        return None, None
    return float(m.group(1)), float(m.group(2))


def _shot_type(text: str) -> str:
    kinds = re.findall(r"\{\{proj\|([^|}]+)", text)
    return "+".join(k.strip().lower() for k in kinds) or _plain(text).lower()


def _weapon(tpl, variables: dict[str, str]) -> dict[str, Any]:
    raw = {str(p.name).strip(): _resolve_vars(str(p.value).strip(), variables) for p in tpl.params}
    falloff_start, falloff_end = parse_falloff(raw.get("damage_falloff_range"))
    headshot = _plain(raw.get("headshot", ""))
    pr = _RANGE.match(_plain(raw.get("pradius", "")))
    return {
        "name": _plain(raw.get("ability_name", "")),
        "fire": (_plain(raw.get("ability_type", "")).split(";;") + [""])[1] or None,
        "shot_type": _shot_type(raw.get("shot_type", "")),
        "damage": parse_damage(raw.get("damage", "")),
        "falloff_start": falloff_start,
        "falloff_end": falloff_end,
        "headshot": headshot.startswith(("✓", "yes", "Yes")),
        "pellets": _int(raw.get("pellets")) or 1,
        "projectile_radius": float(pr.group(1)) if pr else None,
        "raw": {k: _plain(v) for k, v in raw.items()
                if k in ("damage", "damage_falloff_range", "headshot", "pellets", "pradius", "shot_type")},
    }


def parse_patches(code) -> list[dict[str, str]]:
    for tpl in code.filter_templates(recursive=False):
        if _name(tpl) == "changelogstabber" and tpl.has("owpvp"):
            out = []
            for pte in tpl.get("owpvp").value.filter_templates(recursive=False):
                if _name(pte) == "patchtableelement" and len(pte.params) >= 2:
                    date = str(pte.params[0].value).strip()
                    body = str(pte.params[1].value)
                    body = re.sub(r"\{\{DevComment\|(.*?)\}\}", r"Developer comment: \1", body, flags=re.S)
                    body = re.sub(r"^\*+\s*", "- ", body, flags=re.M)
                    text = _plain(body)
                    text = re.sub(r"\n{2,}", "\n", text).strip()
                    out.append({"date": date, "text": text})
            return sorted(out, key=lambda p: p["date"], reverse=True)
    return []


def parse_hero(title: str, wikitext: str) -> dict[str, Any] | None:
    """解析一个英雄页面；不是英雄页面（没有 Infobox character 或没有 role）时返回 None。"""
    infobox = parse_infobox(wikitext)
    if not infobox.get("role"):
        return None
    code = mwparserfromhell.parse(wikitext)
    role = _plain(infobox["role"])
    subrole = _plain(infobox.get("sub-role", "")) or None

    def pool(suffix: str = "") -> dict[str, int | None]:
        base = {k: _int(infobox.get(k)) for k in ("health", "armor", "shield")}
        over = {k: _int(infobox.get(k + suffix)) for k in ("health", "armor", "shield")} if suffix else {}
        merged = {k: (over.get(k) if over.get(k) is not None else base[k]) for k in base}
        return {k: (v if v is not None else (0 if k != "health" else None)) for k, v in merged.items()}

    open_queue = pool()
    role_queue = dict(open_queue)
    if role == "Tank" and role_queue["health"] is not None:
        role_queue["health"] += ROLE_QUEUE_TANK_BONUS

    variables: dict[str, str] = {}
    weapons = []
    for tpl in code.filter_templates():
        if _name(tpl) != "ability details":
            continue
        # 按页面顺序处理，让 {{#var:}} 能引用前面 {{#vardefineecho:}} 定义的值
        ability_type = _param(tpl, "ability_type") or ""
        if ability_type.strip().startswith("Weapon"):
            weapons.append(_weapon(tpl, variables))
        else:
            for p in tpl.params:
                _resolve_vars(str(p.value), variables)

    return {
        "name": title,
        "role": role,
        "subrole": subrole,
        "hp": {"role_queue": role_queue, "open_queue": open_queue, "6v6": pool("6v6")},
        "weapons": weapons,
        "patches": parse_patches(code),
        "source_url": "https://overwatch.fandom.com/wiki/" + title.replace(" ", "_"),
    }
