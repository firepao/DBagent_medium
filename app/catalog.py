import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class RoutingDecision:
    """A deterministic, pre-planning decision derived from published configuration."""

    intent_id: str
    action: str
    required_tables: tuple[str, ...]
    message: str | None = None
    match_type: str = "exact_question"
    customer_context: dict[str, Any] | None = None


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
        administrative_regions_path: str | Path | None = None,
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
        knowledge = self._load_optional_json(query_knowledge_path, {})
        self._rules = knowledge.get("rules", [])
        self._routing_rules = knowledge.get("routing_rules", [])
        self._answer_guidance = knowledge.get("answer_guidance", {})
        self._validation_cases = self._load_optional_json(
            validation_cases_path, {}
        ).get("cases", [])
        self._administrative_regions = self._load_optional_json(
            administrative_regions_path, {}
        )
        self._field_semantics_cache: dict[str, list[dict[str, str]]] = {}

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
            coverage = card.get("coverage")
            if not isinstance(coverage, str) or not coverage.strip():
                issues.append(f"{table}.coverage 缺少业务覆盖范围说明")
            supported_queries = card.get("supported_queries")
            if not (
                isinstance(supported_queries, list)
                and supported_queries
                and all(isinstance(item, str) and item.strip() for item in supported_queries)
            ):
                issues.append(f"{table}.supported_queries 缺少支持问题说明")
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
                (rule.get("status") == "published" and rule.get("runtime_enabled") is True)
                or rule.get("reference_enabled") is True
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
        for rule in self._routing_rules:
            if not (
                rule.get("status") == "published"
                and rule.get("runtime_enabled") is True
            ):
                continue
            if rule.get("action", "allow") not in {
                "allow",
                "reject_capability",
                "reject_scope",
            }:
                issues.append(f"路由规则 {rule.get('id')} action 无效")
            for table in rule.get("required_tables", []):
                if table not in self.allowed_tables:
                    issues.append(f"路由规则 {rule.get('id')} 引用未发布表 {table}")
        return issues

    def region_rule_issues(self) -> list[str]:
        if not self._administrative_regions:
            return []
        issues: list[str] = []
        areas = self._administrative_regions.get("areas", [])
        if not areas:
            issues.append("行政区配置缺少 areas")
        for area in areas:
            if not isinstance(area.get("name"), str) or not area["name"].strip():
                issues.append("行政区配置包含空名称")
            aliases = area.get("aliases", [])
            if not isinstance(aliases, list) or not aliases:
                issues.append(f"行政区 {area.get('name')} 缺少 aliases")
        for table, field in self._administrative_regions.get(
            "table_location_fields", {}
        ).items():
            if table not in self.allowed_tables:
                issues.append(f"行政区配置引用未发布表 {table}")
            elif field not in self.allowed_columns(table):
                issues.append(f"行政区配置引用未发布字段 {table}.{field}")
        return issues

    def routing_decision(self, question: str) -> RoutingDecision | None:
        """Resolve published routing policy before handing table selection to the LLM.

        Exact validation-question matches are intentional: they are the stable intent IDs
        from the customer question set. Only when no exact intent matches do we use
        explicitly published lightweight routing terms.
        """
        case = self.validation_case(question)
        if case is not None:
            customer_context = {
                key: case[key]
                for key in (
                    "source_reference",
                    "reported_issue",
                    "missing_data_or_policy",
                    "customer_note",
                )
                if case.get(key)
            }
            if case.get("routing_enabled") is not True:
                return RoutingDecision(
                    intent_id=str(case.get("id") or "validation_case"),
                    action="advisory",
                    required_tables=(),
                    message=str(case.get("reason") or "") or None,
                    customer_context=customer_context or None,
                )
            status = case.get("status")
            if status in {
                "supported",
                "not_supported",
                "needs_customer_confirmation",
                "out_of_scope",
            }:
                action = {
                    "supported": "allow",
                    "not_supported": "reject_capability",
                    "needs_customer_confirmation": "reject_capability",
                    "out_of_scope": "reject_scope",
                }[status]
                return RoutingDecision(
                    intent_id=str(case.get("id") or "validation_case"),
                    action=action,
                    required_tables=tuple(case.get("scope_tables") or []),
                    message=str(case.get("reason") or "") or None,
                    customer_context=customer_context or None,
                )

        published = [
            rule
            for rule in self._routing_rules
            if rule.get("status") == "published"
            and rule.get("runtime_enabled") is True
        ]
        matched = self._rank_by_terms(published, question, 1)
        if not matched:
            return None
        rule = matched[0]
        required_tables = tuple(rule.get("required_tables") or [])
        if not set(required_tables).issubset(self.allowed_tables):
            raise CatalogError(f"路由规则 {rule.get('id')} 引用了未发布数据表")
        return RoutingDecision(
            intent_id=str(rule.get("id") or "routing_rule"),
            action=str(rule.get("action") or "allow"),
            required_tables=required_tables,
            message=str(rule.get("message") or "") or None,
            match_type="lightweight_terms",
        )

    @staticmethod
    def routing_context(decision: RoutingDecision | None) -> dict[str, Any] | None:
        if decision is None:
            return None
        return {
            "intent_id": decision.intent_id,
            "action": decision.action,
            "required_tables": list(decision.required_tables),
            "message": decision.message,
            "match_type": decision.match_type,
            "customer_context": decision.customer_context,
        }

    def _region_context(self, tables: set[str]) -> list[dict[str, Any]]:
        if not self._administrative_regions:
            return []
        areas = self._administrative_regions.get("areas", [])
        mappings = self._administrative_regions.get("table_location_fields", {})
        rules: list[dict[str, Any]] = []
        for table in sorted(tables):
            field = mappings.get(table)
            if not field:
                continue
            clauses = []
            for area in areas:
                name = area["name"].replace("'", "''")
                for alias in area.get("aliases", []):
                    escaped = str(alias).replace("'", "''")
                    clauses.append(
                        f"WHEN {{location_field}} LIKE '%{escaped}%' THEN '{name}'"
                    )
            rules.append(
                {
                    "table": table,
                    "location_field": field,
                    "derived_dimension": self._administrative_regions.get(
                        "derived_dimension", "county"
                    ),
                    "sql_template": "CASE\n    "
                    + "\n    ".join(clauses)
                    + "\n    ELSE '待核实'\nEND",
                    "default_scope": self._administrative_regions.get(
                        "default_scope", ""
                    ),
                    "unmatched_policy": self._administrative_regions.get(
                        "unmatched_policy", ""
                    ),
                }
            )
        return rules

    def build_planning_context(
        self, decision: RoutingDecision | None = None
    ) -> str:
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
                    "supported_queries": card.get("supported_queries", []),
                    "important_field_semantics": self._field_semantics(
                        card["table"], card.get("important_fields", [])
                    ),
                    "data_limitations": card.get("data_limitations", []),
                }
            )
        return "\n".join(
            [
                "数据域事实：本服务发布的全部业务数据均属于张家口市全域。用户提到“张家口”“全市”“全域”时，表示使用当前数据集全部记录，不需要 city/city_name 等城市字段，也不得添加地址 LIKE '%张家口%' 条件。只有用户明确指定区县、乡镇、项目或场站时才添加对应筛选。",
                "以下是全部已发布表的轻量表卡。先选择 1-4 张相关表，不得猜测表名或字段名。",
                json.dumps(cards, ensure_ascii=False),
                "本次命中的前置路由规则：",
                json.dumps(self.routing_context(decision), ensure_ascii=False)
                if decision is not None
                else "无。",
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

    def _field_semantics(
        self, table: str, fields: list[str] | None = None
    ) -> list[dict[str, str]]:
        if table not in self._field_semantics_cache:
            published = self.allowed_columns(table)
            if not self._ddl_registry:
                dataset_aliases = self.dataset(table).get("aliases", {})
                labels = {field: label for field, label in dataset_aliases.items()}
                with sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True) as connection:
                    columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
                self._field_semantics_cache[table] = [
                    {
                        "name": name,
                        "label": labels.get(name, name),
                        "type": column_type or "TEXT",
                        "description": "无补充字段说明。",
                    }
                    for _, name, column_type, *_ in columns
                    if name in published
                ]
            else:
                ddl = self.load_ddl(table)
                metadata: dict[str, dict[str, str]] = {}
                pattern = re.compile(
                    r"^-- 字段：(?P<name>[^|]+)\s*\|\s*别名：(?P<label>[^|]+)\s*"
                    r"\|\s*类型：(?P<type>[^|]+)\s*\|\s*说明：(?P<description>.+)$"
                )
                for line in ddl.splitlines():
                    matched = pattern.match(line.strip())
                    if matched:
                        entry = {
                            key: value.strip() for key, value in matched.groupdict().items()
                        }
                        metadata[entry["name"]] = entry
                with sqlite3.connect(":memory:") as connection:
                    connection.executescript(ddl)
                    columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
                self._field_semantics_cache[table] = [
                    metadata.get(
                        name,
                        {
                            "name": name,
                            "label": name,
                            "type": column_type or "TEXT",
                            "description": "无补充字段说明。",
                        },
                    )
                    for _, name, column_type, *_ in columns
                    if name in published
                ]
        requested = set(fields or [])
        entries = self._field_semantics_cache[table]
        return [entry for entry in entries if not requested or entry["name"] in requested]

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

        ranked = [item for item in items if score(item) > 0]
        return sorted(ranked, key=lambda item: (-score(item), item.get("id", "")))[:max_items]

    def _selected_examples(self, question: str, tables: set[str]) -> list[dict[str, Any]]:
        applicable = [
            example
            for example in self._examples
            if set(example.get("tables", []))
            and set(example.get("tables", [])).issubset(tables)
        ]
        ranked_examples = [
            {**example, "terms": example.get("terms") or [example.get("question", "")]}
            for example in applicable
        ]
        if not question.strip():
            return ranked_examples[:3]
        return self._rank_by_terms(ranked_examples, question, 3)

    def _rules_for_context(
        self,
        question: str,
        tables: set[str],
        decision: RoutingDecision | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        applicable = [
            rule
            for rule in self._rules
            if set(rule.get("scope_tables", [])).issubset(tables)
        ]

        def select(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            exact = (
                [
                    item
                    for item in items
                    if decision is not None
                    and decision.intent_id in item.get("source_question_ids", [])
                ]
                if decision is not None
                else []
            )
            exact_ids = {item.get("id") for item in exact}
            terms = self._rank_by_terms(
                [item for item in items if item.get("id") not in exact_ids],
                question,
                3,
            )
            return (exact + terms)[:3]

        published = select(
            [
                rule
                for rule in applicable
                if rule.get("status") == "published"
                and rule.get("runtime_enabled") is True
            ]
        )
        references = select(
            [rule for rule in applicable if rule.get("reference_enabled") is True]
        )
        return published, references

    def answer_guidance(
        self,
        question: str,
        tables: set[str],
        decision: RoutingDecision | None,
    ) -> dict[str, Any] | None:
        default = self._answer_guidance.get("default")
        if not isinstance(default, dict):
            return None
        profiles = self._answer_guidance.get("profiles", [])
        applicable = [
            profile
            for profile in profiles
            if set(profile.get("scope_tables", [])).issubset(tables)
        ]
        exact = [
            profile
            for profile in applicable
            if decision is not None
            and decision.intent_id in profile.get("source_question_ids", [])
        ]
        selected = exact[:1] or self._rank_by_terms(applicable, question, 1)
        result = {**default}
        if selected:
            profile = selected[0]
            result["profile_id"] = profile.get("id")
            result["profile_name"] = profile.get("name")
            result["profile_guidance"] = profile.get("guidance", {})
        return result

    def _published_rules(self, question: str, tables: set[str]) -> list[dict[str, Any]]:
        # Compatibility helper for callers that do not carry a routing decision.
        return self._rules_for_context(question, tables, None)[0]

    def build_sql_context(
        self,
        question: str,
        tables: list[str],
        decision: RoutingDecision | None = None,
    ) -> str:
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
        rules, references = self._rules_for_context(question, requested, decision)
        examples = self._selected_examples(question, requested)
        region_rules = self._region_context(requested)
        return "\n\n".join(
            [
                "数据域事实：本服务发布的全部业务数据均属于张家口市全域。“张家口”“全市”“全域”是默认全集范围，不需要城市字段，不得添加地址 LIKE '%张家口%' 条件；仅在用户明确指定区县、乡镇、项目或场站时筛选。",
                "以下为本次已选表的已发布字段 DDL；不得使用其他表或字段。",
                *ddl_blocks,
                "字段语义：",
                json.dumps(
                    [
                        {
                            "table": table,
                            "dataset": self._table_cards[table].get("dataset", table),
                            "description": self._table_cards[table].get("description", ""),
                            "supported_queries": self._table_cards[table].get("supported_queries", []),
                            "fields": self._field_semantics(table),
                        }
                        for table in sorted(requested)
                    ],
                    ensure_ascii=False,
                ),
                "数据限制：",
                json.dumps(limitations, ensure_ascii=False) if limitations else "无",
                "已发布业务规则：",
                json.dumps(rules, ensure_ascii=False) if rules else "无",
                "待确认辅助规则：仅用于识别数据缺口和回答边界，严禁据此生成计算 SQL：",
                json.dumps(references, ensure_ascii=False) if references else "无",
                "行政区派生规则：县区问题必须使用对应表的 location_field 替换 sql_template 中的 {location_field}；待核实记录不得进入县区排名、占比或增速分母：",
                json.dumps(region_rules, ensure_ascii=False) if region_rules else "无",
                "已验证示例：",
                json.dumps(examples, ensure_ascii=False) if examples else "无",
                "前置路由约束：",
                json.dumps(self.routing_context(decision), ensure_ascii=False)
                if decision is not None
                else "无",
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
                "dataset": self.dataset(table).get("dataset")
                or self._table_cards.get(table, {}).get("dataset")
                or table,
                "version": self.dataset(table).get("version"),
                "data_as_of": self.dataset(table).get("data_as_of"),
            }
            for table in sorted(tables)
        ]
