from __future__ import annotations

from typing import Any
import re

from pydantic import BaseModel, Field


class AnswerClaim(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(min_length=1, max_length=4)
    fields: list[str] = Field(default_factory=list, max_length=20)


class AnswerDraft(BaseModel):
    type: str = "final_answer"
    text: str = Field(min_length=1, max_length=4000)
    claims: list[AnswerClaim] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=10)


class AnswerVerifier:
    def verify(self, draft: AnswerDraft, *, approved_evidence_ids: set[str], evidence_payloads: dict[str, dict[str, Any]]) -> tuple[bool, str | None]:
        for claim in draft.claims:
            if not set(claim.evidence_ids).issubset(approved_evidence_ids):
                return False, "ANSWER_NOT_GROUNDED"
            for evidence_id in claim.evidence_ids:
                payload = evidence_payloads.get(evidence_id) or {}
                fields = {str(column.get("name")) for column in payload.get("columns", [])}
                if not set(claim.fields).issubset(fields):
                    return False, "ANSWER_FIELD_NOT_IN_EVIDENCE"
                numbers = self._numbers(claim.text)
                if numbers:
                    known = set()
                    for row in payload.get("rows", []):
                        known.update(self._numbers(json_text(row)))
                    known.update(self._numbers(json_text(payload.get("aggregate_profile", {}))))
                    if payload.get("row_count") is not None:
                        known.add(float(payload["row_count"]))
                    if any(number not in known for number in numbers):
                        return False, "ANSWER_NUMERIC_VALUE_NOT_IN_EVIDENCE"
        return True, None

    @staticmethod
    def _numbers(value: str) -> set[float]:
        return {float(item) for item in re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", str(value))}


def json_text(value: Any) -> str:
    return str(value)
