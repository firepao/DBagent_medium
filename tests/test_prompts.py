import json
from pathlib import Path

import pytest


def valid_prompts():
    return {"planner": "规划", "sql_generator": "生成", "pre_execution_reviewer": "前审", "result_reviewer": "后审"}


def test_prompt_registry_loads_all_required_prompts(tmp_path):
    from app.prompts import PromptRegistry
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps({"version": "test-v1", "system_prompts": valid_prompts()}, ensure_ascii=False), encoding="utf-8")
    registry = PromptRegistry.from_file(path)
    assert registry.version == "test-v1"
    assert registry.get("pre_execution_reviewer") == "前审"
    assert registry.get("result_reviewer") == "后审"


def test_prompt_registry_rejects_missing_required_prompt(tmp_path):
    from app.prompts import PromptConfigurationError, PromptRegistry
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps({"version": "test-v1", "system_prompts": {"planner": "规划"}}), encoding="utf-8")
    with pytest.raises(PromptConfigurationError, match="缺少必需提示词"):
        PromptRegistry.from_file(path)


def test_default_prompts_define_two_non_generating_reviewers():
    from app.prompts import PromptRegistry
    registry = PromptRegistry.from_file(Path(__file__).resolve().parents[1] / "config" / "prompts.json")
    planner = registry.get("planner")
    generator = registry.get("sql_generator")
    pre = registry.get("pre_execution_reviewer")
    result = registry.get("result_reviewer")
    for prompt in (planner, generator, pre, result):
        assert "全部业务数据均属于张家口市全域" in prompt
    assert "corrected_sql" in pre
    assert "禁止输出" in pre
    assert "不生成、不改写" in result
    assert "guard_repair" in generator
    assert "对象范围" in planner
    assert "object_scope" in planner
    assert "对象范围用于选表" in generator
    assert "对象范围词只能选表" in pre
    assert "authoritative_data_as_of" in pre
    assert "ResultEvidence.data_as_of 非空" in result
    assert "只有用户要求逐条记录时间或按时间分组时才需要时间列" in result
