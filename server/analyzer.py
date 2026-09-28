"""截图分析：两步走，保证建议只基于"识别出的事实"。

第 1 步 extract：只看图，输出结构化事实（地图/模式/攻防/阶段/双方阵容 + 置信度），看不清就填 null。
第 2 步 advise：不给模型看图，只给第 1 步的事实 JSON（以及以后接入的队友生涯数据、
         地图分段对照表），要求每条建议注明依据，数据不足就直说。
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from anthropic import AsyncAnthropic

EXTRACT_PROMPT = """You are an Overwatch screen reader. You will receive two screenshots:
- Image 1: the full screen while the player holds Tab (scoreboard)
- Image 2: the top of the screen after Tab is released (objective/progress area)

Report only what you **directly see** on screen; do not guess. Set any unclear or uncertain field to null and put the reason in unreadable.
confidence is 0~1: how sure you are that the field is read correctly.

Output a single JSON object and nothing else, in this format:
{
  "map": {"name": map name or null, "confidence": 0~1},
  "mode": "Escort" | "Hybrid" | "Push" | "Control" | "Other" | null,
  "side": "Attack" | "Defense" | null,
  "segment": {
    "checkpoint": checkpoint / sub-map number (integer) or null,
    "progress": progress description (e.g. "~40%") or null,
    "detail": anything else seen on the progress bar, or null,
    "confidence": 0~1
  },
  "allies":  [{"player": name or null, "hero": hero name or null, "role": "Tank"|"Damage"|"Support"|null, "confidence": 0~1}],
  "enemies": [{"player": name or null, "hero": hero name or null, "role": "Tank"|"Damage"|"Support"|null, "confidence": 0~1}],
  "unreadable": ["what could not be read, and why"]
}
Use the official English names for heroes and maps."""

ADVISE_SYSTEM = """You are mrmeeseeks, an Overwatch hero-swap advisor.
Rules:
1. Use only the data provided below. Do not add facts that are not in the data.
2. Treat any field with confidence below 0.6 as unknown.
3. After each suggestion, cite its basis in parentheses, e.g. (basis: enemy comp Winston + Tracer).
4. Teammates' best heroes may only come from "teammate_career_data"; if it is not provided, do not assume.
5. If a counter relationship comes only from general knowledge and is not backed by "patch_data", mark it "(general counter, not verified against current patch data)".
6. If key information is missing, answer "Not enough data to give advice" and say what is missing.
Output format (English, at most 150 words):
First line: a one-sentence conclusion (who should swap to what, or stay as is)
Then at most 3 reasons, one per line, each starting with "- "."""


def _image_block(data: bytes) -> dict[str, Any]:
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.b64encode(data).decode()}}


# 模型默认开启 adaptive thinking，思考也计入 max_tokens；给小了会只剩 thinking 块、没有正文
MAX_TOKENS = 16000


def _text(resp: Any) -> str:
    if resp.stop_reason == "max_tokens":
        raise ValueError(f"model output truncated at max_tokens={MAX_TOKENS}")
    return "".join(b.text for b in resp.content if b.type == "text")


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        raise ValueError("model did not return JSON")
    return json.loads(match.group(0))


class Analyzer:
    def __init__(self, api_key: str, model: str):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model

    async def extract(self, scoreboard: bytes, hud: bytes) -> dict[str, Any]:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "Image 1 (scoreboard while holding Tab):"},
                _image_block(scoreboard),
                {"type": "text", "text": "Image 2 (top progress area after releasing Tab):"},
                _image_block(hud),
                {"type": "text", "text": EXTRACT_PROMPT},
            ]}],
        )
        return _parse_json(_text(resp))

    async def advise(self, facts: dict[str, Any], context: dict[str, Any]) -> str:
        payload = {"screen_facts": facts, **context}
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=ADVISE_SYSTEM,
            messages=[{"role": "user", "content":
                       "Data:\n" + json.dumps(payload, ensure_ascii=False, indent=2)}],
        )
        return _text(resp).strip()

    async def analyze(self, scoreboard: bytes, hud: bytes) -> tuple[dict[str, Any], str]:
        facts = await self.extract(scoreboard, hud)
        # 以后在这里接入：OverFast 队友生涯数据、地图分段对照表、当前版本英雄数据
        context = {
            "teammate_career_data": "not available yet",
            "map_segment_reference": "not available yet",
            "patch_data": "not available yet",
        }
        advice = await self.advise(facts, context)
        return facts, advice


def format_facts(facts: dict[str, Any]) -> str:
    """把识别结果压成一行，贴在 Discord 建议上方，方便核对模型看到了什么。"""
    def val(x: Any) -> str:
        return "?" if x in (None, "") else str(x)

    m = facts.get("map") or {}
    seg = facts.get("segment") or {}
    enemies = [e.get("hero") or "?" for e in facts.get("enemies") or []]
    allies = [a.get("hero") or "?" for a in facts.get("allies") or []]
    parts = [
        f"Map {val(m.get('name'))}",
        f"{val(facts.get('mode'))}/{val(facts.get('side'))}",
        f"Checkpoint {val(seg.get('checkpoint'))} {val(seg.get('progress'))}",
        f"Allies {', '.join(allies) or '?'}",
        f"Enemies {', '.join(enemies) or '?'}",
    ]
    return " | ".join(parts)
