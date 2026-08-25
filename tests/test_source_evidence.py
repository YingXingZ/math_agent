from __future__ import annotations

import sqlite3
from pathlib import Path

import fitz
import pytest

from src.workbench8014.source_evidence import (
    ensure_source_evidence_schema, probe_pdf, register_document, resolve_document, sha256_file,
)


def _pdf(path: Path, text: str = "") -> Path:
    document = fitz.open(); page = document.new_page()
    if text:
        page.insert_text((72, 72), text)
    document.save(path); document.close()
    return path


def test_register_pdf_uses_relative_path_and_hash(tmp_path: Path) -> None:
    root, db = tmp_path / "root", tmp_path / "workbench.db"; root.mkdir()
    source = _pdf(root / "book.pdf", "chapter 1 native text layer for source registration verification")
    conn = sqlite3.connect(db); ensure_source_evidence_schema(conn)
    record = register_document(conn, textbook_id="t1", document_role="textbook", source_path=source, roots=(root,))
    conn.commit()
    row = conn.execute("SELECT stored_path,sha256,text_layer_type FROM textbook_documents").fetchone()
    assert row == ("book.pdf", sha256_file(source), "TEXT_NATIVE")
    assert record["page_count"] == 1


def test_probe_detects_scanned_pdf_without_running_ocr(tmp_path: Path) -> None:
    report = probe_pdf(_pdf(tmp_path / "scan.pdf"))
    assert report["pdf_type"] == "IMAGE_SCANNED"
    assert report["extractable_text_pages"] == 0


def test_resolver_rejects_missing_and_hash_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "root"; root.mkdir(); source = _pdf(root / "book.pdf", "original")
    expected = sha256_file(source)
    assert resolve_document("missing.pdf", expected, (root,))[1] == "missing"
    (root / "book.pdf").write_bytes(b"not the registered PDF")
    assert resolve_document("book.pdf", expected, (root,))[1] == "hash_mismatch"


def test_resolver_rejects_paths_that_escape_source_root(tmp_path: Path) -> None:
    root = tmp_path / "root"; root.mkdir()
    assert resolve_document("../outside.pdf", "a" * 64, (root,))[1] == "invalid_stored_path"


def test_resolver_rejects_ambiguous_matching_roots(tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"; first.mkdir(); second.mkdir()
    source = _pdf(first / "book.pdf", "same")
    (second / "book.pdf").write_bytes(source.read_bytes())
    assert resolve_document("book.pdf", sha256_file(source), (first, second))[1] == "ambiguous"


def test_registration_refuses_path_outside_roots(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "book.pdf", "outside")
    conn = sqlite3.connect(":memory:"); ensure_source_evidence_schema(conn)
    with pytest.raises(ValueError):
        register_document(conn, textbook_id="t1", document_role="textbook", source_path=source, roots=(tmp_path / "other",))
