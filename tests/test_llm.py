import asyncio
import importlib
import json

import httpx
import pytest

from app.config import Settings


def load_llm_module():
    try:
        return importlib.import_module("app.llm")
    except ModuleNotFoundError:
        pytest.fail("app.llm 尚未实现")


def configured_settings() -> Settings:
    return Settings(
        openai_base_url="https://llm.example.test/v1",
        openai_api_key="test-key",
        openai_model="test-model",
    )


def test_plan_sends_openai_compatible_request_and_parses_strict_json() -> None:
    module = load_llm_module()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "query_type": "aggregation",
                                    "table_hints": [
                                        "t01_operating_renewable_station"
                                    ],
                                    "metrics": ["grid_capacity_mw"],
                                    "filters": {"energy_type": "风电"},
                                    "group_by": [],
                                    "order_by": [],
                                    "limit": 20,
                                    "requires_clarification": False,
                                    "clarification_question": None,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = module.OpenAIQueryLLM(configured_settings(), async_client)
    plan = asyncio.run(
        client.plan(
            "张北县风电装机容量是多少？",
            ["t01_operating_renewable_station"],
        )
    )
    asyncio.run(async_client.aclose())

    assert captured["url"] == "https://llm.example.test/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-key"
    assert captured["payload"]["model"] == "test-model"
    assert captured["payload"]["temperature"] == 0
    assert plan.query_type == "aggregation"
    assert plan.table_hints == ["t01_operating_renewable_station"]


def test_generate_sql_cleans_markdown_fence() -> None:
    module = load_llm_module()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "```sql\nSELECT COUNT(*) AS total FROM t01_operating_renewable_station\n```"
                        }
                    }
                ]
            },
        )

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = module.OpenAIQueryLLM(configured_settings(), async_client)
    plan = module.QueryPlan(
        query_type="aggregation",
        table_hints=["t01_operating_renewable_station"],
    )
    sql = asyncio.run(client.generate_sql("问题", plan, "受控目录"))
    asyncio.run(async_client.aclose())

    assert sql == "SELECT COUNT(*) AS total FROM t01_operating_renewable_station"


def test_missing_llm_configuration_is_rejected_before_request() -> None:
    module = load_llm_module()
    client = module.OpenAIQueryLLM(
        Settings(
            _env_file=None,
            openai_base_url="",
            openai_api_key="",
            openai_model="",
        )
    )

    with pytest.raises(module.LLMConfigurationError):
        asyncio.run(client.plan("查询装机", ["t01_operating_renewable_station"]))
