import hashlib
import re
import sqlite3
import time
import uuid
from typing import Any

from app.audit import AuditRepository
from app.catalog import CatalogError, MetadataCatalog
from app.executor import QueryExecutionError, QueryTimeoutError, SQLiteExecutor
from app.llm import (
    LLMConfigurationError,
    LLMInvalidResponseError,
    LLMPlanSchemaError,
    LLMResponseError,
    LLMSemanticReviewSchemaError,
    LLMTimeoutError,
    LLMUpstreamUnavailableError,
    QueryLLM,
)
from app.models import QueryData, QueryRequest, SourceInfo, ToolResponse
from app.llm_trace import llm_trace_context
from app.sql_guard import SqlGuard, SqlValidationError


class QueryService:
    def __init__(
        self,
        catalog: MetadataCatalog,
        llm: QueryLLM,
        guard: SqlGuard,
        executor: SQLiteExecutor,
        audit: AuditRepository,
        diagnostics_enabled: bool = False,
        max_sql_repair_attempts: int = 1,
        max_semantic_rewrite_attempts: int = 1,
    ) -> None:
        self.catalog = catalog
        self.llm = llm
        self.guard = guard
        self.executor = executor
        self.audit = audit
        self.diagnostics_enabled = diagnostics_enabled
        self.max_sql_repair_attempts = max_sql_repair_attempts
        self.max_semantic_rewrite_attempts = max_semantic_rewrite_attempts

    def _new_diagnostics(self) -> dict[str, Any] | None:
        if not self.diagnostics_enabled:
            return None
        return {
            "stage": "candidate_table_matching",
            "plan": {"status": "not_started"},
            "sql_generation": {"status": "not_started", "sql": None},
            "sql_validation": {"status": "not_started", "result": None},
            "sql_repair": {"attempts": 0, "status": "not_started"},
            "semantic_review": {"status": "not_started", "decision": None},
            "fallback": {
                "attempted": False,
                "exact_question_match": False,
                "used": False,
            },
        }

    async def query(self, request: QueryRequest) -> ToolResponse:
        request_id = f"qry_{uuid.uuid4().hex}"
        started = time.monotonic()
        question_hash = hashlib.sha256(request.question.encode("utf-8")).hexdigest()
        audit_base = {
            "request_id": request_id,
            "question_sha256": question_hash,
        }
        llm_stage = "planning"
        diagnostics = self._new_diagnostics()

        try:
            route = self.catalog.routing_decision(request.question)
            if route is not None and route.action == "reject_scope":
                return self._failure(
                    request_id,
                    "QUERY_NOT_SUPPORTED",
                    route.message or "查询超出当前数据覆盖范围",
                    False,
                    audit_base,
                    started,
                    tables=list(route.required_tables),
                    stage="routing",
                    diagnostics=diagnostics,
                )
            if route is not None and route.action == "reject_capability":
                return self._failure(
                    request_id,
                    "CAPABILITY_NOT_SUPPORTED",
                    route.message or "当前数据或已发布口径不支持该查询",
                    False,
                    audit_base,
                    started,
                    tables=list(route.required_tables),
                    stage="routing",
                    diagnostics=diagnostics,
                )
            if diagnostics is not None:
                diagnostics["stage"] = "planning"
                diagnostics["plan"] = {"status": "started"}
            with llm_trace_context(request_id, "planning"):
                plan = await self.llm.plan(
                    request.question, self.catalog.build_planning_context(route)
                )
            if not set(plan.table_hints).issubset(self.catalog.allowed_tables):
                raise CatalogError("查询规划引用了未发布的数据表")
            if route is not None and route.required_tables:
                # The routing rule constrains the candidate context. SQL may later use a
                # subset, provided the semantic reviewer considers it sufficient.
                plan = plan.model_copy(
                    update={
                        "table_hints": sorted(
                            set(plan.table_hints) | set(route.required_tables)
                        )
                    }
                )
            if diagnostics is not None:
                diagnostics["plan"] = {
                    "status": "passed",
                    "table_hints": plan.table_hints,
                }
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
                    stage="planning",
                    diagnostics=diagnostics,
                )

            context = self.catalog.build_sql_context(
                request.question, plan.table_hints, route
            )
            llm_stage = "sql_generation"
            if diagnostics is not None:
                diagnostics["stage"] = "sql_generation"
                diagnostics["sql_generation"] = {"status": "started", "sql": None}
            with llm_trace_context(request_id, "sql_generation"):
                candidate_sql = await self.llm.generate_sql(
                    request.question, plan, context
                )
            if diagnostics is not None:
                diagnostics["sql_generation"] = {
                    "status": "generated",
                    "sql": candidate_sql,
                }
                diagnostics["stage"] = "sql_validation"
                diagnostics["sql_validation"] = {"status": "started", "result": None}
            validated, candidate_sql, llm_stage = await self._validate_or_repair_sql(
                request_id,
                request.question,
                plan,
                context,
                candidate_sql,
                diagnostics,
            )

            semantic_rewrites = 0
            while True:
                llm_stage = f"semantic_review_{semantic_rewrites + 1}"
                if diagnostics is not None:
                    diagnostics["stage"] = "semantic_review"
                    diagnostics["semantic_review"] = {
                        "status": "started",
                        "decision": None,
                        "attempt": semantic_rewrites + 1,
                    }
                with llm_trace_context(request_id, llm_stage):
                    review = await self.llm.review_sql(
                        request.question, plan, context, validated.sql
                    )
                if diagnostics is not None:
                    diagnostics["semantic_review"] = {
                        "status": "completed",
                        "decision": review.decision,
                        "attempt": semantic_rewrites + 1,
                    }
                if review.decision == "pass":
                    break
                if review.decision == "clarification":
                    return self._failure(
                        request_id,
                        "CLARIFICATION_REQUIRED",
                        review.clarification_question or "请补充查询条件",
                        False,
                        audit_base,
                        started,
                        tables=plan.table_hints,
                        stage="semantic_review",
                        diagnostics=diagnostics,
                    )
                if review.decision == "unsupported":
                    return self._failure(
                        request_id,
                        "CAPABILITY_NOT_SUPPORTED",
                        self._semantic_failure_message(review.semantic_issues),
                        False,
                        audit_base,
                        started,
                        tables=plan.table_hints,
                        stage="semantic_review",
                        diagnostics=diagnostics,
                    )
                if semantic_rewrites >= self.max_semantic_rewrite_attempts:
                    return self._failure(
                        request_id,
                        "SQL_REWRITE_EXHAUSTED",
                        self._rewrite_exhausted_message(review.semantic_issues),
                        False,
                        audit_base,
                        started,
                        tables=plan.table_hints,
                        stage="semantic_review",
                        diagnostics=diagnostics,
                    )
                semantic_rewrites += 1
                candidate_sql = review.corrected_sql or ""
                validated, candidate_sql, llm_stage = await self._validate_or_repair_sql(
                    request_id,
                    request.question,
                    plan,
                    context,
                    candidate_sql,
                    diagnostics,
                )
                if diagnostics is not None:
                    diagnostics["sql_generation"] = {
                        "status": "semantic_rewritten",
                        "sql": candidate_sql,
                    }

            if diagnostics is not None:
                diagnostics["sql_validation"] = {
                    "status": "passed",
                    "result": {"tables": sorted(validated.tables)},
                }
                diagnostics["stage"] = "execution"
            result = await self.executor.execute(validated.sql)
            source_dicts = self.catalog.source_info(validated.tables)
            sources = [SourceInfo(**item) for item in source_dicts]
            warnings: list[str] = []
            if result.truncated:
                warnings.append("RESULT_TRUNCATED")
            if not result.rows:
                warnings.append("NO_DATA")
                warnings.append("NO_DATA_AFTER_VALID_FILTER")
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
            if diagnostics is not None:
                diagnostics["stage"] = "completed"
            response = ToolResponse(
                success=True,
                request_id=request_id,
                data=QueryData(
                    rows=result.rows,
                    summary=summary,
                    schema_=result.schema,
                    data_as_of=max(data_as_of_values) if data_as_of_values else None,
                    result_status="no_match" if not result.rows else "data_found",
                    result_reason=(
                        "当前合法筛选条件下未匹配到记录" if not result.rows else None
                    ),
                ),
                sources=sources,
                warnings=warnings,
                answer_guidance=self.catalog.answer_guidance(
                    request.question, validated.tables, route
                ),
                diagnostics=diagnostics,
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
        except LLMResponseError as exc:
            if diagnostics is not None:
                diagnostics["stage"] = llm_stage
                if llm_stage == "planning":
                    diagnostics["plan"] = {"status": "failed"}
                elif llm_stage == "sql_generation":
                    diagnostics["sql_generation"] = {
                        "status": "failed",
                        "sql": None,
                    }
                else:
                    diagnostics["semantic_review"] = {
                        "status": "failed",
                        "decision": None,
                    }
            if llm_stage in {"planning", "sql_generation"}:
                if diagnostics is not None:
                    diagnostics["fallback"]["attempted"] = True
                    diagnostics["fallback"]["exact_question_match"] = (
                        self.catalog.exact_example(request.question) is not None
                    )
                fallback = await self._try_exact_example_fallback(
                    request_id, request, audit_base, started, diagnostics
                )
                if fallback is not None:
                    return fallback
            return self._failure(
                request_id,
                self._llm_error_code(exc, llm_stage),
                self._llm_error_message(exc, llm_stage),
                not isinstance(exc, (LLMPlanSchemaError, LLMSemanticReviewSchemaError)),
                audit_base,
                started,
                stage=llm_stage,
                diagnostics=diagnostics,
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
            if diagnostics is not None:
                diagnostics["stage"] = "sql_validation"
                diagnostics["sql_validation"] = {
                    "status": "failed",
                    "result": "rejected",
                }
                diagnostics["fallback"]["attempted"] = True
                diagnostics["fallback"]["exact_question_match"] = (
                    self.catalog.exact_example(request.question) is not None
                )
            fallback = await self._try_exact_example_fallback(
                request_id, request, audit_base, started, diagnostics
            )
            if fallback is not None:
                return fallback
            return self._failure(
                request_id,
                "SQL_VALIDATION_FAILED",
                "系统已尝试生成并自动修复查询，但仍未满足只读、字段和语法安全要求。请换一种更明确的问法，说明要查询的指标、时间或对象；如问题不变，请提供请求编号排查模型输出。",
                False,
                audit_base,
                started,
                stage="sql_validation",
                diagnostics=diagnostics,
            )
        except QueryTimeoutError:
            if diagnostics is not None:
                diagnostics["stage"] = "execution"
            return self._failure(
                request_id,
                "QUERY_TIMEOUT",
                "数据查询执行时间超过限制，当前未返回不完整结果。请缩小时间范围、区县范围或返回明细数量后重试。",
                True,
                audit_base,
                started,
                stage="execution",
                diagnostics=diagnostics,
            )
        except QueryExecutionError:
            if diagnostics is not None:
                diagnostics["stage"] = "execution"
            return self._failure(
                request_id,
                "INTERNAL_ERROR",
                "查询已生成，但数据库执行阶段未能完成。请确认筛选范围后重试；若持续出现，请提供请求编号排查字段格式或数据库状态。",
                False,
                audit_base,
                started,
                stage="execution",
                diagnostics=diagnostics,
            )
        except Exception:
            return self._failure(
                request_id,
                "INTERNAL_ERROR",
                "查询服务处理过程中出现内部异常，当前没有返回未经验证的数据。请稍后重试；若持续出现，请提供请求编号排查。",
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
        diagnostics: dict[str, Any] | None = None,
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
            diagnostics=diagnostics,
        )

    def _validate_planned_sql(self, sql: str, planned_tables: list[str]):
        validated = self.guard.validate(sql)
        planned = set(planned_tables)
        if not validated.tables.issubset(planned):
            raise SqlValidationError("SQL 引用了规划范围外的数据表")
        return validated

    async def _validate_or_repair_sql(
        self,
        request_id: str,
        question: str,
        plan: Any,
        context: str,
        candidate_sql: str,
        diagnostics: dict[str, Any] | None,
    ) -> tuple[Any, str, str]:
        try:
            return self._validate_planned_sql(candidate_sql, plan.table_hints), candidate_sql, "sql_validation"
        except SqlValidationError as initial_error:
            if (
                self.max_sql_repair_attempts < 1
                or not self._is_repairable_sql_error(initial_error)
            ):
                raise
            if diagnostics is not None:
                diagnostics["stage"] = "sql_repair"
                diagnostics["sql_repair"] = {"attempts": 1, "status": "started"}
            with llm_trace_context(request_id, "sql_repair_1"):
                repaired_sql = await self.llm.repair_sql(
                    question,
                    plan,
                    context,
                    candidate_sql,
                    str(initial_error),
                )
            validated = self._validate_planned_sql(repaired_sql, plan.table_hints)
            if diagnostics is not None:
                diagnostics["sql_repair"] = {"attempts": 1, "status": "passed"}
            return validated, repaired_sql, "sql_validation"

    @staticmethod
    def _is_repairable_sql_error(error: SqlValidationError) -> bool:
        message = str(error)
        unsafe_markers = (
            "SQL 包含禁止操作",
            "SQL 不允许包含注释",
            "只允许单条 SQL",
            "只允许 SELECT",
        )
        return not any(marker in message for marker in unsafe_markers)

    @staticmethod
    def _llm_error_code(exc: LLMResponseError, stage: str) -> str:
        if isinstance(exc, LLMTimeoutError):
            return "LLM_TIMEOUT"
        if isinstance(exc, LLMUpstreamUnavailableError):
            return "LLM_UPSTREAM_UNAVAILABLE"
        if isinstance(exc, LLMPlanSchemaError):
            return "LLM_PLAN_SCHEMA_INVALID"
        if isinstance(exc, LLMSemanticReviewSchemaError):
            return "SEMANTIC_VALIDATION_FAILED"
        if isinstance(exc, LLMInvalidResponseError):
            return "LLM_INVALID_JSON"
        return "PLANNING_FAILED" if stage == "planning" else "SQL_GENERATION_FAILED"

    @staticmethod
    def _llm_error_message(exc: LLMResponseError, stage: str) -> str:
        if isinstance(exc, LLMTimeoutError):
            return "模型在当前阶段响应超时，系统未执行未经审核的查询。可以缩小查询范围后重试；若问题较复杂，也可稍后重试。"
        if isinstance(exc, LLMUpstreamUnavailableError):
            return "模型服务当前不可用，数据查询尚未执行。请稍后重试；该错误通常不需要修改用户问题。"
        if isinstance(exc, LLMPlanSchemaError):
            return "模型未按约定格式返回查询规划，数据查询尚未执行。请重试一次；若持续出现，请提供请求编号排查模型输出。"
        if isinstance(exc, LLMInvalidResponseError):
            return "模型返回内容无法解析，系统未执行未经验证的查询。请重试一次；若持续出现，请提供请求编号排查模型输出。"
        if isinstance(exc, LLMSemanticReviewSchemaError):
            return "模型未按约定格式完成查询语义审核，因此系统没有执行候选查询。请重试或提供请求编号排查。"
        if stage == "planning":
            return "系统未能形成可靠的查询规划。请补充要查询的指标、时间或对象后重试。"
        return "系统未能生成可安全执行的数据查询。请换一种更明确的问法后重试。"

    @staticmethod
    def _semantic_failure_message(issues: list[str]) -> str:
        useful = QueryService._safe_business_issues(issues)
        if useful:
            return "当前数据或已发布口径暂不能可靠回答。原因：" + "；".join(useful) + "。请按上述缺口补充条件或数据后重试。"
        return "当前数据或已发布口径暂不能可靠回答。请明确查询指标、时间范围或业务口径后重试。"

    @staticmethod
    def _rewrite_exhausted_message(issues: list[str]) -> str:
        useful = QueryService._safe_business_issues(issues)
        reason = "；".join(useful) if useful else "候选查询仍不能完整回答用户问题"
        return f"系统已自动修正查询，但复核后仍不可靠。原因：{reason}。当前未执行该查询，请调整问题范围或提供请求编号排查。"

    @staticmethod
    def _safe_business_issues(issues: list[str]) -> list[str]:
        unsafe = re.compile(
            r"\b(?:select|with|from|join|where|group\s+by|order\s+by|pragma|ddl)\b|"
            r"\bt\d{2}_[a-z0-9_]+\b|[_`*]",
            re.IGNORECASE,
        )
        return [
            issue.strip().rstrip("。")
            for issue in issues[:3]
            if isinstance(issue, str)
            and issue.strip()
            and len(issue.strip()) <= 160
            and not unsafe.search(issue)
        ]

    async def _try_exact_example_fallback(
        self,
        request_id: str,
        request: QueryRequest,
        audit_base: dict[str, Any],
        started: float,
        diagnostics: dict[str, Any] | None,
    ) -> ToolResponse | None:
        example = self.catalog.exact_example(request.question)
        if example is None:
            return None
        if diagnostics is not None:
            diagnostics["stage"] = "fallback"
            diagnostics["fallback"] = {
                "attempted": True,
                "exact_question_match": True,
                "used": False,
            }
            diagnostics["sql_generation"] = {
                "status": "fallback_example",
                "sql": example["sql"],
            }
            diagnostics["sql_validation"] = {"status": "started", "result": None}
        try:
            validated = self.guard.validate(example["sql"])
            result = await self.executor.execute(validated.sql)
        except (SqlValidationError, QueryExecutionError):
            if diagnostics is not None:
                diagnostics["sql_validation"] = {
                    "status": "failed",
                    "result": "fallback_rejected",
                }
            return None

        if diagnostics is not None:
            diagnostics["sql_validation"] = {
                "status": "passed",
                "result": {"tables": sorted(validated.tables)},
            }
            diagnostics["fallback"]["used"] = True
            diagnostics["stage"] = "completed"

        source_dicts = self.catalog.source_info(validated.tables)
        sources = [SourceInfo(**item) for item in source_dicts]
        warnings = ["LLM_FALLBACK_EXAMPLE"]
        if result.truncated:
            warnings.append("RESULT_TRUNCATED")
        if not result.rows:
            warnings.append("NO_DATA")
            warnings.append("NO_DATA_AFTER_VALID_FILTER")
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
                result_status="no_match" if not result.rows else "data_found",
                result_reason=(
                    "当前合法筛选条件下未匹配到记录" if not result.rows else None
                ),
            ),
            sources=sources,
            warnings=warnings,
            answer_guidance=self.catalog.answer_guidance(
                request.question,
                validated.tables,
                self.catalog.routing_decision(request.question),
            ),
            diagnostics=diagnostics,
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
