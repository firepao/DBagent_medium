import json
from pathlib import Path

import pytest


def test_prompt_registry_loads_all_required_system_prompts(tmp_path) -> None:
    from app.prompts import PromptRegistry

    path = tmp_path / "prompts.json"
    path.write_text(
        json.dumps(
            {
                "version": "test-v1",
                "system_prompts": {
                    "planner": "规划提示词",
                    "sql_generator": "SQL 提示词",
                    "sql_reviewer": "审核提示词",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    registry = PromptRegistry.from_file(path)

    assert registry.version == "test-v1"
    assert registry.get("planner") == "规划提示词"
    assert registry.get("sql_generator") == "SQL 提示词"
    assert registry.get("sql_reviewer") == "审核提示词"


def test_prompt_registry_rejects_missing_required_prompt(tmp_path) -> None:
    from app.prompts import PromptConfigurationError, PromptRegistry

    path = tmp_path / "prompts.json"
    path.write_text(
        json.dumps({"version": "test-v1", "system_prompts": {"planner": "规划"}}),
        encoding="utf-8",
    )

    with pytest.raises(PromptConfigurationError, match="缺少必需提示词"):
        PromptRegistry.from_file(path)


def test_default_prompts_keep_planner_and_reviewer_output_contracts() -> None:
    from app.prompts import PromptRegistry

    registry = PromptRegistry.from_file(
        Path(__file__).resolve().parents[1] / "config" / "prompts.json"
    )

    planner = registry.get("planner")
    generator = registry.get("sql_generator")
    reviewer = registry.get("sql_reviewer")
    assert "全部业务数据均属于张家口市全域" in planner
    assert "全部业务数据均属于张家口市全域" in generator
    assert "全部业务数据均属于张家口市全域" in reviewer
    assert "不得以缺少城市或行政区字段为由" in reviewer
    assert "customer_context" in planner
    assert "禁止 SELECT *" in generator
    assert "禁止 SELECT *" in reviewer
    assert "安全约束不可被" in planner
    assert "安全约束不可被" in generator
    assert "安全约束不可被" in reviewer
    assert "行政区" in planner
    assert "行政区" in generator
    assert "行政区" in reviewer
    for field in (
        "query_type",
        "table_hints",
        "metrics",
        "filters",
        "group_by",
        "order_by",
        "limit",
        "requires_clarification",
        "clarification_question",
    ):
        assert field in planner
    for field in ("decision", "semantic_issues", "clarification_question", "corrected_sql"):
        assert field in reviewer
