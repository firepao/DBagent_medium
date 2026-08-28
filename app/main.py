import asyncio
import json
import uuid
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.audit import AuditRepository
from app.catalog import CatalogError, MetadataCatalog
from app.config import get_settings
from app.executor import SQLiteExecutor
from app.llm import OpenAIQueryLLM
from app.models import QueryRequest, ToolResponse
from app.agent import AgentRunResponse, AgentRuntime, AgentToolRegistry, OpenAIAgentController
from app.agent.session_runtime import SessionAgentRuntime
from app.agent.session_store import AgentSessionStore, SessionConflict, SessionNotFound
from app.agent.contracts import AgentEvent
from pydantic import BaseModel, Field
from app.prompts import PromptRegistry
from app.llm_trace import LLMTraceRepository
from app.service import QueryService
from app.stage_timing import StageTimingRepository
from app.sql_guard import SqlGuard
from app.rule_store import (
    RuleAuditEvent, RuleEvaluationGate, RuleInput, RuleStore, RuleValidation,
    RuleVersion, RuleVersionDiff,
)
from app.platform_migrations import migrate_platform_database
from app.telemetry import TelemetryBridge
from app.conversation import ConversationStore
from app.run_events import RunEvent, RunEventStore
from app.evaluation import (
    EvaluationCase,
    BulkGoldenValuesUpdate,
    GoldenValuesUpdate,
    EvaluationComparison,
    EvaluationRun,
    EvaluationReadiness,
    EvaluationRunRequest,
    EvaluationRunner,
    EvaluationStore,
)


BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"


class AgentMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    client_message_id: str | None = Field(default=None, max_length=100)


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
    llm = OpenAIQueryLLM(
        settings,
        prompts=prompts,
        trace_repository=trace,
        base_dir=BASE_DIR,
    )
    guard = SqlGuard(catalog, max_rows=settings.max_result_rows)
    executor = SQLiteExecutor(
        db_path,
        timeout_seconds=settings.query_timeout_seconds,
        max_rows=settings.max_result_rows,
    )
    audit = AuditRepository(_resolve_from_base(settings.audit_log_path))
    stage_timing = StageTimingRepository(
        _resolve_from_base(settings.stage_timing_log_path)
    )
    service = QueryService(
        catalog,
        llm,
        guard,
        executor,
        audit,
        stage_timing=stage_timing,
        diagnostics_enabled=settings.query_diagnostics_enabled,
        max_sql_repair_attempts=settings.max_sql_modification_attempts,
        max_semantic_rewrite_attempts=settings.max_sql_modification_attempts,
        max_result_requery_attempts=settings.max_result_requery_attempts,
        total_timeout_seconds=settings.query_total_timeout_seconds,
        telemetry=TelemetryBridge(settings.otel_exporter_endpoint, settings.otel_service_name),
        conversation_store=ConversationStore(
            settings.conversation_ttl_seconds, settings.conversation_max_sessions
        ),
    )
    service.admin_api_key = settings.admin_api_key
    service.viewer_api_key = settings.viewer_api_key
    service.deployment_mode = settings.deployment_mode
    platform_db_path = _resolve_from_base(settings.platform_db_path)
    migrate_platform_database(platform_db_path)
    service.event_store = RunEventStore(platform_db_path)
    rule_store = RuleStore(platform_db_path, catalog, require_evaluation=True)
    catalog.set_managed_rules_provider(rule_store.published_rules)
    evaluation_store = EvaluationStore(platform_db_path)
    evaluation_store.import_validation_cases(
        _resolve_from_base(settings.validation_cases_path)
    )
    evaluation_runner = EvaluationRunner(service, evaluation_store, rule_store)
    service.rule_store = rule_store
    service.evaluation_store = evaluation_store
    service.evaluation_runner = evaluation_runner
    service.agent_runtime_enabled = settings.agent_runtime_enabled
    service.agent_runtime = AgentRuntime(
        catalog,
        OpenAIAgentController(llm),
        AgentToolRegistry(catalog, guard, executor),
        max_turns=settings.agent_max_turns,
        max_sql_queries=settings.agent_max_sql_queries,
        max_llm_calls=settings.agent_max_llm_calls,
        max_total_tokens=settings.agent_max_total_tokens,
        max_wall_time_seconds=settings.agent_max_wall_time_seconds,
        max_consecutive_tool_errors=settings.agent_max_consecutive_tool_errors,
    )
    service.agent_session_store = AgentSessionStore(
        platform_db_path, clarification_ttl_seconds=settings.conversation_ttl_seconds
    )
    service.agent_session_runtime = SessionAgentRuntime(
        service.agent_runtime,
        service.agent_session_store,
    )
    return service


def create_app(service: QueryService | None = None) -> FastAPI:
    query_service = service or build_default_service()
    rule_store = getattr(query_service, "rule_store", None)
    evaluation_store = getattr(query_service, "evaluation_store", None)
    evaluation_runner = getattr(query_service, "evaluation_runner", None)

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

    admin_api_key = getattr(query_service, "admin_api_key", None)
    viewer_api_key = getattr(query_service, "viewer_api_key", None)
    if admin_api_key is None and service is None:
        admin_api_key = get_settings().admin_api_key
        viewer_api_key = get_settings().viewer_api_key
    admin_api_key = admin_api_key or ""
    viewer_api_key = viewer_api_key or ""

    @application.middleware("http")
    async def protect_management_api(request: Request, call_next):
        managed = request.url.path.startswith(("/api/v1/rules", "/api/v1/evaluations", "/api/v1/query-runs"))
        if managed and (admin_api_key or viewer_api_key):
            supplied = request.headers.get("X-Admin-Key", "")
            is_admin = bool(admin_api_key and secrets.compare_digest(supplied, admin_api_key))
            is_viewer = bool(viewer_api_key and secrets.compare_digest(supplied, viewer_api_key))
            if not is_admin and not is_viewer:
                return JSONResponse(status_code=401, content={"detail": "管理凭据无效"})
            if is_viewer and not is_admin and request.method != "GET":
                return JSONResponse(status_code=403, content={"detail": "只读凭据不能执行管理操作"})
        return await call_next(request)

    if WEB_DIR.is_dir():
        application.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

        @application.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            return RedirectResponse(url="/app")

        @application.get("/app", include_in_schema=False)
        async def query_workbench() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

        @application.get("/rules", include_in_schema=False)
        async def rule_console() -> FileResponse:
            return FileResponse(WEB_DIR / "rules.html")

        @application.get("/evaluations", include_in_schema=False)
        async def evaluation_console() -> FileResponse:
            return FileResponse(WEB_DIR / "evals.html")

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

    @application.get("/live")
    async def liveness() -> dict:
        return {"status": "alive"}

    @application.get("/ready")
    async def readiness() -> JSONResponse:
        state = query_service.health()
        return JSONResponse(
            status_code=200 if state.get("status") == "healthy" else 503,
            content=state,
        )

    @application.post(
        "/api/v1/query-energy-data",
        response_model=ToolResponse,
        response_model_by_alias=True,
        response_model_exclude_none=True,
    )
    async def query_energy_data(payload: QueryRequest) -> ToolResponse:
        return await query_service.query(payload)

    @application.post(
        "/api/v2/agent-query",
        response_model=AgentRunResponse,
        response_model_exclude_none=True,
    )
    async def agent_query(payload: QueryRequest) -> AgentRunResponse:
        runtime = getattr(query_service, "agent_runtime", None)
        enabled = getattr(query_service, "agent_runtime_enabled", False)
        if runtime is None or not enabled:
            raise HTTPException(status_code=503, detail="Agent Runtime 尚未启用")
        return await runtime.run(payload.question)

    @application.post("/api/v2/agent-query/events")
    async def agent_query_events(payload: QueryRequest) -> StreamingResponse:
        runtime = getattr(query_service, "agent_runtime", None)
        enabled = getattr(query_service, "agent_runtime_enabled", False)
        if runtime is None or not enabled:
            raise HTTPException(status_code=503, detail="Agent Runtime 尚未启用")

        async def stream():
            queue: asyncio.Queue[dict] = asyncio.Queue()
            task = asyncio.create_task(
                runtime.run(payload.question, event_sink=queue.put_nowait)
            )
            terminal_seen = False
            while not task.done() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                    yield "event: progress\ndata: " + json.dumps(
                        event, ensure_ascii=False
                    ) + "\n\n"
                except TimeoutError:
                    continue
            result = await task
            response = result.response or ToolResponse.failure(
                request_id=result.request_id,
                code=result.error_code or "AGENT_FAILED",
                message="Agent 查询未能完成。",
                retryable=False,
            )
            yield "event: result\ndata: " + response.model_dump_json(
                by_alias=True, exclude_none=True
            ) + "\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _session_runtime():
        runtime = getattr(query_service, "agent_session_runtime", None)
        if runtime is None or not getattr(query_service, "agent_runtime_enabled", False):
            raise HTTPException(status_code=503, detail="Agent Host 尚未启用")
        return runtime

    @application.post("/api/v3/agent/sessions")
    async def create_agent_session() -> dict:
        runtime = _session_runtime()
        session = runtime.create_session()
        return {"session_id": session.session_id, "status": session.status, "catalog_snapshot": session.catalog_snapshot, "created_at": session.created_at, "updated_at": session.updated_at, "message_count": 0}

    @application.get("/api/v3/agent/sessions/{session_id}")
    async def get_agent_session(session_id: str) -> dict:
        runtime = _session_runtime()
        try:
            session = runtime.get_session(session_id)
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND")
        messages = runtime.list_messages(session_id, limit=1_000)
        return {**session.model_dump(mode="json"), "message_count": len(messages)}

    @application.get("/api/v3/agent/sessions/{session_id}/messages")
    async def list_agent_messages(session_id: str, after: int = 0, limit: int = 200) -> dict:
        runtime = _session_runtime()
        try:
            messages = runtime.list_messages(session_id, after_sequence=max(0, after), limit=min(limit, 1_000))
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND")
        return {"messages": [message.model_dump(mode="json") for message in messages]}

    @application.get("/api/v3/agent/runs/{run_id}")
    async def get_agent_run(run_id: str) -> dict:
        runtime = _session_runtime()
        try:
            return runtime.get_run(run_id).model_dump(mode="json")
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")

    @application.get("/api/v3/agent/runs/{run_id}/events")
    async def list_agent_events(run_id: str, request: Request, after: int = 0, limit: int = 500) -> dict:
        runtime = _session_runtime()
        header_after = request.headers.get("last-event-id")
        if header_after and header_after.isdigit():
            after = max(after, int(header_after))
        try:
            events = runtime.list_events(run_id, after_sequence=max(0, after), limit=min(limit, 2_000))
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
        return {"events": [event.model_dump(mode="json") for event in events]}

    @application.post("/api/v3/agent/sessions/{session_id}/messages/events")
    async def agent_session_message_events(session_id: str, payload: AgentMessageRequest, request: Request, after: int = 0) -> StreamingResponse:
        runtime = _session_runtime()
        queue: asyncio.Queue[dict] = asyncio.Queue()
        try:
            session = runtime.get_session(session_id)
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND")
        header_after = request.headers.get("last-event-id")
        if header_after and header_after.isdigit():
            after = max(after, int(header_after))

        def sink(item: dict) -> None:
            event_type = str(item.get("event_type") or ("tool_result" if item.get("tool") else "turn_start"))
            queue.put_nowait({"event": event_type, "data": item})

        async def stream():
            try:
                active = runtime.get_session(session_id)
                existing = runtime.store.find_message_by_client_id(session_id, payload.client_message_id) if payload.client_message_id else None
                existing_run = runtime.get_run(existing.run_id) if existing and existing.run_id else None
                if existing_run is not None:
                    for event in runtime.list_events(existing_run.run_id, after_sequence=max(0, after), limit=2_000):
                        yield f"event: {event.event_type}\nid: {event.sequence}\ndata: {json.dumps({**event.model_dump(mode='json'), 'request_id': existing_run.request_id}, ensure_ascii=False)}\n\n"
                    if existing_run.status in {"completed", "failed", "cancelled"}:
                        replay = runtime._response_from_run(existing_run)
                        yield "event: run_end\ndata: " + json.dumps({"run_id": replay.run_id, "request_id": replay.request_id, "status": replay.status, "response": replay.model_dump(mode="json")}, ensure_ascii=False) + "\n\n"
                        return
                    if existing_run.status in {"queued", "running"}:
                        while True:
                            if await request.is_disconnected():
                                return
                            current = runtime.get_run(existing_run.run_id)
                            if current.status not in {"queued", "running"}:
                                replay = runtime._response_from_run(current)
                                yield "event: run_end\ndata: " + json.dumps({"run_id": replay.run_id, "request_id": replay.request_id, "status": replay.status, "response": replay.model_dump(mode="json")}, ensure_ascii=False) + "\n\n"
                                return
                            await asyncio.sleep(0.25)
                if active.active_run_id and active.status in {"waiting_user", "active"}:
                    active_run = runtime.get_run(active.active_run_id)
                else:
                    active_run = None
                if active_run is not None and active_run.status in {"waiting_user", "paused"}:
                    task = asyncio.create_task(runtime.resume_run(active_run.run_id, payload.text, event_sink=sink))
                else:
                    task = asyncio.create_task(runtime.start_run(session_id, payload.text, client_message_id=payload.client_message_id, event_sink=sink))
            except SessionConflict:
                yield "event: error\ndata: {\"code\":\"SESSION_BUSY\"}\n\n"
                return
            while not task.done() or not queue.empty():
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.1)
                    terminal_seen = terminal_seen or item.get("event") in {"run_end", "clarification_required", "error"}
                    event_id = item["data"].get("sequence") if isinstance(item.get("data"), dict) else None
                    yield f"event: {item['event']}\n" + (f"id: {event_id}\n" if event_id is not None else "") + f"data: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
                except TimeoutError:
                    continue
            if task.done():
                try:
                    result = task.result()
                    # The host persists and emits a terminal event. Only emit
                    # a synthetic one when an unexpected path produced none.
                    if not terminal_seen:
                        yield "event: run_end\ndata: " + json.dumps({"run_id": result.run_id, "request_id": result.request_id, "status": result.status, "response": result.model_dump(mode="json")}, ensure_ascii=False) + "\n\n"
                except SessionConflict:
                    yield "event: error\ndata: {\"code\":\"SESSION_BUSY\"}\n\n"
                except Exception as exc:
                    yield "event: error\ndata: {\"code\":\"AGENT_FAILED\"}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @application.post("/api/v3/agent/sessions/{session_id}/messages", response_model=AgentRunResponse)
    async def agent_session_message(session_id: str, payload: AgentMessageRequest) -> AgentRunResponse:
        runtime = _session_runtime()
        try:
            session = runtime.get_session(session_id)
            if session.active_run_id and session.status in {"waiting_user", "active"}:
                run = runtime.get_run(session.active_run_id)
                if run.status in {"waiting_user", "paused"}:
                    return await runtime.resume_run(run.run_id, payload.text)
            return await runtime.start_run(session_id, payload.text, client_message_id=payload.client_message_id)
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND")
        except SessionConflict:
            raise HTTPException(status_code=409, detail="SESSION_BUSY")

    @application.post("/api/v3/agent/runs/{run_id}/resume")
    async def resume_agent_run(run_id: str, payload: AgentMessageRequest) -> AgentRunResponse:
        runtime = _session_runtime()
        try:
            return await runtime.resume_run(run_id, payload.text)
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @application.post("/api/v3/agent/runs/{run_id}/cancel")
    async def cancel_agent_run(run_id: str) -> dict:
        runtime = _session_runtime()
        try:
            run = runtime.cancel_run(run_id)
            return run.model_dump(mode="json")
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")

    @application.post("/api/v1/query-energy-data/events")
    async def query_energy_data_events(payload: QueryRequest) -> StreamingResponse:
        async def stream():
            query_events = getattr(query_service, "query_events", None)
            if query_events is None:
                response = await query_service.query(payload)
                yield f"event: result\ndata: {response.model_dump_json(by_alias=True, exclude_none=True)}\n\n"
                return
            async for item in query_events(payload):
                event_name = "progress" if item.__class__.__name__ == "RunEvent" else "result"
                yield f"event: {event_name}\ndata: {item.model_dump_json(by_alias=True, exclude_none=True)}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @application.get("/api/v1/query-runs/{request_id}/events", response_model=list[RunEvent])
    async def replay_query_events(request_id: str) -> list[RunEvent]:
        event_store = getattr(query_service, "event_store", None)
        if event_store is None:
            raise HTTPException(status_code=503, detail="运行追踪未配置")
        events = event_store.list(request_id)
        if not events:
            raise HTTPException(status_code=404, detail="运行轨迹不存在")
        return events

    @application.get("/api/v1/rules", response_model=list[RuleVersion])
    async def list_rules() -> list[RuleVersion]:
        if rule_store is None:
            return []
        return rule_store.list()

    @application.get("/api/v1/rules/evaluation-gates", response_model=list[RuleEvaluationGate])
    async def list_rule_evaluation_gates() -> list[RuleEvaluationGate]:
        if rule_store is None:
            return []
        return rule_store.evaluation_gates()

    @application.get("/api/v1/rules/catalog")
    async def rule_catalog() -> dict:
        catalog = getattr(query_service, "catalog", None)
        if catalog is None:
            return {"tables": []}
        return {
            "tables": [
                {
                    "name": table,
                    "dataset": catalog.dataset(table).get("dataset") or table,
                    "columns": sorted(catalog.allowed_columns(table)),
                }
                for table in sorted(catalog.allowed_tables)
            ]
        }

    @application.get("/api/v1/rules/runtime")
    async def runtime_rules() -> list[dict]:
        catalog = getattr(query_service, "catalog", None)
        if catalog is None or not hasattr(catalog, "runtime_rule_summaries"):
            return []
        return catalog.runtime_rule_summaries()

    @application.get("/api/v1/rules/{rule_id}", response_model=RuleVersion)
    async def get_rule(rule_id: str) -> RuleVersion:
        if rule_store is None:
            raise HTTPException(status_code=503, detail="规则服务未配置")
        try:
            return rule_store.get(rule_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="规则不存在") from None

    @application.get("/api/v1/rules/{rule_id}/diff", response_model=RuleVersionDiff)
    async def get_rule_diff(rule_id: str) -> RuleVersionDiff:
        if rule_store is None:
            raise HTTPException(status_code=503, detail="规则服务未配置")
        try:
            return rule_store.version_diff(rule_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="规则不存在") from None

    @application.get("/api/v1/rules/{rule_id}/audit", response_model=list[RuleAuditEvent])
    async def get_rule_audit(rule_id: str) -> list[RuleAuditEvent]:
        if rule_store is None:
            raise HTTPException(status_code=503, detail="规则服务未配置")
        try:
            return rule_store.audit_events(rule_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="规则不存在") from None

    @application.post("/api/v1/rules", response_model=RuleVersion, status_code=201)
    async def create_rule(payload: RuleInput) -> RuleVersion:
        if rule_store is None:
            raise HTTPException(status_code=503, detail="规则服务未配置")
        return rule_store.create_draft(payload)

    @application.post("/api/v1/rules/{rule_id}/validate", response_model=RuleValidation)
    async def validate_rule(rule_id: str) -> RuleValidation:
        if rule_store is None:
            raise HTTPException(status_code=503, detail="规则服务未配置")
        try:
            return rule_store.validate(rule_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="规则不存在") from None

    @application.post("/api/v1/rules/{rule_id}/publish", response_model=RuleVersion)
    async def publish_rule(rule_id: str) -> RuleVersion:
        if rule_store is None:
            raise HTTPException(status_code=503, detail="规则服务未配置")
        try:
            return rule_store.publish(rule_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="规则不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.post("/api/v1/rules/{rule_id}/evaluate", response_model=EvaluationRun)
    async def evaluate_rule(rule_id: str) -> EvaluationRun:
        if evaluation_store is None or evaluation_runner is None:
            raise HTTPException(status_code=503, detail="评测服务未配置")
        try:
            rule = rule_store.get(rule_id) if rule_store else None
            if rule is None:
                raise KeyError(rule_id)
            return await evaluation_runner.run_cases(
                evaluation_store.select_cases(payload.case_ids),
                target_type="rule",
                target_id=f"{rule.rule_key}@v{rule.version}",
                candidate_rule_id=rule_id,
            )
        except KeyError as exc:
            if payload.case_ids:
                raise HTTPException(status_code=404, detail=f"评测题目不存在：{exc}") from None
            raise HTTPException(status_code=404, detail="规则不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.get("/api/v1/evaluations/cases", response_model=list[EvaluationCase])
    async def list_evaluation_cases() -> list[EvaluationCase]:
        if evaluation_store is None:
            return []
        return evaluation_store.list_cases()

    @application.patch("/api/v1/evaluations/cases/golden-values", response_model=list[EvaluationCase])
    async def update_golden_values_bulk(payload: BulkGoldenValuesUpdate) -> list[EvaluationCase]:
        if evaluation_store is None:
            raise HTTPException(status_code=503, detail="评测服务未配置")
        try:
            return evaluation_store.update_golden_values_bulk(payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"评测题目不存在：{exc}") from None

    @application.patch("/api/v1/evaluations/cases/{case_id}/golden-values", response_model=EvaluationCase)
    async def update_golden_values(case_id: str, payload: GoldenValuesUpdate) -> EvaluationCase:
        if evaluation_store is None:
            raise HTTPException(status_code=503, detail="评测服务未配置")
        try:
            return evaluation_store.update_golden_values(case_id, payload)
        except KeyError:
            raise HTTPException(status_code=404, detail="评测题目不存在") from None

    @application.get("/api/v1/evaluations/runs", response_model=list[EvaluationRun])
    async def list_evaluation_runs() -> list[EvaluationRun]:
        if evaluation_store is None:
            return []
        return evaluation_store.list_runs()

    @application.get("/api/v1/evaluations/readiness", response_model=EvaluationReadiness)
    async def evaluation_readiness() -> EvaluationReadiness:
        if evaluation_store is None:
            raise HTTPException(status_code=503, detail="评测服务未配置")
        return evaluation_store.readiness()

    @application.get("/api/v1/evaluations/runs/{run_id}", response_model=EvaluationRun)
    async def get_evaluation_run(run_id: str) -> EvaluationRun:
        if evaluation_store is None:
            raise HTTPException(status_code=503, detail="评测服务未配置")
        try:
            return evaluation_store.get_run(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="评测运行不存在") from None

    @application.get("/api/v1/evaluations/compare", response_model=EvaluationComparison)
    async def compare_evaluation_runs(
        baseline_run_id: str, candidate_run_id: str
    ) -> EvaluationComparison:
        if evaluation_store is None:
            raise HTTPException(status_code=503, detail="评测服务未配置")
        if baseline_run_id == candidate_run_id:
            raise HTTPException(status_code=422, detail="请选择两个不同的评测运行")
        try:
            return evaluation_store.compare_runs(baseline_run_id, candidate_run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="评测运行不存在") from None

    @application.post("/api/v1/evaluations/run", response_model=EvaluationRun)
    async def run_evaluation(payload: EvaluationRunRequest) -> EvaluationRun:
        if evaluation_store is None or evaluation_runner is None:
            raise HTTPException(status_code=503, detail="评测服务未配置")
        if payload.target_type == "rule" and not payload.rule_id:
            raise HTTPException(status_code=422, detail="规则评测必须提供 rule_id")
        if payload.target_type == "baseline" and payload.rule_id:
            raise HTTPException(status_code=422, detail="基线评测不能提供 rule_id")
        try:
            selected_cases = evaluation_store.select_cases(payload.case_ids)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"评测题目不存在：{exc}") from None
        try:
            return await evaluation_runner.run_cases(
                selected_cases,
                target_type=payload.target_type,
                target_id=payload.target_id,
                candidate_rule_id=payload.rule_id,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="规则不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.post("/api/v1/rules/{rule_key}/rollback/{version}", response_model=RuleVersion)
    async def rollback_rule(rule_key: str, version: int) -> RuleVersion:
        if rule_store is None:
            raise HTTPException(status_code=503, detail="规则服务未配置")
        try:
            return rule_store.rollback(rule_key, version)
        except KeyError:
            raise HTTPException(status_code=404, detail="历史规则版本不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    return application


app = create_app()
