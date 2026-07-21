from pathlib import Path

from app.catalog import MetadataCatalog
from app.config import Settings


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
LATEST_DATABASE = (
    WORKSPACE_ROOT
    / "data"
    / "数据入库_v1.0.1_2026.07.17"
    / "data_1_all"
    / "zhangbei_energy_data_data1.sqlite3"
)
DDL_DIRECTORY = (
    WORKSPACE_ROOT
    / "data"
    / "数据入库_v1.0.1_2026.07.17"
    / "data_1_all"
    / "vanna_table_ddls"
)


def default_catalog() -> MetadataCatalog:
    return MetadataCatalog(
        LATEST_DATABASE,
        WORKSPACE_ROOT / "medium" / "config" / "catalog.json",
        WORKSPACE_ROOT / "medium" / "config" / "examples.json",
        table_cards_path=WORKSPACE_ROOT / "medium" / "config" / "table_cards.json",
        ddl_registry_path=WORKSPACE_ROOT / "medium" / "config" / "ddl_registry.json",
        query_knowledge_path=WORKSPACE_ROOT / "medium" / "config" / "query_knowledge.json",
        validation_cases_path=WORKSPACE_ROOT / "medium" / "config" / "validation_cases.json",
        ddl_directory=DDL_DIRECTORY,
    )


def test_default_settings_target_latest_data1_database() -> None:
    settings = Settings()

    assert settings.sqlite_db_path.as_posix().endswith(
        "data/数据入库_v1.0.1_2026.07.17/data_1_all/zhangbei_energy_data_data1.sqlite3"
    )


def test_catalog_publishes_latest_profile_monthly_and_charging_tables() -> None:
    catalog = MetadataCatalog(
        LATEST_DATABASE,
        WORKSPACE_ROOT / "medium" / "config" / "catalog.json",
        WORKSPACE_ROOT / "medium" / "config" / "examples.json",
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
        WORKSPACE_ROOT / "medium" / "config" / "catalog.json",
        WORKSPACE_ROOT / "medium" / "config" / "examples.json",
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
