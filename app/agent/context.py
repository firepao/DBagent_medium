from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.agent.contracts import AgentMessage


@dataclass(frozen=True)
class AgentContext:
    messages: list[dict[str, Any]]
    source_sequences: list[int]
    context_hash: str
    estimated_tokens: int


class ContextAssembler:
    """Builds a bounded, model-visible context from durable messages."""

    def __init__(self, *, max_messages: int = 80, max_tokens: int = 12000):
        self.max_messages = max_messages
        self.max_tokens = max_tokens

    def assemble(
        self,
        messages: list[AgentMessage],
        *,
        current_text: str | None = None,
        session_summary: str | None = None,
        snapshot: str | None = None,
    ) -> AgentContext:
        selected = messages[-self.max_messages:]
        output: list[dict[str, Any]] = []
        sequences: list[int] = []
        budget = self.max_tokens
        if session_summary or snapshot:
            context_header = "会话受控上下文。"
            if snapshot:
                context_header += f" 数据目录快照：{snapshot[:64]}。"
            if session_summary:
                context_header += f" 历史摘要：{session_summary[:4000]}"
            output.append({"role": "system", "content": context_header})
            budget -= max(1, len(context_header) // 4)
        for message in selected:
            if message.visibility not in {"model", "user"}:
                continue
            content = message.content[:12000]
            estimate = max(1, len(content) // 4)
            if estimate > budget and output:
                break
            budget -= estimate
            sequences.append(message.sequence)
            if message.role == "tool":
                output.append({"role": "tool", "name": message.tool_name, "status": message.metadata.get("status"), "content": content, "payload": {"error_code": message.metadata.get("error_code")} if message.metadata.get("error_code") else {}})
            else:
                output.append({"role": message.role, "content": content})
        if current_text:
            output.append({"role": "user", "content": current_text[:4000]})
        canonical = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return AgentContext(messages=output, source_sequences=sequences, context_hash=hashlib.sha256(canonical.encode()).hexdigest(), estimated_tokens=sum(max(1, len(str(item)) // 4) for item in output))

    @staticmethod
    def make_summary(messages: list[AgentMessage]) -> str:
        users = [message.content.strip() for message in messages if message.role == "user" and message.content.strip()]
        evidence_ids: list[str] = []
        errors: list[str] = []
        for message in messages:
            for evidence_id in message.metadata.get("evidence_ids", []) if isinstance(message.metadata, dict) else []:
                if evidence_id and evidence_id not in evidence_ids:
                    evidence_ids.append(str(evidence_id))
            code = message.metadata.get("error_code") if isinstance(message.metadata, dict) else None
            if code and code not in errors:
                errors.append(str(code))
        lines = ["会话摘要（仅整理既有消息，不新增事实）："]
        if users:
            lines.append("已提出问题：" + "；".join(users[-4:]))
        if evidence_ids:
            lines.append("已获得 Evidence：" + ", ".join(evidence_ids[-12:]))
        if errors:
            lines.append("曾观察到错误：" + ", ".join(errors[-8:]))
        return "\n".join(lines)
