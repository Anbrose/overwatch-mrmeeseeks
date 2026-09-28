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

EXTRACT_PROMPT = """你是守望先锋画面识别器。你会收到两张截图：
- 图1：玩家按住 Tab 时的全屏画面（计分板）
- 图2：松开 Tab 后屏幕顶部的目标/进度区域

只报告你在画面上**直接看到**的信息，不要推测。看不清或不确定的字段填 null，并把原因写进 unreadable。
confidence 取 0~1，表示你对该字段识别正确的把握。

只输出一个 JSON 对象，不要任何其他文字，格式：
{
  "map": {"name": 地图名或null, "confidence": 0~1},
  "mode": "运载" | "混合" | "推进" | "占领" | "其他" | null,
  "side": "进攻" | "防守" | null,
  "segment": {
    "checkpoint": 第几个检查点/子地图(整数)或null,
    "progress": 进度描述(如"约40%")或null,
    "detail": 进度条上看到的其他信息或null,
    "confidence": 0~1
  },
  "allies":  [{"player": 名字或null, "hero": 英雄名或null, "role": "坦克"|"输出"|"支援"|null, "confidence": 0~1}],
  "enemies": [{"player": 名字或null, "hero": 英雄名或null, "role": "坦克"|"输出"|"支援"|null, "confidence": 0~1}],
  "unreadable": ["无法识别的内容及原因"]
}
英雄名和地图名使用简体中文官方译名。"""

ADVISE_SYSTEM = """你是 mrmeeseeks，一个守望先锋换英雄参谋。
规则：
1. 只能使用下面给出的数据做判断，不得补充数据里没有的事实。
2. confidence 低于 0.6 的字段视为未知。
3. 每条建议后用括号注明依据，例如（依据：敌方阵容 温斯顿+猎空）。
4. 队友擅长英雄只能引用"队友生涯数据"里提供的内容；没提供就不要假设。
5. 英雄克制关系如果只来自通用知识，而没有"版本数据"支撑，标注"（通用克制，未经当前版本数据验证）"。
6. 关键信息不足时，直接回答"数据不足，无法给出建议"，并说明缺什么。
输出格式（简体中文，总长不超过 500 字）：
第一行：一句话结论（建议谁换成什么，或维持现状）
然后最多 3 条理由，每条一行，以"- "开头。"""


def _image_block(data: bytes) -> dict[str, Any]:
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.b64encode(data).decode()}}


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        raise ValueError("模型没有返回 JSON")
    return json.loads(match.group(0))


class Analyzer:
    def __init__(self, api_key: str, model: str):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model

    async def extract(self, scoreboard: bytes, hud: bytes) -> dict[str, Any]:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=1500,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "图1（按住 Tab 的计分板）："},
                _image_block(scoreboard),
                {"type": "text", "text": "图2（松开 Tab 后的顶部进度区域）："},
                _image_block(hud),
                {"type": "text", "text": EXTRACT_PROMPT},
            ]}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        return _parse_json(text)

    async def advise(self, facts: dict[str, Any], context: dict[str, Any]) -> str:
        payload = {"画面识别结果": facts, **context}
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=800,
            system=ADVISE_SYSTEM,
            messages=[{"role": "user", "content":
                       "数据如下：\n" + json.dumps(payload, ensure_ascii=False, indent=2)}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    async def analyze(self, scoreboard: bytes, hud: bytes) -> tuple[dict[str, Any], str]:
        facts = await self.extract(scoreboard, hud)
        # 以后在这里接入：OverFast 队友生涯数据、地图分段对照表、当前版本英雄数据
        context = {
            "队友生涯数据": "暂未接入",
            "地图分段对照": "暂未接入",
            "版本数据": "暂未接入",
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
        f"地图 {val(m.get('name'))}",
        f"{val(facts.get('mode'))}/{val(facts.get('side'))}",
        f"第{val(seg.get('checkpoint'))}段 {val(seg.get('progress'))}",
        f"我方 {'、'.join(allies) or '?'}",
        f"敌方 {'、'.join(enemies) or '?'}",
    ]
    return " ｜ ".join(parts)
