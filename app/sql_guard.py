from dataclasses import dataclass

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from app.catalog import CatalogError, MetadataCatalog


class SqlValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedSql:
    sql: str
    tables: set[str]
    columns: set[str]


class SqlGuard:
    ALLOWED_FUNCTIONS = {
        "ABS",
        "AVG",
        "CAST",
        "COALESCE",
        "COUNT",
        "DATE",
        "DATETIME",
        "IFNULL",
        "LENGTH",
        "LOWER",
        "MAX",
        "MIN",
        "NULLIF",
        "ROUND",
        "STRFTIME",
        "SUBSTRING",
        "SUBSTR",
        "SUM",
        "TRIM",
        "UPPER",
    }

    def __init__(self, catalog: MetadataCatalog, max_rows: int) -> None:
        self.catalog = catalog
        self.max_rows = max_rows

    def validate(self, sql: str) -> ValidatedSql:
        if not sql or not sql.strip():
            raise SqlValidationError("SQL 为空")
        if "--" in sql or "/*" in sql or "*/" in sql:
            raise SqlValidationError("SQL 不允许包含注释")

        try:
            statements = [item for item in parse(sql, read="sqlite") if item]
        except ParseError as exc:
            raise SqlValidationError("SQL 无法解析") from exc
        if len(statements) != 1:
            raise SqlValidationError("只允许单条 SQL")

        statement = statements[0]
        if not isinstance(statement, exp.Select):
            raise SqlValidationError("只允许 SELECT 或 WITH...SELECT")
        self._reject_forbidden_nodes(statement)

        cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
        base_tables: set[str] = set()
        alias_to_table: dict[str, str] = {}
        for table in statement.find_all(exp.Table):
            name = table.name
            if name in cte_names:
                continue
            if name not in self.catalog.allowed_tables:
                raise SqlValidationError(f"未发布的数据表: {name}")
            base_tables.add(name)
            alias_to_table[table.alias_or_name] = name
            alias_to_table[name] = name
        if not base_tables:
            raise SqlValidationError("查询没有引用已发布数据表")

        allowed_by_table = {
            table: self.catalog.allowed_columns(table) for table in base_tables
        }
        all_allowed_columns = set().union(*allowed_by_table.values())
        columns: set[str] = set()
        for column in statement.find_all(exp.Column):
            name = column.name
            qualifier = column.table
            if qualifier and qualifier in alias_to_table:
                if name not in allowed_by_table[alias_to_table[qualifier]]:
                    raise SqlValidationError(f"未发布的字段: {qualifier}.{name}")
            elif name not in all_allowed_columns:
                raise SqlValidationError(f"未发布的字段: {name}")
            columns.add(name)

        for star in statement.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                raise SqlValidationError("不允许使用通配字段")

        for function in statement.find_all(exp.Anonymous):
            name = function.sql_name().upper()
            if name not in self.ALLOWED_FUNCTIONS:
                raise SqlValidationError(f"不允许使用函数: {name}")

        statement = self._enforce_limit(statement)
        return ValidatedSql(
            sql=statement.sql(dialect="sqlite"),
            tables=base_tables,
            columns=columns,
        )

    @staticmethod
    def _reject_forbidden_nodes(statement: exp.Expression) -> None:
        forbidden_types = tuple(
            node_type
            for node_type in (
                exp.Insert,
                exp.Update,
                exp.Delete,
                exp.Create,
                exp.Drop,
                exp.Alter,
                exp.Command,
                getattr(exp, "Pragma", None),
                getattr(exp, "Attach", None),
            )
            if node_type is not None
        )
        if any(isinstance(node, forbidden_types) for node in statement.walk()):
            raise SqlValidationError("SQL 包含禁止操作")

    def _enforce_limit(self, statement: exp.Select) -> exp.Select:
        limit = statement.args.get("limit")
        if limit is not None:
            expression = limit.expression
            try:
                value = int(expression.name)
            except (AttributeError, TypeError, ValueError):
                value = self.max_rows + 1
            if value <= self.max_rows:
                return statement
        return statement.limit(self.max_rows, copy=False)
