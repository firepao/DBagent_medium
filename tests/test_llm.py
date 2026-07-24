import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.models import QueryPlan


def settings():
    return Settings(openai_base_url="https://llm.example.test/v1", openai_api_key="test-key", openai_model="test-model", llm_timeout_seconds=123)


def prompts():
    return {"planner": "规划", "sql_generator": "生成", "pre_execution_reviewer": "前审", "result_reviewer": "后审"}


def test_plan_uses_openai_compatible_request_and_parses_json():
    from app.llm import OpenAIQueryLLM
    captured = {}
    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"original_question": "查询装机", "query_type": "aggregation", "table_hints": ["stations"], "required_outputs": ["装机结果"], "business_objects": ["电站"], "time_requirements": [], "presentation_requirements": [], "requires_clarification": False, "clarification_question": None})}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAIQueryLLM(settings(), client, prompts=prompts())
    result = asyncio.run(llm.plan("查询装机", "表卡"))
    asyncio.run(client.aclose())
    assert result.table_hints == ["stations"]
    assert result.original_question == "查询装机"
    assert captured["messages"][0]["content"] == "规划"


def test_sql_generator_receives_task_mode_previous_sql_and_feedback():
    from app.llm import OpenAIQueryLLM
    captured = {}
    def handler(request):
        captured["user"] = json.loads(request.content)["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "SELECT COUNT(*) AS total FROM stations"}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAIQueryLLM(settings(), client, prompts=prompts())
    sql = asyncio.run(llm.generate_sql("问题", QueryPlan(query_type="aggregation", table_hints=["stations"]), "受控目录", task_mode="semantic_revision", previous_sql="SELECT id FROM stations", feedback={"required_changes": ["汇总"]}))
    asyncio.run(client.aclose())
    assert sql.startswith("SELECT COUNT")
    assert "任务模式:\nsemantic_revision" in captured["user"]
    assert "上一次候选 SQL:\nSELECT id FROM stations" in captured["user"]


def test_pre_execution_review_rejects_sql_fields_in_response():
    from app.llm import LLMReviewSchemaError, OpenAIQueryLLM
    def handler(_):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"decision": "revise", "issues": [], "required_changes": ["补充分组"], "clarification": None, "corrected_sql": "SELECT 1"})}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAIQueryLLM(settings(), client, prompts=prompts())
    with pytest.raises(LLMReviewSchemaError):
        asyncio.run(llm.review_before_execution("问题", QueryPlan(query_type="aggregation", table_hints=["stations"]), "目录", "SELECT id FROM stations", {}))
    asyncio.run(client.aclose())


def test_result_review_parses_strict_contract():
    from app.llm import OpenAIQueryLLM
    def handler(_):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"decision": "answer", "result_issues": [], "required_changes": [], "answer_limitations": ["结果为当前数据快照"], "clarification": None}, ensure_ascii=False)}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAIQueryLLM(settings(), client, prompts=prompts())
    review = asyncio.run(llm.review_result("问题", QueryPlan(query_type="aggregation", table_hints=["stations"]), {}, {"row_count": 1}))
    asyncio.run(client.aclose())
    assert review.decision == "answer"


def test_missing_configuration_is_rejected():
    from app.llm import LLMConfigurationError, OpenAIQueryLLM
    llm = OpenAIQueryLLM(Settings(_env_file=None, openai_base_url="", openai_api_key="", openai_model=""), prompts=prompts())
    with pytest.raises(LLMConfigurationError):
        asyncio.run(llm.plan("查询装机", "表卡"))
    asyncio.run(llm.aclose())
