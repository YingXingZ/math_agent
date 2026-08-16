"""Render a reviewable source-page preview for each imported answer candidate.

Run with the existing OCR Python environment.  It only creates JPG previews;
it never changes standard answers or candidate matching status.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import fitz


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scale", type=float, default=1.7)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    rows = conn.execute("""
        SELECT id, source_pdf, source_page FROM answer_import_candidates
        ORDER BY id
    """).fetchall()
    docs: dict[str, fitz.Document] = {}
    rendered = skipped = 0
    for candidate_id, source_pdf, source_page in rows:
        target = args.out / f"candidate-{candidate_id}.jpg"
        if target.exists():
            skipped += 1
            continue
        source = str(source_pdf)
        if source not in docs:
            docs[source] = fitz.open(source)
        doc = docs[source]
        page = doc[int(source_page) - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(args.scale, args.scale), alpha=False)
        pix.save(str(target), output="jpg", jpg_quality=84)
        rendered += 1
    for doc in docs.values():
        doc.close()
    print({"rendered": rendered, "skipped": skipped, "total": len(rows)})


if __name__ == "__main__":
    main()
