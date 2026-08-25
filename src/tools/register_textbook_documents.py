"""Register verified textbook-source PDFs without changing questions or anchors."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench8014.source_evidence import ensure_source_evidence_schema, register_document, resolve_document, source_roots  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True, help="JSON entries: textbook_id, document_role, stored_path, sha256")
    parser.add_argument("--source-root", action="append", default=[], help="repeatable configured source root")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    roots = tuple(Path(value).resolve() for value in args.source_root) or source_roots()
    if not roots:
        raise SystemExit("No SOURCE_DOCUMENT_ROOT or --source-root was supplied.")
    conn = sqlite3.connect(args.db)
    try:
        ensure_source_evidence_schema(conn)
        results = []
        for item in mapping["documents"]:
            path, state = resolve_document(item["stored_path"], item["sha256"], roots)
            if state != "ok" or not path:
                raise SystemExit(f"Refusing to register {item['textbook_id']}: {state}")
            result = register_document(conn, textbook_id=item["textbook_id"], document_role=item["document_role"], source_path=path, roots=roots)
            results.append(result)
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
        print(json.dumps({"mode": "dry-run" if args.dry_run else "registered", "documents": results}, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
