from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Literal
import json
import re
import asyncio
import os
import csv
import sqlite3
import httpx


# --- LaTeX helpers for HTML rendering ----------------------------------------
# 8014 content_text often contains raw LaTeX without $ delimiters (e.g.
# \begin{cases}..., \frac{...}{...}).  Wrap them so MathJax can render them.
_LATEX_INLINE_RE = re.compile(
    r'\\(?!(?:begin|end)\{)[a-zA-Z]+.*?(?=[\u4e00-\u9fa5，。；：？！、;!]|\\begin|\$\$|$)',
    re.DOTALL,
)
_LATEX_ENV_RE = re.compile(r'\\begin\{[^}]+\}.*?\\end\{[^}]+\}', re.DOTALL)
_PRINT_PAGE_HEADER_RE = re.compile(r'(?m)^\s*\d{3}\s*第[一二三四五六七八九十]+章[^\n]*$')


def _wrap_latex_for_html(text: str) -> str:
    """Add $ / $$ delimiters around raw LaTeX fragments without double-wrapping."""
    # Answer-book imports occasionally retain a printed page header.  It is
    # useful provenance in the source record but must not appear in a student
    # worksheet.  This is display-only; the stored question is untouched.
    text = _PRINT_PAGE_HEADER_RE.sub('', text or '')
    placeholders: list[str] = []

    def _stash(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f'\x00MATH{len(placeholders) - 1}\x00'

    # 1. Preserve every supported existing delimiter.  In particular, do this
    # before searching for raw commands: otherwise ``\\( \\frac{1}{2} \\)``
    # becomes the invalid nested form ``\\( $\\frac{1}{2}$ \\)``.
    text = re.sub(r'\\\(.*?\\\)', _stash, text, flags=re.DOTALL)
    text = re.sub(r'\\\[.*?\\\]', _stash, text, flags=re.DOTALL)
    text = re.sub(r'\$\$.*?\$\$', _stash, text, flags=re.DOTALL)
    text = re.sub(r'(?<!\$)\$(?!\$)[^\$]*?\$(?!\$)', _stash, text, flags=re.DOTALL)

    # 2. Convert \begin{...} ... \end{...} environments to display math.
    def _wrap_env(m: re.Match) -> str:
        wrapped = f'\n$$\n{m.group(0)}\n$$\n'
        placeholders.append(wrapped)
        return f'\x00MATH{len(placeholders) - 1}\x00'

    text = _LATEX_ENV_RE.sub(_wrap_env, text)

    # 3. Wrap remaining inline LaTeX commands (but not \begin / \end).
    def _wrap_inline(m: re.Match) -> str:
        s = m.group(0).strip()
        return f'${s}$' if s else m.group(0)

    text = _LATEX_INLINE_RE.sub(_wrap_inline, text)

    # 4. Restore stashed math.
    for i, ph in enumerate(placeholders):
        text = text.replace(f'\x00MATH{i}\x00', ph)
    return text

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field

from .config import settings
from .db import connection, init_db, queue_due_grading as enqueue_due_grading
from .dify import run_workflow
from .knowledge_bridge import build_image_solve_candidate, evidence_status, list_evidence_sections, rescue_formula_from_crop, retrieve_section_problems
from .grading_pipeline import run_grading_job
from .mineru_staging import match_staged, stage_markdown
from .answer_matcher import answer_json_to_staged, canonical_section, match_section_questions
from .question_validation import validate_question, first_issue_message
from .assignment_pdf import (POINTS_PER_QUESTION, build_assignment_pdf,
                             display_problem_no, export_latex_source,
                             latex_document, strip_source_problem_prefix)
from .orchestrator import publish_homework
from .mineru_review import (
    approve_item as approve_mineru_item,
    create_session as create_mineru_session,
    get_session as get_mineru_session,
    list_audit_log as list_mineru_audit_log,
    list_items as list_mineru_items,
    list_pending_sync as list_mineru_pending_sync,
    mark_section_synced,
    reject_item as reject_mineru_item,
)
from .ai_stem_review import (
    approve_candidate as approve_ai_stem_candidate,
    list_candidates as list_ai_stem_candidates,
    reject_candidate as reject_ai_stem_candidate,
    store_pending_candidate,
)
from .question_bank_review import _looks_corrupt, _scan_looks_corrupt


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.prepare_dirs()
    init_db()
    yield


app = FastAPI(title="高数作业助手", version="0.1.0", lifespan=lifespan)


class QuestionIn(BaseModel):
    content: str = Field(min_length=3)
    chapter: str
    difficulty: Literal["基础", "提高", "综合"] = "基础"
    question_type: str = "计算题"
    answer: str = ""
    rubric: str = ""
    source_page: int | None = None


class QuestionUpdateIn(BaseModel):
    """Manual edit of an existing question.  All fields optional (PATCH-style);
    only the provided fields are written.  Intentionally NOT gated by
    validate_question — a teacher correcting OCR garble or raw LaTeX must be able
    to save text the auto-validator would otherwise flag."""

    content: str | None = None
    chapter: str | None = None
    difficulty: Literal["基础", "提高", "综合"] | None = None
    question_type: str | None = None
    answer: str | None = None
    rubric: str | None = None
    knowledge_points: str | None = None
    review_status: str | None = None
    sync_8014: bool = True  # when True, also push the edited fields back to the 8014 evidence DB


class AssignmentIn(BaseModel):
    title: str
    chapter: str
    class_id: int = Field(gt=0)
    due_at: datetime
    semester: str = ""
    question_count: int = Field(ge=1, le=30, default=6)
    basic_ratio: float = Field(ge=0, le=1, default=.5)
    advanced_ratio: float = Field(ge=0, le=1, default=.35)


class ReviewDecisionIn(BaseModel):
    score: float = Field(ge=0)
    feedback: str = ""


class AssignmentUpdateIn(BaseModel):
    title: str = Field(min_length=1)
    due_at: datetime


class ClassIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    semester: str = Field(default="", max_length=40)


class StudentListIn(BaseModel):
    students: list[dict]


class MineruStageIn(BaseModel):
    role: Literal["textbook", "answer_book"]
    name: str = Field(min_length=1, max_length=120)
    markdown: str = Field(min_length=10)


class MineruMatchIn(BaseModel):
    textbook: dict
    answer_book: dict


class MineruReviewStartIn(BaseModel):
    answer_book: dict


class AnswerMatchIn(BaseModel):
    answer_parse_result: dict
    threshold: float = Field(default=0.90, gt=0, le=1)
    create_review_on_block: bool = True


class MineruReviewDecisionIn(BaseModel):
    action: Literal["approve", "reject"]
    std_answer: str = ""
    full_solution: str = ""
    note: str = ""
    overwrite_verified: bool = False


def _difficulty_label(value: object) -> str:
    """Normalise 8014's numeric/text difficulty to the local paper levels."""
    try:
        number = int(value)  # 8014 currently uses 1 / 2 / 3.
        return "基础" if number <= 1 else "提高" if number == 2 else "综合"
    except (TypeError, ValueError):
        text = str(value or "")
        return text if text in {"基础", "提高", "综合"} else "提高"


def _parse_sections(value: str) -> list[str]:
    """Accept teacher-friendly multi-section input: 1.1, 1.2 / 1.3."""
    sections: list[str] = []
    for section in re.split(r"[，,、;；\s]+", value or ""):
        section = section.strip()
        if section and section not in sections:
            sections.append(section)
    if not sections:
        raise HTTPException(422, "请至少选择一个章节，例如 1.1、1.2")
    return sections


def _problem_text_issue(item: dict) -> str | None:
    """Reject OCR fragments before they can enter an assignment.

    Older 8014 imports may contain a long string which is nevertheless not a
    readable question (for example dozens of one-character OCR lines).  Length
    alone is therefore deliberately not a publication criterion.
    """
    content = (item.get("content_text") or "").strip()
    if len(content) < 10:
        return "题干缺失"
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    one_char_lines = sum(len(line) <= 1 for line in lines)
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", content))
    replacement_count = content.count("�")
    # Mathematical notation is allowed, but a question still needs natural
    # language context and must not be a vertical stream of OCR fragments.
    if replacement_count:
        return "题干含无法解码字符"
    if len(lines) >= 7 and one_char_lines / len(lines) >= 0.45:
        return "题干是公式碎片，需按原题图重识"
    if cjk_count < 3 and len(content) > 20:
        return "题干缺少可读文字，需按原题图重识"
    # These are not legitimate mathematical tokens.  They are the recurring
    # artefacts produced when the previous OCR confused radicals, infinity,
    # brackets and fractions with CJK/Latin glyphs.  Do not try to repair them
    # with regex: require the original image and a vision re-read instead.
    garbled_formula = re.compile(r"[←ζ]|(?:[一二三四五六七八九]oo)|(?:[JHVUR]{2,})|(?:[VJH][0-9])|(?:[一丨川]（)|例\s*\d")
    if garbled_formula.search(content):
        return "题干含数学 OCR 污染，需按原题图重识"
    return None


def _usable_problem(item: dict) -> bool:
    """Do not issue a paper containing a missing or garbled OCR result."""
    return _problem_text_issue(item) is None


async def _validate_or_rescue(item: dict) -> tuple[dict, bool]:
    """Apply the unified gate. Returns ``(usable_item_or_None, rescues_attempted)``.

    ``item`` may be mutated in place when an image rescue produces a usable text.

    Admission rule: a problem is usable for the local working cache when it is
    *readable and structurally sound* (``report["valid"]`` — no block/error
    severity issues).  OCR-sourced text rarely reaches ``decision == "pass"``
    because its source confidence (0.78) caps the score below ``PASS_THRESHOLD``
    (0.90); a ``review`` decision carrying only warnings is still perfectly
    usable here because the 8014 source answer is already teacher-verified and
    the generated paper is previewed by the teacher before distribution
    (设计一.1).  Items that are ``block``ed (unreadable) or carry structural
    errors are *not* admitted; they fall through to the Qwen vision rescue or
    remain unresolved for the teacher to fix at the source.
    """
    crop = (item.get("evidence") or {}).get("crop_image_path")
    report = validate_question(
        item.get("content_text") or "",
        source_type="ocr",
        source_confidence=0.78,
        crop_image_path=crop,
    )
    if report["valid"]:
        item = dict(item)
        item["evidence"] = {
            **item.get("evidence", {}),
            "validation": {
                "decision": report["decision"],
                "score": report["score"],
                "source_confidence": report["source_confidence"],
            },
        }
        return item, False
    if report["needs_formula_rescue"] and crop and not item.get("_rescued"):
        rescue = await rescue_formula_from_crop(crop)
        rescued_text = (rescue.get("candidate_text") or "").strip()
        if rescued_text:
            recheck = validate_question(
                rescued_text,
                source_type="pix2text",
                source_confidence=float(rescue.get("confidence", 0.45) or 0.45),
                crop_image_path=crop,
            )
            if recheck["valid"]:
                item = dict(item)
                item["content_text"] = rescued_text
                item["_rescued"] = True
                item["evidence"] = {
                    **item["evidence"],
                    "formula_rescue": rescue,
                    "validation": {"decision": recheck["decision"], "score": recheck["score"]},
                }
                return item, True
    return None, False


async def _sync_section_into_local_cache(section_no: str, limit: int = 80) -> dict:
    """Import a traceable working copy of one 8014 section without changing 8014."""
    packet = await retrieve_section_problems(section_no, limit)
    source_items = packet["items"]
    usable_items: list[dict] = []
    ai_candidate_count = 0
    pending_review_count = 0
    pending_review_items: list[dict] = []
    unresolved_count = 0
    unresolved_items: list[dict] = []
    # Per-item processing used to be a serial `for` loop; with VLM/Pix2Text in
    # the rescue path each problem can stall 5-10s, so a chapter of 80 items
    # easily blew past the 90s hard cap even after we made `publish_homework`
    # run its sections concurrently.  Run item handling through a small
    # semaphore so we fan out without flooding 8014.
    PER_ITEM_SEM = asyncio.Semaphore(6)
    PER_ITEM_TIMEOUT = 18  # seconds per problem (VLM 15 + headroom)

    async def _process_one(item: dict) -> str:
        """Return 'usable' | 'ai' | 'pending' | 'unresolved' to classify."""
        async with PER_ITEM_SEM:
            try:
                return await asyncio.wait_for(_handle_one_item(item), PER_ITEM_TIMEOUT)
            except asyncio.TimeoutError:
                return "timeout"
            except Exception:
                return "unresolved"

    async def _handle_one_item(item: dict) -> str:
        text_issue = _problem_text_issue(item)
        if not text_issue and (item.get("std_answer") or "").strip():
            usable, _ = await _validate_or_rescue(item)
            if usable is not None:
                usable_items.append(usable)
                return "usable"
        candidate = await build_image_solve_candidate(item)
        if candidate.get("status") == "eligible":
            item2 = dict(item)
            item2["content_text"] = candidate["problem_text"]
            item2["ptype"] = candidate["ptype"]
            item2["std_answer"] = candidate["std_answer"]
            item2["full_solution"] = candidate["full_solution"]
            item2["answer_status"] = "ai_candidate"
            item2["evidence"] = {**item2["evidence"], "ai_candidate": candidate}
            usable_items.append(item2)
            nonlocal ai_candidate_count; ai_candidate_count += 1
            return "ai"
        if candidate.get("status") == "pending_review":
            stored = store_pending_candidate(item, candidate)
            pending_review_count += 1
            pending_review_items.append({
                "candidate_id": stored.get("id"),
                "source_problem_id": str(item.get("source_problem_id") or ""),
                "problem_no": str(item.get("problem_no") or "?"),
                "difficulty": _difficulty_label(item.get("difficulty")),
                "ptype": str(item.get("ptype") or ""),
                "candidate_text": (candidate.get("problem_text") or "").strip(),
                "std_answer": str(candidate.get("std_answer") or ""),
                "confidence": candidate.get("confidence"),
                "reason": candidate.get("reason", ""),
                "has_source_image": bool(crop := (item.get("evidence") or {}).get("crop_image_path")),
                "next_action": "教师核对原题图后确认写回",
            })
            return "pending"
        text_issue_final = _problem_text_issue(item)
        unresolved_count += 1
        unresolved_items.append({
            "source_problem_id": str(item.get("source_problem_id") or ""),
            "problem_no": str(item.get("problem_no") or "?"),
            "ptype": str(item.get("ptype") or ""),
            "reason": text_issue_final or "缺少可用标准答案",
            "has_source_image": bool(crop := (item.get("evidence") or {}).get("crop_image_path")),
            "next_action": "系统将按原题图重识" if crop else "8014 缺少原题裁切图，需补绑定原题证据",
        })
        return "unresolved"

    if source_items:
        await asyncio.gather(*[_process_one(it) for it in source_items])
    if not usable_items:
        raise HTTPException(
            422,
            "该章节暂无可直接发布的可读题目；请先在 8014 修复题目截图或 OCR，避免向学生发出乱码题目。",
        )

    synced_ids: list[int] = []
    available_by_level = {"基础": 0, "提高": 0, "综合": 0}
    with connection() as conn:
        # Old local cache rows are never allowed to remain selectable after a
        # newer quality gate has found that their source text is unreadable.
        usable_source_ids = {str(item["source_problem_id"]) for item in usable_items}
        for item in source_items:
            source_id = str(item.get("source_problem_id") or "")
            if source_id and source_id not in usable_source_ids:
                conn.execute("UPDATE questions SET review_status='blocked' WHERE source_problem_id=?", (source_id,))
        for item in usable_items:
            source_id = str(item["source_problem_id"])
            evidence = json.dumps(item["evidence"], ensure_ascii=False)
            difficulty = _difficulty_label(item.get("difficulty"))
            available_by_level[difficulty] += 1
            values = (
                item["content_text"].strip(),
                section_no,
                difficulty,
                str(item.get("ptype") or "计算题"),
                str(item.get("std_answer") or ""),
                str(item.get("grading_steps") or item.get("full_solution") or ""),
                evidence,
                str(item.get("problem_no") or ""),
            )
            existing = conn.execute(
                "SELECT id FROM questions WHERE source_problem_id=?", (source_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE questions SET content=?, chapter=?, difficulty=?, question_type=?,
                       answer=?, rubric=?, source_evidence_json=?, source_problem_no=?, review_status='published'
                       WHERE id=?""",
                    (*values, existing["id"]),
                )
                synced_ids.append(existing["id"])
            else:
                cursor = conn.execute(
                    """INSERT INTO questions
                       (content,chapter,difficulty,question_type,answer,rubric,source_evidence_json,source_problem_id,source_problem_no)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (*values[:7], source_id, values[7]),
                )
                synced_ids.append(cursor.lastrowid)
    return {
        "section_no": section_no,
        "source_total": packet["total"],
        "synced_count": len(synced_ids),
        "skipped_unreadable": unresolved_count,
        "ai_candidate_count": ai_candidate_count,
        "pending_review_count": pending_review_count,
        "pending_review_items": pending_review_items,
        "unresolved_items": unresolved_items,
        "available_by_level": available_by_level,
        "question_ids": synced_ids,
    }


@app.get("/api/questions")
def list_questions(chapter: str | None = None):
    sql, args = "SELECT * FROM questions WHERE review_status='published'", []
    if chapter:
        sql += " AND chapter=?"; args.append(chapter)
    with connection() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC", args)]


@app.post("/api/mineru/stage")
def stage_mineru_document(payload: MineruStageIn):
    """Preview MinerU chapter/question extraction without database writes."""
    return stage_markdown(payload.markdown, payload.role, payload.name)


@app.post("/api/mineru/match")
def match_mineru_documents(payload: MineruMatchIn):
    """Match staged textbook and answer-book JSON; confirmation is a separate step."""
    if payload.textbook.get("document", {}).get("role") != "textbook":
        raise HTTPException(422, "textbook 必须是教材暂存 JSON")
    if payload.answer_book.get("document", {}).get("role") != "answer_book":
        raise HTTPException(422, "answer_book 必须是答案书暂存 JSON")
    return match_staged(payload.textbook, payload.answer_book)


@app.post("/api/mineru/answer-match")
async def match_parsed_answers(payload: AnswerMatchIn):
    """Match server_answer_parse JSON to 8014 and enforce the 90% publish gate."""
    try:
        answer_book = answer_json_to_staged(payload.answer_parse_result)
        section = canonical_section(payload.answer_parse_result.get("meta", {}).get("target_section"))
        evidence = await retrieve_section_problems(section, limit=100)
        result = match_section_questions(section, evidence.get("items", []), answer_book, payload.threshold)
        result["answer_book"] = answer_book
        if not result["publish_gate"]["can_publish"] and payload.create_review_on_block:
            result["review_session"] = await create_mineru_session(answer_book)
        return result
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "8014 题目查询超时") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, "8014 题目查询失败：" + exc.response.text[:180]) from exc


@app.post("/api/mineru/review-sessions", status_code=201)
async def create_mineru_review_session(payload: MineruReviewStartIn):
    """Create local-only review records from a staged answer-book JSON."""
    try:
        return await create_mineru_session(payload.answer_book)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/mineru/review-sessions/{session_id}")
def get_mineru_review_session(session_id: int):
    """Return session metadata plus a live summary of approved/rejected/pending counts."""
    try:
        return get_mineru_session(session_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/mineru/review-sessions/{session_id}/items")
def get_mineru_review_items(session_id: int):
    return list_mineru_items(session_id)


@app.post("/api/mineru/review-items/{item_id}")
async def decide_mineru_review_item(item_id: int, payload: MineruReviewDecisionIn):
    """Approve writes the verified answer back to 8014; reject records the decision locally.

    Approval follows an all-or-nothing contract: 8014 must accept the PUT before the
    local review item is marked approved, so a failed upstream write never leaves the
    local state inconsistent.
    """
    try:
        if payload.action == "reject":
            return reject_mineru_item(item_id, payload.note)
        return await approve_mineru_item(
            item_id,
            payload.std_answer,
            payload.full_solution,
            payload.note,
            payload.overwrite_verified,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        # 8014 timeout / write failure: local state was not changed, safe to retry.
        raise HTTPException(502, str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, "8014 写回失败：" + exc.response.text[:180]) from exc


@app.get("/api/mineru/review-sessions/{session_id}/audit")
def get_mineru_review_audit(session_id: int):
    """Audit trail for a review session (approve/reject actions and before/after values)."""
    return {"session_id": session_id, "entries": list_mineru_audit_log(session_id=session_id)}


@app.post("/api/mineru/review-sessions/{session_id}/sync")
async def sync_mineru_review_session(session_id: int):
    """Sync every section that has at least one approved item back into the local cache.

    This closes the upstream loop: answers verified through MinerU are now visible to
    the assignment/grading pipeline without a manual re-sync.
    """
    with connection() as conn:
        rows = conn.execute(
            """SELECT DISTINCT section_no FROM mineru_review_items
               WHERE session_id=? AND status='approved' AND section_no IS NOT NULL""",
            (session_id,),
        ).fetchall()
    sections = [row["section_no"] for row in rows]
    if not sections:
        raise HTTPException(422, "当前批次没有已确认项，无需同步")

    results = []
    errors = []
    async def _sync_one(section: str):
        import time as _t
        _t0 = _t.time()
        print(f"[publish_sync] START section={section}", flush=True)
        try:
            result = await _sync_section_into_local_cache(section, limit=80)
            mark_section_synced(section)
            print(f"[publish_sync] DONE section={section} in {_t.time()-_t0:.1f}s", flush=True)
            return (section, result, None)
        except HTTPException as exc:
            print(f"[publish_sync] FAIL section={section} in {_t.time()-_t0:.1f}s", flush=True)
            return (section, None, exc.detail)
        except Exception as exc:  # noqa: BLE001
            print(f"[publish_sync] FAIL section={section} in {_t.time()-_t0:.1f}s: {str(exc)[:120]}", flush=True)
            return (section, None, str(exc)[:200])
    pairs = await asyncio.gather(*[_sync_one(s) for s in sections])
    for section, result, err in pairs:
        if err is not None:
            errors.append({"section_no": section, "error": err})
        else:
            results.append({"section_no": section, "sync": result})

    return {
        "session_id": session_id,
        "sections_synced": len(results),
        "sections": [r["section_no"] for r in results],
        "results": results,
        "errors": errors,
    }


@app.get("/api/mineru/pending-sync")
def get_mineru_pending_sync():
    """Sections that have been written back to 8014 and should be re-synced soon."""
    return {"items": list_mineru_pending_sync()}


# ---------------------------------------------------------------------------
# VLM-recognised stems awaiting teacher review (Route 1: OCR-completion loop)
# ---------------------------------------------------------------------------
class AiStemDecisionIn(BaseModel):
    content_text: str = ""
    std_answer: str = ""
    full_solution: str = ""
    note: str = ""


@app.get("/api/agent/ai-stem-candidates")
def list_ai_stem_candidates_endpoint(status: str | None = "pending", section_no: str | None = None):
    """Teacher queue of VLM-recognised stems that need confirmation before publication."""
    return {"items": list_ai_stem_candidates(status=status, section_no=section_no)}


@app.post("/api/agent/ai-stem-candidates/{candidate_id}/approve")
async def approve_ai_stem_endpoint(candidate_id: int, payload: AiStemDecisionIn):
    """Confirm a recognised stem: write content_text back to 8014 + local cache."""
    try:
        return await approve_ai_stem_candidate(
            candidate_id, payload.content_text, payload.std_answer, payload.full_solution, payload.note
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/agent/ai-stem-candidates/{candidate_id}/reject")
def reject_ai_stem_endpoint(candidate_id: int, payload: AiStemDecisionIn):
    """Reject a candidate; it is recorded locally but never published."""
    try:
        return reject_ai_stem_candidate(candidate_id, payload.note)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/agent/ai-stem-candidates/approve-section/{section_no}")
async def approve_section_ai_stems(section_no: str):
    """Batch-approve every pending stem candidate for a section (uses VLM text as-is)."""
    candidates = list_ai_stem_candidates(status="pending", section_no=section_no)
    results: list[dict] = []
    errors: list[dict] = []
    for candidate in candidates:
        try:
            results.append(await approve_ai_stem_candidate(candidate["id"]))
        except Exception as exc:  # noqa: BLE001
            errors.append({"candidate_id": candidate["id"], "error": str(exc)[:200]})
    return {"approved": len(results), "errors": errors, "results": results}


@app.post("/api/mineru/pending-sync/{section_no}")
def mark_mineru_section_synced(section_no: str):
    """Mark a section as synced (used after a manual /api/agent/sync-section)."""
    return mark_section_synced(section_no)


@app.get("/answer-import-review", response_class=HTMLResponse)
def mineru_review_page():
    return FileResponse(Path(__file__).with_name("answer_import_review.html"))


@app.post("/api/questions", status_code=201)
def create_question(question: QuestionIn):
    report = validate_question(
        question.content,
        source_type="manual",
        source_confidence=None,
        crop_image_path=None,
    )
    if not report["publish_allowed"]:
        raise HTTPException(
            422,
            "题目未通过数学校验，不能发布：" + (first_issue_message(report) or "存在结构问题"),
        )
    with connection() as conn:
        cursor = conn.execute("""INSERT INTO questions(content,chapter,difficulty,question_type,answer,rubric,source_page,review_status)
          VALUES(?,?,?,?,?,?,?,?)""", (
            question.content, question.chapter, question.difficulty,
            question.question_type, question.answer, question.rubric,
            question.source_page, "published",
        ))
        return {"id": cursor.lastrowid, "message": "题目已入库，待教师审核后可参与组卷。"}


@app.get("/api/questions/{question_id}")
def get_question(question_id: int):
    """Return a single question by id (any review_status, not just published)."""
    with connection() as conn:
        row = conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"题目 {question_id} 不存在")
    return dict(row)


@app.put("/api/questions/{question_id}")
def update_question(question_id: int, payload: QuestionUpdateIn):
    """Manual edit of an existing question.  Provided fields are written; missing
    fields are left untouched.  Saving is NOT blocked by the math validator — we
    only surface a non-fatal warning when the new content looks suspicious."""
    with connection() as conn:
        row = conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"题目 {question_id} 不存在")
        fields = {}
        if payload.content is not None:
            fields["content"] = payload.content
        if payload.chapter is not None:
            fields["chapter"] = payload.chapter
        if payload.difficulty is not None:
            fields["difficulty"] = payload.difficulty
        if payload.question_type is not None:
            fields["question_type"] = payload.question_type
        if payload.answer is not None:
            fields["answer"] = payload.answer
        if payload.rubric is not None:
            fields["rubric"] = payload.rubric
        if payload.knowledge_points is not None:
            fields["knowledge_points"] = payload.knowledge_points
        if payload.review_status is not None:
            fields["review_status"] = payload.review_status
        if not fields:
            return {"id": question_id, "message": "无字段变更", "question": dict(row)}
        set_sql = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE questions SET {set_sql} WHERE id=?",
            list(fields.values()) + [question_id],
        )
        new_row = conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
    # Best-effort push the edited fields back to the 8014 evidence DB so the
    # source of truth stays consistent with the manual correction.
    sync_status = {"synced": False, "skipped": "未请求同步"}
    if payload.sync_8014:
        sync_status = _sync_to_8014(row["source_problem_id"], fields)
    # Non-fatal validation hint so the teacher knows if the text looks off.
    validation_warning = None
    try:
        rep = validate_question(
            new_row["content"], source_type="manual",
            source_confidence=None, crop_image_path=None,
        )
        if not rep["publish_allowed"]:
            validation_warning = first_issue_message(rep)
    except Exception:  # noqa: BLE001 - validator must never block the save
        validation_warning = None
    return {
        "id": question_id,
        "message": "题目已更新",
        "question": dict(new_row),
        "validation_warning": validation_warning,
        "sync_8014_status": sync_status,
    }


# Teacher-facing OCR-garble backlog.  The CSV is produced by the offline audit
# script (scan_garble_precise / fix_fullwidth_and_audit); this endpoint simply
# surfaces it so the edit panel can walk the backlog in priority order.
# Deployments can override the repository default with GARBLE_AUDIT_CSV.
# Degrades gracefully if absent.
_GARBLE_AUDIT_CSV = Path(settings.garble_audit_csv)
_LEGACY_OCR_MARKERS = re.compile(r"[锟�叫咱呗]")

# 8014 证据库（独立 SQLite）。本地 questions 通过 source_problem_id 指回它的
# problems.id；人工订正时若 sync_8014=True，则把对应字段写回这里，保持证据源一致。
# Deployments can override this repository default with WORKBENCH_DB_PATH.
_WORKBENCH_DB = Path(settings.workbench_db_path)


def _sync_to_8014(source_problem_id: object, fields: dict) -> dict:
    """Best-effort write of edited fields back to the 8014 evidence DB.

    Returns a status dict the endpoint surfaces to the teacher; NEVER raises, so a
    8014 hiccup can't break the local save that already committed.
    Maps: content->content_text, answer->std_answer, rubric->full_solution.
    Clears answer_invalid_reason when the answer/solution is fixed.
    """
    if not source_problem_id:
        return {"synced": False, "skipped": "本题无 source_problem_id，8014 中无对应记录"}
    if not _WORKBENCH_DB.exists():
        return {"synced": False, "skipped": "未找到 8014 资料库文件"}
    col_map = {"content": "content_text", "answer": "std_answer", "rubric": "full_solution"}
    updates = {tgt: fields[src] for src, tgt in col_map.items() if src in fields}
    if not updates:
        return {"synced": False, "skipped": "未提供可同步到 8014 的字段"}
    try:
        wb = sqlite3.connect(str(_WORKBENCH_DB), timeout=5)
        try:
            cur = wb.execute("SELECT id FROM problems WHERE id=?", (source_problem_id,)).fetchone()
            if not cur:
                return {"synced": False, "reason": f"8014 中找不到 id={source_problem_id} 的记录"}
            set_sql = ", ".join(f"{k}=?" for k in updates)
            if "std_answer" in updates or "full_solution" in updates:
                set_sql += ", answer_invalid_reason=NULL"
            wb.execute(
                f"UPDATE problems SET {set_sql} WHERE id=?",
                list(updates.values()) + [source_problem_id],
            )
            wb.commit()
        finally:
            wb.close()
        return {"synced": True, "problem_id": str(source_problem_id)}
    except Exception as e:  # noqa: BLE001 - must not block the local save
        return {"synced": False, "reason": f"写回 8014 失败：{e}"}



def _live_garble_reasons(question: dict) -> tuple[str, list[str]]:
    """Classify current local text without writing or changing publication.

    The historic CSV is only a snapshot. This deliberately conservative scan
    combines the existing full-width/ASCII-salad rules with recurring OCR glyph
    substitutions, so corrected questions drop out while suspicious text stays
    visible for a teacher to check against the source image.
    """
    reasons: list[str] = []
    high_risk = False
    for label, key in (("题干", "content"), ("答案", "answer"), ("评分参考", "rubric")):
        text = str(question.get(key) or "").strip()
        if not text:
            # A rubric is optional.  Its absence must not make an otherwise
            # usable question appear to be corrupted.
            if key != "rubric":
                reasons.append(f"{label}为空")
                high_risk = True
            continue
        if _looks_corrupt(text):
            reasons.append(f"{label}含全角/替换字符")
            high_risk = True
        if len(_LEGACY_OCR_MARKERS.findall(text)) >= 3:
            reasons.append(f"{label}含重复 OCR 代字")
            high_risk = True
        elif _scan_looks_corrupt(text):
            reasons.append(f"{label}含可疑公式或字母序列")
    return ("high" if high_risk else "medium"), reasons


def _live_garble_queue(review_status: str | None = None) -> list[dict]:
    with connection() as conn:
        sql = "SELECT id, content, answer, rubric, review_status FROM questions"
        args: list[str] = []
        if review_status:
            sql += " WHERE review_status=?"
            args.append(review_status)
        rows = [dict(row) for row in conn.execute(sql, args).fetchall()]
    items = []
    for row in rows:
        risk, reasons = _live_garble_reasons(row)
        if not reasons:
            continue
        items.append({
            "question_id": row["id"],
            "review_status": row["review_status"],
            "risk": risk,
            "garble_hint": "；".join(reasons),
            "preview": (row["content"] or "")[:80],
        })
    return sorted(items, key=lambda item: (item["risk"] != "high", item["question_id"]))


@app.get("/api/garble-queue")
def garble_queue(review_status: str | None = None, source: Literal["audit", "live"] = "audit"):
    """Return the OCR-audit queue, optionally using live local review status.

    The CSV is an immutable audit trail, so its status column can be stale after
    a teacher fixes an item. Filtering therefore consults ``questions`` rather
    than trusting the CSV snapshot.
    """
    if source == "live":
        items = _live_garble_queue(review_status)
        high = sum(item["risk"] == "high" for item in items)
        return {
            "items": items,
            "note": f"实时扫描：{len(items)} 道疑似问题（高风险 {high} 道）；仅供复核，不会自动修改状态",
        }
    if not _GARBLE_AUDIT_CSV.exists():
        return {"items": [], "note": "未找到 garble_audit.csv（离线审计脚本未运行）"}
    items = []
    with _GARBLE_AUDIT_CSV.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                qid = int(row.get("question_id") or row.get("id") or "")
            except ValueError:
                continue
            items.append({
                "question_id": qid,
                "assignments_using": int(row.get("assignments_using") or 0),
                "review_status": row.get("review_status") or "",
                "garble_hint": (row.get("garble_hint") or "")[:60],
                "preview": (row.get("content_preview") or row.get("preview") or "")[:80],
            })
    if review_status:
        ids = [item["question_id"] for item in items]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            with connection() as conn:
                rows = conn.execute(
                    f"SELECT id FROM questions WHERE review_status=? AND id IN ({placeholders})",
                    [review_status, *ids],
                ).fetchall()
            allowed_ids = {row["id"] for row in rows}
            items = [item for item in items if item["question_id"] in allowed_ids]
        else:
            items = []
    # priority: most-used assignments first
    items.sort(key=lambda x: -x["assignments_using"])
    label = f"状态为 {review_status} 的" if review_status else ""
    return {"items": items, "note": f"共 {len(items)} 道{label}待修订题目"}


def _require_class(conn: sqlite3.Connection, class_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT id,name,semester FROM classes WHERE id=?", (class_id,)).fetchone()
    if not row:
        raise HTTPException(404, "班级不存在；请先在“班级与名单”中创建班级")
    return row


def _require_roster(conn: sqlite3.Connection, class_id: int) -> None:
    if not conn.execute("SELECT 1 FROM students WHERE class_id=? LIMIT 1", (class_id,)).fetchone():
        raise HTTPException(422, "该班尚未导入学生名单，不能发布作业")


def _normalise_students(students: list[dict]) -> tuple[list[tuple[str, str]], int]:
    """Validate a roster without silently guessing identity columns."""
    clean: list[tuple[str, str]] = []
    invalid = 0
    seen: set[str] = set()
    for row in students:
        no = str(row.get("student_no", "")).strip()
        name = str(row.get("name", "")).strip()
        if not no or not name or not _STUDENT_NO_RE.fullmatch(no) or no in seen:
            invalid += 1
            continue
        seen.add(no)
        clean.append((no, name))
    return clean, invalid


def _parse_roster_file(filename: str, raw: bytes) -> list[dict]:
    """Read an explicit 学号/姓名 roster from CSV or XLSX; never infer names."""
    suffix = Path(filename or "").suffix.lower()
    rows: list[list[object]] = []
    if suffix == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise HTTPException(503, "服务器未安装 Excel 导入依赖 openpyxl，请改用 UTF-8 CSV") from exc
        try:
            book = load_workbook(BytesIO(raw), read_only=True, data_only=True)
            sheet = book.active
            rows = [list(row) for row in sheet.iter_rows(values_only=True)]
        except Exception as exc:
            raise HTTPException(422, "Excel 文件无法读取；请确认是 .xlsx 格式") from exc
    elif suffix == ".csv":
        text = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise HTTPException(422, "CSV 编码无法读取，请另存为 UTF-8 CSV")
        rows = list(csv.reader(text.splitlines()))
    else:
        raise HTTPException(422, "仅支持 .xlsx 或 .csv 名单文件")
    if not rows:
        raise HTTPException(422, "名单文件为空")
    headers = [str(v or "").strip().lower().replace(" ", "") for v in rows[0]]
    no_names = {"学号", "student_no", "studentno", "studentnumber"}
    name_names = {"姓名", "name", "student_name", "studentname"}
    try:
        no_index = next(i for i, h in enumerate(headers) if h in no_names)
        name_index = next(i for i, h in enumerate(headers) if h in name_names)
    except StopIteration as exc:
        raise HTTPException(422, "首行必须包含“学号”和“姓名”两列；其他列可保留") from exc
    return [
        {"student_no": str(row[no_index] or "").strip(), "name": str(row[name_index] or "").strip()}
        for row in rows[1:]
        if len(row) > max(no_index, name_index)
    ]


def _save_roster(class_id: int, students: list[dict]) -> dict:
    clean, invalid = _normalise_students(students)
    with connection() as conn:
        _require_class(conn, class_id)
        inserted = updated = 0
        for student_no, name in clean:
            existing = conn.execute(
                "SELECT id,name FROM students WHERE class_id=? AND student_no=?", (class_id, student_no)
            ).fetchone()
            if existing:
                if existing["name"] != name:
                    conn.execute("UPDATE students SET name=? WHERE id=?", (name, existing["id"]))
                    updated += 1
                continue
            conn.execute(
                "INSERT INTO students(class_id,student_no,name) VALUES(?,?,?)", (class_id, student_no, name)
            )
            inserted += 1
    return {"inserted": inserted, "updated": updated, "invalid_or_duplicate": invalid,
            "accepted": len(clean)}


@app.get("/api/classes")
def list_classes():
    with connection() as conn:
        rows = conn.execute(
            """SELECT c.id,c.name,c.semester,c.created_at,COUNT(DISTINCT st.id) AS student_count,
                      COUNT(DISTINCT a.id) AS assignment_count
               FROM classes c
               LEFT JOIN students st ON st.class_id=c.id
               LEFT JOIN assignments a ON a.class_id=c.id
               GROUP BY c.id ORDER BY c.semester DESC,c.name"""
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/classes", status_code=201)
def create_class(payload: ClassIn):
    name, semester = payload.name.strip(), payload.semester.strip()
    with connection() as conn:
        try:
            cur = conn.execute("INSERT INTO classes(name,semester) VALUES(?,?)", (name, semester))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "该学期已存在同名班级") from exc
    return {"id": cur.lastrowid, "name": name, "semester": semester}


@app.get("/api/classes/{class_id}/students")
def list_class_students(class_id: int):
    with connection() as conn:
        _require_class(conn, class_id)
        rows = conn.execute(
            "SELECT id,student_no,name,created_at FROM students WHERE class_id=? ORDER BY student_no", (class_id,)
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/classes/{class_id}/students/import")
def import_roster_json(class_id: int, payload: StudentListIn):
    return _save_roster(class_id, payload.students)


@app.post("/api/classes/{class_id}/students/import-file")
async def import_roster_file(class_id: int, file: UploadFile = File(...)):
    filename = file.filename or "名单"
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "名单文件不能超过 10 MB")
    return {"filename": filename, **_save_roster(class_id, _parse_roster_file(filename, raw))}


@app.post("/api/assignments", status_code=201)
async def create_assignment(payload: AssignmentIn):
    if payload.basic_ratio + payload.advanced_ratio > 1:
        raise HTTPException(422, "基础和提高比例之和不能超过 1")
    levels = (["基础"] * round(payload.question_count * payload.basic_ratio) +
              ["提高"] * round(payload.question_count * payload.advanced_ratio))
    levels += ["综合"] * (payload.question_count - len(levels))
    with connection() as conn:
        class_row = _require_class(conn, payload.class_id)
        _require_roster(conn, payload.class_id)
        picked, used = [], set()
        for level in levels:
            row = conn.execute("SELECT * FROM questions WHERE chapter=? AND difficulty=? AND review_status='published' AND id NOT IN ({}) ORDER BY RANDOM() LIMIT 1".format(",".join("?" * len(used)) if used else "0"), [payload.chapter, level, *used]).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM questions WHERE chapter=? AND review_status='published' AND id NOT IN ({}) ORDER BY RANDOM() LIMIT 1".format(",".join("?" * len(used)) if used else "0"), [payload.chapter, *used]).fetchone()
            if row: picked.append(dict(row)); used.add(row["id"])
        if not picked: raise HTTPException(404, "该章节暂无已发布题目")
        score = POINTS_PER_QUESTION
        semester = payload.semester.strip() or class_row["semester"]
        cur = conn.execute("INSERT INTO assignments(title,chapter,class_name,class_id,due_at,total_score,semester) VALUES(?,?,?,?,?,?,?)", (payload.title, payload.chapter, class_row["name"], payload.class_id, payload.due_at.isoformat(), score * len(picked), semester))
        assignment_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO assignment_questions(assignment_id,question_id,sort_order,score,original_no) VALUES(?,?,?,?,?)",
            [(assignment_id, q["id"], i + 1, score, str(i + 1)) for i, q in enumerate(picked)],
        )
    ai_note = await run_workflow({"task": "assignment_review", "chapter": payload.chapter, "question_ids": list(used)})
    return {"id": assignment_id, "questions": picked, "ai_review": ai_note}


@app.post("/api/agent/sync-section")
async def sync_section(section_no: str, limit: int = 80):
    """Teacher-visible operation: refresh a chapter's working cache from 8014."""
    sections = _parse_sections(section_no)
    safe_limit = max(1, min(limit, 80))
    results = await asyncio.gather(
        *[_sync_section_into_local_cache(s, safe_limit) for s in sections]
    )
    if len(results) == 1:
        return results[0]
    return {
        "section_no": "、".join(sections), "sections": results,
        "source_total": sum(item["source_total"] for item in results),
        "synced_count": sum(item["synced_count"] for item in results),
        "skipped_unreadable": sum(item["skipped_unreadable"] for item in results),
        "ai_candidate_count": sum(item["ai_candidate_count"] for item in results),
        "pending_review_count": sum(item["pending_review_count"] for item in results),
        "pending_review_items": [dict(item, section_no=result["section_no"]) for result in results for item in result["pending_review_items"]],
        "unresolved_items": [dict(item, section_no=result["section_no"]) for result in results for item in result["unresolved_items"]],
        "available_by_level": {level: sum(result["available_by_level"][level] for result in results) for level in ("基础", "提高", "综合")},
        "question_ids": [question_id for result in results for question_id in result["question_ids"]],
    }


@app.get("/api/agent/sections")
async def evidence_sections():
    return await list_evidence_sections()


@app.post("/api/agent/assignments", status_code=201)
async def create_evidence_backed_assignment(payload: AssignmentIn):
    """One-call publish: extract → stratify → assemble for the chosen section(s).

    Delegates the ingestion pipeline to the Agent Orchestrator and then runs the
    existing Dify AI-review workflow over the selected source problems.
    """
    # Top-level hard cap so a stuck external dependency (VLM / Pix2Text / Dify)
    # never freezes the teacher's browser.  When we trip the cap we tell the
    # teacher the truth: the cache could not refresh in time, but they can
    # either retry or open the draft directly from the local cache without
    # waiting on the slow path.
    ASSIGNMENT_DRAFT_TIMEOUT = float(os.environ.get("ASSIGNMENT_DRAFT_TIMEOUT", "90"))
    with connection() as conn:
        class_row = _require_class(conn, payload.class_id)
        _require_roster(conn, payload.class_id)
    try:
        pipeline = await asyncio.wait_for(
            publish_homework(
                sections=_parse_sections(payload.chapter),
                title=payload.title,
                class_id=payload.class_id,
                class_name=class_row["name"],
                due_at=payload.due_at,
                question_count=payload.question_count,
                basic_ratio=payload.basic_ratio,
                advanced_ratio=payload.advanced_ratio,
            ),
            timeout=ASSIGNMENT_DRAFT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            504,
            "题库同步暂未响应（外部服务较慢），请稍候再试；如反复失败可先在『同步本章节』单独同步后再生成作业。",
        ) from None
    ai_note = await run_workflow({
        "task": "assignment_review",
        "chapter": payload.chapter,
        "source_problem_ids": pipeline["source_problem_ids"],
    })
    return {
        "id": pipeline["assignment_id"],
        "actual_composition": pipeline["composition"],
        "knowledge_sync": {
            "section_no": "、".join(pipeline["sections"]),
            "synced_count": sum(s["synced_count"] for s in pipeline["sync"]),
            "available_by_level": {
                level: sum(s["available_by_level"][level] for s in pipeline["sync"])
                for level in ("基础", "提高", "综合")
            },
            "unresolved_items": [u for s in pipeline["sync"] for u in s["unresolved_items"]],
        },
        "pdf_path": pipeline["pdf_path"],
        "page_count": pipeline["page_count"],
        "problems": pipeline["problems"],
        "ai_review": ai_note,
    }


@app.post("/api/agent/pipeline/publish", status_code=201)
async def pipeline_publish(payload: AssignmentIn):
    """Full pipeline surface: returns sync + stratify + assemble diagnostics."""
    with connection() as conn:
        class_row = _require_class(conn, payload.class_id)
        _require_roster(conn, payload.class_id)
    return await publish_homework(
        sections=_parse_sections(payload.chapter),
        title=payload.title,
        class_id=payload.class_id,
        class_name=class_row["name"],
        due_at=payload.due_at,
        question_count=payload.question_count,
        basic_ratio=payload.basic_ratio,
        advanced_ratio=payload.advanced_ratio,
    )


@app.get("/api/assignments")
def list_assignments(class_name: str | None = None, include_legacy: bool = False):
    sql, args = "SELECT * FROM assignments", []
    clauses = [] if include_legacy else ["class_id IS NOT NULL"]
    if class_name:
        clauses.append("class_name=?")
        args.append(class_name)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    with connection() as conn: return [dict(r) for r in conn.execute(sql + " ORDER BY due_at DESC", args)]


@app.patch("/api/assignments/{assignment_id}")
def update_assignment(assignment_id: int, payload: AssignmentUpdateIn):
    with connection() as conn:
        changed = conn.execute(
            "UPDATE assignments SET title=?, due_at=? WHERE id=?",
            (payload.title.strip(), payload.due_at.isoformat(), assignment_id),
        ).rowcount
    if not changed:
        raise HTTPException(404, "作业不存在")
    return {"ok": True, "message": "作业信息已更新"}


@app.delete("/api/assignments/{assignment_id}")
def delete_assignment(assignment_id: int):
    with connection() as conn:
        assignment = conn.execute("SELECT id FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        if not assignment:
            raise HTTPException(404, "作业不存在")
        submission_count = conn.execute("SELECT COUNT(*) FROM submissions WHERE assignment_id=?", (assignment_id,)).fetchone()[0]
        if submission_count:
            raise HTTPException(409, "该作业已有学生提交，不能删除；请保留评分证据。")
        conn.execute("DELETE FROM assignment_questions WHERE assignment_id=?", (assignment_id,))
        conn.execute("DELETE FROM assignments WHERE id=?", (assignment_id,))
    return {"ok": True, "message": "作业已删除"}


@app.get("/api/assignments/{assignment_id}/print", response_class=HTMLResponse)
def printable_assignment(assignment_id: int):
    with connection() as conn:
        assignment = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        if not assignment: raise HTTPException(404, "作业不存在")
        rows = conn.execute("""SELECT q.*, aq.sort_order, aq.score, aq.original_no FROM assignment_questions aq
          JOIN questions q ON q.id=aq.question_id WHERE aq.assignment_id=? ORDER BY aq.sort_order""", (assignment_id,)).fetchall()
    # Use assignment sequence numbers, and remove the source number from each
    # stem. Wrap raw LaTeX fragments first, then escape HTML so entities inside math
    # (e.g. >) are handled by the browser/MathJax correctly.
    items = "".join(
        f"<section><h3>{display_problem_no(dict(r), i)}.（{POINTS_PER_QUESTION}分）{escape(_wrap_latex_for_html(strip_source_problem_prefix(r['content'])))}</h3><div class='space'></div></section>"
        for i, r in enumerate(rows)
    )
    return f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>{escape(assignment['title'])}</title>
    <style>@page{{size:A4;margin:18mm}}body{{font-family:'Microsoft YaHei',sans-serif;color:#111;line-height:1.65}}header{{border-bottom:2px solid #1e3a5f}}h1{{text-align:center}}.meta{{display:flex;justify-content:space-between}}section{{break-inside:avoid;margin-top:20px}}.space{{height:115px;border-bottom:1px dashed #cbd5e1}}</style>
    <script>
    MathJax = {{
      tex: {{ inlineMath: [['$', '$'], ['\\\\(', '\\\\)']], displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']] }},
      svg: {{ fontCache: 'global' }}
    }};
    </script>
    <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
    <header><h1>{escape(assignment['title'])}</h1><div class='meta'><span>班级：{escape(assignment['class_name'])}</span><span>姓名：__________</span><span>学号：__________</span></div><p>章节：{escape(assignment['chapter'])}　截止：{escape(assignment['due_at'])}　总分：{assignment['total_score']}</p></header>{items}</html>"""


def _load_assignment_items(assignment_id: int) -> tuple[dict, list[dict]]:
    with connection() as conn:
        assignment = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        if not assignment:
            raise HTTPException(404, "作业不存在")
        rows = conn.execute("""SELECT q.content, q.question_type, q.source_problem_no, aq.sort_order, aq.score, aq.original_no
          FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id
          WHERE aq.assignment_id=? ORDER BY aq.sort_order""", (assignment_id,)).fetchall()
    return dict(assignment), [dict(r) for r in rows]


@app.get("/api/assignments/{assignment_id}/pdf")
def download_assignment_pdf(assignment_id: int):
    """Render the real printable A4 homework PDF (original numbers + answer blanks)."""
    assignment, items = _load_assignment_items(assignment_id)
    out_dir = Path(settings.upload_dir) / "assignments"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"assignment_{assignment_id}.pdf"
    try:
        path, _ = build_assignment_pdf(assignment, items, out_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"PDF 生成失败：{str(exc)[:220]}")
    return FileResponse(path, filename=f"作业_{assignment_id}.pdf", media_type="application/pdf")


@app.get("/api/assignments/{assignment_id}/latex")
def download_assignment_latex(assignment_id: int):
    """Return the LaTeX source of every selected problem as JSON (设计二：含 latex 源码)."""
    assignment, items = _load_assignment_items(assignment_id)
    return export_latex_source(assignment, items)


@app.get("/api/assignments/{assignment_id}/latex.tex")
def download_assignment_latex_tex(assignment_id: int):
    """Return a compilable .tex document wrapping the selected problems."""
    assignment, items = _load_assignment_items(assignment_id)
    out_dir = Path(settings.upload_dir) / "assignments"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"assignment_{assignment_id}.tex"
    out_path.write_text(latex_document(assignment, items), encoding="utf-8")
    return FileResponse(out_path, filename=f"作业_{assignment_id}.tex", media_type="application/x-tex")


@app.get("/submit", response_class=HTMLResponse)
def student_submit_page():
    """Shareable upload page; it coexists with the original student client."""
    return FileResponse(Path(__file__).with_name("student_submit.html"))


# 学生提交加固常量
_ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
_MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 30MB 上限
_STUDENT_NO_RE = re.compile(r"[A-Za-z0-9_]{1,32}")


@app.post("/api/assignments/{assignment_id}/submissions", status_code=201)
async def submit_homework(assignment_id: int, background_tasks: BackgroundTasks, student_no: str = Form(...), student_name: str = Form(""), file: UploadFile = File(...)):
    # 1) 学号格式与路径穿越防护：仅允许字母/数字/下划线，避免 folder = upload_dir/student_no 被注入
    student_no = (student_no or "").strip()
    if not _STUDENT_NO_RE.fullmatch(student_no):
        raise HTTPException(422, "学号格式不合法（仅允许字母、数字、下划线，最长 32 位）")
    student_name = (student_name or "").strip()[:40]

    # 2) 文件名与类型校验
    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(422, "文件名缺失")
    suffix = Path(filename).suffix.lower()
    if suffix not in _ALLOWED_EXT:
        raise HTTPException(415, "仅支持 PDF、图片或 Word 文档")

    # 3) 防重复提交：同一作业+学号若已有正在批改（queued/running）的任务则拒绝
    with connection() as conn:
        assignment = conn.execute("SELECT id,class_id FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        if not assignment:
            raise HTTPException(404, "作业不存在")
        if assignment["class_id"] is None:
            raise HTTPException(409, "这是历史演示作业，不能再接收提交；请从已建班级重新发布作业")
        enrolled = conn.execute(
            "SELECT name FROM students WHERE class_id=? AND student_no=?", (assignment["class_id"], student_no)
        ).fetchone()
        if not enrolled:
            raise HTTPException(403, "该学号不在本班名单中，请联系教师核对班级名单")
        if not student_name:
            student_name = enrolled["name"]
        if conn.execute(
            """SELECT 1 FROM submissions s JOIN grading_jobs j ON j.submission_id=s.id
               WHERE s.assignment_id=? AND s.student_no=? AND j.status IN ('queued','running') LIMIT 1""",
            (assignment_id, student_no),
        ).fetchone():
            raise HTTPException(409, "该作业已有正在批改的提交，请稍候或联系老师。")

        # 4) 落盘：流式写入并强制 30MB 上限，防止大文件撑爆磁盘/超时
        folder = Path(settings.upload_dir) / str(assignment_id) / student_no
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{Path(filename).name}"
        written = 0
        try:
            with path.open("wb") as buffer:
                while True:
                    chunk = file.file.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > _MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "文件过大（上限 30MB），请压缩后重试")
                    buffer.write(chunk)
        except HTTPException:
            path.unlink(missing_ok=True)
            raise
        except Exception:
            path.unlink(missing_ok=True)
            raise HTTPException(500, "文件保存失败，请重试")
        if written == 0:
            path.unlink(missing_ok=True)
            raise HTTPException(422, "空文件，请重新选择")

        # 始终存绝对路径，避免批改时依赖服务器进程 cwd（曾致相对路径提交无法定位原文件）。
        abs_path = path.resolve()
        cur = conn.execute(
            "INSERT INTO submissions(assignment_id,student_no,student_name,file_path,status) VALUES(?,?,?,?,?)",
            (assignment_id, student_no, student_name, str(abs_path), "submitted"),
        )
        submission_id = cur.lastrowid
        job_id = conn.execute("INSERT INTO grading_jobs(submission_id) VALUES(?)", (submission_id,)).lastrowid
    background_tasks.add_task(run_grading_job, job_id)
    return {"id": submission_id, "grading_job_id": job_id, "message": "提交成功，系统已归档并启动智能初评。"}


@app.post("/api/grading/run-due")
def queue_due_grading():
    now = datetime.now(timezone.utc).isoformat()
    queued = enqueue_due_grading(now)
    return {"queued": queued, "message": "已为截止作业创建批改任务；主观题将进入复核队列。"}


@app.get("/api/reviews")
def list_reviews():
    """Teacher queue: only evidence and candidates, never silently published marks."""
    with connection() as conn:
        rows = conn.execute(
            """SELECT s.id, s.assignment_id, s.student_no, s.student_name, s.status, s.score,
                      s.feedback, s.submitted_at, j.status AS grading_status, j.result_json, a.title
               FROM submissions s JOIN assignments a ON a.id=s.assignment_id
               LEFT JOIN grading_jobs j ON j.submission_id=s.id
               WHERE a.class_id IS NOT NULL AND (s.needs_review=1 OR j.status IN ('queued','running','failed'))
               ORDER BY s.submitted_at DESC"""
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        result = json.loads(item.pop("result_json") or "{}")
        item["review_count"] = sum(1 for r in result.get("results", []) if r.get("needs_review"))
        item["qwen_error"] = result.get("qwen_error", "")
        items.append(item)
    return items


@app.get("/api/submissions/{submission_id}/grading")
def grading_evidence(submission_id: int):
    with connection() as conn:
        row = conn.execute(
            """SELECT s.*, j.status AS grading_status, j.result_json, a.title
               FROM submissions s JOIN assignments a ON a.id=s.assignment_id
               LEFT JOIN grading_jobs j ON j.submission_id=s.id WHERE s.id=?""",
            (submission_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "提交不存在")
    payload = dict(row)
    payload["grading_result"] = json.loads(payload.pop("result_json") or "{}")
    payload["original_file_url"] = f"/api/submissions/{submission_id}/file"
    payload["original_file_name"] = Path(payload["file_path"]).name
    return payload


@app.get("/api/submissions/{submission_id}/file")
def original_submission_file(submission_id: int):
    """Serve only the original file belonging to this submission for review."""
    with connection() as conn:
        row = conn.execute("SELECT file_path FROM submissions WHERE id=?", (submission_id,)).fetchone()
    if not row:
        raise HTTPException(404, "提交不存在")
    path = Path(row["file_path"]).resolve()
    upload_root = Path(settings.upload_dir).resolve()
    if not path.is_file() or upload_root not in path.parents:
        raise HTTPException(404, "原作业文件不存在")
    return FileResponse(path, filename=path.name, content_disposition_type="inline")


@app.post("/api/submissions/{submission_id}/review")
def confirm_review(submission_id: int, decision: ReviewDecisionIn):
    """Only the teacher's decision becomes reusable experience."""
    with connection() as conn:
        submission = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if not submission:
            raise HTTPException(404, "提交不存在")
        job = conn.execute("SELECT result_json FROM grading_jobs WHERE submission_id=?", (submission_id,)).fetchone()
        evidence = (job["result_json"] if job else "") or "{}"
        conn.execute(
            "UPDATE submissions SET status='graded', score=?, feedback=?, needs_review=0 WHERE id=?",
            (decision.score, decision.feedback, submission_id),
        )
        conn.execute(
            """INSERT INTO grading_experiences
               (submission_id,assignment_id,confirmed_score,teacher_feedback,evidence_json)
               VALUES(?,?,?,?,?)""",
            (submission_id, submission["assignment_id"], decision.score, decision.feedback, evidence),
        )
    return {"ok": True, "message": "教师复核已确认，评分证据已沉淀为可复用经验。"}


REVIEW_QUOTA_PER_CLASS = 2  # 设计文档要求：每班不少于 2 次人工复核


@app.get("/api/reports/review-quota")
def review_quota():
    """人工复核配额：每个班级（按学期）已完成的教师复核次数；少于配额则提示。"""
    with connection() as conn:
        rows = conn.execute(
            """SELECT a.class_name, a.semester, COUNT(DISTINCT ge.id) AS reviewed
               FROM grading_experiences ge
               JOIN submissions s ON s.id=ge.submission_id
               JOIN assignments a ON a.id=ge.assignment_id
               JOIN classes c ON c.id=a.class_id
               GROUP BY a.class_name, a.semester"""
        ).fetchall()
    items = []
    for r in rows:
        reviewed = r["reviewed"]
        items.append({
            "class_name": r["class_name"], "semester": r["semester"],
            "teacher_reviews": reviewed, "quota": REVIEW_QUOTA_PER_CLASS,
            "meets_quota": reviewed >= REVIEW_QUOTA_PER_CLASS,
        })
    return {"quota_per_class": REVIEW_QUOTA_PER_CLASS, "classes": items,
            "classes_below_quota": [i for i in items if not i["meets_quota"]]}


@app.get("/api/reports/weak-points")
def weak_points(class_name: str | None = None, semester: str | None = None, top: int = 10):
    """薄弱知识点建议：按知识点（无标签时回退章节）聚合得分率，低于阈值给出补习建议。"""
    with connection() as conn:
        qmap = {row["id"]: (row["knowledge_points"] or row["chapter"])
                for row in conn.execute("SELECT id, knowledge_points, chapter FROM questions")}
        clauses, params = ["a.class_id IS NOT NULL"], []
        if class_name:
            clauses.append("a.class_name=?"); params.append(class_name)
        if semester:
            clauses.append("a.semester=?"); params.append(semester)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"""SELECT ge.evidence_json FROM grading_experiences ge
                JOIN submissions s ON s.id=ge.submission_id
                JOIN assignments a ON a.id=ge.assignment_id
                JOIN classes c ON c.id=a.class_id {where}""",
            params,
        ).fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        ev = json.loads(r["evidence_json"] or "{}")
        for item in ev.get("results", []):
            qid = int(item.get("question_id") or 0)
            kp = qmap.get(qid)
            if not kp:
                continue
            max_s = float(item.get("max_score") or 0) or 1.0
            sc = float(item.get("score") or 0)
            bucket = agg.setdefault(kp, {"kp": kp, "score_sum": 0.0, "max_sum": 0.0, "count": 0})
            bucket["score_sum"] += sc
            bucket["max_sum"] += max_s
            bucket["count"] += 1
    points = []
    for b in agg.values():
        rate = round(b["score_sum"] / b["max_sum"], 3) if b["max_sum"] else 0
        points.append({"knowledge_point": b["kp"], "avg_rate": rate,
                       "sample_count": b["count"],
                       "suggestion": ("建议加强练习与错题重做" if rate < 0.7 else "掌握良好")})
    points.sort(key=lambda x: x["avg_rate"])
    return {"weak_points": points[:top], "threshold": 0.7}


@app.get("/api/reports/semester-summary")
def semester_summary(class_name: str | None = None, semester: str | None = None):
    """学期末分数汇总：按学生聚合总分/平均分/排名，含班级均分、分布与书写整洁度。"""
    with connection() as conn:
        clauses = ["s.status='graded'", "a.class_id IS NOT NULL"]
        params = []
        if class_name:
            clauses.append("a.class_name=?"); params.append(class_name)
        if semester:
            clauses.append("a.semester=?"); params.append(semester)
        where = " AND ".join(clauses)
        rows = conn.execute(
            f"""SELECT s.student_no, s.student_name, s.score, s.handwriting_score,
                       a.class_name, a.semester, a.total_score
                FROM submissions s JOIN assignments a ON a.id=s.assignment_id
                JOIN classes c ON c.id=a.class_id
                WHERE {where}""",
            params,
        ).fetchall()
    students: dict[str, dict] = {}
    for r in rows:
        no = r["student_no"]
        st = students.setdefault(no, {"student_no": no, "student_name": r["student_name"],
                                       "score_sum": 0.0, "max_sum": 0.0, "count": 0, "hw_scores": []})
        st["score_sum"] += float(r["score"] or 0)
        st["max_sum"] += float(r["total_score"] or 0)
        st["count"] += 1
        if r["handwriting_score"] is not None:
            st["hw_scores"].append(float(r["handwriting_score"]))
    for st in students.values():
        st["average"] = round(st["score_sum"] / st["count"], 1) if st["count"] else 0
        st["avg_rate"] = round(st["score_sum"] / st["max_sum"], 3) if st["max_sum"] else 0
        st["handwriting_avg"] = round(sum(st["hw_scores"]) / len(st["hw_scores"]), 1) if st["hw_scores"] else None
        st.pop("hw_scores", None)
    ranked = sorted(students.values(), key=lambda x: x["average"], reverse=True)
    for i, st in enumerate(ranked, 1):
        st["rank"] = i
    avg_class = round(sum(s["average"] for s in ranked) / len(ranked), 1) if ranked else 0
    dist = {"优秀": 0, "良好": 0, "及格": 0, "不及格": 0}
    for s in ranked:
        a = s["average"]
        dist["优秀" if a >= 90 else "良好" if a >= 75 else "及格" if a >= 60 else "不及格"] += 1
    return {"student_count": len(ranked), "class_average": avg_class,
            "distribution": dist, "students": ranked}


@app.get("/api/reports/summary")
async def summary():
    with connection() as conn:
        local = dict(conn.execute("""SELECT (SELECT COUNT(*) FROM questions) question_count,
          (SELECT COUNT(*) FROM assignments WHERE status='published' AND class_id IS NOT NULL) assignment_count,
          (SELECT COUNT(*) FROM submissions s JOIN assignments a ON a.id=s.assignment_id WHERE a.class_id IS NOT NULL) submission_count,
          (SELECT COUNT(*) FROM grading_jobs WHERE status='queued') review_queue""").fetchone())
        hw = conn.execute(
            """SELECT AVG(s.handwriting_score) FROM submissions s
               JOIN assignments a ON a.id=s.assignment_id
               WHERE a.class_id IS NOT NULL AND s.handwriting_score IS NOT NULL"""
        ).fetchone()[0]
        below = conn.execute(
            """SELECT COUNT(*) FROM (SELECT a.class_name, a.semester
                FROM grading_experiences ge JOIN submissions s ON s.id=ge.submission_id
                JOIN assignments a ON a.id=ge.assignment_id JOIN classes c ON c.id=a.class_id GROUP BY a.class_name, a.semester
                HAVING COUNT(DISTINCT ge.id) < ?)""", (REVIEW_QUOTA_PER_CLASS,)).fetchone()[0]
    evidence = await evidence_status()
    return {
        **local,
        "knowledge_total": evidence["problem_count"] if evidence["connected"] else None,
        "synced_cache_count": local["question_count"],
        "handwriting_avg": round(hw, 1) if hw is not None else None,
        "classes_below_review_quota": below,
        "review_quota_per_class": REVIEW_QUOTA_PER_CLASS,
    }


@app.get("/api/agent/capabilities")
def agent_capabilities():
    """Capabilities exposed to the teacher-facing agent shell."""
    return {
        "agent": "高数教材智能体 v1",
        "tasks": [
            {"id": "assignment", "name": "智能生成作业", "status": "available",
             "description": "按章节、难度和题量从题库组卷，生成留白作业单。"},
            {"id": "publish", "name": "发布与收作业", "status": "available",
             "description": "发布给班级，保留学生照片、PDF 和手写文件提交入口。"},
            {"id": "grading", "name": "智能批改", "status": "review-first",
             "description": "按评分点初评；低置信度或主观题必须转教师复核。"},
            {"id": "material", "name": "教材资料库", "status": "backend",
             "description": "检索教材、答案 PDF 与教师确认经验，不要求先建完整答案库。"},
            {"id": "answer_import", "name": "MinerU 答案导入审核", "status": "available",
             "description": "审核 MinerU 解析的答案书，确认后写回 8014 并同步到本地缓存。"},
            {"id": "reports", "name": "教学报表", "status": "available",
             "description": "复核配额、薄弱知识点建议、学期末分数汇总与书写整洁度维度，辅助教学决策。"},
            {"id": "ai_stem", "name": "AI 题干候选复核", "status": "review-first",
             "description": "VLM 已识别但双模型校验未过的题干，教师勾选确认后写回 8014 并进入本地缓存。"},
        ],
        "evidence_policy": "模型结果仅作候选；涉及成绩发布时保留评分证据与教师复核入口。",
    }


@app.get("/api/agent/knowledge-status")
async def knowledge_status():
    """Expose the 8014 evidence library health to the teacher portal."""
    return await evidence_status()


@app.get("/api/agent/retrieve-section")
async def retrieve_section(section_no: str, limit: int = 30):
    """Evidence packet used by the assignment and grading agents."""
    if not section_no.strip():
        raise HTTPException(422, "请输入章节编号，例如 1.1")
    return await retrieve_section_problems(section_no.strip(), max(1, min(limit, 80)))


@app.get("/", response_class=HTMLResponse)
def teacher_agent_portal():
    return FileResponse(Path(__file__).with_name("teacher_portal.html"))


@app.get("/api-demo", response_class=HTMLResponse)
def dashboard():
    return """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>高数作业助手</title>
    <style>body{margin:0;background:#f5f7fb;font:16px 'Microsoft YaHei',sans-serif;color:#15243a}.hero{padding:56px max(6vw,32px);background:#102a43;color:white}.hero h1{font-size:38px;margin:0 0 12px}.hero p{color:#c7d7e8}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:18px;padding:32px max(6vw,32px)}.card{background:#fff;border-radius:14px;padding:22px;box-shadow:0 7px 25px #102a4311}.number{font-size:32px;color:#0b7285;font-weight:700}.hint{color:#62758a}.flow{margin:0 max(6vw,32px) 32px;background:#e6fffb;border-left:4px solid #0b7285;padding:20px;border-radius:8px}a{color:#0b7285}</style>
    <main><section class='hero'><h1>高数作业助手</h1><p>从章节题库到作业提交、批改复核的一体化工作台</p></section><section class='grid' id='metrics'></section><section class='flow'><b>当前可用：</b>示例题库已初始化。教师可通过 <a href='/docs'>接口文档</a> 录题、生成作业、打印作业单，并让学生上传提交件。Dify 与 OCR 通过环境变量接入。</section></main>
    <script>fetch('/api/reports/summary').then(r=>r.json()).then(x=>{let labels={question_count:'已发布题目',assignment_count:'进行中作业',submission_count:'已归档提交',review_queue:'待批改/复核'};metrics.innerHTML=Object.entries(labels).map(([k,v])=>`<article class=card><div class=number>${x[k]}</div><div class=hint>${v}</div></article>`).join('')})</script></html>"""
