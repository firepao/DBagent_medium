import asyncio
from pathlib import Path

from app.catalog import MetadataCatalog
from app.config import Settings
from app.executor import SQLiteExecutor
from app.sql_guard import SqlGuard


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
LATEST_DATABASE = (
    WORKSPACE_ROOT
    / "data"
    / "数据入库v_1.1_0722"
    / "query_ready_v2"
    / "zhangbei_energy_query_ready_v2.sqlite3"
)
DDL_DIRECTORY = (
    WORKSPACE_ROOT
    / "data"
    / "数据入库v_1.1_0722"
    / "query_ready_v2"
    / "ddl"
)
MEDIUM_CONFIG = WORKSPACE_ROOT / "medium" / "config"


def default_catalog() -> MetadataCatalog:
    return MetadataCatalog(
        LATEST_DATABASE,
        MEDIUM_CONFIG / "catalog.json",
        MEDIUM_CONFIG / "examples.json",
        table_cards_path=MEDIUM_CONFIG / "table_cards.json",
        ddl_registry_path=MEDIUM_CONFIG / "ddl_registry.json",
        query_knowledge_path=MEDIUM_CONFIG / "query_knowledge.json",
        validation_cases_path=WORKSPACE_ROOT / "medium" / "config" / "validation_cases.json",
        ddl_directory=DDL_DIRECTORY,
    )


def test_default_settings_target_query_ready_database() -> None:
    settings = Settings()

    assert settings.sqlite_db_path.as_posix().endswith(
        "data/数据入库v_1.1_0722/query_ready_v2/zhangbei_energy_query_ready_v2.sqlite3"
    )


def test_catalog_publishes_latest_profile_monthly_and_charging_tables() -> None:
    catalog = MetadataCatalog(
        LATEST_DATABASE,
        MEDIUM_CONFIG / "catalog.json",
        MEDIUM_CONFIG / "examples.json",
    )

    assert {
        "t01_operating_renewable_station_profile",
        "t01_operating_generation_monthly",
        "t01_operating_curtailment_monthly",
        "t09_charging_station",
        "t11_weather_data_element",
    }.issubset(catalog.allowed_tables)


def test_catalog_exposes_latest_profile_and_charging_fields() -> None:
    catalog = MetadataCatalog(
        LATEST_DATABASE,
        MEDIUM_CONFIG / "catalog.json",
        MEDIUM_CONFIG / "examples.json",
    )

    assert {"station_name", "grid_capacity_mw", "energy_type"}.issubset(
        catalog.allowed_columns("t01_operating_renewable_station_profile")
    )
    assert {"station_name", "charging_pile_count", "total_charging_power"}.issubset(
        catalog.allowed_columns("t09_charging_station")
    )
    assert "contact" not in catalog.allowed_columns("t09_charging_station")


def test_registered_ddls_match_the_data1_sqlite_schema() -> None:
    catalog = default_catalog()

    for table in catalog.allowed_tables:
        assert catalog.load_ddl(table)


def test_customer_question_q1_has_a_published_capacity_rule() -> None:
    catalog = default_catalog()

    case = catalog.validation_case("全市新能源总装机现在是多少？分类型多少？")

    assert case["status"] == "supported"
    assert "installed_capacity_from_grid_capacity" in catalog.published_rule_ids()
    assert "q1_citywide_installed_capacity" in catalog.published_rule_ids()


def test_q1_customer_note_context_and_verified_sql_cover_both_capacity_tables() -> None:
    catalog = default_catalog()
    question = "全市新能源总装机现在是多少？分类型多少？"
    route = catalog.routing_decision(question)
    context = catalog.build_sql_context(question, list(route.required_tables), route)
    example = catalog.exact_example(question)
    guard = SqlGuard(catalog, max_rows=100)
    executor = SQLiteExecutor(LATEST_DATABASE, timeout_seconds=2, max_rows=100)

    validated = guard.validate(example["sql"])
    result = asyncio.run(executor.execute(validated.sql))

    assert route.customer_context["customer_note"] == "并网容量就是装机容量"
    assert "q1_citywide_installed_capacity" in context
    assert validated.tables == {
        "t01_operating_renewable_station_profile",
        "t07_distributed_pv_wind",
    }
    assert result.rows == [
        {
            "result_level": "总计",
            "capacity_type": "全市新能源",
            "capacity_mw": 26573.7,
        },
        {"result_level": "分类型", "capacity_type": "光伏", "capacity_mw": 10696.7},
        {
            "result_level": "分类型",
            "capacity_type": "风光储一体化",
            "capacity_mw": 4000.0,
        },
        {"result_level": "分类型", "capacity_type": "风电", "capacity_mw": 11877.0},
    ]


def test_q9_customer_note_is_published_as_a_plan_overdue_rule() -> None:
    catalog = default_catalog()
    question = "哪些县区新能源开发滞后，需要督导？"
    route = catalog.routing_decision(question)
    context = catalog.build_sql_context(question, list(route.required_tables), route)
    example = catalog.exact_example(question)
    guard = SqlGuard(catalog, max_rows=100)
    executor = SQLiteExecutor(LATEST_DATABASE, timeout_seconds=2, max_rows=100)

    result = asyncio.run(executor.execute(guard.validate(example["sql"]).sql))

    assert route.action == "allow"
    assert route.required_tables == ("t02_construction_project_station",)
    assert (
        route.customer_context["customer_note"]
        == "按照当前项目进度字段，判断延期方法：“当前进展阶段”值为非已完成且晚于计划并网日期，判定为滞后"
    )
    assert "按照当前项目进度字段，判断延期方法" in context
    assert "project_delay_candidate" in context
    assert "不得表述为停工、违规或备案后未开工" in context
    assert result.rows == [
        {"county": "下花园区", "overdue_project_count": 1},
        {"county": "宣化区", "overdue_project_count": 1},
        {"county": "尚义县", "overdue_project_count": 1},
        {"county": "崇礼区", "overdue_project_count": 1},
        {"county": "怀来县", "overdue_project_count": 1},
        {"county": "涿鹿县", "overdue_project_count": 1},
        {"county": "阳原县", "overdue_project_count": 1},
    ]


def test_county_derivation_context_and_verified_rankings_use_location_fields() -> None:
    catalog = default_catalog()
    guard = SqlGuard(catalog, max_rows=100)
    executor = SQLiteExecutor(LATEST_DATABASE, timeout_seconds=2, max_rows=100)
    expected = {
        "哪个县区新能源装机最多？": {
            "county": "沽源县",
            "total_capacity_mw": 2550.0,
        },
        "哪个县区分布式光伏装机量最高？": {
            "county": "怀来县",
            "total_capacity_mw": 80.3,
        },
    }

    for question, expected_row in expected.items():
        route = catalog.routing_decision(question)
        case = catalog.validation_case(question)
        context = catalog.build_sql_context(question, case["scope_tables"], route)
        example = catalog.exact_example(question)
        result = asyncio.run(executor.execute(guard.validate(example["sql"]).sql))

        assert "行政区派生规则" in context
        assert "待核实记录不得进入县区排名" in context
        assert result.rows == [expected_row]


def test_default_table_cards_only_reference_published_sqlite_fields() -> None:
    catalog = default_catalog()

    assert catalog.table_card_issues() == []


def test_customer_question_q13_is_not_supported_without_filing_date() -> None:
    catalog = default_catalog()

    case = catalog.validation_case("最近3个月新增备案多少？")

    assert case["status"] == "not_supported"
    assert "备案日期" in case["reason"]


def test_published_runtime_rules_only_reference_published_fields() -> None:
    catalog = default_catalog()

    assert catalog.runtime_rule_issues() == []
    assert catalog.region_rule_issues() == []


def test_date_precision_rules_are_injected_for_the_relevant_tables() -> None:
    catalog = default_catalog()

    construction_context = catalog.build_sql_context(
        "在建项目开工时间", ["t02_construction_project_station"]
    )
    operating_context = catalog.build_sql_context(
        "已运行项目并网时间", ["t01_operating_renewable_station_profile"]
    )
    filing_context = catalog.build_sql_context(
        "备案项目计划开工", ["t04_filing_project"]
    )

    assert "construction_schedule_date_normalization" in construction_context
    assert "YYYY-MM-DD" in construction_context
    assert "operating_station_month_precision_dates" in operating_context
    assert "start_month" in operating_context
    assert "filing_project_month_precision_dates" in filing_context
    assert "planned_start_month" in filing_context
