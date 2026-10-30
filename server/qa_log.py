"""英雄问答的记录和私信汇报。

每次英雄问答（私信和频道里 @ 的都算）追加一行 JSON 到 STATE_DIR/qa_log.jsonl，日后排查答得好不好用；
文件超过 MAX_BYTES 时改名成 qa_log.jsonl.1（只留一份旧的）。私信里的问答和其他指令另外汇报到
QA_REPORT_CHANNEL_ID 频道（汇报文案见 qa_report / dm_report）。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024
REPORT_LIMIT = 2000          # Discord 单条消息上限

# _ask 的结果
ANSWERED = "answered"
COOLDOWN = "cooldown"
ERROR = "error"
NO_API_KEY = "no_api_key"
NOT_READY = "not_ready"
OUTCOME_LABELS = {ANSWERED: "Answer", COOLDOWN: "Rate-limited", ERROR: "Failed",
                  NO_API_KEY: "No API key", NOT_READY: "Hero data not ready"}


class QaLog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def record(self, *, user_id: int, user_name: str, guild_id: int | None, channel_id: int, message_id: int,
               question: str, outcome: str, answer: str, now: datetime | None = None) -> None:
        """追加一行；写不进去只记日志，不影响回复。"""
        entry = {"time": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
                 "where": "guild" if guild_id is not None else "dm",
                 "guild_id": guild_id, "channel_id": channel_id, "message_id": message_id,
                 "user_id": user_id, "user_name": user_name,
                 "question": question, "outcome": outcome, "answer": answer}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size >= MAX_BYTES:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("Could not write the Q&A log %s", self.path)


def _quote(text: str) -> str:
    return "\n".join("> " + line for line in (text.strip() or "(empty)").splitlines())


def qa_report(user_id: int, user_name: str, question: str, outcome: str, answer: str) -> str:
    """私信问答的汇报：谁问的、问题、回答（或没答的原因），超长时截断回答。"""
    head = f"❓ **{user_name}** (<@{user_id}>) asked in DM:\n{_quote(question)}\n"
    if len(head) > REPORT_LIMIT // 2:
        head = head[:REPORT_LIMIT // 2 - 2] + "…\n"
    label = f"**{OUTCOME_LABELS.get(outcome, outcome)}:**\n"
    body = answer.strip()
    room = REPORT_LIMIT - len(head) - len(label)
    if len(body) > room:
        cut = "\n…(truncated; full text in qa_log.jsonl)"
        body = body[:room - len(cut)] + cut
    return head + label + body


def dm_report(user_id: int, user_name: str, text: str) -> str:
    """私信里英雄问答以外的指令：一行，谁、发了什么。"""
    line = " ".join(text.split()) or "(no text)"
    return f"💬 **{user_name}** (<@{user_id}>) in DM: `{line[:300].replace('`', 'ˋ')}`"
