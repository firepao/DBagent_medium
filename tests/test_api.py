import asyncio
import importlib

import httpx
import pytest

from app.models import QueryData, SourceInfo, ToolResponse


def create_app(service):
    try:
        return importlib.import_module("app.main").create_app(service)
    except ModuleNotFoundError:
        pytest.fail("app.main 尚未实现")


class StubService:
    async def query(self, request):
        return ToolResponse(
            success=True,
            request_id="qry_test",
            data=QueryData(rows=[{"total": 2}], summary={"total": 2}),
            sources=[SourceInfo(dataset="测试数据", version="v1")],
        )

    def health(self):
        return {
            "status": "healthy",
            "checks": {"database": "healthy", "llm": "configured"},
        }


async def request(app, method: str, path: str, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.request(method, path, **kwargs)


def test_query_endpoint_returns_tool_response() -> None:
    response = asyncio.run(
        request(
            create_app(StubService()),
            "POST",
            "/api/v1/query-energy-data",
            json={
                "question": "查询项目数量",
                "user_id": "u_1",
                "session_id": "s_1",
            },
        )
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["data"]["summary"] == {"total": 2}
    assert response.json()["data"]["schema"] == []


def test_query_endpoint_returns_stable_validation_error() -> None:
    response = asyncio.run(
        request(
            create_app(StubService()),
            "POST",
            "/api/v1/query-energy-data",
            json={"question": "   ", "user_id": "u_1", "session_id": "s_1"},
        )
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"] == {
        "code": "INVALID_ARGUMENT",
        "message": "请求参数不合法",
        "retryable": False,
    }


def test_health_endpoint_reports_component_state() -> None:
    response = asyncio.run(
        request(create_app(StubService()), "GET", "/health")
    )

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["checks"]["database"] == "healthy"
