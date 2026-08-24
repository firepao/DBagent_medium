"""Generate a read-only source-truth audit report for the Medium query service."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MEDIUM_DIR = Path(__file__).resolve().parents[1]
if str(MEDIUM_DIR) not in sys.path:
    sys.path.insert(0, str(MEDIUM_DIR))

from app.source_truth_audit import SourceTruthAuditor, render_markdown


def _default_data_root() -> Path:
    return MEDIUM_DIR.parent / "data" / "数据入库_v1.0.1_2026.07.17" / "data_1_all"


def _default_runtime_root() -> Path:
    return MEDIUM_DIR / "data" / "数据入库v_1.1_0722" / "query_ready_v2"


def build_parser() -> argparse.ArgumentParser:
    root = _default_data_root()
    runtime_root = _default_runtime_root()
    parser = argparse.ArgumentParser(description="核查源 Excel、SQLite、DDL 与 TableCard 的一致性")
    parser.add_argument("--source-data-dir", type=Path, default=root / "data")
    parser.add_argument("--processed-data-dir", type=Path, default=root / "data_1")
    parser.add_argument("--sqlite-db-path", type=Path, default=runtime_root / "zhangbei_energy_query_ready_v2.sqlite3")
    parser.add_argument("--ddl-directory", type=Path, default=runtime_root / "ddl")
    parser.add_argument("--catalog-path", type=Path, default=MEDIUM_DIR / "config" / "catalog.json")
    parser.add_argument("--table-cards-path", type=Path, default=MEDIUM_DIR / "config" / "table_cards.json")
    parser.add_argument("--ddl-registry-path", type=Path, default=MEDIUM_DIR / "config" / "ddl_registry.json")
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = SourceTruthAuditor(
        source_data_dir=args.source_data_dir,
        processed_data_dir=args.processed_data_dir,
        sqlite_db_path=args.sqlite_db_path,
        ddl_directory=args.ddl_directory,
        catalog_path=args.catalog_path,
        table_cards_path=args.table_cards_path,
        ddl_registry_path=args.ddl_registry_path,
    ).run()
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.write_text(render_markdown(report) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 1 if report["summary"]["failed_tables"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
