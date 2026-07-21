import json
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app.source_truth_audit import SourceTruthAuditor, render_markdown


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_minimal_xlsx(path: Path, sheet_name: str, rows: list[list[str]]) -> None:
    """Create a tiny XLSX fixture without requiring an Excel writer dependency."""
    def column_name(index: int) -> str:
        return chr(ord("A") + index)

    rows_xml = []
    for row_index, values in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(values):
            if value:
                escaped = str(value).replace("&", "&amp;").replace("<", "&lt;")
                cells.append(
                    f'<c r="{column_name(column_index)}{row_index}" t="inlineStr">'
                    f"<is><t>{escaped}</t></is></c>"
                )
        rows_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
              <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
              <Default Extension="xml" ContentType="application/xml"/>
              <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
              <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
            </Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
            </Relationships>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            f"""<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
             xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets>
            </workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
            </Relationships>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>{rows}</sheetData>
            </worksheet>""".format(rows="".join(rows_xml)),
        )


def _build_fixture(tmp_path: Path) -> dict[str, Path]:
    source_dir = tmp_path / "data"
    processed_dir = tmp_path / "data_1"
    ddl_dir = tmp_path / "ddls"
    for directory in (source_dir, processed_dir, ddl_dir):
        directory.mkdir()

    _write_minimal_xlsx(
        source_dir / "source.xlsx", "原始项目数据", [["项目名称", "容量"], ["示例项目", "12.5"]]
    )
    _write_minimal_xlsx(
        processed_dir / "source.xlsx", "项目", [["项目名称", "容量"], ["示例项目", "12.5"]]
    )
    ddl = (
        'CREATE TABLE "energy" ('
        '"id" INTEGER, "capacity" REAL, "source_file" TEXT, '
        '"source_sheet" TEXT, "excel_row" INTEGER);\n'
    )
    (ddl_dir / "energy.txt").write_text(ddl, encoding="utf-8")

    database = tmp_path / "energy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE energy (
                id INTEGER,
                capacity REAL,
                source_file TEXT,
                source_sheet TEXT,
                excel_row INTEGER
            );
            INSERT INTO energy VALUES (1, 12.5, 'source.xlsx', '项目', 4);
            CREATE TABLE vanna_tables (
                table_name TEXT PRIMARY KEY,
                source_file TEXT,
                source_sheet TEXT,
                row_count INTEGER
            );
            INSERT INTO vanna_tables VALUES ('energy', 'source.xlsx', '项目', 1);
            CREATE TABLE vanna_fields (
                table_name TEXT,
                column_name TEXT,
                field_alias TEXT
            );
            INSERT INTO vanna_fields VALUES ('energy', 'capacity', '容量');
            CREATE TABLE import_audit (
                source_file TEXT,
                source_sheet TEXT,
                table_name TEXT,
                row_count INTEGER
            );
            INSERT INTO import_audit VALUES ('source.xlsx', '项目', 'energy', 1);
            """
        )

    catalog = tmp_path / "catalog.json"
    cards = tmp_path / "table_cards.json"
    registry = tmp_path / "ddl_registry.json"
    _write_json(catalog, {"datasets": [{"table": "energy", "excluded_columns": ["id", "source_file", "source_sheet", "excel_row"]}]})
    _write_json(cards, {"table_cards": [{"table": "energy", "aliases": {"容量": "capacity", "虚构字段": "missing"}, "important_fields": ["capacity", "unknown"]}]})
    _write_json(registry, {"tables": {"energy": {"file": "energy.txt"}}})
    return {
        "source_dir": source_dir,
        "processed_dir": processed_dir,
        "ddl_dir": ddl_dir,
        "database": database,
        "catalog": catalog,
        "cards": cards,
        "registry": registry,
    }


def _auditor(paths: dict[str, Path]) -> SourceTruthAuditor:
    return SourceTruthAuditor(
        source_data_dir=paths["source_dir"],
        processed_data_dir=paths["processed_dir"],
        sqlite_db_path=paths["database"],
        ddl_directory=paths["ddl_dir"],
        catalog_path=paths["catalog"],
        table_cards_path=paths["cards"],
        ddl_registry_path=paths["registry"],
    )


def test_audit_reports_source_traceability_and_configuration_field_drift(tmp_path: Path) -> None:
    report = _auditor(_build_fixture(tmp_path)).run()

    assert report["summary"]["table_count"] == 1
    table = report["tables"][0]
    assert table["source"]["raw_exists"] is True
    assert table["source"]["processed_exists"] is True
    assert table["sqlite"]["row_count"] == 1
    assert table["sqlite"]["vanna_fields"] == ["capacity"]
    codes = {issue["code"] for issue in table["issues"]}
    assert {
        "TABLE_CARD_ALIAS_FIELD_MISSING",
        "TABLE_CARD_IMPORTANT_FIELD_MISSING",
    }.issubset(codes)
    assert "VANNA_FIELDS_SCHEMA_MISMATCH" not in codes
    assert table["status"] == "fail"


def test_audit_treats_processed_sheet_as_authoritative_when_raw_sheet_is_not_mapped(
    tmp_path: Path,
) -> None:
    report = _auditor(_build_fixture(tmp_path)).run()

    source = report["tables"][0]["source"]
    assert source["raw_sheet_exists"] is None
    assert source["raw_sheet_status"] == "not_mapped"
    assert source["raw_sheet_summary"] is None
    assert source["processed_sheet_exists"] is True
    assert source["processed_sheet_summary"] == {
        "headers": ["项目名称", "容量"],
        "sample_row_count": 1,
        "sample_non_empty_cell_count": 2,
    }
    codes = {issue["code"] for issue in report["tables"][0]["issues"]}
    assert "RAW_SOURCE_SHEET_MISSING" not in codes


def test_audit_verifies_raw_sheet_only_when_explicit_mapping_is_configured(tmp_path: Path) -> None:
    paths = _build_fixture(tmp_path)
    catalog = json.loads(paths["catalog"].read_text(encoding="utf-8"))
    catalog["datasets"][0]["raw_source_sheet"] = "原始项目数据"
    _write_json(paths["catalog"], catalog)

    source = _auditor(paths).run()["tables"][0]["source"]

    assert source["raw_sheet_mapping"] == "原始项目数据"
    assert source["raw_sheet_status"] == "verified"
    assert source["raw_sheet_exists"] is True
    assert source["raw_sheet_summary"] == {
        "headers": ["项目名称", "容量"],
        "sample_row_count": 1,
        "sample_non_empty_cell_count": 2,
    }


def test_xlsx_xml_fallback_reads_the_same_sheet_summary(tmp_path: Path) -> None:
    paths = _build_fixture(tmp_path)

    exists, summary, error = SourceTruthAuditor._read_sheet_summary_xlsx_xml(
        paths["processed_dir"] / "source.xlsx", "项目"
    )

    assert exists is True
    assert error is None
    assert summary == {
        "headers": ["项目名称", "容量"],
        "sample_row_count": 1,
        "sample_non_empty_cell_count": 2,
    }


def test_audit_reports_vanna_business_field_drift(tmp_path: Path) -> None:
    paths = _build_fixture(tmp_path)
    with sqlite3.connect(paths["database"]) as connection:
        connection.execute("UPDATE vanna_fields SET column_name = 'wrong_field'")

    report = _auditor(paths).run()

    codes = {issue["code"] for issue in report["tables"][0]["issues"]}
    assert "VANNA_FIELDS_SCHEMA_MISMATCH" in codes


def test_audit_reports_ddl_schema_drift_and_missing_processed_file(tmp_path: Path) -> None:
    paths = _build_fixture(tmp_path)
    (paths["processed_dir"] / "source.xlsx").unlink()
    (paths["ddl_dir"] / "energy.txt").write_text(
        'CREATE TABLE "energy" ("id" INTEGER, "other" TEXT);\n', encoding="utf-8"
    )

    report = _auditor(paths).run()
    table = report["tables"][0]
    codes = {issue["code"] for issue in table["issues"]}

    assert "PROCESSED_SOURCE_FILE_MISSING" in codes
    assert "DDL_SQLITE_STRUCTURE_MISMATCH" in codes


def test_audit_reports_ddl_type_drift(tmp_path: Path) -> None:
    paths = _build_fixture(tmp_path)
    (paths["ddl_dir"] / "energy.txt").write_text(
        'CREATE TABLE "energy" ('
        '"id" INTEGER, "capacity" TEXT, "source_file" TEXT, '
        '"source_sheet" TEXT, "excel_row" INTEGER);\n',
        encoding="utf-8",
    )

    report = _auditor(paths).run()
    codes = {issue["code"] for issue in report["tables"][0]["issues"]}

    assert "DDL_SQLITE_STRUCTURE_MISMATCH" in codes


def test_markdown_renders_table_status_and_issues(tmp_path: Path) -> None:
    report = _auditor(_build_fixture(tmp_path)).run()

    markdown = render_markdown(report)

    assert "# 源数据事实核查报告" in markdown
    assert "energy" in markdown
    assert "TABLE_CARD_ALIAS_FIELD_MISSING" in markdown


def test_cli_writes_json_and_markdown_reports(tmp_path: Path) -> None:
    paths = _build_fixture(tmp_path)
    output_json = tmp_path / "audit.json"
    output_markdown = tmp_path / "audit.md"
    script = Path(__file__).resolve().parents[1] / "tools" / "audit_source_truth.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--source-data-dir", str(paths["source_dir"]),
            "--processed-data-dir", str(paths["processed_dir"]),
            "--sqlite-db-path", str(paths["database"]),
            "--ddl-directory", str(paths["ddl_dir"]),
            "--catalog-path", str(paths["catalog"]),
            "--table-cards-path", str(paths["cards"]),
            "--ddl-registry-path", str(paths["registry"]),
            "--json-output", str(output_json),
            "--markdown-output", str(output_markdown),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(output_json.read_text(encoding="utf-8"))["summary"]["failed_tables"] == 1
    assert "源数据事实核查报告" in output_markdown.read_text(encoding="utf-8")
