"""Permission-scoped teacher chat actions for the MathBench workspace."""

from datetime import datetime
import asyncio
import re
import os
import base64
import json

import httpx

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from .auth import audit, require_roles, teacher_id_for_scope
from .config import settings
from .db import connection


router = APIRouter(prefix="/api/agent", tags=["teacher-chat"])


class TeacherChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    class_id: int = Field(gt=0)


class ExactAssignmentIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    class_id: int = Field(gt=0)
    due_at: datetime
    items: list[dict] = Field(min_length=1, max_length=30)
class TeachingTaskConfirmIn(BaseModel):
    class_id: int = Field(gt=0)
    task_kind: str = Field(min_length=2, max_length=80)
    context_id: int | None = Field(default=None, gt=0)


_EXERCISE_RE = re.compile(
    r"(?:P|第\s*)(?P<page>\d+)\s*(?:页)?\s*[,，]?\s*(?:EX|练习|习题)\s*(?P<section>\d+\.\d+)\s*[:：]?\s*(?P<body>[^\n。；;]+)",
    re.IGNORECASE,
)


def _class_for_actor(conn, class_id: int, actor: dict):
    row = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
    if not row:
        raise HTTPException(404, "班级不存在")
    scope = teacher_id_for_scope(actor)
    if scope is not None and row["teacher_user_id"] != scope:
        raise HTTPException(403, "无权访问该班级")
    return row


def _instruction(text: str, number: str) -> str:
    match = re.search(rf"(?:第\s*)?{re.escape(number)}\s*[（(]([^）)]+)[）)]", text)
    if match:
        return match.group(1).strip()
    match = re.search(rf"第\s*{re.escape(number)}\s*题\s*([^，,。；;\n]+)", text)
    if match:
        return match.group(1).strip(" ：:")
    match = re.search(rf"(?<!\d){re.escape(number)}\s*(偶数|奇数|[（(][^）)]+[）)])", text)
    return match.group(1).strip(" （）()") if match else ""


def _requested_problem_numbers(text: str) -> list[str]:
    """Extract only explicit exercise numbers, including assignment-slide shorthand."""
    numbers: list[str] = []
    for raw in re.findall(r"第\s*((?:\d+\s*[、,，和及与]\s*)+\d+|\d+)\s*题", text):
        numbers.extend(re.findall(r"\d+", raw))
    for raw in re.findall(r"(?<!\d)((?:\d+\s*[、,，和及与]\s*)+\d+)\s*题", text):
        numbers.extend(re.findall(r"\d+", raw))
    # Slides often write “Ex1.5：1，2（铅直渐近线）”, omitting “第…题”.
    if not numbers:
        body = re.split(r"(?:截止|提交|上交|交作业)", text, maxsplit=1)[0]
        for token in re.split(r"[、,，和及与]", body):
            match = re.match(r"^\s*(?:第\s*)?(\d+)\s*(?:题)?(?:\s*[（(][^）)]*[）)])?\s*$", token)
            if match:
                numbers.append(match.group(1))
    return list(dict.fromkeys(numbers))

def _parse_references(message: str) -> list[dict]:
    """Split at every page + exercise marker; never leak numbers across sections."""
    marker = re.compile(
        r"(?:P|第\s*)(?P<page>\d+)\s*(?:页)?\s*[,，]?\s*(?:EX|练习|习题)\s*(?P<section>\d+\.\d+)\s*[:：]?",
        re.IGNORECASE,
    )
    matches = list(marker.finditer(message))
    items = []
    for index, match in enumerate(matches):
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(message)
        body = message[match.end():body_end]
        for number in _requested_problem_numbers(body):
            items.append({"page": int(match.group("page")), "section": match.group("section"),
                          "problem_no": number, "instruction": _instruction(body, number)})
    return items

_CHINESE_NUMBERS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_SIMPLE_EX_RE = re.compile(r"(?:EX|练习|习题)\s*(?P<section>\d+\.\d+)\s*(?:第\s*)?(?P<numbers>[零一二三四五六七八九十\d、,，]+)\s*题", re.I)


def _parse_simple_exercise_references(message: str) -> list[dict]:
    """Parse explicit exercise numbers even when page information is absent."""
    items = []
    for match in re.finditer(r"(?:EX|练习|习题)\s*(?P<section>\d+\.\d+)(?P<body>[^\n。；;]{0,160})", message, re.I):
        for number in _requested_problem_numbers(match.group("body")):
            items.append({"page": None, "section": match.group("section"),
                          "problem_no": number, "instruction": _instruction(message, number)})
    return items

def _due_hint(message: str) -> str | None:
    """Extract a teacher-stated deadline, retaining an explicit time when present."""
    leading = re.search(
        r"(?:截止(?:时间)?|提交(?:时间)?|上交(?:时间)?|交作业)(?:\s*(?:为|是))?\s*"
        r"(?:(20\d{2})\s*[年./-])?\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})(?:日|号)?"
        r"(?:\s*(\d{1,2})(?:(?::|点)(\d{1,2})?)?)?",
        message,
    )
    trailing = re.search(
        r"(?:(20\d{2})\s*[年./-])?\s*(\d{1,2})[月./-](\d{1,2})(?:日|号)?"
        r"(?:\s*(\d{1,2})(?:(?::|点)(\d{1,2})?)?)?[^。；;\n]{0,18}(?:交|提交|截止)",
        message,
    )
    match = leading or trailing
    if not match:
        return None
    year = int(match.group(1) or datetime.now().year)
    try:
        hour = int(match.group(4)) if match.group(4) is not None else 23
        minute = int(match.group(5)) if match.group(5) is not None else (0 if match.group(4) is not None else 59)
        return datetime(year, int(match.group(2)), int(match.group(3)), hour, minute).strftime('%Y-%m-%d %H:%M')
    except ValueError:
        return None


def _assignment_draft(message: str) -> dict:
    refs = _parse_references(message)
    if not refs:
        refs = _parse_simple_exercise_references(message)
    title_match = re.search(r"第\s*(?:\d+|[一二三四五六七八九十])\s*次作业(?:[-—]\s*\d+)?", message)
    title = title_match.group(0).replace(" ", "") if title_match else "对话作业草案"
    with connection() as conn:
        for item in refs:
            row = conn.execute("""SELECT id,content,source_page FROM questions
                WHERE chapter=? AND source_problem_no=? AND review_status='published'
                ORDER BY id LIMIT 1""", (item["section"], item["problem_no"])).fetchone()
            item["question_id"] = row["id"] if row else None
            item["content_preview"] = row["content"][:100] if row else "未在已发布题库中定位到原题"
            item["source_page"] = row["source_page"] if row else None
    return {"title": title, "items": refs, "matched_count": sum(x["question_id"] is not None for x in refs), "due_hint": _due_hint(message)}


_ASSIGNMENT_TITLE_RE = re.compile(r"第\s*(?:\d+|[一二三四五六七八九十])\s*次作业(?:[-—]\s*\d+)?")

def _assignment_drafts(message: str) -> list[dict]:
    """Split a publish message into independently reviewable assignment drafts."""
    titles = list(_ASSIGNMENT_TITLE_RE.finditer(message))
    if len(titles) < 2:
        return [_assignment_draft(message)]
    drafts = []
    for index, title_match in enumerate(titles):
        segment = message[title_match.start(): titles[index + 1].start() if index + 1 < len(titles) else len(message)]
        draft = _assignment_draft(segment)
        draft["title"] = title_match.group(0).replace(" ", "")
        drafts.append(draft)
    return drafts


def _goal_assignment_draft(message: str) -> dict | None:
    """Build a teacher-reviewable draft from an explicit chapter and goal.

    This selects only published bank questions; it never invents a problem.
    """
    section_match = re.search(r"(?:Ex\s*)?(\d+\.\d+)", message, re.I)
    if not section_match:
        return None
    section = section_match.group(1)
    topics = ("极限", "连续", "导数", "微分", "积分", "数列", "函数", "无穷小")
    topic = next((word for word in topics if word in message), "")
    with connection() as conn:
        rows = conn.execute("""SELECT id,content,source_page,source_problem_no,knowledge_points
            FROM questions WHERE chapter=? AND review_status='published'
            ORDER BY id LIMIT 150""", (section,)).fetchall()
    if topic:
        rows = [row for row in rows if topic in (row["knowledge_points"] or "") or topic in (row["content"] or "")]
    rows = rows[:5]
    title_match = re.search(r"第\s*(?:\d+|[一二三四五六七八九十])\s*次作业(?:[-—]\s*\d+)?", message)
    items = [{"page": row["source_page"], "section": section,
              "problem_no": str(row["source_problem_no"] or index + 1),
              "question_id": row["id"], "content_preview": row["content"][:100],
              "source_page": row["source_page"], "instruction": ""}
             for index, row in enumerate(rows)]
    return {"title": title_match.group(0).replace(" ", "") if title_match else "对话作业草案",
            "items": items, "matched_count": len(items), "due_hint": _due_hint(message),
            "selection_basis": f"已发布题库 · Ex{section}" + (f" · {topic}" if topic else "")}
def _latest_snapshot(class_id: int, actor: dict) -> dict:
    with connection() as conn:
        _class_for_actor(conn, class_id, actor)
        assignment = conn.execute("""SELECT * FROM assignments WHERE class_id=?
            ORDER BY due_at DESC, id DESC LIMIT 1""", (class_id,)).fetchone()
        if not assignment:
            return {"available": False, "message": "当前班级还没有作业。"}
        rows = conn.execute("""SELECT s.student_no,s.student_name,s.status,s.score,s.needs_review,
            s.submitted_at,j.status AS grading_status FROM submissions s
            LEFT JOIN grading_jobs j ON j.submission_id=s.id WHERE s.assignment_id=?
            ORDER BY s.submitted_at DESC""", (assignment["id"],)).fetchall()
    people = [dict(row) for row in rows]
    scores = [row["score"] for row in rows if row["score"] is not None]
    return {"available": True, "assignment": {key: assignment[key] for key in ("id", "title", "class_name", "due_at")},
            "people": people, "quality": {"submitted": len(rows), "graded": len(scores),
            "needs_review": sum(bool(row["needs_review"]) for row in rows),
            "average_score": round(sum(scores) / len(scores), 1) if scores else None}}



async def _recognize_uploaded_math_image(file: UploadFile, reference: str, endpoint: str) -> dict:
    """Turn one teacher-supplied source image into a review-only math candidate."""
    raw = await file.read()
    if not raw:
        raise HTTPException(422, f"图片“{file.filename or '未命名'}”为空")
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(413, f"图片“{file.filename or '未命名'}”超过 12 MB")
    content_type = (file.content_type or "").lower()
    if not content_type.startswith("image/"):
        raise HTTPException(422, "目前请上传 PNG、JPG 或 WebP 图片；PDF 请先导出为图片")
    try:
        timeout = httpx.Timeout(connect=5.0, read=100.0, write=20.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            headers = {"X-Internal-API-Key": os.environ.get("VLM_INTERNAL_API_KEY", "")} if os.environ.get("VLM_INTERNAL_API_KEY") else {}
            response = await client.post(endpoint + "/solve-from-image", headers=headers, json={
                "image_base64": base64.b64encode(raw).decode("ascii"),
                "section_no": "教师上传",
                "problem_no": reference or "待编号",
            })
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "题图识别超时；图片仍保留，请重试或减少本次图片数量") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(503, "题图识别服务暂不可用，请稍后重试") from exc


def _teacher_image_vlm_endpoints() -> list[str]:
    qwen_base = settings.qwen_grading_url.rsplit("/", 1)[0].rstrip("/")
    # These are independent GPU workers running the same Qwen vision model.
    # Use them only when the configured endpoint is the local 18080 worker.
    if re.search(r":18080$", qwen_base):
        return [re.sub(r":18080$", f":{port}", qwen_base) for port in (18080, 18081, 18082, 18083)]
    return [qwen_base]


async def _recognize_uploaded_math_images(files: list[UploadFile], reference: str, kind: str) -> list[dict]:
    if not files:
        return []
    if len(files) > 12:
        raise HTTPException(413, f"一次最多上传 12 张{kind}图")
    endpoints = _teacher_image_vlm_endpoints()
    tasks = [
        _recognize_uploaded_math_image(file, reference, endpoints[index % len(endpoints)])
        for index, file in enumerate(files)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ordered: list[dict] = []
    for index, result in enumerate(results, start=1):
        if isinstance(result, HTTPException):
            raise HTTPException(result.status_code, f"第 {index} 张{kind}图识别失败：{result.detail}") from result
        if isinstance(result, Exception):
            raise HTTPException(503, f"第 {index} 张{kind}图识别失败：识别服务暂不可用，请重试") from result
        ordered.append(result)
    return ordered


def _join_recognized_text(results: list[dict], *keys: str) -> str:
    """Join unique OCR/VLM fragments in upload order; never discard a continuation page."""
    parts: list[str] = []
    seen: set[str] = set()
    for result in results:
        for key in keys:
            text = str(result.get(key) or "").strip()
            normalized = re.sub(r"\s+", " ", text)
            if normalized and normalized not in seen:
                parts.append(text)
                seen.add(normalized)
                break
    return "\n\n".join(parts)


@router.post("/latex-preview")
async def uploaded_latex_preview(
    request: Request,
    class_id: int = Form(...),
    reference: str = Form(""),
    question_files: list[UploadFile] = File(...),
    answer_files: list[UploadFile] = File(default=[]),
):
    """Recognize an ordered set of pages for one problem and its answer.

    Pages are deliberately processed in the teacher's selected order and joined into
    one review-only candidate.  Nothing enters the question bank without confirmation.
    """
    actor = require_roles(request, {"admin", "teacher"})
    with connection() as conn:
        _class_for_actor(conn, class_id, actor)
    reference = reference.strip()[:80]
    questions_task = _recognize_uploaded_math_images(question_files, reference, "题目")
    answers_task = _recognize_uploaded_math_images(answer_files, reference, "答案")
    questions, answers = await asyncio.gather(questions_task, answers_task)
    stem = _join_recognized_text(questions, "problem_text")
    if len(stem) < 6:
        raise HTTPException(422, "没有识别到完整题干，请检查题目图顺序或上传更清晰的单题图片")
    answer_text = _join_recognized_text(answers, "std_answer", "problem_text") or _join_recognized_text(questions, "std_answer")
    solution = _join_recognized_text(questions + answers, "full_solution")
    confidences = [float(item.get("confidence") or 0) for item in questions]
    confidences.extend(float(item.get("confidence") or 1) for item in answers)
    confidence = min(confidences) if confidences else 0
    audit(actor, "teacher_chat.latex_preview", "class", class_id, None, {
        "reference": reference,
        "question_image_count": len(questions),
        "answer_image_count": len(answers),
    })
    return {
        "intent": "uploaded_latex_preview",
        "message": f"已按顺序合并识别 {len(questions)} 张题目图和 {len(answers)} 张答案图；请核对后再入库。",
        "preview": {
            "reference": reference or "教师上传题目",
            "content": stem,
            "answer": answer_text,
            "solution": solution,
            "confidence": round(confidence, 2),
            "needs_teacher_review": True,
        },
    }


class UploadedQuestionConfirmIn(BaseModel):
    class_id: int = Field(gt=0)
    reference: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=6, max_length=12000)
    answer: str = Field(max_length=12000, default="")
    solution: str = Field(max_length=24000, default="")


@router.post("/uploaded-questions/confirm", status_code=201)
def confirm_uploaded_question(payload: UploadedQuestionConfirmIn, request: Request):
    actor = require_roles(request, {"admin", "teacher"})
    chapter_match = re.search(r"(?:Ex\s*)?(\d+\.\d+)", payload.reference, re.I)
    problem_match = re.search(r"第\s*(\d+)\s*题", payload.reference)
    chapter = chapter_match.group(1) if chapter_match else "教师上传"
    problem_no = problem_match.group(1) if problem_match else payload.reference
    with connection() as conn:
        class_row = _class_for_actor(conn, payload.class_id, actor)
        existing = conn.execute("SELECT id FROM questions WHERE owner_teacher_id=? AND chapter=? AND source_problem_no=? AND content=? ORDER BY id DESC LIMIT 1", (actor["id"], chapter, problem_no, payload.content.strip())).fetchone()
        if existing:
            return {"id": existing["id"], "message": "这道题已在题库中，无需重复入库。"}
        question_type = "证明题" if "证明" in payload.content else "计算题"
        question_id = conn.execute("""INSERT INTO questions(content,chapter,difficulty,question_type,answer,rubric,source_page,review_status,owner_teacher_id,source_problem_no,source_evidence_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (payload.content.strip(), chapter, "基础", question_type, payload.answer.strip(), payload.solution.strip() or payload.answer.strip(), None, "published", actor["id"] if actor["role"] == "teacher" else None, problem_no, '{"source":"teacher_chat_qwen","teacher_confirmed":true}')).lastrowid
    audit(actor, "teacher_chat.uploaded_question_confirm", "question", question_id, class_row["teacher_user_id"], {"class_id": payload.class_id, "reference": payload.reference})
    return {"id": question_id, "message": "已由教师确认并写入题库，可用于当前作业草稿。"}
def _latest_assignment_status(class_id: int, actor: dict) -> dict:
    """Task-safe homework status: targeted roster, completion and review counts."""
    with connection() as conn:
        _class_for_actor(conn, class_id, actor)
        assignment = conn.execute("""SELECT * FROM assignments WHERE class_id=?
            ORDER BY CASE status WHEN 'published' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END, due_at DESC, id DESC LIMIT 1""", (class_id,)).fetchone()
        if not assignment:
            return {"available": False, "message": "当前班级还没有可分析的作业。"}
        explicit = conn.execute("SELECT COUNT(*) FROM assignment_recipients WHERE assignment_id=?", (assignment["id"],)).fetchone()[0]
        expected = explicit or conn.execute("SELECT COUNT(*) FROM students WHERE class_id=?", (class_id,)).fetchone()[0]
        submitted = conn.execute("SELECT COUNT(*) FROM submissions WHERE assignment_id=?", (assignment["id"],)).fetchone()[0]
        reviewed = conn.execute("SELECT COUNT(*) FROM submissions WHERE assignment_id=? AND needs_review=1", (assignment["id"],)).fetchone()[0]
        graded = conn.execute("SELECT COUNT(*) FROM submissions WHERE assignment_id=? AND status='graded'", (assignment["id"],)).fetchone()[0]
    return {"available": True, "assignment": {"id": assignment["id"], "title": assignment["title"], "due_at": assignment["due_at"], "status": assignment["status"]},
            "expected": expected, "submitted": submitted, "unsubmitted": max(0, expected-submitted),
            "needs_review": reviewed, "graded": graded,
            "selection_mode": "指定名单" if explicit else "全班名单"}


_SCOPE_SECTION_RE = re.compile(r"(?:Ex\s*)?(\d+\.\d+)", re.IGNORECASE)
_SCOPE_PAGE_RE = re.compile(r"(?:P\s*(\d+)|第\s*(\d+)\s*页)(?:\s*[-–—~至到]\s*(?:P\s*)?(\d+)\s*(?:页)?)?", re.IGNORECASE)


def _textbook_scope(scope: str) -> tuple[str | None, int | None, int | None]:
    section_match = _SCOPE_SECTION_RE.search(scope)
    page_match = _SCOPE_PAGE_RE.search(scope)
    section = section_match.group(1) if section_match else None
    if not page_match:
        return section, None, None
    first = int(page_match.group(1) or page_match.group(2))
    last = int(page_match.group(3) or first)
    return section, min(first, last), max(first, last)


def _textbook_task(scope_text: str, class_id: int, actor: dict) -> dict:
    """Build an evidence-only textbook summary for the scope explicitly named by a teacher."""
    requested = re.sub(r"\s+", " ", scope_text or "").strip()[:200]
    section, page_start, page_end = _textbook_scope(requested)
    with connection() as conn:
        _class_for_actor(conn, class_id, actor)
        teacher_scope = teacher_id_for_scope(actor)
        sql = "SELECT id,title,page_count FROM textbook_documents"
        args: list[object] = []
        if teacher_scope is not None:
            sql += " WHERE owner_teacher_id=?"
            args.append(teacher_scope)
        documents = [dict(row) for row in conn.execute(sql + " ORDER BY id DESC", args).fetchall()]

        # A document title named in the request takes precedence.  If there is just one
        # uploaded book, it is a safe implicit choice; otherwise never guess a book.
        normalized = requested.casefold()
        matched_docs = [doc for doc in documents if len(str(doc["title"] or "")) > 1 and str(doc["title"]).casefold() in normalized]
        if matched_docs:
            selected_docs = matched_docs
        elif len(documents) == 1:
            selected_docs = documents
        elif not documents:
            selected_docs = []
        else:
            choices = [str(doc["title"]) for doc in documents[:8]]
            return {"available": False, "scope_label": requested, "document_choices": choices,
                    "message": "检测到多本已上传教材。请在请求中写明教材名称，再补充章节号或页码；例如：‘教材理解：高等数学（上） Ex1.2’。"}

        selected_sections: list[dict] = []
        if selected_docs:
            placeholders = ",".join("?" for _ in selected_docs)
            section_sql = f"""SELECT s.id,s.section_no,s.title,s.page_start,s.page_end,d.title AS document_title
                FROM textbook_sections s JOIN textbook_documents d ON d.id=s.document_id
                WHERE s.status='approved' AND s.document_id IN ({placeholders})"""
            section_args: list[object] = [doc["id"] for doc in selected_docs]
            if section:
                section_sql += " AND s.section_no=?"
                section_args.append(section)
            elif page_start is not None:
                section_sql += " AND COALESCE(s.page_start,0)<=? AND COALESCE(s.page_end,d.page_count)>=?"
                section_args.extend([page_end, page_start])
            selected_sections = [dict(row) for row in conn.execute(section_sql + " ORDER BY s.page_start,s.id", section_args).fetchall()]

        # Earlier imported question banks may not yet have a textbook-document row.
        # Keep that validated source usable only when the teacher explicitly gives a section.
        section_numbers = list(dict.fromkeys([str(row["section_no"]) for row in selected_sections]))
        if section and section not in section_numbers:
            section_numbers.append(section)
        if not selected_sections and selected_docs and not section:
            return {"available": False, "scope_label": requested,
                    "document_titles": [doc["title"] for doc in selected_docs],
                    "message": "该教材范围内尚无已审核章节。请先在资料库确认章节，或改为明确的章节号 / 页码。"}
        if not section_numbers:
            return {"available": False, "scope_label": requested,
                    "message": "请补充要理解的章节号或页码，例如：‘高等数学（上） Ex1.2’或‘高等数学（上）P15–24’。"}

        placeholders = ",".join("?" for _ in section_numbers)
        rows = conn.execute(f"""SELECT source_problem_no, question_type, knowledge_points, content, chapter
            FROM questions WHERE chapter IN ({placeholders}) AND review_status='published'
            ORDER BY chapter,source_problem_no LIMIT 40""", section_numbers).fetchall()
        knowledge_rows = []
        if selected_sections:
            section_ids = [row["id"] for row in selected_sections]
            id_placeholders = ",".join("?" for _ in section_ids)
            knowledge_rows = conn.execute(f"""SELECT name FROM textbook_section_knowledge_points
                WHERE section_id IN ({id_placeholders}) AND status='approved' ORDER BY id""", section_ids).fetchall()

    if not rows and not selected_sections:
        reference = f"Ex{section}" if section else requested
        return {"available": False, "scope_label": requested,
                "message": f"未找到“{reference}”的已审核教材内容。可先在资料库确认章节或同步题目。"}
    points: list[str] = []
    for row in knowledge_rows:
        name = str(row["name"] or "").strip()
        if name and name not in points:
            points.append(name)
    for row in rows:
        for point in str(row["knowledge_points"] or "").split("、"):
            point = point.strip()
            if point and point not in points:
                points.append(point)
    types = sorted({str(row["question_type"] or "综合题") for row in rows})
    refs = [f"Ex{row['chapter']}第{row['source_problem_no']}题" for row in rows[:5] if row["source_problem_no"]]
    return {"available": True, "scope_label": requested,
            "section": section or "、".join(section_numbers),
            "document_titles": list(dict.fromkeys([str(doc["title"]) for doc in selected_docs])),
            "sections": [{key: row.get(key) for key in ("section_no", "title", "page_start", "page_end", "document_title")} for row in selected_sections],
            "question_count": len(rows), "knowledge_points": points[:12],
            "question_types": types, "sample_refs": refs}


def _error_patterns(class_id: int, actor: dict) -> dict:
    status = _latest_assignment_status(class_id, actor)
    if not status.get("available"):
        return status
    assignment_id = status["assignment"]["id"]
    with connection() as conn:
        rows = conn.execute("""SELECT ge.evidence_json FROM grading_experiences ge
            JOIN submissions s ON s.id=ge.submission_id WHERE s.assignment_id=?""", (assignment_id,)).fetchall()
        qmap = {row["id"]: (row["knowledge_points"] or row["chapter"] or "未标注知识点") for row in conn.execute("SELECT id,knowledge_points,chapter FROM questions")}
    buckets: dict[str, int] = {}
    for row in rows:
        evidence = json.loads(row["evidence_json"] or "{}")
        for item in evidence.get("results", []):
            if item.get("correct") is True and not item.get("needs_review"):
                continue
            point = qmap.get(int(item.get("question_id") or 0), "未标注知识点")
            buckets[point] = buckets.get(point, 0) + 1
    patterns = [{"knowledge_point": point, "count": count,
                 "suggestion": "先回看定义与关键条件，再完成一题同类基础练习。"}
                for point, count in sorted(buckets.items(), key=lambda item: item[1], reverse=True)[:5]]
    return {"available": True, "assignment": status["assignment"], "analyzed_count": len(rows), "patterns": patterns,
            "message": "基于教师已确认的批改证据聚合；不会把未确认的 AI 判断当作学生错误。"}


def _learning_suggestions(class_id: int, actor: dict) -> dict:
    diagnosis = _error_patterns(class_id, actor)
    if not diagnosis.get("available"):
        return diagnosis
    suggestions = []
    for item in diagnosis.get("patterns", []):
        suggestions.append({"knowledge_point": item["knowledge_point"], "reason": f"在已确认批改证据中出现 {item['count']} 次需关注情况",
                            "action": "下次课用 5 分钟回顾概念，再布置 1 道基础巩固与 1 道变式练习。"})
    if not suggestions:
        suggestions.append({"knowledge_point": "当前无明显集中错因", "reason": "已确认批改证据暂不足或整体稳定", "action": "继续收集 1 次作业数据后再生成针对性建议。"})
    return {"available": True, "assignment": diagnosis["assignment"], "suggestions": suggestions}


@router.post("/tasks/confirm")
def confirm_teaching_task(payload: TeachingTaskConfirmIn, request: Request):
    """Record a teacher acknowledgement before the UI opens a teaching workflow."""
    actor = require_roles(request, {"admin", "teacher"})
    with connection() as conn:
        _class_for_actor(conn, payload.class_id, actor)
    audit(actor, "teacher_agent.task_confirm", "class", payload.class_id, None,
          {"task_kind": payload.task_kind, "context_id": payload.context_id})
    return {"ok": True, "message": "已记录教师确认。"}


@router.post("/chat")
def teacher_chat(payload: TeacherChatIn, request: Request):
    actor = require_roles(request, {"admin", "teacher"})
    with connection() as conn:
        _class_for_actor(conn, payload.class_id, actor)
    message = payload.message.strip()
    explicit_exercise_request = bool(_EXERCISE_RE.search(message) or re.search(r"(?:EX|练习|习题)\s*\d+\.\d+", message, re.I))
    if explicit_exercise_request:
        drafts = _assignment_drafts(message)
        if len(drafts) > 1:
            total = sum(len(draft["items"]) for draft in drafts)
            return {"intent": "assignment_drafts", "message": f"已按发布消息拆分为 {len(drafts)} 份作业，共定位 {total} 道指定题目；请分别确认。", "drafts": drafts, "requires_teacher_confirmation": True}
        draft = drafts[0]
        missing = len(draft["items"]) - draft["matched_count"]
        note = f"；另有 {missing} 道未定位，不能自动生成" if missing else "；请确认后创建草稿"
        return {"intent": "assignment_draft", "message": f"已整理出 {len(draft['items'])} 道指定教材原题{note}。", "draft": draft, "requires_teacher_confirmation": True}
    if any(word in message for word in ("发布作业", "布置", "组题", "组一份", "出作业", "生成作业", "作业草稿", "练习草稿")):
        # Explicit numbering is authoritative. Never silently replace it with
        # a chapter-wide recommendation when parsing or matching is incomplete.
        if re.search(r"(?:EX|练习|习题)\s*\d+\.\d+.*?(?:第\s*)?\d+.*?题", message, re.I):
            return {"intent": "help", "message": "已识别到明确题号，但未能完整解析。请按“Ex1.3 第7题和第8题”重试；系统不会用本章节其他题自动补齐。"}
        draft = _goal_assignment_draft(message)
        if draft is None:
            return {"intent": "help", "message": "请在发布消息中写明页码、章节和题号，例如：‘第3次作业-01，P39 Ex1.5：1、2；10月9日交’。如果有两份作业，用分号继续写第二份；系统只使用你指定的题号。"}
        if not draft["items"]:
            return {"intent": "help", "message": "指定范围的已发布题库没有匹配题目。请换一个知识点，或先在题库审核并发布原题。"}
        return {"intent": "assignment_draft", "message": f"根据 {draft['selection_basis']} 选出 {len(draft['items'])} 道候选题；请核对后创建草稿。", "draft": draft, "requires_teacher_confirmation": True}
    if any(word in message for word in ("教材理解", "知识点", "本章", "章节理解", "章节内容", "教材重点", "理解", "重点")):
        requested_scope = re.sub(r"^\s*(?:请|帮我)?\s*(?:理解|教材理解|章节理解|教材重点)\s*[:：]?\s*", "", message).strip()
        if not requested_scope:
            return {"intent": "help", "message": "请指定教材与范围，例如：‘教材理解：高等数学（上） Ex1.2’或‘教材理解：高等数学（上）P15–24’。"}
        task = _textbook_task(requested_scope, payload.class_id, actor)
        summary = "已按你指定的教材范围整理已审核内容，等待教师确认后进入资料库。" if task.get("available") else "暂时无法定位指定教材范围，请根据提示补充教材名称、章节号或页码。"
        return {"intent": "textbook_understanding", "message": summary, "task": task, "requires_teacher_confirmation": True}
    if any(word in message for word in ("作业状态", "未交", "进度", "作业进展", "提交进度")):
        task = _latest_assignment_status(payload.class_id, actor)
        return {"intent": "assignment_status", "message": "已读取当前班级作业状态，等待教师确认后打开明细。", "task": task, "requires_teacher_confirmation": True}
    if any(word in message for word in ("错因", "错误原因", "错题诊断", "哪里错", "错误分析")):
        task = _error_patterns(payload.class_id, actor)
        return {"intent": "error_diagnosis", "message": "已按教师确认的批改证据归纳错因，等待教师确认后进入复核工作台。", "task": task, "requires_teacher_confirmation": True}
    if any(word in message for word in ("学情", "教学建议", "薄弱", "建议怎么教", "学习建议")):
        task = _learning_suggestions(payload.class_id, actor)
        return {"intent": "learning_suggestions", "message": "已生成基于已确认批改证据的学情建议，等待教师确认后打开报告。", "task": task, "requires_teacher_confirmation": True}
    snapshot = _latest_snapshot(payload.class_id, actor)
    if any(word in message for word in ("谁交", "提交名单", "交作业的人")):
        return {"intent": "submission_people", "message": snapshot["message"] if not snapshot["available"] else f"这是“{snapshot['assignment']['title']}”的提交名单。", "snapshot": snapshot, "requires_teacher_confirmation": True}
    if any(word in message for word in ("质量", "提交情况", "批改情况", "平均分")):
        return {"intent": "submission_quality", "message": snapshot["message"] if not snapshot["available"] else f"这是“{snapshot['assignment']['title']}”的提交与批改概览。", "snapshot": snapshot, "requires_teacher_confirmation": True}
    return {"intent": "help", "message": "我可以完成五类教学任务：教材理解（如‘理解 Ex1.2 重点’）、对话组题、作业状态、错因诊断、学情建议。每一步都先给你看依据，确认后才进入下一步。"}


@router.post("/assignments/exact", status_code=201)
def create_exact_assignment(payload: ExactAssignmentIn, request: Request):
    actor = require_roles(request, {"admin", "teacher"})
    ids = []
    for item in payload.items:
        try:
            question_id = int(item["question_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, "作业草案中存在无效题目") from exc
        if question_id in ids:
            raise HTTPException(422, "同一道题不能重复加入作业")
        ids.append(question_id)
    with connection() as conn:
        class_row = _class_for_actor(conn, payload.class_id, actor)
        roster_count = conn.execute("SELECT COUNT(*) FROM students WHERE class_id=?", (payload.class_id,)).fetchone()[0]
        if not roster_count:
            raise HTTPException(409, "请先为班级导入学生名单")
        marks = ",".join("?" for _ in ids)
        rows = conn.execute(f"SELECT id,chapter,content FROM questions WHERE id IN ({marks}) AND review_status='published'", ids).fetchall()
        if len(rows) != len(ids):
            raise HTTPException(409, "有题目尚未发布或已移除，请重新生成草案")
        base, remainder = divmod(80, len(ids))
        assignment_id = conn.execute("""INSERT INTO assignments(title,chapter,class_name,class_id,due_at,total_score,semester,status,score_policy,completion_points)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (payload.title.strip(), "、".join(sorted({r['chapter'] for r in rows})), class_row["name"], payload.class_id,
            payload.due_at.isoformat(), 100, class_row["semester"], "draft", "completion20_quality80_v1", 20)).lastrowid
        by_id = {row["id"]: row for row in rows}
        conn.executemany("INSERT INTO assignment_questions(assignment_id,question_id,sort_order,score,original_no) VALUES(?,?,?,?,?)", [
            (assignment_id, int(item["question_id"]), index + 1, base + (1 if index < remainder else 0), str(item.get("original_no") or index + 1))
            for index, item in enumerate(payload.items)])
        for item in payload.items:
            instruction = str(item.get("instruction") or "").strip()
            if instruction:
                question_id = int(item["question_id"])
                content = by_id[question_id]["content"].rstrip() + "\n\n【本次作业要求】" + instruction
                conn.execute("""INSERT INTO assignment_question_overrides(assignment_id,question_id,content,score,updated_at)
                    VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(assignment_id,question_id) DO UPDATE SET
                      content=excluded.content,score=excluded.score,updated_at=CURRENT_TIMESTAMP""",
                    (assignment_id, question_id, content, base + (1 if payload.items.index(item) < remainder else 0)))
    audit(actor, "assignment.chat_create", "assignment", assignment_id, class_row["teacher_user_id"], {"class_id": payload.class_id, "question_count": len(ids)})
    return {"id": assignment_id, "message": "已按确认的教材原题创建作业草稿，请在作业列表中审核后发布。"}
