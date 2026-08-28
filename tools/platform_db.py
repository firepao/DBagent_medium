from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path


def verify(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"SQLite 完整性检查失败: {result}")


def prune_backups(directory: Path, *, keep: int = 7, max_age_days: int | None = 30) -> list[Path]:
    if keep < 1:
        raise ValueError("keep 必须至少为 1")
    candidates = sorted(directory.glob("platform-*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days) if max_age_days is not None else None
    removed: list[Path] = []
    for index, item in enumerate(candidates):
        too_old = cutoff is not None and datetime.fromtimestamp(item.stat().st_mtime, UTC) < cutoff
        if index >= keep or too_old:
            item.unlink()
            removed.append(item)
    return removed


def backup(source: Path, directory: Path, *, keep: int = 7, max_age_days: int | None = 30) -> Path:
    verify(source)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"platform-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
    with closing(sqlite3.connect(source)) as source_db, closing(sqlite3.connect(target)) as target_db:
        source_db.backup(target_db)
    verify(target)
    prune_backups(directory, keep=keep, max_age_days=max_age_days)
    return target


def restore(source: Path, target: Path, *, confirmed: bool, dry_run: bool = False) -> dict[str, str | bool]:
    if not confirmed and not dry_run:
        raise ValueError("恢复会覆盖目标数据库，必须传入 --confirm")
    verify(source)
    report: dict[str, str | bool] = {"source": str(source), "target": str(target), "verified": True, "dry_run": dry_run}
    if dry_run:
        return report
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".restore", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    previous: Path | None = None
    installed = False
    try:
        with closing(sqlite3.connect(source)) as source_db, closing(
            sqlite3.connect(temporary)
        ) as target_db:
            source_db.backup(target_db)
        verify(temporary)
        if target.exists():
            previous_descriptor, previous_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".previous", dir=target.parent
            )
            os.close(previous_descriptor)
            previous = Path(previous_name)
            previous.unlink(missing_ok=True)
            os.replace(target, previous)
        try:
            os.replace(temporary, target)
            installed = True
        except BaseException:
            if previous is not None and previous.exists():
                os.replace(previous, target)
            raise
        try:
            verify(target)
        except BaseException:
            target.unlink(missing_ok=True)
            if previous is not None and previous.exists():
                os.replace(previous, target)
            raise
        if previous is not None:
            previous.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)
        if previous is not None and previous.exists() and not installed:
            previous.unlink(missing_ok=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="平台 SQLite 备份、校验与恢复")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("verify")
    check.add_argument("path", type=Path)
    create = subparsers.add_parser("backup")
    create.add_argument("source", type=Path)
    create.add_argument("--output", type=Path, default=Path("backups"))
    create.add_argument("--keep", type=int, default=7)
    create.add_argument("--max-age-days", type=int, default=30)
    recover = subparsers.add_parser("restore")
    recover.add_argument("source", type=Path)
    recover.add_argument("target", type=Path)
    recover.add_argument("--confirm", action="store_true")
    recover.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "verify":
        verify(args.path)
        print("数据库完整性检查通过")
    elif args.command == "backup":
        print(backup(args.source, args.output, keep=args.keep, max_age_days=args.max_age_days))
    else:
        report = restore(args.source, args.target, confirmed=args.confirm, dry_run=args.dry_run)
        print("恢复演练校验通过" if args.dry_run else "数据库恢复完成")


if __name__ == "__main__":
    main()
