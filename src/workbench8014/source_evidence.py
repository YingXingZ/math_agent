"""Portable textbook-source evidence utilities for 8014.

This module is intentionally limited to document registration and probing.  It
does not locate a question, crop a page, call OCR, or change any problem text.
``stored_path`` is always relative to an operator-configured source root, while
the SHA-256 is the authority used to select the correct file after migration.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import fitz


DOCUMENT_STATUSES = {"registered", "missing", "hash_mismatch"}
BBOX_SPACES = {"pdf_points", "normalized"}
ANCHOR_METHODS = {"existing_crop", "page_bbox", "text_match", "question_number", "chapter_search", "manual"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_roots(value: str | None = None) -> tuple[Path, ...]:
    """Read one or more roots from ``SOURCE_DOCUMENT_ROOT``.

    Windows uses ``;`` as ``os.pathsep``, so multiple roots can be configured
    without embedding workstation-specific absolute paths in SQLite.
    """
    raw = value if value is not None else os.environ.get("SOURCE_DOCUMENT_ROOT", "")
    return tuple(Path(part.strip()).resolve() for part in raw.split(os.pathsep) if part.strip())


def relative_to_roots(path: Path, roots: Iterable[Path]) -> str:
    resolved = path.resolve()
    for root in roots:
        try:
            return resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
    raise ValueError("document is outside SOURCE_DOCUMENT_ROOT")


def resolve_document(stored_path: str, expected_sha256: str, roots: Iterable[Path]) -> tuple[Path | None, str]:
    """Resolve a relative document path and verify its immutable fingerprint."""
    if not stored_path or Path(stored_path).is_absolute():
        return None, "invalid_stored_path"
    matches: list[Path] = []
    mismatch = False
    for root in roots:
        root = root.resolve()
        candidate = (root / stored_path.replace("\\", "/")).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None, "invalid_stored_path"
        if not candidate.is_file():
            continue
        if sha256_file(candidate) == expected_sha256:
            matches.append(candidate.resolve())
        else:
            mismatch = True
    if len(matches) == 1:
        return matches[0], "ok"
    if len(matches) > 1:
        return None, "ambiguous"
    return None, "hash_mismatch" if mismatch else "missing"


def probe_pdf(path: Path) -> dict:
    """Measure native text availability without invoking any OCR provider."""
    document = fitz.open(path)
    try:
        chars = [len(page.get_text("text").strip()) for page in document]
        page_count = len(chars)
        extractable = sum(value >= 40 for value in chars)
        ratio = extractable / max(1, page_count)
        if ratio >= 0.9:
            pdf_type = "TEXT_NATIVE"
        elif ratio >= 0.1:
            pdf_type = "TEXT_PARTIAL"
        else:
            pdf_type = "IMAGE_SCANNED"
        return {
            "page_count": page_count,
            "extractable_text_pages": extractable,
            "text_layer_ratio": round(ratio, 4),
            "average_text_chars_per_page": round(sum(chars) / max(1, page_count), 1),
            "scanned_image_pages": sum(value < 10 for value in chars),
            "pdf_type": pdf_type,
        }
    finally:
        document.close()


def ensure_source_evidence_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS textbook_documents(
        id TEXT PRIMARY KEY, textbook_id TEXT NOT NULL, document_role TEXT NOT NULL,
        filename TEXT NOT NULL, stored_path TEXT NOT NULL, sha256 TEXT NOT NULL,
        file_size INTEGER NOT NULL, page_count INTEGER NOT NULL,
        text_layer_type TEXT NOT NULL, text_layer_ratio REAL NOT NULL,
        document_status TEXT NOT NULL DEFAULT 'registered',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(textbook_id, document_role, sha256)
    );
    CREATE INDEX IF NOT EXISTS idx_textbook_documents_textbook ON textbook_documents(textbook_id);
    CREATE TABLE IF NOT EXISTS problem_source_anchors(
        id INTEGER PRIMARY KEY AUTOINCREMENT, problem_id TEXT NOT NULL,
        document_id TEXT NOT NULL, pdf_page_index INTEGER NOT NULL CHECK(pdf_page_index >= 0),
        printed_page_no TEXT, bbox_json TEXT NOT NULL DEFAULT '[]',
        bbox_space TEXT NOT NULL DEFAULT 'pdf_points', segment_index INTEGER NOT NULL DEFAULT 0,
        crop_path TEXT DEFAULT '', resolution_method TEXT NOT NULL,
        confidence REAL, status TEXT NOT NULL DEFAULT 'candidate',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(problem_id, document_id, pdf_page_index, segment_index)
    );
    CREATE INDEX IF NOT EXISTS idx_problem_source_anchor_problem ON problem_source_anchors(problem_id, status);
    CREATE INDEX IF NOT EXISTS idx_problem_source_anchor_document ON problem_source_anchors(document_id, pdf_page_index);
    """)


def register_document(
    conn: sqlite3.Connection,
    *,
    textbook_id: str,
    document_role: str,
    source_path: Path,
    roots: Iterable[Path],
) -> dict:
    if not textbook_id or not document_role:
        raise ValueError("textbook_id and document_role are required")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    roots = tuple(roots)
    relative = relative_to_roots(source_path, roots)
    fingerprint = sha256_file(source_path)
    probe = probe_pdf(source_path)
    now = now_iso()
    existing = conn.execute(
        "SELECT id FROM textbook_documents WHERE textbook_id=? AND document_role=? AND sha256=?",
        (textbook_id, document_role, fingerprint),
    ).fetchone()
    payload = (
        document_role, source_path.name, relative, fingerprint, source_path.stat().st_size,
        probe["page_count"], probe["pdf_type"], probe["text_layer_ratio"], "registered", now,
    )
    if existing:
        conn.execute(
            """UPDATE textbook_documents SET document_role=?,filename=?,stored_path=?,sha256=?,file_size=?,
               page_count=?,text_layer_type=?,text_layer_ratio=?,document_status=?,updated_at=? WHERE id=?""",
            (*payload, existing[0]),
        )
        document_id = existing[0]
    else:
        document_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO textbook_documents
               (id,textbook_id,document_role,filename,stored_path,sha256,file_size,page_count,
                text_layer_type,text_layer_ratio,document_status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (document_id, textbook_id, *payload[:-1], now, now),
        )
    return {"id": document_id, "textbook_id": textbook_id, "document_role": document_role,
            "stored_path": relative, "sha256": fingerprint, **probe}
