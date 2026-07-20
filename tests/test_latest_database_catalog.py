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
