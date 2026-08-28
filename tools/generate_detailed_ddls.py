"""Generate strict, data-profiled DDL documents for Medium's published tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


MEDIUM_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = MEDIUM_DIR / "data" / "数据入库v_1.1_0722" / "query_ready_v2"

FIELD_PATTERN = re.compile(
    r"^-- 字段：(?P<name>[^|]+)\s*\|\s*别名：(?P<label>[^|]+)\s*"
    r"\|\s*类型：(?P<type>[^|]+)\s*\|\s*说明：(?P<description>.+)$"
)

TABLE_GUIDANCE: dict[str, tuple[str, str]] = {
    "t01_operating_renewable_station_profile": (
        "每行代表一座已运行集中式新能源电站。",
        "可按场站名称关联表01发电量/月度限电量；并网容量字段按已确认口径作为集中式装机容量。",
    ),
    "t01_operating_generation_monthly": (
        "每行代表一座集中式场站在一个自然月的一项发电量记录。",
        "按场站名称和月份关联限电量；按场站名称关联已运行电站画像取得装机容量。",
    ),
    "t01_operating_curtailment_monthly": (
        "每行代表一座集中式场站在一个自然月的一项限电量记录。",
        "按场站名称和月份关联发电量；限电率必须先汇总同期分子、分母后计算。",
    ),
    "t02_construction_project_station": (
        "每行代表一个在建新能源或储能项目。",
        "本表的‘在建’是整表对象范围，不应自动转换为当前阶段进展字段的筛选值。",
    ),
    "t03_large_scale_enterprise": (
        "每行代表一家规上企业。",
        "仅用于企业基本信息、年度用能与配套设施；不含营收、税收和就业人数。",
    ),
    "t04_filing_project": (
        "每行代表一个已进入当前备案项目库的拟建项目。",
        "计划日期只表示计划月份；本表没有备案日期、实际开工日期或实际项目状态。",
    ),
    "t05_land_planning_merged_catalog": (
        "每行代表土地规划源表中的一个合并分类目录项。",
        "用于解释分类层级，不用于图斑数量或面积统计。",
    ),
    "t05_land_planning_detail": (
        "每行代表一条土地规划分类明细。",
        "只可依据已有分类和选址判定查询，不得自行作法律合规判断。",
    ),
    "t05_land_polygon_point": (
        "每行代表一个候选土地图斑点位或图斑记录。",
        "面积统计使用数值面积字段；选址结论仅使用源数据已填判定。",
    ),
    "t06_grid_side_storage": (
        "每行代表一个已投运电网侧储能项目。",
        "MW 功率与 MWh 能量必须分开聚合，不与电源侧或工商业储能自动相加。",
    ),
    "t07_distributed_pv_wind": (
        "每行代表一个已投运分布式光伏或分散式风电项目。",
        "只覆盖分布式/分散式项目；不得与集中式项目混为同一对象集而不说明口径。",
    ),
    "t08_industrial_commercial_storage": (
        "每行代表一个已投运工商业储能项目。",
        "MW 功率与 MWh 能量分开统计；两个 2026 年总电量源字段语义未确认时不得混用。",
    ),
    "t09_charging_station": (
        "每行代表一座充换电站。",
        "站点数与充电桩数是不同指标；缺少行政区名录分母时不能计算覆盖率。",
    ),
    "t10_power_grid_material": (
        "每行代表一座已收资变电站。",
        "主变容量单位 MVA，剩余可接入容量单位 MW，不能直接相除；近饱和按已发布规则判断。",
    ),
    "t11_weather_data_element": (
        "每行代表一种可用气象历史数据要素。",
        "这是数据要素目录，不是具体观测事实表。",
    ),
    "t11_weather_grid_sample": (
        "每行代表 2025 年一个固定气象格点的年度样本。",
        "不含逐时观测、极端天气事件或跨年度趋势。",
    ),
}

FIELD_GUIDANCE: dict[tuple[str, str], str] = {
    ("t01_operating_renewable_station_profile", "energy_type"): "业务分类字段；风电、光伏、风光储一体化按源值精确分组，风光储一体化不得拆分为风电或光伏。",
    ("t01_operating_renewable_station_profile", "station_type"): "并网/上网类别，不是能源类型；不得用它判断风电或光伏。",
    ("t01_operating_renewable_station_profile", "has_storage"): "是否配置电源侧储能的标志；统计已配储项目时按已发布枚举精确筛选。",
    ("t01_operating_renewable_station_profile", "project_builder"): "项目建设方，不等同于已确认的业主单位或归属上级集团。",
    ("t01_operating_renewable_station_profile", "parent_group"): "项目归属的上级集团，可用于集团维度汇总；不应静默解释为业主单位。",
    ("t01_operating_renewable_station_profile", "grid_capacity_mw"): "可筛选、排序和求和；按客户验证口径作为已运行集中式新能源装机容量，单位 MW。",
    ("t02_construction_project_station", "current_progress"): "项目当前施工阶段；只有用户明确指定阶段时才按枚举筛选，不能用‘在建’作为字段值。",
    ("t02_construction_project_station", "land_approval"): "建设用地批复办理状态，与电力接入批复是两个不同手续。",
    ("t02_construction_project_station", "grid_access_approval"): "电力接入批复办理状态；题目中的接入审批/接入批复优先映射到本字段。",
    ("t04_filing_project", "owner_unit"): "拟定业主单位，仅适用于备案项目，不代表已运行集中式电站业主。",
    ("t10_power_grid_material", "remaining_access_capacity_mw"): "规范化数值字段，单位 MW；‘不超过10MW’直接使用 <= 10，不做单位换算。",
    ("t10_power_grid_material", "main_transformer_total_capacity_mva"): "规范化主变总容量，单位 MVA；不得与 MW 字段直接相除。",
}

NO_SAMPLE_MARKERS = (
    "contact",
    "phone",
    "coordinate",
    "longitude",
    "latitude",
    "source_file",
    "source_sheet",
)


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def compact(value: Any, limit: int = 80) -> str:
    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_existing_ddl(path: Path) -> tuple[dict[str, dict[str, str]], str]:
    text = path.read_text(encoding="utf-8")
    fields: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        match = FIELD_PATTERN.match(line.strip())
        if match:
            entry = {key: value.strip() for key, value in match.groupdict().items()}
            fields[entry["name"]] = entry
    create_at = text.find("CREATE TABLE")
    if create_at < 0:
        raise ValueError(f"DDL 缺少 CREATE TABLE: {path}")
    return fields, text[create_at:].strip() + "\n"


def value_list(values: list[Any]) -> str:
    return "、".join(json.dumps(compact(value), ensure_ascii=False) for value in values)


def field_profile(
    connection: sqlite3.Connection,
    table: str,
    name: str,
    sqlite_type: str,
    row_count: int,
    excluded: bool,
) -> str:
    column = quote(name)
    table_sql = quote(table)
    non_null, distinct_count = connection.execute(
        f"SELECT COUNT({column}), COUNT(DISTINCT {column}) FROM {table_sql}"
    ).fetchone()
    null_count = row_count - int(non_null)
    parts = [f"当前库画像：总行数={row_count}，非空={non_null}，空值={null_count}，非空去重值={distinct_count}"]
    normalized_type = (sqlite_type or "TEXT").upper()
    if non_null == 0:
        parts.append("当前全部为空，不得据此形成业务结论")
        return "；".join(parts) + "。"

    if excluded or any(marker in name.casefold() for marker in NO_SAMPLE_MARKERS):
        parts.append("该字段不发布实际取值或代表样例")
        return "；".join(parts) + "。"

    if normalized_type in {"INTEGER", "REAL", "NUMERIC"}:
        minimum, maximum = connection.execute(
            f"SELECT MIN({column}), MAX({column}) FROM {table_sql} WHERE {column} IS NOT NULL"
        ).fetchone()
        parts.append(f"当前范围=[{minimum}, {maximum}]")
        if distinct_count <= 20:
            values = [
                row[0]
                for row in connection.execute(
                    f"SELECT DISTINCT {column} FROM {table_sql} WHERE {column} IS NOT NULL ORDER BY {column}"
                )
            ]
            parts.append("当前完整取值=" + value_list(values))
        parts.append("数值比较和聚合使用本列，不对带单位原文列做字符串计算")
        return "；".join(parts) + "。"

    if distinct_count <= 20:
        values = [
            row[0]
            for row in connection.execute(
                f"SELECT DISTINCT {column} FROM {table_sql} WHERE {column} IS NOT NULL "
                f"ORDER BY CAST({column} AS TEXT)"
            )
        ]
        parts.append("当前完整枚举=" + value_list(values))
        parts.append("筛选时优先使用上述源值精确匹配")
    else:
        samples = [
            row[0]
            for row in connection.execute(
                f"SELECT {column} FROM {table_sql} WHERE {column} IS NOT NULL "
                f"AND TRIM(CAST({column} AS TEXT)) <> '' GROUP BY {column} "
                f"ORDER BY COUNT(*) DESC, CAST({column} AS TEXT) LIMIT 3"
            )
        ]
        if samples:
            parts.append("代表值=" + value_list(samples))
        parts.append("高基数字段，不应把代表值误认为完整枚举")
    return "；".join(parts) + "。"


def usage_guidance(name: str, sqlite_type: str, distinct_count: int) -> str:
    lower = name.casefold()
    if lower == "id" or lower.endswith("_id") or "serial_no" in lower or lower == "excel_row":
        return "用途=标识/追溯，不作为业务指标求和。"
    if lower.endswith("_raw"):
        return "用途=保留源文本，仅用于追溯；数值、日期计算必须使用对应规范化字段。"
    if lower.endswith("_parse_status"):
        return "用途=数据质量检查；业务统计应优先限定为 valid，不能把状态值当业务分类。"
    if any(token in lower for token in ("date", "month", "year")):
        return "用途=时间筛选与排序；必须遵守字段说明中的日期精度。"
    if (sqlite_type or "").upper() in {"INTEGER", "REAL", "NUMERIC"}:
        return "用途=数值筛选、排序或聚合；单位以字段说明为准。"
    if distinct_count <= 20:
        return "用途=低基数分类筛选或分组；只能使用已发布枚举值。"
    return "用途=明细展示、名称匹配或分组；用户未明确时不得臆造取值。"


def generate(args: argparse.Namespace) -> dict[str, Any]:
    cards_payload = load_json(args.table_cards_path)
    cards = {item["table"]: item for item in cards_payload["table_cards"]}
    catalog_payload = load_json(args.catalog_path)
    datasets = {item["table"]: item for item in catalog_payload["datasets"]}
    registry_payload = load_json(args.registry_path)
    registry = registry_payload["tables"]
    validation_cases = load_json(args.validation_cases_path).get("cases", [])
    knowledge = load_json(args.query_knowledge_path)
    examples = load_json(args.examples_path)
    uri = f"file:{args.db_path.resolve().as_posix()}?mode=ro"
    report: dict[str, Any] = {"tables": {}}

    with sqlite3.connect(uri, uri=True) as connection:
        for table in sorted(cards):
            if table not in registry:
                raise ValueError(f"DDL 注册缺少表: {table}")
            ddl_path = args.ddl_directory / registry[table]["file"]
            existing_fields, create_sql = parse_existing_ddl(ddl_path)
            schema = connection.execute(f"PRAGMA table_info({quote(table)})").fetchall()
            row_count = connection.execute(f"SELECT COUNT(*) FROM {quote(table)}").fetchone()[0]
            excluded_columns = set(datasets[table].get("excluded_columns", []))
            grain, relation = TABLE_GUIDANCE.get(
                table,
                ("每行代表一条源数据记录。", "仅使用已发布字段和规则关联其他表。"),
            )
            related_cases = [
                item for item in validation_cases if table in item.get("scope_tables", [])
            ]
            published_rules = [
                item
                for item in knowledge.get("rules", [])
                if table in item.get("scope_tables", [])
                and item.get("status") == "published"
                and item.get("runtime_enabled") is True
            ]
            reference_rules = [
                item
                for item in knowledge.get("rules", [])
                if table in item.get("scope_tables", [])
                and item.get("reference_enabled") is True
            ]
            related_examples = [
                item for item in examples if table in item.get("tables", [])
            ]
            lines = [
                f"-- 数据集：{cards[table].get('dataset', table)}",
                f"-- 技术表：{table}",
                f"-- 描述：{cards[table].get('description', '')}",
                f"-- 覆盖范围：{cards[table].get('coverage', '')}",
                f"-- 行粒度：{grain}",
                f"-- 当前数据画像：共 {row_count} 行；全部业务数据属于张家口市全域；画像值来自当前查询数据库。",
                f"-- 关联与口径：{relation}",
                "-- 关键词：" + "、".join(sorted(cards[table].get("aliases", {}).keys())),
                "-- 适用问题：" + "；".join(cards[table].get("supported_queries", [])),
                "-- 限制：" + "；".join(cards[table].get("data_limitations", [])),
                "-- 题集问题映射："
                + (
                    "；".join(
                        f"{item.get('id')}[{item.get('status')}] {compact(item.get('question', ''), 120)}"
                        for item in related_cases
                    )
                    if related_cases
                    else "当前题集没有直接映射。"
                ),
                "-- 题集已发布口径："
                + (
                    "；".join(
                        f"{item.get('id')}：{compact(item.get('content') or item.get('formula') or '', 500)}"
                        for item in published_rules
                    )
                    if published_rules
                    else "无；不得自行补造计算公式或业务阈值。"
                ),
                "-- 题集待确认边界："
                + (
                    "；".join(
                        f"{item.get('id')}：{compact(item.get('content') or item.get('formula') or '', 500)}"
                        for item in reference_rules
                    )
                    if reference_rules
                    else "无。"
                ),
                "-- 已验证问题示例："
                + (
                    "；".join(compact(item.get("question", ""), 140) for item in related_examples)
                    if related_examples
                    else "无。"
                ),
            ]
            field_report: dict[str, Any] = {}
            for _, name, sqlite_type, *_ in schema:
                metadata = existing_fields.get(
                    name,
                    {
                        "label": datasets[table].get("aliases", {}).get(name, name),
                        "type": sqlite_type or "TEXT",
                        "description": "无补充字段说明。",
                    },
                )
                non_null, distinct_count = connection.execute(
                    f"SELECT COUNT({quote(name)}), COUNT(DISTINCT {quote(name)}) FROM {quote(table)}"
                ).fetchone()
                profile = field_profile(
                    connection,
                    table,
                    name,
                    sqlite_type or "TEXT",
                    row_count,
                    name in excluded_columns,
                )
                guidance = FIELD_GUIDANCE.get((table, name), "")
                base_description = metadata["description"].split(" 用途=", 1)[0].strip()
                if guidance and base_description.endswith(guidance):
                    base_description = base_description[: -len(guidance)].rstrip()
                usage = (
                    "用途=内部追溯或敏感字段，不进入公开查询上下文。"
                    if name in excluded_columns
                    else usage_guidance(name, sqlite_type or "TEXT", int(distinct_count))
                )
                description = " ".join(
                    part
                    for part in (
                        compact(base_description, 1000),
                        guidance,
                        usage,
                        profile,
                    )
                    if part
                )
                lines.append(
                    f"-- 字段：{name} | 别名：{metadata['label']} | 类型：{sqlite_type or 'TEXT'} | 说明：{description}"
                )
                field_report[name] = {
                    "type": sqlite_type or "TEXT",
                    "non_null": non_null,
                    "distinct": distinct_count,
                }
            ddl_text = "\n".join(lines) + "\n" + create_sql
            ddl_path.write_text(ddl_text, encoding="utf-8")
            registry[table]["sha256"] = hashlib.sha256(ddl_text.encode("utf-8")).hexdigest()
            report["tables"][table] = {
                "rows": row_count,
                "columns": len(schema),
                "fields": field_report,
            }

    args.registry_path.write_text(
        json.dumps(registry_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="依据真实 SQLite 生成包含字段画像的严格 DDL")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DATA_DIR / "zhangbei_energy_query_ready_v2.sqlite3")
    parser.add_argument("--ddl-directory", type=Path, default=DEFAULT_DATA_DIR / "ddl")
    parser.add_argument("--catalog-path", type=Path, default=MEDIUM_DIR / "config" / "catalog.json")
    parser.add_argument("--table-cards-path", type=Path, default=MEDIUM_DIR / "config" / "table_cards.json")
    parser.add_argument("--registry-path", type=Path, default=MEDIUM_DIR / "config" / "ddl_registry.json")
    parser.add_argument("--validation-cases-path", type=Path, default=MEDIUM_DIR / "config" / "validation_cases.json")
    parser.add_argument("--query-knowledge-path", type=Path, default=MEDIUM_DIR / "config" / "query_knowledge.json")
    parser.add_argument("--examples-path", type=Path, default=MEDIUM_DIR / "config" / "examples.json")
    return parser


def main() -> int:
    report = generate(build_parser().parse_args())
    summary = {
        "tables": len(report["tables"]),
        "columns": sum(item["columns"] for item in report["tables"].values()),
        "rows": sum(item["rows"] for item in report["tables"].values()),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
