import asyncio
import importlib

import httpx
import pytest

from app.models import QueryData, SourceInfo, ToolResponse
from app.catalog import CatalogError


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
            },
        )
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["data"]["summary"] == {"total": 2}
    assert response.json()["data"]["schema"] == []
    assert "diagnostics" not in response.json()


def test_query_endpoint_returns_stable_validation_error() -> None:
    response = asyncio.run(
        request(
            create_app(StubService()),
            "POST",
            "/api/v1/query-energy-data",
            json={"question": "   "},
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


def test_default_service_validation_rejects_invalid_table_cards() -> None:
    main = importlib.import_module("app.main")

    class InvalidCatalog:
        def table_card_issues(self):
            return ["t04_filing_project.aliases.备案日期 引用未发布字段 filing_date"]

        def runtime_rule_issues(self):
            return []

    with pytest.raises(CatalogError, match="TableCard 配置无效"):
        main.ensure_valid_table_cards(InvalidCatalog())
