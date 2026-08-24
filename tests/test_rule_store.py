from app.rule_store import RuleInput, RuleStore


class Catalog:
    allowed_tables = {"stations", "generation"}

    @staticmethod
    def allowed_columns(table):
        return {
            "stations": {"capacity_mw", "county", "station_name"},
            "generation": {"station_name", "generation_mwh"},
        }[table]


def payload(**updates):
    values = {
        "rule_key": "station_capacity",
        "name": "电站装机容量口径",
        "description": "按已发布电站并网容量统计装机容量。",
        "business_objects": ["已运行电站"],
        "metric": "装机容量",
        "dimensions": ["区县"],
        "scope_tables": ["stations"],
        "required_fields": {"stations": ["capacity_mw", "county"]},
        "calculation": "装机容量合计 = SUM(capacity_mw)",
        "unit": "MW",
        "constraints": ["空值不参与汇总"],
        "examples": ["各区县装机容量是多少"],
    }
    values.update(updates)
    return RuleInput(**values)


def test_draft_validate_publish_and_runtime_projection(tmp_path):
    store = RuleStore(tmp_path / "platform.sqlite3", Catalog())
    draft = store.create_draft(payload(), actor="tester")

    assert draft.status == "draft"
    assert store.validate(draft.id).valid is True

    published = store.publish(draft.id, actor="reviewer")
    runtime = store.published_rules()

    assert published.status == "published"
    assert runtime[0]["id"] == "managed:station_capacity:v1"
    assert runtime[0]["formula"] == "装机容量合计 = SUM(capacity_mw)"


def test_unknown_table_and_field_block_publication(tmp_path):
    store = RuleStore(tmp_path / "platform.sqlite3", Catalog())
    draft = store.create_draft(
        payload(
            scope_tables=["stations", "secret"],
            required_fields={"stations": ["unknown_field"]},
        )
    )

    validation = store.validate(draft.id)

    assert validation.valid is False
    assert any("未发布数据表" in issue for issue in validation.issues)
    assert any("未发布字段" in issue for issue in validation.issues)


def test_conflicting_metric_formula_is_blocked(tmp_path):
    store = RuleStore(tmp_path / "platform.sqlite3", Catalog())
    first = store.create_draft(payload())
    store.publish(first.id)
    conflict = store.create_draft(
        payload(rule_key="station_capacity_alt", calculation="装机容量 = AVG(capacity_mw)")
    )

    validation = store.validate(conflict.id)

    assert validation.valid is False
    assert "计算口径冲突" in validation.conflicts[0]


def test_rollback_creates_new_immutable_version(tmp_path):
    store = RuleStore(tmp_path / "platform.sqlite3", Catalog())
    v1 = store.publish(store.create_draft(payload()).id)
    v2 = store.publish(
        store.create_draft(payload(description="新的业务说明，计算公式保持一致。" )).id
    )

    restored = store.rollback("station_capacity", v1.version)
    versions = store.list()

    assert restored.version == 3
    assert restored.status == "published"
    assert restored.payload.description == v1.payload.description
    assert next(item for item in versions if item.id == v2.id).status == "archived"


def test_same_rule_key_can_publish_a_changed_formula_as_new_version(tmp_path):
    store = RuleStore(tmp_path / "platform.sqlite3", Catalog())
    v1 = store.publish(store.create_draft(payload()).id)
    changed = store.create_draft(
        payload(calculation="装机容量合计 = SUM(COALESCE(capacity_mw, 0))")
    )

    assert store.validate(changed.id).valid is True
    v2 = store.publish(changed.id)

    assert v2.version == 2
    assert v2.status == "published"
    assert store.get(v1.id).status == "archived"


def test_rule_version_diff_and_audit_are_queryable(tmp_path):
    store = RuleStore(tmp_path / "platform.sqlite3", Catalog())
    first = store.publish(store.create_draft(payload()).id, actor="reviewer")
    second = store.create_draft(
        payload(description="新的业务说明，计算公式保持一致。"), actor="editor"
    )
    diff = store.version_diff(second.id)
    audit = store.audit_events(first.id)

    assert diff.from_version == 1
    assert diff.to_version == 2
    assert [change.field for change in diff.changes] == ["description"]
    assert diff.changes[0].before == "按已发布电站并网容量统计装机容量。"
    assert diff.changes[0].after == "新的业务说明，计算公式保持一致。"
    assert [event.action for event in audit] == ["draft_created", "published"]
    assert [event.actor for event in audit] == ["local-admin", "reviewer"]


def test_only_published_managed_rules_are_injected_into_catalog_context(tmp_path):
    from tests.test_catalog import build_catalog_files, load_catalog_module

    module = load_catalog_module()
    db_path, catalog_path, examples_path = build_catalog_files(tmp_path)
    catalog = module.MetadataCatalog(db_path, catalog_path, examples_path)
    store = RuleStore(tmp_path / "platform.sqlite3", catalog)
    catalog.set_managed_rules_provider(store.published_rules)
    draft = store.create_draft(
        payload(
            scope_tables=["published_station"],
            required_fields={"published_station": ["capacity_mw"]},
        )
    )

    before = catalog.build_sql_context("查询装机容量", ["published_station"])
    store.publish(draft.id)
    after = catalog.build_sql_context("查询装机容量", ["published_station"])

    assert "managed:station_capacity:v1" not in before
    assert "managed:station_capacity:v1" in after
    assert "装机容量合计 = SUM(capacity_mw)" in after
