import asyncio
import sqlite3
from pathlib import Path

from app.catalog import MetadataCatalog
from app.config import Settings
from app.executor import SQLiteExecutor
from app.sql_guard import SqlGuard


REPO_ROOT = Path(__file__).resolve().parents[1]
LATEST_DATABASE = (
    REPO_ROOT
    / "data"
    / "数据入库v_1.1_0722"
    / "query_ready_v2"
    / "zhangbei_energy_query_ready_v2.sqlite3"
)
DDL_DIRECTORY = (
    REPO_ROOT
    / "data"
    / "数据入库v_1.1_0722"
    / "query_ready_v2"
    / "ddl"
)
MEDIUM_CONFIG = REPO_ROOT / "config"


def default_catalog() -> MetadataCatalog:
    return MetadataCatalog(
        LATEST_DATABASE,
        MEDIUM_CONFIG / "catalog.json",
        MEDIUM_CONFIG / "examples.json",
        table_cards_path=MEDIUM_CONFIG / "table_cards.json",
        ddl_registry_path=MEDIUM_CONFIG / "ddl_registry.json",
        query_knowledge_path=MEDIUM_CONFIG / "query_knowledge.json",
        validation_cases_path=MEDIUM_CONFIG / "validation_cases.json",
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


def test_detailed_ddls_cover_every_field_with_real_data_profiles() -> None:
    catalog = default_catalog()

    for table in catalog.allowed_tables:
        ddl = catalog.load_ddl(table)
        field_lines = [line for line in ddl.splitlines() if line.startswith("-- 字段：")]
        with sqlite3.connect(LATEST_DATABASE) as connection:
            schema_columns = connection.execute(
                f'PRAGMA table_info("{table}")'
            ).fetchall()
        assert "-- 行粒度：" in ddl
        assert "-- 当前数据画像：" in ddl
        assert "-- 题集问题映射：" in ddl
        assert "-- 题集已发布口径：" in ddl
        assert len(field_lines) == len(schema_columns)
        assert all(line.count("当前库画像：") == 1 for line in field_lines)
        assert all(line.count("用途=") == 1 for line in field_lines)


def test_categorical_values_and_question_rules_enter_sql_context() -> None:
    catalog = default_catalog()
    operating_context = catalog.build_sql_context(
        "统计风电、光伏和风光储一体化项目的配储情况",
        ["t01_operating_renewable_station_profile"],
    )
    construction_context = catalog.build_sql_context(
        "未取得电力接入批复的在建项目有哪些？",
        ["t02_construction_project_station"],
    )

    assert "题集问题映射" in operating_context
    assert "当前完整枚举" in operating_context
    assert "风光储一体化" in operating_context
    assert "是否配储" in operating_context
    assert "否" in operating_context
    assert "是" in operating_context
    assert "电站类型" in operating_context
    assert "不是能源类型" in operating_context
    assert "当前完整枚举" in construction_context
    assert "办理中" in construction_context
    assert "电力接入批复办理状态" in construction_context
    assert "建设用地批复办理状态" in construction_context


def test_planning_context_stays_lightweight_until_tables_are_selected() -> None:
    catalog = default_catalog()
    planning_context = catalog.build_planning_context()
    sql_context = catalog.build_sql_context(
        "统计能源类型和配储情况",
        ["t01_operating_renewable_station_profile"],
    )

    assert "能源类型" in planning_context
    assert "当前库画像" not in planning_context
    assert "当前完整枚举" not in planning_context
    assert "当前完整枚举" in sql_context


def test_distributed_pv_categories_enter_lightweight_planning_context() -> None:
    catalog = default_catalog()

    planning_context = catalog.build_planning_context()
    resolved = catalog.resolved_categorical_values(
        "张家口市已投运分布式光伏项目装机容量按县区排名",
        ["t07_distributed_pv_wind"],
    )

    assert '"field": "station_type"' in planning_context
    assert '"allowed_values": ["分布式光伏", "分散式风电"]' in planning_context
    assert '"field": "station_type_2"' in planning_context
    assert "自发自用余电上网" in planning_context
    assert resolved == [
        {
            "table": "t07_distributed_pv_wind",
            "field": "station_type",
            "business_label": "项目类型",
            "value": "分布式光伏",
        }
    ]


def test_detailed_ddl_and_context_do_not_publish_contact_values() -> None:
    catalog = default_catalog()
    ddl = catalog.load_ddl("t02_construction_project_station")
    context = catalog.build_sql_context(
        "查询在建项目进度", ["t02_construction_project_station"]
    )

    assert "该字段不发布实际取值或代表样例" in ddl
    assert "13031331234" not in ddl
    assert "13803131234" not in ddl
    assert '"contact"' not in context


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


def test_grid_side_storage_distribution_aggregates_all_rows_before_limit() -> None:
    catalog = default_catalog()
    guard = SqlGuard(catalog, max_rows=100)
    executor = SQLiteExecutor(LATEST_DATABASE, timeout_seconds=2, max_rows=100)
    question = "电网侧储能项目都分布在哪？"

    route = catalog.routing_decision(question)
    case = catalog.validation_case(question)
    context = catalog.build_sql_context(question, case["scope_tables"], route)
    example = catalog.exact_example(question)
    result = asyncio.run(executor.execute(guard.validate(example["sql"]).sql))

    assert '"table": "t06_grid_side_storage"' in context
    assert "station_name" in context
    assert len(result.rows) == 16
    assert result.truncated is False
    assert {row["total_project_count"] for row in result.rows} == {218}
    assert {row["district_count"] for row in result.rows} == {16}
    assert sum(row["project_count"] for row in result.rows) == 218


def test_default_table_cards_only_reference_published_sqlite_fields() -> None:
    catalog = default_catalog()

    assert catalog.table_card_issues() == []


def test_customer_question_q13_is_not_supported_without_filing_date() -> None:
    catalog = default_catalog()

    case = catalog.validation_case("最近3个月新增备案多少？")

    assert case["status"] == "not_supported"
    assert "备案日期" in case["reason"]


def test_q24_parent_group_top5_and_central_enterprise_share() -> None:
    catalog = default_catalog()
    question = "装机最多的5家业主单位？央企占比多少？"
    route = catalog.routing_decision(question)
    context = catalog.build_sql_context(question, list(route.required_tables), route)
    example = catalog.exact_example(question)
    guard = SqlGuard(catalog, max_rows=100)
    executor = SQLiteExecutor(LATEST_DATABASE, timeout_seconds=2, max_rows=100)

    result = asyncio.run(executor.execute(guard.validate(example["sql"]).sql))

    assert route.action == "allow"
    assert route.required_tables == ("t01_operating_renewable_station_profile",)
    assert catalog.concept_clarification(question) is None
    assert catalog.concept_clarification("按上级集团汇总业主装机") is None
    assert "q24_parent_group_top5_and_central_enterprise_share" in context
    assert result.rows[:5] == [
        {"section_order": 1, "result_section": "TOP5", "parent_group": "国家能源集团", "capacity_mw": 4940.0, "ranking": 1, "central_capacity_mw": None, "total_capacity_mw": None, "central_share_pct": None},
        {"section_order": 1, "result_section": "TOP5", "parent_group": "中国广核集团", "capacity_mw": 2530.0, "ranking": 2, "central_capacity_mw": None, "total_capacity_mw": None, "central_share_pct": None},
        {"section_order": 1, "result_section": "TOP5", "parent_group": "河北建投集团", "capacity_mw": 2420.0, "ranking": 3, "central_capacity_mw": None, "total_capacity_mw": None, "central_share_pct": None},
        {"section_order": 1, "result_section": "TOP5", "parent_group": "北京能源集团", "capacity_mw": 2390.0, "ranking": 4, "central_capacity_mw": None, "total_capacity_mw": None, "central_share_pct": None},
        {"section_order": 1, "result_section": "TOP5", "parent_group": "中国核工业集团", "capacity_mw": 1965.0, "ranking": 5, "central_capacity_mw": None, "total_capacity_mw": None, "central_share_pct": None},
    ]
    assert result.rows[5] == {
        "section_order": 2,
        "result_section": "央企占比",
        "parent_group": None,
        "capacity_mw": None,
        "ranking": None,
        "central_capacity_mw": 19395.0,
        "total_capacity_mw": 25255.0,
        "central_share_pct": 76.797,
    }


def test_customer_confirmed_calculation_examples_execute() -> None:
    catalog = default_catalog()
    guard = SqlGuard(catalog, max_rows=100)
    executor = SQLiteExecutor(LATEST_DATABASE, timeout_seconds=2, max_rows=100)
    expected_rules = {
        "储能装机现状？电网侧、电源侧、工商业各多少？": "storage_categories_non_additive",
        "全市新能源行业2025年总营收？": "theoretical_revenue_2025",
        "当前新能源消纳情况？弃风弃光率？": "curtailment_rate_and_utilization_hours",
        "哪些变电站接入容量快饱和了？": "substation_near_saturation_q17_q33",
        "哪些变电站已接近饱和？是否已有扩容计划？": "substation_near_saturation_q17_q33",
        "等效利用小时最高、限电率最低的场站TOP5？": "curtailment_rate_and_utilization_hours",
    }

    for question, rule_id in expected_rules.items():
        route = catalog.routing_decision(question)
        context = catalog.build_sql_context(question, list(route.required_tables), route)
        example = catalog.exact_example(question)
        result = asyncio.run(executor.execute(guard.validate(example["sql"]).sql))

        assert route.action == "allow"
        assert rule_id in context
        assert result.rows


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


def test_q18_uses_table_object_scope_instead_of_inventing_a_progress_value() -> None:
    catalog = default_catalog()
    question = "哪些在建项目还没拿到接入意见函？"
    route = catalog.routing_decision(question)
    context = catalog.build_sql_context(
        question, list(route.required_tables), route
    )
    example = catalog.exact_example("未取得电力接入批复的在建项目有哪些？")
    guard = SqlGuard(catalog, max_rows=100)
    executor = SQLiteExecutor(LATEST_DATABASE, timeout_seconds=2, max_rows=100)

    result = asyncio.run(executor.execute(guard.validate(example["sql"]).sql))

    assert route.action == "allow"
    assert route.required_tables == ("t02_construction_project_station",)
    assert "construction_grid_access_approval_pending" in context
    assert "对象范围与状态筛选约束" in context
    assert "本表每一行均属于在建新能源及储能项目" in context
    assert "未取得电力接入批复的在建项目有哪些" in context
    assert len(result.rows) == 2
    assert sum(row["project_capacity_mw"] for row in result.rows) == 250.0


def test_object_scope_rejects_implicit_status_filter_but_allows_explicit_enum() -> None:
    catalog = default_catalog()
    table = {"t02_construction_project_station"}

    implicit = catalog.scope_filter_issues(
        "哪些在建项目还没拿到接入意见函？",
        "SELECT project_name FROM t02_construction_project_station "
        "WHERE current_progress = '在建' AND grid_access_approval = '办理中'",
        table,
    )
    explicit = catalog.scope_filter_issues(
        "当前阶段是前期的在建项目有哪些？",
        "SELECT project_name FROM t02_construction_project_station "
        "WHERE current_progress = '前期'",
        table,
    )

    assert implicit == [
        "对象范围词不能自动转换为当前阶段筛选；仅当用户明确给出已发布阶段值时才可筛选该字段"
    ]
    assert explicit == []
