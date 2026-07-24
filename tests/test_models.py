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
    )

    assert request.question == "张北县已运行风电项目有多少个？"


@pytest.mark.parametrize("field", ["question"])
def test_query_request_rejects_blank_required_fields(field: str) -> None:
    models = load_models()
    payload = {
        "question": "查询已运行项目",
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
    assert response.answer_guidance["response_mode"] == "error"
    assert "不得推测字段不存在" in response.answer_guidance["hard_constraints"][1]
    assert "sql" not in response.model_dump_json().lower()


def test_clarification_failure_uses_clarification_answer_mode() -> None:
    models = load_models()
    response = models.ToolResponse.failure(
        request_id="qry_test",
        code="CLARIFICATION_REQUIRED",
        message="请确认统计口径。",
        retryable=False,
    )

    assert response.answer_guidance["response_mode"] == "clarification"
    assert response.answer_guidance["template"].startswith("需要补充查询口径")
