import json

import asyncio
import httpx

from app.config import Settings
from app.llm import OpenAIQueryLLM
from app.llm_trace import LLMTraceRepository, llm_trace_context


def test_trace_repository_records_raw_model_output_by_request_and_stage(tmp_path) -> None:
    path = tmp_path / "llm_trace.jsonl"
    repository = LLMTraceRepository(path)

    with llm_trace_context("qry_test", "planning"):
        repository.record_output("gpt-test", '{"query_type":"aggregation"}')

    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["request_id"] == "qry_test"
    assert entry["stage"] == "planning"
    assert entry["model"] == "gpt-test"
    assert entry["output"] == '{"query_type":"aggregation"}'
    assert "question" not in entry


def test_llm_client_writes_planning_output_to_server_trace(tmp_path) -> None:
    path = tmp_path / "llm_trace.jsonl"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "query_type": "list",
                                    "table_hints": ["stations"],
                                    "metrics": [],
                                    "filters": {},
                                    "group_by": [],
                                    "order_by": [],
                                    "limit": 20,
                                    "requires_clarification": False,
                                    "clarification_question": None,
                                }
                            )
                        }
                    }
                ]
            },
        )

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIQueryLLM(
        Settings(
            openai_base_url="https://llm.example.test/v1",
            openai_api_key="test-key",
            openai_model="test-model",
        ),
        async_client,
        prompts={
            "planner": "测试规划提示词",
            "sql_generator": "测试 SQL 提示词",
            "sql_reviewer": "测试审核提示词",
        },
        trace_repository=LLMTraceRepository(path),
    )

    async def invoke() -> None:
        with llm_trace_context("qry_trace", "planning"):
            await client.plan("问题", "表卡")
        await async_client.aclose()

    asyncio.run(invoke())

    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["request_id"] == "qry_trace"
    assert entry["stage"] == "planning"
    assert entry["event"] == "output"
