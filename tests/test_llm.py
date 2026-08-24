import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.models import QueryPlan
from app.llm_providers import LLMProvider, LLMProviderPool


def settings():
    return Settings(_env_file=None, openai_base_url="https://llm.example.test/v1", openai_api_key="test-key", openai_model="test-model", llm_timeout_seconds=123)


def prompts():
    return {"planner": "规划", "sql_generator": "生成", "pre_execution_reviewer": "前审", "result_reviewer": "后审"}


def provider_pool():
    return LLMProviderPool(
        [
            LLMProvider("primary", "https://primary.test/v1", "key-1", "model-1", 1),
            LLMProvider("fallback", "https://fallback.test/v1", "key-2", "model-2", 2),
        ],
        {},
        failure_threshold=3,
        cooldown_seconds=60,
        max_attempts=2,
    )


def test_plan_uses_openai_compatible_request_and_parses_json():
    from app.llm import OpenAIQueryLLM
    captured = {}
    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"original_question": "查询装机", "query_type": "aggregation", "table_hints": ["stations"], "required_outputs": ["装机结果"], "business_objects": ["电站"], "time_requirements": [], "presentation_requirements": [], "requires_clarification": False, "clarification_question": None})}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAIQueryLLM(settings(), client, prompts=prompts())
    async def invoke():
        result = await llm.plan("查询装机", "表卡")
        return result, llm.last_call_metadata

    result, metadata = asyncio.run(invoke())
    asyncio.run(client.aclose())
    assert result.table_hints == ["stations"]
    assert result.original_question == "查询装机"
    assert captured["messages"][0]["content"] == "规划"
    assert "thinking" not in captured


def test_reasoning_false_sends_deepseek_thinking_disabled():
    from app.llm import OpenAIQueryLLM

    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "SELECT 1"}}]},
        )

    pool = LLMProviderPool(
        [
            LLMProvider(
                "primary",
                "https://primary.test/v1",
                "key-1",
                "deepseek-v4-flash",
                1,
                reasoning=False,
            )
        ],
        {},
        failure_threshold=3,
        cooldown_seconds=60,
        max_attempts=1,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAIQueryLLM(
        settings(), client, prompts=prompts(), provider_pool=pool
    )
    sql = asyncio.run(
        llm.generate_sql(
            "问题", QueryPlan(query_type="detail", table_hints=["stations"]), "目录"
        )
    )
    asyncio.run(client.aclose())

    assert sql == "SELECT 1"
    assert captured["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in captured


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
    with pytest.raises(LLMConfigurationError):
        OpenAIQueryLLM(Settings(_env_file=None, openai_base_url="", openai_api_key="", openai_model=""), prompts=prompts())


def test_http_failure_falls_back_to_next_provider():
    from app.llm import OpenAIQueryLLM

    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"original_question": "查询装机", "query_type": "aggregation", "table_hints": ["stations"], "required_outputs": ["装机结果"], "business_objects": ["电站"], "time_requirements": [], "presentation_requirements": [], "requires_clarification": False, "clarification_question": None})}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAIQueryLLM(
        Settings(_env_file=None, openai_base_url="x", openai_api_key="x", openai_model="x", llm_provider_retry_count=0),
        client,
        prompts=prompts(),
        provider_pool=provider_pool(),
    )
    async def invoke():
        result = await llm.plan("查询装机", "表卡")
        return result, llm.last_call_metadata

    result, metadata = asyncio.run(invoke())
    asyncio.run(client.aclose())
    assert result.table_hints == ["stations"]
    assert calls == ["primary.test", "fallback.test"]
    assert metadata == {
        "model": "model-2",
        "provider": "fallback",
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }


def test_successful_provider_exposes_real_usage_metadata():
    from app.llm import OpenAIQueryLLM

    def handler(_):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "SELECT 1"}}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAIQueryLLM(
        settings(), client, prompts=prompts(), provider_pool=provider_pool()
    )
    async def invoke():
        await llm.generate_sql(
            "问题", QueryPlan(query_type="detail", table_hints=["stations"]), "目录"
        )
        return llm.last_call_metadata

    metadata = asyncio.run(invoke())
    asyncio.run(client.aclose())
    assert metadata == {
        "model": "model-1",
        "provider": "primary",
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
    }


def test_empty_content_falls_back_to_next_provider():
    from app.llm import OpenAIQueryLLM

    def handler(request):
        if request.url.host == "primary.test":
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "SELECT 1"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAIQueryLLM(
        Settings(_env_file=None, openai_base_url="x", openai_api_key="x", openai_model="x", llm_provider_retry_count=0),
        client,
        prompts=prompts(),
        provider_pool=provider_pool(),
    )
    sql = asyncio.run(
        llm.generate_sql(
            "问题",
            QueryPlan(query_type="detail", table_hints=["stations"]),
            "目录",
        )
    )
    asyncio.run(client.aclose())
    assert sql == "SELECT 1"
