"""Agent Orchestrator — the coordinator behind the homework pipeline.

It wires the three textbook-ingestion tools

    extract_section.sync_section            (8014 → local cache, quality gated)
    stratify_difficulty.stratify_section    (基础/提高/综合 tiering)
    assemble_assignment.assemble            (3-2-1 select + 原号 + PDF)

into one teacher-facing operation, and then hands the resulting assignment to
the *existing* downstream chain: Dify AI review (``run_workflow``) and the
Qwen/SymPy grading pipeline (``run_grading_job`` / ``/api/grading/run-due``).

The orchestrator deliberately does not re-implement the quality gate, the
validation, or the grading logic — those live in their own modules. It only
sequences them and normalises the data passed between them.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .db import connection
from .education_document_tools.extract_section import sync_section
from .education_document_tools.stratify_difficulty import stratify_section_difficulty
from .education_document_tools.assemble_assignment import assemble


async def publish_homework(
    sections: list[str],
    *,
    title: str,
    class_name: str,
    due_at: datetime,
    question_count: int = 6,
    basic_ratio: float = 0.5,
    advanced_ratio: float = 0.35,
    build_pdf: bool = True,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """Run the full ingestion → assignment pipeline for one or more sections.

    Steps:
        1. extract  — pull readable, gated problems from 8014;
        2. stratify — assign / repair 基础/提高/综合 tiers;
        3. assemble — 3-2-1 selection, preserve original numbers, render PDF.

    Returns the assembly result enriched with the per-section sync/stratify
    diagnostics and the ``source_problem_ids`` needed by the downstream
    AI-review workflow.
    """
    step_sync: list[dict[str, Any]] = []
    for section in sections:
        step_sync.append(await sync_section(section))

    step_strat: list[dict[str, Any]] = []
    for section in sections:
        step_strat.append(await stratify_section_difficulty(section))

    result = assemble(
        sections,
        title=title,
        class_name=class_name,
        due_at=due_at,
        question_count=question_count,
        basic_ratio=basic_ratio,
        advanced_ratio=advanced_ratio,
        build_pdf=build_pdf,
        out_dir=out_dir,
    )

    # Resolve each selected question back to its 8014 source id so the
    # AI-review workflow (Dify) and the teacher audit can cite evidence.
    source_problem_ids: list[str] = []
    with connection() as conn:
        for qid in result["selected_ids"]:
            row = conn.execute(
                "SELECT source_problem_id FROM questions WHERE id=?", (qid,)
            ).fetchone()
            if row and row["source_problem_id"]:
                source_problem_ids.append(str(row["source_problem_id"]))

    result["source_problem_ids"] = source_problem_ids
    result["sync"] = step_sync
    result["stratify"] = step_strat
    return result


def grade_due_submissions() -> dict[str, Any]:
    """Trigger grading for every submission whose assignment is past due.

    Thin wrapper over ``db.queue_due_grading`` + the per-submission grading
    pipeline the service already runs (Qwen + SymPy, with teacher confirmation).
    The actual per-job execution is performed by the worker; this only enqueues
    and reports how many jobs were created.
    """
    from .db import queue_due_grading

    now = datetime.now().isoformat(timespec="seconds")
    queued = queue_due_grading(now)
    return {"now": now, "queued_jobs": queued}
