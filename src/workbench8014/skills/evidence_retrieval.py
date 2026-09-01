"""从已核验题库读取可追溯元数据的 Evidence Retrieval Skill。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .registry import registry
from .schemas import EvidenceRecord, EvidenceRetrievalInput, EvidenceRetrievalResult


def _db_path() -> str:
    return os.environ.get("WORKBENCH_DB", str(Path(__file__).resolve().parents[1] / "api.db"))


@registry.register("evidence_retrieval", version="1.0.0", config={"source": "verified_question_bank"})
def evidence_retrieval(payload: EvidenceRetrievalInput) -> EvidenceRetrievalResult:
    """只返回已核验题的安全元数据，绝不返回标准答案或完整推导。"""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT s.section_no,p.problem_no,p.knowledge_pts,p.crop_image_path,
                      p.full_solution,p.answer_status,p.answer_invalid_reason,p.content_text
               FROM problems p JOIN sections s ON s.id=p.section_id WHERE p.id=?""",
            (payload.problem_id,),
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return EvidenceRetrievalResult(
            success=False, confidence=0.0, error_code="EVIDENCE_STORE_UNAVAILABLE",
            warnings=["题库证据服务暂不可用。"],
        )
    if not row:
        return EvidenceRetrievalResult(
            success=False, confidence=0.0, error_code="PROBLEM_NOT_FOUND",
            warnings=["题库中不存在该题。"],
        )
    usable = (
        row["answer_status"] == "verified"
        and not str(row["answer_invalid_reason"] or "").strip()
        and len(str(row["content_text"] or "").strip()) >= 12
    )
    if not usable:
        return EvidenceRetrievalResult(
            success=False, confidence=0.0, error_code="PROBLEM_NOT_VERIFIED",
            warnings=["该题尚未通过题干与标准答案质量核验，不能作为教学依据。"],
        )
    points = [x.strip() for x in str(row["knowledge_pts"] or "").replace("；", ",").split(",") if x.strip()]
    record = EvidenceRecord(
        section_no=str(row["section_no"] or ""),
        problem_no=str(row["problem_no"] or ""),
        knowledge_points=points,
        has_problem_image=bool(str(row["crop_image_path"] or "").strip()),
        has_full_solution=bool(str(row["full_solution"] or "").strip()),
        answer_status=str(row["answer_status"]),
    )
    return EvidenceRetrievalResult(
        success=True, confidence=1.0, record=record,
        evidence=[f"教材第 {record.section_no} 节，第 {record.problem_no} 题。"],
    )
