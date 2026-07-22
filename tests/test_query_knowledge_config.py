import hashlib
import json
from pathlib import Path


MEDIUM_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = MEDIUM_DIR.parent
CONFIG_DIR = MEDIUM_DIR / "config"
DDL_DIR = (
    WORKSPACE_ROOT
    / "data"
    / "数据入库v_1.1_0722"
    / "query_ready_v2"
    / "ddl"
)


def load_config(name: str) -> dict:
    return json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))


def test_table_cards_cover_every_published_catalog_table() -> None:
    catalog = load_config("catalog.json")
    table_cards = load_config("table_cards.json")

    published_tables = {dataset["table"] for dataset in catalog["datasets"]}
    cards = table_cards["table_cards"]

    assert {card["table"] for card in cards} == published_tables
    assert all(card["aliases"] for card in cards)
    assert all(card["important_fields"] for card in cards)


def test_every_published_table_has_a_valid_ddl_registration() -> None:
    catalog = load_config("catalog.json")
    registry = load_config("ddl_registry.json")

    published_tables = {dataset["table"] for dataset in catalog["datasets"]}
    assert set(registry["tables"]) == published_tables

    for table, entry in registry["tables"].items():
        ddl_path = DDL_DIR / entry["file"]
        ddl = ddl_path.read_text(encoding="utf-8")
        assert f'CREATE TABLE "{table}"' in ddl
        assert entry["sha256"] == hashlib.sha256(ddl.encode("utf-8")).hexdigest()


def test_runtime_knowledge_only_contains_published_rules() -> None:
    knowledge = load_config("query_knowledge.json")

    assert all(
        rule["status"] == "published"
        for rule in knowledge["rules"]
        if rule.get("runtime_enabled")
    )
    assert all(
        not rule.get("runtime_enabled")
        for rule in knowledge["rules"]
        if rule["status"] != "published"
    )


def test_validation_cases_preserve_supported_and_out_of_scope_questions() -> None:
    cases = load_config("validation_cases.json")["cases"]
    by_id = {case["id"]: case for case in cases}

    assert by_id["Q1"]["status"] == "supported"
    assert by_id["Q16"]["status"] == "not_supported"
    assert by_id["Q38"]["status"] == "out_of_scope"
