"""Audit date-like fields in the published SQLite datasets.

The report distinguishes normalized ISO dates from raw Excel serial values and
unparseable text so query rules are grounded in the imported data, not DDL alone.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any


ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
YEAR_MONTH_DASH = re.compile(r"^\d{4}-(?:0?[1-9]|1[0-2])$")
YEAR_MONTH_DOT = re.compile(r"^\d{4}\.(?:0?[1-9]|1[0-2])$")
EXCEL_SERIAL = re.compile(r"^\d{1,5}(?:\.0+)?$")


def is_iso_date(value: str) -> bool:
    if not ISO_DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_excel_serial(value: str) -> bool:
    if not EXCEL_SERIAL.fullmatch(value):
        return False
    serial = float(value)
    return 1 <= serial <= 100_000


def classify(value: Any) -> str:
    if value is None or not str(value).strip():
        return "empty"
    text = str(value).strip()
    if is_iso_date(text):
        return "iso_date"
    if YEAR_MONTH_DASH.fullmatch(text):
        return "year_month_dash"
    if YEAR_MONTH_DOT.fullmatch(text):
        return "year_month_dot"
    if is_excel_serial(text):
        return "excel_serial"
    return "unparseable"


def audit_column(connection: sqlite3.Connection, table: str, column: str) -> dict[str, Any]:
    quoted_table = '"' + table.replace('"', '""') + '"'
    quoted_column = '"' + column.replace('"', '""') + '"'
    values = [row[0] for row in connection.execute(f"SELECT {quoted_column} FROM {quoted_table}")]
    categories = {
        name: 0
        for name in (
            "empty",
            "iso_date",
            "year_month_dash",
            "year_month_dot",
            "excel_serial",
            "unparseable",
        )
    }
    samples: list[str] = []
    for value in values:
        category = classify(value)
        categories[category] += 1
        if category in {"excel_serial", "unparseable"} and len(samples) < 10:
            samples.append(str(value))
    date_safe = categories["excel_serial"] == 0 and categories["unparseable"] == 0
    precision = (
        "day"
        if categories["iso_date"] and not (categories["year_month_dash"] or categories["year_month_dot"])
        else "month"
        if (categories["year_month_dash"] or categories["year_month_dot"])
        and not categories["iso_date"]
        else "mixed_or_unknown"
    )
    return {
        "table": table,
        "column": column,
        "total_rows": len(values),
        **categories,
        "safe_for_sqlite_date": date_safe and precision == "day",
        "date_precision": precision,
        "problem_samples": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    published_tables = {item["table"] for item in catalog["datasets"]}
    with sqlite3.connect(args.database) as connection:
        report = []
        for table in sorted(published_tables):
            fields = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            for column in fields:
                normalized = column.casefold()
                if normalized.endswith(("_date", "_time")) or "date" in normalized:
                    report.append(audit_column(connection, table, column))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"date_fields": report}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"date_fields": report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
