"""Read-only question-bank repair queue for the teacher portal.

This module deliberately does not call a model, write 8014, or change a
candidate's review state.  It is only the visual worklist that tells a teacher
which records can be reviewed from a crop and which records first need a source
textbook/answer image.
"""
from __future__ import annotations

import os
import re
import sqlite3
import uuid
import json
from collections import Counter
from pathlib import Path
from typing import Literal


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FULLWIDTH_GARBAGE = re.compile(r"[\uFF21-\uFF3A\uFF41-\uFF5A\uFF5E\uFF07\uFF3C\uFF5C\uE000-\uF8FF\uFFFD]")
ASCII_RUN = re.compile(r"[A-Za-z]{2,}")
MATH_WORDS = set("""sin cos tan cot sec cosec csc alt lim log ln lg exp dx dy dz dt du dv dw ds
grad div curl det max min inf sup sum int alpha beta gamma delta theta lambda mu nu pi sigma omega phi psi rho tau xi eta zeta forall exists frac sqrt partial infty equiv approx le ge ne pm times mathbb mathrm mathbf mathcal vec hat bar tilde sinx cosx tanx expx logx lnx arctan arcsin arccos arccot arctanh deg mod res sub adj ker rank col row span null dim prod sympy oo dfrac tfrac cn eta iota kappa tanh sinh cosh arsinh arcosh artanh""".split())
MATH_PAIRS = set("xy ab uv ij kl mn pq rs wx yz ax bx cx ay by cy az bz ac bc ad bd".split())
ReviewStatus = Literal["ready_for_teacher_review", "requires_source_image", "corrupt"]


def _looks_corrupt(value: str) -> bool:
    text = (value or "").strip()
    return bool(text) and ("锟" in text or len(FULLWIDTH_GARBAGE.findall(text)) / len(text) > 0.006)


def _scan_looks_corrupt(value: str) -> bool:
    """The historic 73-record scan: full-width garbage or ASCII salad in a stem."""
    text = (value or "").strip()
    if _looks_corrupt(text):
        return True
    # Match the historical scan's "ASCII salad" rule, but do not flag normal
    # structured LaTex or familiar mathematical identifiers.
    if "\\" in text or "{" in text:
        return False
    odd = [word.lower() for word in ASCII_RUN.findall(text)
           if word.lower() not in MATH_WORDS and word.lower() not in MATH_PAIRS
           and not (len(word) == 2 and word[0].lower() == word[1].lower())]
    return len(odd) >= 4


def _crop_exists(root: Path, stored_path: str | None) -> bool:
    return bool(stored_path) and (root / stored_path.replace("\\", "/")).is_file()


def _paths() -> tuple[Path, Path]:
    return (
        Path(os.environ.get("WORKBENCH_DB", REPOSITORY_ROOT / "api.workbench.db")),
        Path(os.environ.get("IMAGE_ROOT", REPOSITORY_ROOT / "extract_img")),
    )


def attach_source_image(problem_id: str, filename: str, payload: bytes) -> dict:
    """Store a teacher-provided crop and bind it to exactly one 8014 problem."""
    if not payload or len(payload) > 12 * 1024 * 1024:
        raise ValueError("Image must be between 1 byte and 12 MB")
    signatures = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF")
    if not payload.startswith(signatures):
        raise ValueError("Only PNG, JPEG, GIF, or WebP image files are accepted")
    suffix = Path(filename or "upload.png").suffix.lower()
    suffix = suffix if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"} else ".png"
    database, images = _paths()
    if not database.is_file():
        raise FileNotFoundError(f"8014 database not found: {database}")
    folder = images / "teacher_uploads"
    folder.mkdir(parents=True, exist_ok=True)
    relative = f"teacher_uploads/problem-{problem_id}-{uuid.uuid4().hex}{suffix}"
    target = images / relative
    conn = sqlite3.connect(database)
    try:
        exists = conn.execute("SELECT 1 FROM problems WHERE id=?", (problem_id,)).fetchone()
        if not exists:
            raise LookupError("8014 problem not found")
        target.write_bytes(payload)
        conn.execute("UPDATE problems SET crop_image_path=? WHERE id=?", (relative, problem_id))
        conn.commit()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        conn.close()
    return {"problem_id": problem_id, "crop_image_path": relative}


def attach_evidence_images(problem_id: str, role: str, uploads: list[tuple[str, bytes]]) -> dict:
    """Bind a multi-image question or answer packet to one problem."""
    if role not in {"question", "answer"} or not uploads or len(uploads) > 12:
        raise ValueError("Choose 1-12 question or answer images")
    database, images = _paths()
    conn = sqlite3.connect(database)
    current = conn.execute("SELECT crop_image_path FROM problems WHERE id=?", (problem_id,)).fetchone()
    conn.close()
    if not current:
        raise LookupError("8014 problem not found")
    saved = [attach_source_image(problem_id, name, content) for name, content in uploads]
    meta = images / "teacher_uploads" / f"problem-{problem_id}-evidence.json"
    existing = json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {"question": [], "answer": []}
    existing[role] = [item["crop_image_path"] for item in saved]
    # crop_image_path remains the first question image so the existing 8014 image
    # route and approved-candidate workflow stay compatible.
    if role == "question":
        conn = sqlite3.connect(database)
        conn.execute("UPDATE problems SET crop_image_path=? WHERE id=?", (existing[role][0], problem_id))
        conn.commit(); conn.close()
    else:
        # Answer evidence must never replace the problem crop used by 8014.
        conn = sqlite3.connect(database)
        conn.execute("UPDATE problems SET crop_image_path=? WHERE id=?", (current[0], problem_id))
        conn.commit(); conn.close()
    meta.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
    return {"problem_id": problem_id, "role": role, "paths": existing[role], "evidence": existing}


def evidence_images_for_problem(problem_id: str) -> dict:
    """Return all teacher-bound images; legacy crop becomes a one-image question packet."""
    item = source_item_for_problem(problem_id)
    _, images = _paths()
    meta = images / "teacher_uploads" / f"problem-{problem_id}-evidence.json"
    evidence = json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {"question": [item["evidence"]["crop_image_path"]], "answer": []}
    return {"item": item, "question": evidence.get("question") or [item["evidence"]["crop_image_path"]], "answer": evidence.get("answer") or []}


def source_item_for_problem(problem_id: str) -> dict:
    """Load one crop-backed 8014 record in the format expected by the VLM bridge."""
    database, images = _paths()
    if not database.is_file():
        raise FileNotFoundError(f"8014 database not found: {database}")
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT p.id,p.problem_no,p.sub_no,p.ptype,p.difficulty,p.crop_image_path,s.section_no
                 FROM problems p JOIN sections s ON s.id=p.section_id WHERE p.id=?""", (problem_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise LookupError("8014 problem not found")
    if not _crop_exists(images, row["crop_image_path"]):
        raise ValueError("No readable source crop is bound to this problem")
    return {
        "source_problem_id": str(row["id"]), "problem_no": str(row["problem_no"] or ""),
        "sub_no": str(row["sub_no"] or ""), "ptype": row["ptype"] or "calc",
        "difficulty": row["difficulty"],
        "evidence": {"section_no": row["section_no"], "crop_image_path": row["crop_image_path"]},
    }


def build_review_queue(
    status: ReviewStatus | None = None,
    limit: int = 200,
    *,
    db_path: Path | None = None,
    crop_root: Path | None = None,
) -> dict:
    """Return a bounded, read-only queue plus all-state counts."""
    database = db_path or Path(os.environ.get("WORKBENCH_DB", REPOSITORY_ROOT / "api.workbench.db"))
    images = crop_root or Path(os.environ.get("IMAGE_ROOT", REPOSITORY_ROOT / "extract_img"))
    if not database.is_file():
        raise FileNotFoundError(f"8014 database not found: {database}")
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT p.id, p.problem_no, p.sub_no, p.ptype, p.difficulty,
                      p.content_text, p.std_answer, p.answer_status, p.crop_image_path,
                      s.section_no
               FROM problems p JOIN sections s ON s.id=p.section_id
               ORDER BY s.section_no, p.problem_no, p.sub_no"""
        ).fetchall()
    finally:
        conn.close()

    items: list[dict] = []
    corrupt_items: list[dict] = []
    complete = 0
    for row in rows:
        stem, answer = (row["content_text"] or "").strip(), (row["std_answer"] or "").strip()
        corrupt = row["answer_status"] == "corrupt_ocr" or _looks_corrupt(stem) or _looks_corrupt(answer)
        incomplete = not stem or not answer
        scan_corrupt = bool(stem and answer and _scan_looks_corrupt(stem))
        if not corrupt and not incomplete:
            complete += 1
            if not scan_corrupt:
                continue
        has_crop = _crop_exists(images, row["crop_image_path"])
        # Availability is the primary workflow state.  Corruption is a second,
        # orthogonal warning: a corrupt record can still be missing its crop.
        item_status: ReviewStatus = "ready_for_teacher_review" if has_crop else "requires_source_image"
        record = {
            "problem_id": row["id"], "section_no": row["section_no"],
            "problem_no": row["problem_no"], "sub_no": row["sub_no"] or "",
            "ptype": row["ptype"] or "calc", "difficulty": row["difficulty"],
            "status": item_status, "corrupt": scan_corrupt, "crop_available": has_crop,
            "crop_image_path": row["crop_image_path"] or "",
            "missing": [name for name, value in (("content_text", stem), ("std_answer", answer)) if not value],
        }
        # Source-image repair retains its original 17 / 92 readiness semantics.
        if corrupt or incomplete:
            items.append(record)
        # The historic corruption scan is a separate worklist, including records
        # with both fields present; it must not inflate the missing-image queue.
        if scan_corrupt:
            corrupt_items.append(record)
    counts = Counter(item["status"] for item in items)
    counts["corrupt"] = len(corrupt_items)
    counts.update({"complete": complete, "total": len(rows)})
    filtered = corrupt_items if status == "corrupt" else [
        item for item in items if not status or item["status"] == status
    ]
    return {"counts": dict(counts), "items": filtered[:max(1, min(limit, 500))], "image_root": str(images)}
