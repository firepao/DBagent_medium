import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.audit import AuditRepository
from app.catalog import MetadataCatalog
from app.config import get_settings
from app.executor import SQLiteExecutor
from app.llm import OpenAIQueryLLM
from app.models import QueryRequest, ToolResponse
from app.service import QueryService
from app.sql_guard import SqlGuard


BASE_DIR = Path(__file__).resolve().parent.parent


def _resolve_from_base(path: Path) -> Path:
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


def build_default_service() -> QueryService:
    settings = get_settings()
    db_path = _resolve_from_base(settings.sqlite_db_path)
    catalog = MetadataCatalog(
        db_path,
        BASE_DIR / "config" / "catalog.json",
        BASE_DIR / "config" / "examples.json",
    )
    llm = OpenAIQueryLLM(settings)
    guard = SqlGuard(catalog, max_rows=settings.max_result_rows)
    executor = SQLiteExecutor(
        db_path,
        timeout_seconds=settings.query_timeout_seconds,
        max_rows=settings.max_result_rows,
    )
    audit = AuditRepository(_resolve_from_base(settings.audit_log_path))
    return QueryService(catalog, llm, guard, executor, audit)


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
    )
    async def query_energy_data(payload: QueryRequest) -> ToolResponse:
        return await query_service.query(payload)

    return application


app = create_app()
