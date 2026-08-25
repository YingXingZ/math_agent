"""Build a non-destructive evidence plan for locally flagged questions.

The plan joins the 8000 cache to its 8014 source problem and reports whether a
real crop or a registered PDF page is available.  It never calls a model,
changes a review status, or writes either database.  Only crop-backed entries
are eligible for VLM staging.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .question_bank_review import _looks_corrupt, _scan_looks_corrupt


LEGACY_OCR_MARKERS = re.compile(r"[锟�叫咱呗]")


def live_garble_reasons(question: dict[str, Any]) -> tuple[str, list[str]]:
    """Return the same conservative signal used by the live teacher queue."""
    reasons: list[str] = []
    high_risk = False
    for field, label in (("content", "题干"), ("answer", "答案"), ("rubric", "评分参考")):
        text = str(question.get(field) or "").strip()
        if not text:
            if field in {"content", "answer"}:
                reasons.append(f"{label}为空")
                high_risk = True
            continue
        if _looks_corrupt(text):
            reasons.append(f"{label}含全角/替换字符")
            high_risk = True
        if len(LEGACY_OCR_MARKERS.findall(text)) >= 3:
            reasons.append(f"{label}含重复 OCR 代字")
            high_risk = True
        elif _scan_looks_corrupt(text):
            reasons.append(f"{label}含可疑公式或字母序列")
    return ("high" if high_risk else "medium"), reasons


def _file_from_recorded_path(recorded: str | None, repository_root: Path) -> Path | None:
    """Resolve a registered PDF path without guessing a different source file."""
    if not recorded:
        return None
    candidate = Path(recorded)
    if candidate.is_file():
        return candidate.resolve()
    files = [item for item in repository_root.rglob(candidate.name) if item.is_file()]
    return files[0].resolve() if len(files) == 1 else None


def _crop_file(image_root: Path, recorded: str | None) -> Path | None:
    if not recorded:
        return None
    candidate = Path(recorded)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    candidate = image_root / recorded.replace("\\", "/")
    return candidate if candidate.is_file() else None


def build_evidence_manifest(agent_db: Path, workbench_db: Path, image_root: Path, repository_root: Path) -> dict[str, Any]:
    """Produce a JSON-serialisable, read-only live repair manifest."""
    agent = sqlite3.connect(agent_db)
    agent.row_factory = sqlite3.Row
    try:
        local_rows = [dict(row) for row in agent.execute(
            "SELECT id,content,answer,rubric,chapter,review_status,source_problem_id FROM questions ORDER BY id"
        ).fetchall()]
    finally:
        agent.close()
    workbench = sqlite3.connect(workbench_db)
    workbench.row_factory = sqlite3.Row
    try:
        source_rows = {str(row["id"]): dict(row) for row in workbench.execute(
            """SELECT p.id,p.problem_no,p.sub_no,p.ptype,p.difficulty,p.crop_image_path,p.source_page,
                      s.section_no,t.name AS textbook_name,t.pdf_path
               FROM problems p JOIN sections s ON s.id=p.section_id
               JOIN textbooks t ON t.id=s.textbook_id"""
        ).fetchall()}
    finally:
        workbench.close()
    candidates: list[dict[str, Any]] = []
    for local in local_rows:
        risk, reasons = live_garble_reasons(local)
        if not reasons:
            continue
        source_id = str(local.get("source_problem_id") or "")
        source = source_rows.get(source_id)
        item: dict[str, Any] = {
            "question_id": local["id"], "review_status": local["review_status"], "risk": risk,
            "reasons": reasons, "chapter": local["chapter"], "source_problem_id": source_id or None,
            "disposition": "needs_source_binding",
            "teacher_action": "将本地题绑定到 8014 来源题，或提供教材/答案裁切图。",
        }
        if source is not None:
            crop = _crop_file(image_root, source["crop_image_path"])
            pdf = _file_from_recorded_path(source["pdf_path"], repository_root)
            item.update({
                "problem_id": str(source["id"]), "section_no": source["section_no"],
                "problem_no": str(source["problem_no"] or ""), "sub_no": str(source["sub_no"] or ""),
                "ptype": source["ptype"] or "calc", "difficulty": source["difficulty"],
                "crop_image_path": source["crop_image_path"] or "", "source_page": source["source_page"],
                "textbook_name": source["textbook_name"] or "", "registered_pdf_path": source["pdf_path"] or "",
                "resolved_pdf_path": str(pdf) if pdf else "",
            })
            if crop:
                item.update({"disposition": "ready_for_teacher_review", "teacher_action": "可送入 VLM 候选暂存；教师必须对照裁切图确认后写回。"})
            elif pdf and source["source_page"] is not None:
                item.update({"disposition": "ready_for_pdf_page", "teacher_action": "先渲染已登记 PDF 页，再人工裁出本题原图；不可按乱码补题。"})
            else:
                item.update({"disposition": "needs_source_evidence", "teacher_action": "缺少可用裁切图或已登记 PDF 页；请提供裁切图或 IMA 原页授权。"})
        candidates.append(item)
    candidates.sort(key=lambda item: (item["disposition"] != "ready_for_teacher_review", item["risk"] != "high", item["question_id"]))
    counts = Counter(item["disposition"] for item in candidates)
    counts.update({"total": len(candidates), "high_risk": sum(item["risk"] == "high" for item in candidates)})
    return {"schema_version": 1, "mode": "read_only_evidence_plan", "image_root": str(image_root), "summary": dict(counts), "candidates": candidates,
            "next_step": "Only ready_for_teacher_review records may be sent to stage_image_review_candidates.py."}
