from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.agent.models import AgentRunState, AgentToolResult
from app.catalog import CatalogError, MetadataCatalog
from app.evidence import ResultEvidenceBuilder, SENSITIVE_NAME
from app.executor import ExecutionResult, QueryExecutionError, QueryTimeoutError, SQLiteExecutor
from app.models import Coverage, QueryData, ResultSet, SourceInfo, ToolResponse
from app.sql_guard import SqlGuard, SqlValidationError
from app.agent.answers import AnswerClaim, AnswerDraft, AnswerVerifier


@dataclass(frozen=True)
class LoadedContext:
    context_id: str
    question: str
    tables: frozenset[str]
    content: str


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    context_id: str
    tables: frozenset[str]
    result: ExecutionResult
    evidence: Any
    sources: tuple[SourceInfo, ...]


class AgentToolRegistry:
    """The only bridge between the model-controlled loop and domain capabilities."""

    def __init__(
        self,
        catalog: MetadataCatalog,
        guard: SqlGuard,
        executor: SQLiteExecutor,
        evidence_builder: ResultEvidenceBuilder | None = None,
    ) -> None:
        self.catalog = catalog
        self.guard = guard
        self.executor = executor
        self.evidence_builder = evidence_builder or ResultEvidenceBuilder()
        self.contexts: dict[str, LoadedContext] = {}
        self.evidence: dict[str, EvidenceRecord] = {}
        self.final_responses: dict[str, ToolResponse] = {}
        self.answer_verifier = AnswerVerifier()
        self._handlers: dict[
            str, Callable[[dict[str, Any], AgentRunState], Awaitable[AgentToolResult]]
        ] = {
            "get_table_context": self.get_table_context,
            "inspect_field_profile": self.inspect_field_profile,
            "execute_readonly_query": self.execute_readonly_query,
            "review_evidence": self.review_evidence,
            "ask_user_question": self.ask_user_question,
            "finalize_answer": self.finalize_answer,
        }

    async def invoke(
        self, tool_name: str, arguments: dict[str, Any], state: AgentRunState
    ) -> AgentToolResult:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return self._result(tool_name, "blocked", "工具不在允许列表中。")
        try:
            return await handler(arguments, state)
        except (CatalogError, ValueError, TypeError):
            return self._result(
                tool_name,
                "revision_required",
                "工具参数不符合已发布数据目录，请根据已有观察修正。",
                payload={"error_code": "INVALID_TOOL_ARGUMENTS", "retryable": True},
                retryable=True,
            )
        except Exception:
            return self._result(
                tool_name,
                "error",
                "工具执行出现内部错误，未向模型暴露内部异常。",
                payload={"error_code": "TOOL_INTERNAL_ERROR", "retryable": False},
            )

    def export_checkpoint(self, state: AgentRunState) -> dict[str, Any]:
        """Serialize only bounded, non-secret tool state needed for resume."""
        contexts = {
            key: {"context_id": value.context_id, "question": value.question,
                  "tables": sorted(value.tables), "content": value.content[:30000]}
            for key, value in self.contexts.items() if key in state.loaded_context_ids
        }
        evidence: dict[str, Any] = {}
        for key in state.evidence_ids:
            record = self.evidence.get(key)
            if record is None:
                continue
            evidence[key] = {
                "evidence_id": record.evidence_id,
                "context_id": record.context_id,
                "tables": sorted(record.tables),
                "result": {
                    "rows": record.result.rows[: self.executor.max_rows],
                    "row_count": record.result.row_count,
                    "schema": record.result.schema,
                    "truncated": record.result.truncated,
                    "duration_ms": record.result.duration_ms,
                },
                "evidence": record.evidence.model_dump(mode="json"),
                "sources": [source.model_dump(mode="json") for source in record.sources],
            }
        return {"contexts": contexts, "evidence": evidence}

    def restore_checkpoint(self, payload: dict[str, Any]) -> None:
        for key, item in (payload.get("contexts") or {}).items():
            self.contexts[key] = LoadedContext(
                context_id=str(item["context_id"]), question=str(item["question"]),
                tables=frozenset(str(table) for table in item.get("tables", [])),
                content=str(item.get("content", "")),
            )
        for key, item in (payload.get("evidence") or {}).items():
            result = ExecutionResult(**item["result"])
            evidence = self.evidence_builder.build(
                result,
                sources=[SourceInfo.model_validate(source) for source in item.get("sources", [])],
                applied_scope=self.catalog.data_domain()["administrative_scope"],
                field_labels=self.catalog.result_field_labels(set(item.get("tables", []))),
            )
            # Preserve the original evidence id and the original reviewed payload.
            self.evidence[str(key)] = EvidenceRecord(
                evidence_id=str(item["evidence_id"]), context_id=str(item["context_id"]),
                tables=frozenset(item.get("tables", [])), result=result,
                evidence=evidence, sources=tuple(SourceInfo.model_validate(source) for source in item.get("sources", [])),
            )
    async def get_table_context(
        self, arguments: dict[str, Any], state: AgentRunState
    ) -> AgentToolResult:
        hints = arguments.get("table_hints")
        if not isinstance(hints, list) or not 1 <= len(hints) <= 4:
            raise ValueError("table_hints 必须包含 1-4 张表")
        tables = [str(item).strip() for item in hints]
        if len(set(tables)) != len(tables) or not set(tables).issubset(
            self.catalog.allowed_tables
        ):
            raise CatalogError("上下文包含未发布数据表")
        content = self.catalog.build_sql_context(state.original_question, tables)
        context_id = f"ctx_{uuid.uuid4().hex}"
        self.contexts[context_id] = LoadedContext(
            context_id=context_id,
            question=state.original_question,
            tables=frozenset(tables),
            content=content,
        )
        return self._result(
            "get_table_context",
            "ok",
            "已加载受控表上下文。",
            payload={
                "context_id": context_id,
                "tables": sorted(tables),
                "sql_context": content,
            },
            details_ref=context_id,
        )

    async def inspect_field_profile(
        self, arguments: dict[str, Any], state: AgentRunState
    ) -> AgentToolResult:
        context = self._context(arguments.get("context_id"), state)
        field = str(arguments.get("field") or "").strip()
        requested_table = str(arguments.get("table") or "").strip() or None
        candidate_tables = [requested_table] if requested_table else sorted(context.tables)
        matches = [
            table
            for table in candidate_tables
            if table in context.tables and field in self.catalog.allowed_columns(table)
        ]
        if len(matches) != 1 or SENSITIVE_NAME.search(field):
            return self._result(
                "inspect_field_profile",
                "blocked" if SENSITIVE_NAME.search(field) else "revision_required",
                "该字段无法在当前受控上下文中唯一、安全地检查。",
                payload={"error_code": "FIELD_NOT_AVAILABLE", "retryable": False},
            )
        table = matches[0]
        profile = await asyncio.to_thread(self._field_profile_sync, table, field)
        return self._result(
            "inspect_field_profile",
            "ok",
            "已返回受控字段画像；仅包含聚合统计和有界枚举。",
            payload={"context_id": context.context_id, "table": table, "field": field, **profile},
        )

    def _field_profile_sync(self, table: str, field: str) -> dict[str, Any]:
        uri = f"file:{self.catalog.db_path.as_posix()}?mode=ro"
        quoted_table = table.replace('"', '""')
        quoted_field = field.replace('"', '""')
        with sqlite3.connect(uri, uri=True) as connection:
            total, null_count, distinct_count, minimum, maximum = connection.execute(
                f'SELECT COUNT(*), SUM(CASE WHEN "{quoted_field}" IS NULL THEN 1 ELSE 0 END), '
                f'COUNT(DISTINCT "{quoted_field}"), MIN("{quoted_field}"), MAX("{quoted_field}") '
                f'FROM "{quoted_table}"'
            ).fetchone()
            values: list[Any] = []
            if int(distinct_count or 0) <= 50:
                values = [
                    row[0]
                    for row in connection.execute(
                        f'SELECT DISTINCT "{quoted_field}" FROM "{quoted_table}" '
                        f'WHERE "{quoted_field}" IS NOT NULL ORDER BY 1 LIMIT 20'
                    ).fetchall()
                ]
        return {
            "row_count": int(total or 0),
            "null_count": int(null_count or 0),
            "distinct_count": int(distinct_count or 0),
            "min": minimum,
            "max": maximum,
            "allowed_values": values,
            "values_truncated": int(distinct_count or 0) > len(values),
        }

    async def execute_readonly_query(
        self, arguments: dict[str, Any], state: AgentRunState
    ) -> AgentToolResult:
        context = self._context(arguments.get("context_id"), state)
        sql = str(arguments.get("sql") or "").strip()
        if not sql:
            raise ValueError("缺少 SQL")
        try:
            validated = self.guard.validate(sql)
            if not validated.tables.issubset(context.tables):
                raise SqlValidationError("SQL 引用了当前上下文之外的数据表")
            scope_issues = self.catalog.scope_filter_issues(
                state.original_question, validated.sql, validated.tables
            )
            if scope_issues:
                raise SqlValidationError("；".join(scope_issues))
        except SqlValidationError as exc:
            return self._result(
                "execute_readonly_query",
                "revision_required",
                "候选查询被确定性安全或目录检查拒绝，请根据真实错误修正后重试。",
                payload={
                    "error_code": "SQL_VALIDATION_FAILED",
                    "guard_error": str(exc),
                    "retryable": True,
                },
                retryable=True,
            )
        try:
            result = await self.executor.execute(validated.sql)
        except QueryTimeoutError:
            return self._result(
                "execute_readonly_query",
                "revision_required",
                "查询执行超时，请缩小范围或降低查询复杂度后重试。",
                payload={"error_code": "QUERY_TIMEOUT", "retryable": True},
                retryable=True,
            )
        except QueryExecutionError as exc:
            error_code, hint = self._classify_execution_error(exc)
            return self._result(
                "execute_readonly_query",
                "revision_required",
                "只读查询执行失败，请结合已加载上下文修改查询。",
                payload={
                    "error_code": error_code,
                    "business_hint": hint,
                    "retryable": True,
                },
                retryable=True,
            )

        sources = tuple(SourceInfo(**item) for item in self.catalog.source_info(validated.tables))
        evidence = self.evidence_builder.build(
            result,
            sources=sources,
            applied_scope=self.catalog.data_domain()["administrative_scope"],
            field_labels=self.catalog.result_field_labels(validated.tables),
        )
        evidence_id = f"ev_{uuid.uuid4().hex}"
        self.evidence[evidence_id] = EvidenceRecord(
            evidence_id=evidence_id,
            context_id=context.context_id,
            tables=frozenset(validated.tables),
            result=result,
            evidence=evidence,
            sources=sources,
        )
        status = "ok" if result.rows else "no_match"
        content = (
            "只读查询执行成功，已生成脱敏结果画像和 Evidence。"
            if result.rows
            else "查询执行成功但结果为空；空结果不能直接解释为数据库没有数据，请检查字段画像、筛选条件或业务口径。"
        )
        return self._result(
            "execute_readonly_query",
            status,
            content,
            payload={
                "evidence_id": evidence_id,
                "result_status": evidence.result_status,
                "row_count": evidence.row_count,
                "truncated": evidence.truncated,
                "columns": [item.model_dump() for item in evidence.columns],
                "rows_preview": evidence.rows_preview,
                "aggregate_profile": evidence.aggregate_profile,
                "source_datasets": evidence.source_datasets,
                "data_as_of": evidence.data_as_of,
                "limitations": evidence.limitations,
            },
            details_ref=evidence_id,
            evidence_ids=[evidence_id],
        )

    async def review_evidence(
        self, arguments: dict[str, Any], state: AgentRunState
    ) -> AgentToolResult:
        records = self._evidence_records(arguments.get("evidence_ids"), state)
        if any(record.evidence.result_status == "no_match" for record in records):
            return self._result(
                "review_evidence",
                "revision_required",
                "Evidence 包含空结果，尚不足以支持结论；请检查筛选、字段画像或请求用户澄清。",
                payload={"error_code": "EMPTY_RESULT_NOT_PROVEN", "retryable": True},
                evidence_ids=[record.evidence_id for record in records],
                retryable=True,
            )
        if any(not record.sources for record in records):
            return self._result(
                "review_evidence",
                "blocked",
                "Evidence 缺少已发布来源，不能形成最终回答。",
                payload={"error_code": "SOURCE_MISSING", "retryable": False},
            )
        if any(not record.evidence.columns for record in records):
            return self._result(
                "review_evidence",
                "blocked",
                "Evidence 不包含可安全展示的结果列，不能形成最终回答。",
                payload={"error_code": "NO_PUBLIC_RESULT_COLUMNS", "retryable": False},
            )
        evidence_ids = [record.evidence_id for record in records]
        return self._result(
            "review_evidence",
            "ok",
            "Evidence 已通过确定性检查，可用于最终回答。",
            payload={
                "approved_evidence_ids": evidence_ids,
                "checks": ["data_found", "source_present", "published_scope", "bounded_result"],
            },
            evidence_ids=evidence_ids,
        )

    async def ask_user_question(
        self, arguments: dict[str, Any], state: AgentRunState
    ) -> AgentToolResult:
        question = str(arguments.get("question") or "").strip()
        unsafe = re.compile(
            r"\b(?:select|from|where|join|ddl|sql|pragma)\b|[A-Za-z0-9]+_[A-Za-z0-9_]+",
            re.IGNORECASE,
        )
        if not question or len(question) > 240 or unsafe.search(question):
            return self._result(
                "ask_user_question",
                "blocked",
                "澄清问题必须使用简短业务语言，不能包含内部结构或 SQL。",
                payload={"error_code": "UNSAFE_CLARIFICATION", "retryable": True},
                retryable=True,
            )
        return self._result(
            "ask_user_question",
            "needs_user_input",
            question,
            payload={"question": question},
            terminate=True,
        )

    async def finalize_answer(
        self, arguments: dict[str, Any], state: AgentRunState
    ) -> AgentToolResult:
        records = self._evidence_records(arguments.get("evidence_ids"), state)
        evidence_ids = [record.evidence_id for record in records]
        if not set(evidence_ids).issubset(set(state.approved_evidence_ids)):
            return self._result(
                "finalize_answer",
                "blocked",
                "Evidence 尚未通过 review_evidence，不能生成最终回答。",
                payload={"error_code": "EVIDENCE_NOT_APPROVED", "retryable": True},
                retryable=True,
            )
        raw_claims = arguments.get("claims")
        if raw_claims is not None:
            try:
                draft = AnswerDraft(
                    text=str(arguments.get("text") or "已根据审核后的数据证据生成回答。"),
                    claims=[AnswerClaim.model_validate(item) for item in raw_claims],
                    limitations=[str(item) for item in arguments.get("limitations", [])],
                )
            except Exception:
                return self._result("finalize_answer", "revision_required", "最终回答 claims 格式不合法，请只引用已审核 Evidence 的字段。", payload={"error_code": "ANSWER_NOT_GROUNDED", "retryable": True}, retryable=True)
            payloads = {record.evidence_id: {"columns": [column.model_dump() for column in record.evidence.columns], "rows": record.evidence.rows_preview} for record in records}
            valid, code = self.answer_verifier.verify(draft, approved_evidence_ids=set(state.approved_evidence_ids), evidence_payloads=payloads)
            if not valid:
                return self._result("finalize_answer", "revision_required", "回答包含未被当前 Evidence 支持的事实，请重新检查证据后再回答。", payload={"error_code": code or "ANSWER_NOT_GROUNDED", "retryable": True}, retryable=True)
        primary = records[0]
        data_as_of = max(
            (source.data_as_of for record in records for source in record.sources if source.data_as_of),
            default=None,
        )
        data = self._query_data(primary, data_as_of)
        result_sets = [
            ResultSet(
                id="primary" if index == 0 else "supplemental",
                purpose="主查询" if index == 0 else "补充证据",
                data=self._query_data(record, data_as_of),
            )
            for index, record in enumerate(records[:2])
        ]
        all_sources = list(
            {
                (source.dataset, source.version, source.data_as_of): source
                for record in records
                for source in record.sources
            }.values()
        )
        response = ToolResponse(
            success=True,
            request_id=state.request_id,
            data=data,
            result_sets=result_sets,
            sources=all_sources,
            warnings=["RESULT_TRUNCATED"] if primary.result.truncated else [],
            limitations=list(
                dict.fromkeys(
                    limitation
                    for record in records
                    for limitation in record.evidence.limitations
                )
            ),
            coverage=self._coverage(records),
            answer_guidance={
                "response_mode": "evidence_grounded",
                "hard_constraints": ["所有数值均来自已审核 Evidence", "不得补造未查询事实"],
            },
        )
        self.final_responses[state.run_id] = response
        answer = self._render_answer(records, data_as_of)
        return self._result(
            "finalize_answer",
            "ok",
            "最终回答已由已审核 Evidence 确定性生成。",
            payload={"answer": answer},
            evidence_ids=evidence_ids,
            terminate=True,
        )

    def _context(self, context_id: Any, state: AgentRunState) -> LoadedContext:
        key = str(context_id or "")
        context = self.contexts.get(key)
        if context is None or key not in state.loaded_context_ids:
            raise ValueError("context_id 不属于当前运行")
        if context.question != state.original_question:
            raise ValueError("上下文原始问题不一致")
        return context

    def _evidence_records(
        self, raw_ids: Any, state: AgentRunState
    ) -> list[EvidenceRecord]:
        if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 2:
            raise ValueError("evidence_ids 必须包含 1-2 个 Evidence")
        ids = [str(item) for item in raw_ids]
        if len(set(ids)) != len(ids) or not set(ids).issubset(set(state.evidence_ids)):
            raise ValueError("Evidence 不属于当前运行")
        return [self.evidence[evidence_id] for evidence_id in ids]

    @staticmethod
    def _query_data(record: EvidenceRecord, data_as_of: str | None) -> QueryData:
        result = record.result
        visible_names = {column.name for column in record.evidence.columns}
        rows = [
            {key: value for key, value in row.items() if key in visible_names}
            for row in record.evidence.rows_preview
        ]
        schema = []
        labels = {column.name: column.semantic_label for column in record.evidence.columns}
        for column in result.schema:
            if column.get("name") not in visible_names:
                continue
            public_column = dict(column)
            public_column["semantic_label"] = labels.get(str(column.get("name")))
            schema.append(public_column)
        return QueryData(
            rows=rows,
            summary=(
                dict(rows[0])
                if len(rows) == 1
                else {"row_count": result.row_count, "truncated": result.truncated}
            ),
            schema_=schema,
            data_as_of=data_as_of,
            result_status="data_found" if rows else "no_match",
            result_reason=None if rows else "当前合法筛选条件下未匹配到记录",
        )

    @staticmethod
    def _classify_execution_error(error: QueryExecutionError) -> tuple[str, str]:
        """Expose a stable, non-sensitive error category to the controller."""
        raw = str(getattr(error, "__cause__", "") or error).casefold()
        if "no such column" in raw:
            return "SQLITE_NO_SUCH_COLUMN", "请依据已加载的字段语义修正字段。"
        if "no such table" in raw:
            return "SQLITE_NO_SUCH_TABLE", "请重新加载相关已发布表上下文。"
        if "ambiguous column" in raw:
            return "AMBIGUOUS_COLUMN", "请为关联查询字段补充明确的已发布表范围。"
        if "syntax error" in raw or "near " in raw:
            return "SQL_SYNTAX_ERROR", "请依据 SQLite 语法和已加载上下文修正查询。"
        return "QUERY_EXECUTION_FAILED", "请检查查询条件、聚合和字段语义后修正。"

    def _coverage(self, records: list[EvidenceRecord]) -> Coverage:
        dimensions: list[str] = []
        measures: list[str] = []
        for record in records:
            for column in record.evidence.columns:
                name = column.semantic_label or column.name
                target = measures if column.type in {"integer", "number"} else dimensions
                if name and name not in target:
                    target.append(name)
        return Coverage(
            applied_scope=self.catalog.data_domain()["administrative_scope"],
            dimensions=dimensions,
            measures=measures,
        )

    @staticmethod
    def _render_answer(records: list[EvidenceRecord], data_as_of: str | None) -> str:
        primary = records[0]
        rows = primary.evidence.rows_preview
        if len(rows) == 1:
            body = "；".join(f"{key}：{value}" for key, value in rows[0].items())
        else:
            body = f"共返回 {primary.result.row_count} 条结果：" + json.dumps(
                rows, ensure_ascii=False, separators=(",", ":")
            )
        sources = "、".join(source.dataset for source in primary.sources)
        snapshot = f"，数据截至 {data_as_of}" if data_as_of else ""
        return f"{body}。来源：{sources}{snapshot}。"

    @staticmethod
    def _result(
        tool_name: str,
        status: str,
        content: str,
        *,
        payload: dict[str, Any] | None = None,
        details_ref: str | None = None,
        evidence_ids: list[str] | None = None,
        retryable: bool = False,
        terminate: bool = False,
    ) -> AgentToolResult:
        return AgentToolResult(
            tool_name=tool_name,
            status=status,
            content=content,
            model_payload=payload or {},
            details_ref=details_ref,
            evidence_ids=evidence_ids or [],
            retryable=retryable,
            terminate=terminate,
        )
