"""Render candidate-anchor crops as review evidence without changing the question bank.

Only ``problem_source_anchors.crop_path`` is updated.  This deliberately never
writes ``problems.crop_image_path`` or any question/answer content: generated
crops remain evidence awaiting teacher confirmation.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import fitz

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench8014.source_evidence import ensure_source_evidence_schema, resolve_document, source_roots  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--image-root", type=Path, required=True,
                        help="Root for generated evidence; paths stored relative to it")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=400)
    parser.add_argument("--apply", action="store_true", help="Write only candidate crop paths to anchor metadata")
    args = parser.parse_args()
    args.image_root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db); conn.row_factory = sqlite3.Row
    try:
        ensure_source_evidence_schema(conn)
        rows = conn.execute("""
            SELECT a.id AS anchor_id,a.problem_id,a.pdf_page_index,a.bbox_json,a.status,
                   d.stored_path,d.sha256,d.filename
            FROM problem_source_anchors a JOIN textbook_documents d ON d.id=a.document_id
            WHERE a.status='candidate' AND (a.crop_path IS NULL OR a.crop_path='')
            ORDER BY a.id
        """).fetchall()
        roots = source_roots()
        results: list[dict] = []
        for row in rows:
            source, state = resolve_document(row['stored_path'], row['sha256'], roots)
            item = {"anchor_id": row['anchor_id'], "problem_id": row['problem_id'], "source_state": state,
                    "pdf_page_index": row['pdf_page_index'], "status": "blocked"}
            if source is None:
                results.append(item); continue
            try:
                bbox = json.loads(row['bbox_json'])
                if not isinstance(bbox, list) or len(bbox) != 4:
                    raise ValueError('bbox must have four pdf-point values')
                document = fitz.open(source)
                try:
                    page = document[int(row['pdf_page_index'])]
                    clip = fitz.Rect(*bbox) & page.rect
                    if clip.is_empty or clip.is_infinite:
                        raise ValueError('empty or invalid bbox after page clipping')
                    relative = Path('candidate_anchor_crops') / f"anchor-{row['anchor_id']}-p{row['pdf_page_index'] + 1}.png"
                    target = args.image_root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    page.get_pixmap(matrix=fitz.Matrix(args.dpi / 72, args.dpi / 72), clip=clip, alpha=False).save(target)
                finally:
                    document.close()
                item.update({"status": "rendered", "crop_path": relative.as_posix(), "bbox_pdf_points": list(clip)})
                if args.apply:
                    conn.execute("UPDATE problem_source_anchors SET crop_path=?,updated_at=? WHERE id=? AND status='candidate'",
                                 (relative.as_posix(), datetime.now(timezone.utc).isoformat(), row['anchor_id']))
                    item['anchor_metadata_written'] = True
            except Exception as exc:
                item.update({"status": "failed", "error": str(exc)})
            results.append(item)
        if args.apply:
            conn.commit()
    finally:
        conn.close()
    payload = {"schema_version": "candidate-anchor-crop-materialization/v1",
               "created_at": datetime.now(timezone.utc).isoformat(), "mode": "apply" if args.apply else "dry-run",
               "counts": {key: sum(row['status'] == key for row in results) for key in ('rendered', 'blocked', 'failed')},
               "results": results}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload['counts'], ensure_ascii=False))


if __name__ == '__main__':
    main()
