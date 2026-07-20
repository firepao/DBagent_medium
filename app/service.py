import hashlib
import sqlite3
import time
import uuid
from typing import Any

from app.audit import AuditRepository
from app.catalog import CatalogError, MetadataCatalog
from app.executor import QueryExecutionError, QueryTimeoutError, SQLiteExecutor
from app.llm import (
    LLMConfigurationError,
    LLMResponseError,
    QueryLLM,
)
from app.models import QueryData, QueryRequest, SourceInfo, ToolResponse
from app.sql_guard import SqlGuard, SqlValidationError


class QueryService:
    def __init__(
        self,
        catalog: MetadataCatalog,
        llm: QueryLLM,
        guard: SqlGuard,
        executor: SQLiteExecutor,
        audit: AuditRepository,
    ) -> None:
        self.catalog = catalog
        self.llm = llm
        self.guard = guard
        self.executor = executor
        self.audit = audit

    async def query(self, request: QueryRequest) -> ToolResponse:
        request_id = f"qry_{uuid.uuid4().hex}"
        started = time.monotonic()
        question_hash = hashlib.sha256(request.question.encode("utf-8")).hexdigest()
        audit_base = {
            "request_id": request_id,
            "user_id": request.user_id,
            "session_id": request.session_id,
            "question_sha256": question_hash,
        }
        llm_stage = "planning"

        try:
            candidate_tables = self.catalog.match_tables(request.question)
            if not candidate_tables:
                return self._failure(
                    request_id,
                    "QUERY_NOT_SUPPORTED",
                    "当前已发布数据无法支撑该问题",
                    False,
                    audit_base,
                    started,
                )

            plan = await self.llm.plan(request.question, candidate_tables)
            if not set(plan.table_hints).issubset(set(candidate_tables)):
                raise CatalogError("查询规划引用了候选范围外的数据表")
            if plan.requires_clarification:
                message = plan.clarification_question or "请补充查询所需条件"
                return self._failure(
                    request_id,
                    "CLARIFICATION_REQUIRED",
                    message,
                    False,
                    audit_base,
                    started,
                    tables=plan.table_hints,
                )

            context = self.catalog.build_context(plan.table_hints)
            llm_stage = "sql_generation"
            candidate_sql = await self.llm.generate_sql(
                request.question, plan, context
            )
            validated = self.guard.validate(candidate_sql)
            if not validated.tables.issubset(set(plan.table_hints)):
                raise SqlValidationError("SQL 引用了规划范围外的数据表")

            result = await self.executor.execute(validated.sql)
            source_dicts = self.catalog.source_info(validated.tables)
            sources = [SourceInfo(**item) for item in source_dicts]
            warnings: list[str] = []
            if result.truncated:
                warnings.append("RESULT_TRUNCATED")
            if not result.rows:
                warnings.append("NO_DATA")
            summary: dict[str, Any]
            if len(result.rows) == 1:
                summary = dict(result.rows[0])
            else:
                summary = {
                    "row_count": result.row_count,
                    "truncated": result.truncated,
                }
            data_as_of_values = [
                source.data_as_of for source in sources if source.data_as_of
            ]
            response = ToolResponse(
                success=True,
                request_id=request_id,
                data=QueryData(
                    rows=result.rows,
                    summary=summary,
                    schema_=result.schema,
                    data_as_of=max(data_as_of_values) if data_as_of_values else None,
                ),
                sources=sources,
                warnings=warnings,
            )
            self.audit.record(
                {
                    **audit_base,
                    "status": "success",
                    "query_type": plan.query_type,
                    "tables": sorted(validated.tables),
                    "row_count": result.row_count,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )
            return response
        except LLMConfigurationError:
            return self._failure(
                request_id,
                "LLM_NOT_CONFIGURED",
                "模型接口尚未配置",
                False,
                audit_base,
                started,
            )
        except LLMResponseError:
            fallback = await self._try_exact_example_fallback(
                request_id, request, audit_base, started
            )
            if fallback is not None:
                return fallback
            return self._failure(
                request_id,
                "SQL_GENERATION_FAILED",
                "查询规划或 SQL 生成失败",
                True,
                audit_base,
                started,
                stage=llm_stage,
            )
        except CatalogError:
            return self._failure(
                request_id,
                "QUERY_NOT_SUPPORTED",
                "查询超出已发布数据范围",
                False,
                audit_base,
                started,
            )
        except SqlValidationError:
            return self._failure(
                request_id,
                "SQL_VALIDATION_FAILED",
                "生成的查询未通过安全校验",
                False,
                audit_base,
                started,
            )
        except QueryTimeoutError:
            return self._failure(
                request_id,
                "QUERY_TIMEOUT",
                "查询执行超时",
                True,
                audit_base,
                started,
            )
        except QueryExecutionError:
            return self._failure(
                request_id,
                "INTERNAL_ERROR",
                "查询服务执行失败",
                False,
                audit_base,
                started,
            )
        except Exception:
            return self._failure(
                request_id,
                "INTERNAL_ERROR",
                "查询服务内部异常",
                False,
                audit_base,
                started,
            )

    def _failure(
        self,
        request_id: str,
        code: str,
        message: str,
        retryable: bool,
        audit_base: dict[str, Any],
        started: float,
        tables: list[str] | None = None,
        stage: str | None = None,
    ) -> ToolResponse:
        self.audit.record(
            {
                **audit_base,
                "status": "failed",
                "error_code": code,
                "tables": sorted(tables or []),
                "stage": stage,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        )
        return ToolResponse.failure(
            request_id=request_id,
            code=code,
            message=message,
            retryable=retryable,
        )

    async def _try_exact_example_fallback(
        self,
        request_id: str,
        request: QueryRequest,
        audit_base: dict[str, Any],
        started: float,
    ) -> ToolResponse | None:
        example = self.catalog.exact_example(request.question)
        if example is None:
            return None
        try:
            validated = self.guard.validate(example["sql"])
            result = await self.executor.execute(validated.sql)
        except (SqlValidationError, QueryExecutionError):
            return None

        source_dicts = self.catalog.source_info(validated.tables)
        sources = [SourceInfo(**item) for item in source_dicts]
        warnings = ["LLM_FALLBACK_EXAMPLE"]
        if result.truncated:
            warnings.append("RESULT_TRUNCATED")
        if not result.rows:
            warnings.append("NO_DATA")
        summary = (
            dict(result.rows[0])
            if len(result.rows) == 1
            else {"row_count": result.row_count, "truncated": result.truncated}
        )
        data_as_of_values = [
            source.data_as_of for source in sources if source.data_as_of
        ]
        response = ToolResponse(
            success=True,
            request_id=request_id,
            data=QueryData(
                rows=result.rows,
                summary=summary,
                schema_=result.schema,
                data_as_of=max(data_as_of_values) if data_as_of_values else None,
            ),
            sources=sources,
            warnings=warnings,
        )
        self.audit.record(
            {
                **audit_base,
                "status": "success",
                "fallback": "verified_example",
                "tables": sorted(validated.tables),
                "row_count": result.row_count,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        )
        return response

    def health(self) -> dict[str, Any]:
        database_state = "unhealthy"
        if self.executor.db_path.is_file():
            uri = f"file:{self.executor.db_path.as_posix()}?mode=ro"
            try:
                with sqlite3.connect(uri, uri=True) as connection:
                    connection.execute("SELECT 1").fetchone()
                database_state = "healthy"
            except sqlite3.DatabaseError:
                database_state = "unhealthy"
        llm_state = "configured" if getattr(self.llm, "is_configured", False) else "missing"
        status = (
            "healthy"
            if database_state == "healthy" and llm_state == "configured"
            else "degraded"
        )
        return {
            "status": status,
            "checks": {"database": database_state, "llm": llm_state},
        }

    async def aclose(self) -> None:
        close = getattr(self.llm, "aclose", None)
        if close is not None:
            await close()
