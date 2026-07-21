import hashlib
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
        *,
        table_cards_path: str | Path | None = None,
        ddl_registry_path: str | Path | None = None,
        query_knowledge_path: str | Path | None = None,
        validation_cases_path: str | Path | None = None,
        ddl_directory: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.catalog_path = Path(catalog_path)
        self.examples_path = Path(examples_path)
        raw_catalog = self._load_json(self.catalog_path)
        self._datasets = {
            item["table"]: item for item in raw_catalog.get("datasets", [])
        }
        self._examples = self._load_json(self.examples_path)
        self.ddl_directory = Path(ddl_directory) if ddl_directory else None
        self._ddl_registry = self._load_optional_json(ddl_registry_path, {}).get(
            "tables", {}
        )
        table_cards = self._load_optional_json(table_cards_path, {}).get(
            "table_cards", []
        )
        self._table_cards = {
            card["table"]: card for card in table_cards if card.get("table")
        }
        if not self._table_cards:
            self._table_cards = {
                table: self._fallback_table_card(table, dataset)
                for table, dataset in self._datasets.items()
            }
        self._rules = self._load_optional_json(query_knowledge_path, {}).get(
            "rules", []
        )
        self._validation_cases = self._load_optional_json(
            validation_cases_path, {}
        ).get("cases", [])

    @staticmethod
    def _load_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _load_optional_json(path: str | Path | None, default: Any) -> Any:
        if path is None:
            return default
        resolved = Path(path)
        if not resolved.is_file():
            raise CatalogError(f"配置文件不存在: {resolved}")
        return json.loads(resolved.read_text(encoding="utf-8"))

    @staticmethod
    def _fallback_table_card(table: str, dataset: dict[str, Any]) -> dict[str, Any]:
        return {
            "table": table,
            "dataset": dataset.get("dataset", table),
            "description": dataset.get("description", ""),
            "aliases": dataset.get("aliases", {}),
            "metrics": [],
            "dimensions": [],
            "important_fields": [],
        }

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

    def table_cards(self) -> list[dict[str, Any]]:
        missing = self.allowed_tables - set(self._table_cards)
        if missing:
            raise CatalogError(f"缺少表卡配置: {', '.join(sorted(missing))}")
        return [self._table_cards[table] for table in sorted(self.allowed_tables)]

    def table_card_issues(self) -> list[str]:
        issues: list[str] = []
        for card in self.table_cards():
            table = card["table"]
            allowed = self.allowed_columns(table)
            for term, field in card.get("aliases", {}).items():
                if field not in allowed:
                    issues.append(
                        f"{table}.aliases.{term} 引用未发布字段 {field}"
                    )
            for field in card.get("important_fields", []):
                if field not in allowed:
                    issues.append(
                        f"{table}.important_fields 引用未发布字段 {field}"
                    )
            for section in ("metrics", "dimensions"):
                for item in card.get(section, []):
                    if not isinstance(item, dict):
                        continue
                    for field in item.get("fields", []):
                        if field not in allowed:
                            issues.append(
                                f"{table}.{section}.{item.get('name', '')} "
                                f"引用未发布字段 {field}"
                            )
        return issues

    def runtime_rule_issues(self) -> list[str]:
        issues: list[str] = []
        for rule in self._rules:
            if not (
                rule.get("status") == "published"
                and rule.get("runtime_enabled") is True
            ):
                continue
            for table, fields in rule.get("required_fields", {}).items():
                if table not in self.allowed_tables:
                    issues.append(f"规则 {rule.get('id')} 引用未发布表 {table}")
                    continue
                allowed = self.allowed_columns(table)
                for field in fields:
                    if field not in allowed:
                        issues.append(
                            f"规则 {rule.get('id')} 引用未发布字段 {table}.{field}"
                        )
        return issues

    def build_planning_context(self) -> str:
        cards: list[dict[str, Any]] = []
        for card in self.table_cards():
            cards.append(
                {
                    "table": card["table"],
                    "dataset": card.get("dataset", card["table"]),
                    "description": card.get("description", ""),
                    "business_terms": sorted(card.get("aliases", {}).keys()),
                    "metrics": card.get("metrics", []),
                    "dimensions": card.get("dimensions", []),
                    "data_limitations": card.get("data_limitations", []),
                }
            )
        return "\n".join(
            [
                "以下是全部已发布表的轻量表卡。先选择 1-4 张相关表，不得猜测表名或字段名。",
                json.dumps(cards, ensure_ascii=False),
            ]
        )

    def match_tables(self, question: str, max_tables: int = 4) -> list[str]:
        """仅保留给诊断和离线分析；运行路径不再依赖关键词匹配。"""
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

    def load_ddl(self, table: str) -> str:
        self.dataset(table)
        if not self.ddl_directory or table not in self._ddl_registry:
            raise CatalogError(f"未注册 DDL: {table}")
        entry = self._ddl_registry[table]
        path = (self.ddl_directory / entry["file"]).resolve()
        if path.parent != self.ddl_directory.resolve() or not path.is_file():
            raise CatalogError(f"DDL 文件不可用: {table}")
        ddl = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
        if digest != entry.get("sha256"):
            raise CatalogError(f"DDL 校验失败: {table}")
        if f'CREATE TABLE "{table}"' not in ddl:
            raise CatalogError(f"DDL 表名不匹配: {table}")
        self._validate_ddl_schema(table, ddl)
        return ddl

    def _validate_ddl_schema(self, table: str, ddl: str) -> None:
        with sqlite3.connect(":memory:") as ddl_connection:
            ddl_connection.executescript(ddl)
            ddl_columns = self._schema_signature(ddl_connection, table)
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as data_connection:
            data_columns = self._schema_signature(data_connection, table)
        if ddl_columns != data_columns:
            raise CatalogError(f"DDL 与 SQLite 结构不一致: {table}")

    def _schema_signature(
        self, connection: sqlite3.Connection, table: str
    ) -> list[tuple[str, str, int, int]]:
        return [
            (row[1], (row[2] or "").upper(), int(row[3]), int(row[5]))
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        ]

    def _published_ddl(self, table: str) -> str:
        ddl = self.load_ddl(table)
        with sqlite3.connect(":memory:") as connection:
            connection.executescript(ddl)
            columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        published = self.allowed_columns(table)
        definitions = [
            f'"{name}" {column_type or "TEXT"}'
            for _, name, column_type, *_ in columns
            if name in published
        ]
        return f'CREATE TABLE "{table}" (\n    ' + ",\n    ".join(definitions) + "\n);"

    @staticmethod
    def _rank_by_terms(
        items: list[dict[str, Any]], question: str, max_items: int
    ) -> list[dict[str, Any]]:
        normalized = question.casefold()

        def score(item: dict[str, Any]) -> int:
            return sum(
                normalized.count(str(term).casefold())
                for term in item.get("terms", [])
                if term
            )

        return sorted(items, key=lambda item: (-score(item), item.get("id", "")))[:max_items]

    def _selected_examples(self, question: str, tables: set[str]) -> list[dict[str, Any]]:
        applicable = [
            example
            for example in self._examples
            if set(example.get("tables", []))
            and set(example.get("tables", [])).issubset(tables)
        ]
        for example in applicable:
            example.setdefault("terms", [example.get("question", "")])
        return self._rank_by_terms(applicable, question, 3)

    def _published_rules(self, question: str, tables: set[str]) -> list[dict[str, Any]]:
        applicable = [
            rule
            for rule in self._rules
            if rule.get("status") == "published"
            and rule.get("runtime_enabled") is True
            and set(rule.get("scope_tables", [])).issubset(tables)
        ]
        return self._rank_by_terms(applicable, question, 3)

    def build_sql_context(self, question: str, tables: list[str]) -> str:
        if not tables:
            raise CatalogError("未选择任何已发布数据表")
        requested = set(tables)
        if not requested.issubset(self.allowed_tables):
            raise CatalogError("SQL 上下文包含未发布数据表")
        if not self._ddl_registry:
            return self.build_context(tables)
        ddl_blocks = [self._published_ddl(table) for table in sorted(requested)]
        limitations = [
            {
                "table": table,
                "data_limitations": self._table_cards[table].get(
                    "data_limitations", []
                ),
            }
            for table in sorted(requested)
            if self._table_cards[table].get("data_limitations")
        ]
        rules = self._published_rules(question, requested)
        examples = self._selected_examples(question, requested)
        return "\n\n".join(
            [
                "以下为本次已选表的已发布字段 DDL；不得使用其他表或字段。",
                *ddl_blocks,
                "数据限制：",
                json.dumps(limitations, ensure_ascii=False) if limitations else "无",
                "已发布业务规则：",
                json.dumps(rules, ensure_ascii=False) if rules else "无",
                "已验证示例：",
                json.dumps(examples, ensure_ascii=False) if examples else "无",
            ]
        )

    def build_context(self, tables: list[str], max_examples: int = 5) -> str:
        if not tables:
            raise CatalogError("未选择任何已发布数据表")
        if self._ddl_registry:
            return self.build_sql_context("", tables)
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
        examples = self._selected_examples("", requested)[:max_examples]
        return "\n\n".join(
            [
                "以下是本次查询允许使用的数据目录。不得使用未列出的表和字段。",
                *blocks,
                "已验证示例（仅作生成参考，仍需独立校验）：",
                json.dumps(examples, ensure_ascii=False) if examples else "无",
            ]
        )

    def validation_case(self, question: str) -> dict[str, Any] | None:
        normalized = question.strip()
        return next(
            (
                case
                for case in self._validation_cases
                if case.get("question", "").strip() == normalized
            ),
            None,
        )

    def published_rule_ids(self) -> set[str]:
        return {
            rule["id"]
            for rule in self._rules
            if rule.get("status") == "published" and rule.get("runtime_enabled") is True
        }

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
