import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class QueryExecutionError(RuntimeError):
    pass


class QueryTimeoutError(QueryExecutionError):
    pass


@dataclass(frozen=True)
class ExecutionResult:
    rows: list[dict[str, Any]]
    row_count: int
    schema: list[dict[str, str]]
    truncated: bool
    duration_ms: int


class SQLiteExecutor:
    def __init__(
        self,
        db_path: str | Path,
        timeout_seconds: float,
        max_rows: int,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.timeout_seconds = timeout_seconds
        self.max_rows = max_rows

    async def execute(self, sql: str) -> ExecutionResult:
        return await asyncio.to_thread(self._execute_sync, sql)

    def _execute_sync(self, sql: str) -> ExecutionResult:
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.set_progress_handler(
                    lambda: 1 if time.monotonic() >= deadline else 0,
                    100,
                )
                cursor = connection.execute(sql)
                fetched = cursor.fetchmany(self.max_rows + 1)
                truncated = len(fetched) > self.max_rows
                rows = [dict(row) for row in fetched[: self.max_rows]]
                schema = self._build_schema(cursor.description or [], rows)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower() or time.monotonic() >= deadline:
                raise QueryTimeoutError("查询执行超时") from exc
            raise QueryExecutionError("SQLite 查询执行失败") from exc
        except sqlite3.DatabaseError as exc:
            raise QueryExecutionError("SQLite 查询执行失败") from exc

        return ExecutionResult(
            rows=rows,
            row_count=len(rows),
            schema=schema,
            truncated=truncated,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _build_schema(
        description: list[tuple[Any, ...]], rows: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for item in description:
            name = str(item[0])
            value = next(
                (row[name] for row in rows if row.get(name) is not None),
                None,
            )
            column = {"name": name, "type": SQLiteExecutor._json_type(value)}
            unit = SQLiteExecutor._infer_unit(name)
            if unit is not None:
                column["unit"] = unit
            result.append(column)
        return result

    @staticmethod
    def _infer_unit(name: str) -> str | None:
        """Infer only units encoded unambiguously in a result column name.

        SQL aliases are part of the public result contract. Keeping this mapping
        deliberately conservative avoids asking an LLM to guess a display unit.
        """
        normalized = name.strip().lower()
        suffix_units = (
            ("_100m_yuan", "亿元"),
            ("_10k_kwh", "万kWh"),
            ("_mwh", "MWh"),
            ("_mva", "MVA"),
            ("_mw", "MW"),
            ("_kv", "kV"),
            ("_pct", "%"),
        )
        for suffix, unit in suffix_units:
            if normalized.endswith(suffix):
                return unit
        chinese_units = (
            (("装机容量", "并网容量", "接入容量"), "MW"),
            (("电压等级",), "kV"),
            (("占比", "百分比", "增速", "限电率", "利用率"), "%"),
        )
        for terms, unit in chinese_units:
            if any(term in name for term in terms):
                return unit
        return None

    @staticmethod
    def _json_type(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, bytes):
            return "binary"
        return "string"
