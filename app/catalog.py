import json
import sqlite3
from pathlib import Path
from typing import Any


class CatalogError(ValueError):
    pass


class MetadataCatalog:
    def __init__(
        self,
        db_path: str | Path,
        catalog_path: str | Path,
        examples_path: str | Path,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.catalog_path = Path(catalog_path)
        self.examples_path = Path(examples_path)
        raw_catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        self._datasets = {
            item["table"]: item for item in raw_catalog.get("datasets", [])
        }
        self._examples = json.loads(
            self.examples_path.read_text(encoding="utf-8")
        )

    @property
    def allowed_tables(self) -> set[str]:
        return set(self._datasets)

    def dataset(self, table: str) -> dict[str, Any]:
        try:
            return self._datasets[table]
        except KeyError as exc:
            raise CatalogError(f"未发布的数据表: {table}") from exc

    def allowed_columns(self, table: str) -> set[str]:
        dataset = self.dataset(table)
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not rows:
            raise CatalogError(f"数据表不存在: {table}")
        excluded = set(dataset.get("excluded_columns", []))
        return {row[1] for row in rows if row[1] not in excluded}

    def match_tables(self, question: str, max_tables: int = 4) -> list[str]:
        normalized = question.casefold()
        scored: list[tuple[int, str]] = []
        for table, dataset in self._datasets.items():
            terms = [
                dataset.get("dataset", ""),
                dataset.get("description", ""),
                *dataset.get("keywords", []),
                *dataset.get("aliases", {}).values(),
            ]
            score = sum(1 for term in terms if term and term.casefold() in normalized)
            if score:
                scored.append((score, table))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [table for _, table in scored[:max_tables]]

    def build_context(self, tables: list[str], max_examples: int = 5) -> str:
        if not tables:
            raise CatalogError("未选择任何已发布数据表")

        blocks: list[str] = []
        for table in tables:
            dataset = self.dataset(table)
            columns = sorted(self.allowed_columns(table))
            aliases = dataset.get("aliases", {})
            column_lines = [
                f'- "{column}" ({aliases.get(column, column)})' for column in columns
            ]
            blocks.append(
                "\n".join(
                    [
                        f'表: "{table}"',
                        f'数据集: {dataset.get("dataset", table)}',
                        f'说明: {dataset.get("description", "")}',
                        "允许字段:",
                        *column_lines,
                    ]
                )
            )

        requested = set(tables)
        examples = [
            example
            for example in self._examples
            if set(example.get("tables", [])).issubset(requested)
            and set(example.get("tables", []))
        ][:max_examples]
        example_text = "\n".join(
            json.dumps(example, ensure_ascii=False) for example in examples
        )
        return "\n\n".join(
            [
                "以下是本次查询允许使用的数据目录。不得使用未列出的表和字段。",
                *blocks,
                "已验证示例（仅作生成参考，仍需独立校验）:",
                example_text or "无",
            ]
        )

    def exact_example(self, question: str) -> dict[str, Any] | None:
        normalized_question = question.strip()
        for example in self._examples:
            tables = set(example.get("tables", []))
            if (
                example.get("question", "").strip() == normalized_question
                and tables
                and tables.issubset(self.allowed_tables)
                and isinstance(example.get("sql"), str)
            ):
                return example
        return None

    def source_info(self, tables: set[str]) -> list[dict[str, Any]]:
        return [
            {
                "dataset": self.dataset(table).get("dataset", table),
                "version": self.dataset(table).get("version"),
                "data_as_of": self.dataset(table).get("data_as_of"),
            }
            for table in sorted(tables)
        ]
