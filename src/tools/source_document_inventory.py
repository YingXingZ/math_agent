"""Create a read-only inventory of PDF source documents and native text layers."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench8014.source_evidence import probe_pdf, sha256_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", default=[], help="Directory to scan recursively; repeatable")
    parser.add_argument("--file", action="append", default=[], help="Specific PDF to inventory; repeatable")
    parser.add_argument("--out", required=True, help="JSON output path")
    args = parser.parse_args()
    if not args.root and not args.file:
        parser.error("at least one --root or --file is required")
    rows = []
    for raw_root in args.root:
        root = Path(raw_root).resolve()
        if not root.is_dir():
            raise SystemExit(f"Not a directory: {root}")
        for pdf in sorted(root.rglob("*.pdf")):
            row = {
                "source_root": f"scan-root-{len(rows) + 1}", "stored_path": pdf.relative_to(root).as_posix(),
                "filename": pdf.name, "file_size": pdf.stat().st_size, "sha256": sha256_file(pdf),
            }
            try:
                row.update(probe_pdf(pdf))
            except Exception as exc:  # inventory must describe corrupt inputs, not hide them
                row.update({"pdf_type": "UNREADABLE", "probe_error": str(exc)})
            rows.append(row)
    for raw_file in args.file:
        pdf = Path(raw_file).resolve()
        if not pdf.is_file():
            raise SystemExit(f"Not a file: {pdf}")
        row = {"source_root": "explicit-file-parent", "stored_path": pdf.name, "filename": pdf.name,
               "file_size": pdf.stat().st_size, "sha256": sha256_file(pdf)}
        try:
            row.update(probe_pdf(pdf))
        except Exception as exc:
            row.update({"pdf_type": "UNREADABLE", "probe_error": str(exc)})
        rows.append(row)
    output = {"created_at": datetime.now(timezone.utc).isoformat(), "documents": rows}
    destination = Path(args.out); destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Inventoried {len(rows)} PDF(s) -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
