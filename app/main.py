import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.audit import AuditRepository
from app.catalog import CatalogError, MetadataCatalog
from app.config import get_settings
from app.executor import SQLiteExecutor
from app.llm import OpenAIQueryLLM
from app.models import QueryRequest, ToolResponse
from app.prompts import PromptRegistry
from app.llm_trace import LLMTraceRepository
from app.service import QueryService
from app.sql_guard import SqlGuard


BASE_DIR = Path(__file__).resolve().parent.parent


def _resolve_from_base(path: Path) -> Path:
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


def ensure_valid_table_cards(catalog: MetadataCatalog) -> None:
    region_issues = getattr(catalog, "region_rule_issues", lambda: [])()
    issues = (
        catalog.table_card_issues()
        + catalog.runtime_rule_issues()
        + region_issues
    )
    if issues:
        raise CatalogError("TableCard 配置无效: " + "; ".join(issues))


def build_default_service() -> QueryService:
    settings = get_settings()
    db_path = _resolve_from_base(settings.sqlite_db_path)
    catalog = MetadataCatalog(
        db_path,
        _resolve_from_base(settings.catalog_path),
        _resolve_from_base(settings.examples_path),
        table_cards_path=_resolve_from_base(settings.table_cards_path),
        ddl_registry_path=_resolve_from_base(settings.ddl_registry_path),
        query_knowledge_path=_resolve_from_base(settings.query_knowledge_path),
        validation_cases_path=_resolve_from_base(settings.validation_cases_path),
        administrative_regions_path=_resolve_from_base(
            settings.administrative_regions_path
        ),
        ddl_directory=_resolve_from_base(settings.ddl_directory),
    )
    ensure_valid_table_cards(catalog)
    prompts = PromptRegistry.from_file(_resolve_from_base(settings.prompts_path))
    trace = (
        LLMTraceRepository(_resolve_from_base(settings.llm_trace_log_path))
        if settings.enable_llm_trace
        else None
    )
    llm = OpenAIQueryLLM(settings, prompts=prompts, trace_repository=trace)
    guard = SqlGuard(catalog, max_rows=settings.max_result_rows)
    executor = SQLiteExecutor(
        db_path,
        timeout_seconds=settings.query_timeout_seconds,
        max_rows=settings.max_result_rows,
    )
    audit = AuditRepository(_resolve_from_base(settings.audit_log_path))
    return QueryService(
        catalog,
        llm,
        guard,
        executor,
        audit,
        diagnostics_enabled=settings.query_diagnostics_enabled,
    )


def create_app(service: QueryService | None = None) -> FastAPI:
    query_service = service or build_default_service()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        close = getattr(query_service, "aclose", None)
        if close is not None:
            await close()

    application = FastAPI(
        title="Medium Data Query Service",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _: Request, __: RequestValidationError
    ) -> JSONResponse:
        response = ToolResponse.failure(
            request_id=f"qry_{uuid.uuid4().hex}",
            code="INVALID_ARGUMENT",
            message="请求参数不合法",
            retryable=False,
        )
        return JSONResponse(
            status_code=422,
            content=response.model_dump(mode="json", by_alias=True),
        )

    @application.get("/health")
    async def health() -> dict:
        return query_service.health()

    @application.post(
        "/api/v1/query-energy-data",
        response_model=ToolResponse,
        response_model_by_alias=True,
        response_model_exclude_none=True,
    )
    async def query_energy_data(payload: QueryRequest) -> ToolResponse:
        return await query_service.query(payload)

    return application


app = create_app()
