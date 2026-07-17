import importlib

import pytest
from pydantic import ValidationError


def load_models():
    try:
        return importlib.import_module("app.models")
    except ModuleNotFoundError:
        pytest.fail("app.models 尚未实现")


def test_query_request_trims_required_identifiers() -> None:
    models = load_models()
    request = models.QueryRequest(
        question="  张北县已运行风电项目有多少个？  ",
        user_id="  u_1  ",
        session_id="  s_1  ",
    )

    assert request.question == "张北县已运行风电项目有多少个？"
    assert request.user_id == "u_1"
    assert request.session_id == "s_1"


@pytest.mark.parametrize("field", ["question", "user_id", "session_id"])
def test_query_request_rejects_blank_required_fields(field: str) -> None:
    models = load_models()
    payload = {
        "question": "查询已运行项目",
        "user_id": "u_1",
        "session_id": "s_1",
    }
    payload[field] = "   "

    with pytest.raises(ValidationError):
        models.QueryRequest(**payload)


def test_tool_error_has_stable_non_leaking_shape() -> None:
    models = load_models()
    response = models.ToolResponse.failure(
        request_id="qry_1",
        code="INVALID_ARGUMENT",
        message="请求参数不合法",
        retryable=False,
    )

    assert response.success is False
    assert response.data is None
    assert response.error == models.ErrorInfo(
        code="INVALID_ARGUMENT",
        message="请求参数不合法",
        retryable=False,
    )
    assert "sql" not in response.model_dump_json().lower()
