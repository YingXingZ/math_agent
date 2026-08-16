r"""Build auditable answer candidates directly from OCR answer-book PDFs.

This importer deliberately does *not* overwrite ``problems.std_answer``.
Whole-page OCR is too easy to misalign for a mathematics answer book.  Instead
it stores one candidate per (volume, section, problem, subproblem) with its
source page and confidence.  A candidate must be approved in the workbench
before it can enable automatic grading.

Requires the existing Workbuddy OCR environment, which provides PyMuPDF and
RapidOCR:
  C:\Users\YXZ\.workbuddy\binaries\python\envs\ocr\Scripts\python.exe
      pdf_answer_importer.py --db api.db --up <上册答案OCR.pdf> --down <下册答案OCR.pdf>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import fitz
from rapidocr_onnxruntime import RapidOCR


SECTION_RE = re.compile(r"习题\s*(\d+\.\d+)")
# Deliberately strict: a number inside an equation must not become a question.
PROBLEM_RE = re.compile(r"(?:^|\n)\s*(\d{1,2})\s*[\.．、]\s*(?=\S)")
SUB_RE = re.compile(r"(?:^|\n)\s*[（(]\s*(\d{1,2})\s*[）)]\s*(?=\S)")


# Keep Chinese tokens as Unicode escapes so this script remains portable even
# when edited from a Windows console configured with a legacy code page.
SECTION_RE = re.compile(r"\u4e60\u9898\s*(\d+\.\d+)")
# A real exercise header has a space after its number.  Requiring it prevents
# a figure label such as "1.5" from being mistaken for exercise 1.
PROBLEM_RE = re.compile(r"(?:^|\n)\s*(\d{1,2})\s*[\.\u3002]\s+(?=\S)")
SUB_RE = re.compile(r"(?:^|\n)\s*[\(\uff08]\s*(\d{1,2})\s*[\)\uff09]\s*(?=\S)")


def init_candidate_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS answer_import_candidates (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      problem_id TEXT NOT NULL,
      volume TEXT NOT NULL,
      section_no TEXT NOT NULL,
      problem_no TEXT NOT NULL,
      sub_no TEXT,
      source_pdf TEXT NOT NULL,
      source_page INTEGER NOT NULL,
      ocr_text TEXT NOT NULL,
      ocr_confidence REAL NOT NULL,
      match_status TEXT NOT NULL DEFAULT 'pending',
      match_reason TEXT DEFAULT '',
      content_hash TEXT NOT NULL UNIQUE,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candidate_problem ON answer_import_candidates(problem_id, match_status)")
    conn.commit()


def page_lines(engine: RapidOCR, page: fitz.Page, scale: float) -> tuple[list[tuple[float, float, str, float]], float]:
    # These PDFs already contain an OCR text layer.  It is both faster and less
    # lossy than running a second general-purpose OCR pass.  Keep the original
    # block coordinates so the existing reading-order logic still applies.
    embedded: list[tuple[float, float, str, float]] = []
    for block in page.get_text("blocks"):
        x0, y0, _x1, _y1, value = block[:5]
        value = str(value).strip()
        if value:
            embedded.append((float(y0), float(x0), value, 0.99))
    if sum(len(line[2]) for line in embedded) >= 80:
        embedded.sort(key=lambda row: (round(row[0] / 12), row[1]))
        return embedded, 0.99

    # Some cover and scanned pages have no usable text layer; only those pages
    # fall back to visual OCR.
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    # RapidOCR accepts encoded PNG bytes; this avoids a temporary image file.
    result, _ = engine(pix.tobytes("png"))
    if not result:
        return [], 0.0
    lines: list[tuple[float, float, str, float]] = []
    confidences = []
    for item in result:
        box, text = item[0], str(item[1]).strip()
        confidence = float(item[2]) if len(item) > 2 else 0.0
        if not text:
            continue
        x = min(point[0] for point in box)
        y = min(point[1] for point in box)
        lines.append((y, x, text, confidence))
        confidences.append(confidence)
    # Keep reading order stable.  For a two-column page, y is intentionally
    # retained with an x tie-breaker; the candidate also keeps source_page for
    # human verification rather than pretending this is a perfect layout model.
    lines.sort(key=lambda row: (round(row[0] / 12), row[1]))
    return lines, sum(confidences) / len(confidences) if confidences else 0.0


def split_blocks(text: str) -> Iterable[tuple[str, str | None, str]]:
    """Yield strict problem/subproblem blocks from one OCR page.

    The section is supplied by the caller.  No header means no block: we never
    fall back to assigning an entire page or section to an arbitrary problem.
    """
    headers = list(PROBLEM_RE.finditer(text))
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        problem_no = header.group(1)
        body = text[header.end():end].strip()
        subs = list(SUB_RE.finditer(body))
        if subs:
            for sub_index, sub in enumerate(subs):
                sub_end = subs[sub_index + 1].start() if sub_index + 1 < len(subs) else len(body)
                candidate = body[sub.end():sub_end].strip()
                if candidate:
                    yield problem_no, sub.group(1), candidate
        elif body:
            yield problem_no, None, body


def problem_index(conn: sqlite3.Connection) -> dict[tuple[str, str | None], list[str]]:
    rows = conn.execute("""
        SELECT p.id, p.problem_no, p.sub_no
        FROM problems p JOIN sections s ON s.id=p.section_id
        WHERE s.section_no=?
    """, ("__placeholder__",)).fetchall()
    # Section-specific indexes are built lazily in import_volume.
    return {}


def section_problem_index(conn: sqlite3.Connection, section_no: str) -> dict[tuple[str, str | None], list[str]]:
    index: dict[tuple[str, str | None], list[str]] = defaultdict(list)
    for row in conn.execute("""
        SELECT p.id, p.problem_no, p.sub_no
        FROM problems p JOIN sections s ON s.id=p.section_id
        WHERE s.section_no=?
    """, (section_no,)):
        index[(str(row[1]), str(row[2]) if row[2] is not None else None)].append(row[0])
    return index


def store_candidate(conn: sqlite3.Connection, *, problem_id: str, volume: str, section: str,
                    problem: str, sub: str | None, pdf: Path, page: int, text: str,
                    confidence: float, reason: str) -> bool:
    digest = hashlib.sha256(f"{problem_id}|{pdf}|{page}|{text}".encode("utf-8")).hexdigest()
    cur = conn.execute("""
        INSERT OR IGNORE INTO answer_import_candidates
        (problem_id,volume,section_no,problem_no,sub_no,source_pdf,source_page,ocr_text,ocr_confidence,match_status,match_reason,content_hash)
        VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)
    """, (problem_id, volume, section, problem, sub, str(pdf), page, text, confidence, reason, digest))
    return cur.rowcount == 1


def import_volume(conn: sqlite3.Connection, engine: RapidOCR, pdf_path: Path, volume: str,
                  start_page: int = 1, end_page: int | None = None, scale: float = 2.2) -> dict[str, int]:
    doc = fitz.open(pdf_path)
    end = min(end_page or len(doc), len(doc))
    section: str | None = None
    stats = defaultdict(int)
    indexes: dict[str, dict[tuple[str, str | None], list[str]]] = {}
    for number in range(max(1, start_page), end + 1):
        lines, confidence = page_lines(engine, doc[number - 1], scale)
        text = "\n".join(line[2] for line in lines)
        stats["pages"] += 1
        if not text:
            stats["empty_pages"] += 1
            continue
        found_sections = SECTION_RE.findall(text)
        if found_sections:
            section = found_sections[-1]
            indexes.setdefault(section, section_problem_index(conn, section))
        if not section:
            stats["pages_without_section"] += 1
            continue
        idx = indexes.setdefault(section, section_problem_index(conn, section))
        for problem, sub, candidate in split_blocks(text):
            stats["detected_blocks"] += 1
            matches = idx.get((problem, sub), [])
            # A page-level problem may correspond to exactly one database item.
            if not matches and sub is None:
                matches = idx.get((problem, None), [])
            if len(matches) != 1:
                stats["unmatched_blocks"] += 1
                continue
            if len(candidate) < 2 or confidence < 0.45:
                stats["low_confidence_blocks"] += 1
                continue
            if store_candidate(conn, problem_id=matches[0], volume=volume, section=section,
                               problem=problem, sub=sub, pdf=pdf_path, page=number,
                               text=candidate, confidence=confidence,
                               reason="strict section/problem/subproblem match"):
                stats["candidates"] += 1
    doc.close()
    conn.commit()
    return dict(stats)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import answer-book OCR candidates without overwriting verified answers")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--up", type=Path, help="上册答案 OCR PDF")
    parser.add_argument("--down", type=Path, help="下册答案 OCR PDF")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--end-page", type=int)
    parser.add_argument("--scale", type=float, default=2.2)
    args = parser.parse_args()
    sources = [("up", args.up), ("down", args.down)]
    if not any(path for _, path in sources):
        parser.error("at least one of --up / --down is required")
    with sqlite3.connect(args.db) as conn:
        init_candidate_table(conn)
        engine = RapidOCR()
        for volume, path in sources:
            if not path:
                continue
            if not path.is_file():
                raise SystemExit(f"PDF not found: {path}")
            result = import_volume(conn, engine, path, volume, args.start_page, args.end_page, args.scale)
            print(volume, json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
