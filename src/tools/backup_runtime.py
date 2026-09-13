#!/usr/bin/env python3
"""Create a consistent local backup of Math Agent runtime data.

The script copies SQLite databases with SQLite's online backup API, so it is
safe to run while the API and worker are serving requests. Uploads are copied
into the same timestamped snapshot. A completed snapshot always contains a
manifest.json; incomplete snapshots are never considered restorable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

DATABASES = {
    "agent/homework.db": "agent/homework.db",
    "workbench/api.workbench.db": "workbench/api.workbench.db",
}
UPLOADS = "uploads"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)
            row = destination_db.execute("PRAGMA integrity_check").fetchone()
    if not row or row[0] != "ok":
        raise RuntimeError(f"SQLite integrity check failed for {source}")


def upload_summary(path: Path) -> dict[str, int]:
    files = [item for item in path.rglob("*") if item.is_file()]
    return {"file_count": len(files), "byte_count": sum(item.stat().st_size for item in files)}


def create_backup(source_root: Path, backup_root: Path, retention_days: int) -> Path:
    source_root = source_root.resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    required = [source_root / relative for relative in DATABASES]
    required.append(source_root / UPLOADS)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("runtime data missing: " + ", ".join(missing))

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    temporary = backup_root / f".{stamp}.incomplete"
    destination = backup_root / stamp
    if temporary.exists() or destination.exists():
        raise FileExistsError(f"backup timestamp already exists: {stamp}")
    temporary.mkdir()
    try:
        database_manifest: dict[str, dict[str, int | str]] = {}
        for relative, target_relative in DATABASES.items():
            target = temporary / target_relative
            backup_sqlite(source_root / relative, target)
            database_manifest[relative] = {"bytes": target.stat().st_size, "sha256": sha256(target)}
        shutil.copytree(source_root / UPLOADS, temporary / UPLOADS)
        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "source_root": str(source_root),
            "databases": database_manifest,
            "uploads": upload_summary(temporary / UPLOADS),
        }
        (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    for candidate in backup_root.iterdir():
        manifest = candidate / "manifest.json"
        if not candidate.is_dir() or not manifest.exists() or candidate == destination:
            continue
        if datetime.fromtimestamp(candidate.stat().st_mtime, UTC) < cutoff:
            shutil.rmtree(candidate)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--retention-days", type=int, default=14)
    args = parser.parse_args()
    if args.retention_days < 1:
        parser.error("--retention-days must be at least 1")
    backup = create_backup(args.source_root, args.backup_root, args.retention_days)
    print(backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
