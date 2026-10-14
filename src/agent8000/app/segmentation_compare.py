"""Teacher-only, read-only comparison of saved regions and the private locator.

No submission, grading job, teacher decision or grade is written here.
The Gateway receives only assignment question text and the existing page images.
"""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from PIL import Image

from .auth import require_roles, teacher_id_for_scope
from .config import settings
from .db import connection
from .grading_pipeline import _submission_page_paths
from .region_sets import expand_regions


router = APIRouter()
GATEWAY_URL = "http://127.0.0.1:18086/locate-homework"
MAX_TOTAL_IMAGE_BYTES = 17_000_000
LOCATOR_MAX_EDGE = 1700


def _locator_image_base64(page: Path) -> str:
    """Reduce visual-token load only for semantic location; keep originals intact."""
    with Image.open(page) as original:
        if max(original.size) <= LOCATOR_MAX_EDGE:
            raw = page.read_bytes()
        else:
            view = original.convert("RGB")
            view.thumbnail((LOCATOR_MAX_EDGE, LOCATOR_MAX_EDGE), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            view.save(buffer, format="PNG")
            raw = buffer.getvalue()
    return base64.b64encode(raw).decode("ascii")


def _region(raw: Any, page_count: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        page = int(raw["page_no"]) if "page_no" in raw else int(raw["page_index"]) + 1
        x, y, width, height = (float(raw[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return None
    if not (1 <= page <= page_count and 0 <= x < 1 and 0 <= y < 1
            and .015 < width <= 1 and .015 < height <= 1
            and x + width <= 1.001 and y + height <= 1.001):
        return None
    return {"page_no": page, "x": x, "y": y, "width": width, "height": height}


def _overlap(one: dict[str, Any], other: dict[str, Any]) -> float:
    if one["page_no"] != other["page_no"]:
        return 0.0
    dx = max(0.0, min(one["x"] + one["width"], other["x"] + other["width"]) - max(one["x"], other["x"]))
    dy = max(0.0, min(one["y"] + one["height"], other["y"] + other["height"]) - max(one["y"], other["y"]))
    smaller = min(one["width"] * one["height"], other["width"] * other["height"])
    return dx * dy / smaller if smaller else 0.0


def _intersection_area(one: dict[str, Any], other: dict[str, Any]) -> float:
    if one["page_no"] != other["page_no"]:
        return 0.0
    width = max(0.0, min(one["x"] + one["width"], other["x"] + other["width"])
                - max(one["x"], other["x"]))
    height = max(0.0, min(one["y"] + one["height"], other["y"] + other["height"])
                 - max(one["y"], other["y"]))
    return width * height


def _covered_area(teacher: dict[str, Any], candidates: list[dict[str, Any]]) -> float:
    """Area of the union inside a teacher box; duplicate proposals count once."""
    clipped = []
    for box in candidates:
        if box["page_no"] != teacher["page_no"]:
            continue
        x0, y0 = max(teacher["x"], box["x"]), max(teacher["y"], box["y"])
        x1 = min(teacher["x"] + teacher["width"], box["x"] + box["width"])
        y1 = min(teacher["y"] + teacher["height"], box["y"] + box["height"])
        if x1 > x0 and y1 > y0:
            clipped.append((x0, y0, x1, y1))
    edges = sorted({x for x0, _, x1, _ in clipped for x in (x0, x1)})
    area = 0.0
    for left, right in zip(edges, edges[1:]):
        intervals = sorted((y0, y1) for x0, y0, x1, y1 in clipped
                           if x0 < right and x1 > left)
        end = 0.0
        height = 0.0
        for y0, y1 in intervals:
            height += max(0.0, y1 - max(y0, end))
            end = max(end, y1)
        area += (right - left) * height
    return area


def compare_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Deterministic diagnostics; no box is accepted as a corrected answer."""
    totals = {"missing": 0, "overlap": 0, "disagreement": 0,
              "teacher_gap": 0, "review": 0}
    for row in rows:
        old, candidate = row["current_regions"], row["new_regions"]
        risks = row["risks"]
        if not candidate:
            risks.append("18086 未定位此题（漏题或空白，须结合原图确认）")
            totals["missing"] += 1
        if old and candidate and max((_overlap(a, b) for a in old for b in candidate), default=0) < .55:
            risks.append("现有题区与新候选明显不一致")
            totals["disagreement"] += 1
        teacher_gap = False
        coverage_values = []
        for teacher in row.get("teacher_regions") or []:
            teacher_area = teacher["width"] * teacher["height"]
            covered = _covered_area(teacher, candidate) / teacher_area if teacher_area else 0.0
            coverage_values.append(round(covered, 3))
            same_page = [box for box in candidate if box["page_no"] == teacher["page_no"]]
            oversized = any(box["width"] * box["height"] > teacher_area * 1.8
                            for box in same_page) if teacher_area else False
            # A teacher box is the strongest available boundary evidence.
            # Even one clipped handwritten line is unacceptable here.
            if covered < .95 or oversized:
                teacher_gap = True
        if teacher_gap:
            risks.append("新候选未充分覆盖教师确认区域，或明显越界；须核对完整推导")
            totals["teacher_gap"] += 1
        row["teacher_coverage"] = min(coverage_values) if coverage_values else None
        if row["new_needs_review"] or risks:
            row["needs_review"] = True
            totals["review"] += 1
        else:
            row["needs_review"] = False
    for index, first in enumerate(rows):
        for second in rows[index + 1:]:
            if first["question_id"] == second["question_id"]:
                continue
            if any(_overlap(a, b) > .18 for a in first["new_regions"] for b in second["new_regions"]):
                for row in (first, second):
                    if "相邻题新候选区域明显重叠" not in row["risks"]:
                        row["risks"].append("相邻题新候选区域明显重叠")
                    row["needs_review"] = True
                totals["overlap"] += 1
    totals["review"] = sum(bool(row["needs_review"]) for row in rows)
    return totals


@router.get("/teacher/segmentation-compare")
def comparison_page(request: Request):
    if not settings.auth_required:
        raise HTTPException(503, "只读诊断要求教师身份验证已启用")
    try:
        require_roles(request, {"admin", "teacher"})
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/login?next=/teacher/segmentation-compare", status_code=303)
        raise
    return FileResponse(Path(__file__).with_name("segmentation_compare.html"),
                        headers={"Cache-Control": "no-store, max-age=0"})


@router.get("/api/submissions/{submission_id}/segmentation-compare")
async def comparison_data(submission_id: int, request: Request):
    if not settings.auth_required:
        raise HTTPException(503, "只读诊断要求教师身份验证已启用")
    actor = require_roles(request, {"admin", "teacher"})
    with connection() as conn:
        submission = conn.execute(
            """SELECT s.id,s.file_path,s.assignment_id,c.teacher_user_id
               FROM submissions s JOIN assignments a ON a.id=s.assignment_id
               JOIN classes c ON c.id=a.class_id WHERE s.id=?""", (submission_id,)
        ).fetchone()
        if not submission or (teacher_id_for_scope(actor) is not None
                              and submission["teacher_user_id"] != teacher_id_for_scope(actor)):
            raise HTTPException(404, "提交不存在")
        questions = [dict(item) for item in conn.execute(
            """SELECT aq.question_id,aq.sort_order,aq.original_no,q.content
               FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id
               WHERE aq.assignment_id=? ORDER BY aq.sort_order""", (submission["assignment_id"],)
        ).fetchall()]
        parts = [dict(item) for item in conn.execute(
            """SELECT question_id,subpart_no,part_order,content FROM assignment_question_parts
               WHERE assignment_id=? ORDER BY question_id,part_order""", (submission["assignment_id"],)
        ).fetchall()]
        saved = [dict(item) for item in conn.execute(
            """SELECT question_id,subpart_no,sort_order,page_no,x,y,width,height,confirmed_at,regions_json
               FROM submission_question_regions WHERE submission_id=?""", (submission_id,)
        ).fetchall()]
        job = conn.execute("SELECT result_json FROM grading_jobs WHERE submission_id=?", (submission_id,)).fetchone()
    if not questions:
        raise HTTPException(409, "作业未关联题目")
    try:
        pages = _submission_page_paths(submission["file_path"])
    except (OSError, ValueError) as exc:
        raise HTTPException(409, "原作业页图暂不可用") from exc
    if not 1 <= len(pages) <= 12:
        raise HTTPException(422, "页数超出旁路诊断上限")
    page_sizes = [page.stat().st_size for page in pages]
    if sum(page_sizes) > MAX_TOTAL_IMAGE_BYTES:
        raise HTTPException(422, "图像超过旁路诊断上限；原提交未更改")

    by_parent: dict[int, list[dict[str, Any]]] = {}
    for part in parts:
        by_parent.setdefault(int(part["question_id"]), []).append(part)
    result = json.loads(job["result_json"] or "{}") if job else {}
    old_results = {(int(item.get("question_id") or 0), str(item.get("subpart_no") or ""),
                    int(item.get("sort_order") or 0)): item
                   for item in result.get("results", []) if isinstance(item, dict)}
    saved_by_key = {(int(item["question_id"]), str(item.get("subpart_no") or ""),
                     int(item["sort_order"])): item for item in saved}
    rows: list[dict[str, Any]] = []
    for parent in questions:
        qid, order = int(parent["question_id"]), int(parent["sort_order"])
        selected = by_parent.get(qid) or [None]
        for part in selected:
            subpart = str(part.get("subpart_no") or "") if part else ""
            key = (qid, subpart, order)
            old = old_results.get(key, {})
            manual = _region(saved_by_key.get(key), len(pages))
            prior = _region(old.get("teacher_page_mapping"), len(pages))
            suggested = _region((old.get("qwen") or {}).get("suggested_region"), len(pages))
            current = manual or prior or suggested
            manual_regions = [_region(r, len(pages)) for r in expand_regions(saved_by_key[key])] if key in saved_by_key else []
            manual_regions = [r for r in manual_regions if r]
            prior_mapping = old.get('teacher_page_mapping') or {}
            prior_regions = [_region(r, len(pages)) for r in expand_regions(prior_mapping)] if prior_mapping else []
            prior_regions = [r for r in prior_regions if r]
            source = "教师确认" if manual else ("已有批改区域" if prior else ("旧模型建议" if suggested else "未定位"))
            rows.append({"problem_id": f"{qid}:{subpart}" if subpart else str(qid),
                         "question_id": qid, "sort_order": order, "subpart_no": subpart,
                         "problem_no": f"{parent['original_no'] or order}（{subpart}）" if subpart
                         else str(parent["original_no"] or order),
                         "problem_text": str(part["content"] if part else parent["content"] or "")[:1200],
                         "current_regions": manual_regions or prior_regions or ([current] if current else []), "current_source": source,
                         "teacher_regions": manual_regions, "new_regions": [],
                         "new_raw_regions": [], "new_proposed_regions": [], "new_needs_review": True,
                         "risks": [], "needs_review": True})
    if len(rows) > 30:
        raise HTTPException(422, "题目数量超出旁路诊断上限")
    # The browser and any later crop still use the untouched original page.
    # Locator coordinates are normalized, so uniform resizing preserves them.
    images = [_locator_image_base64(page) for page in pages]
    gateway_error = None
    try:
        async with httpx.AsyncClient(timeout=95) as client:
            response = await client.post(GATEWAY_URL, json={
                "images_base64": images,
                "problems": [{key: row[key] for key in ("problem_id", "problem_no", "problem_text")}
                             for row in rows],
            })
            response.raise_for_status()
            payload = response.json()
        new_by_id = {str(item.get("problem_id")): item for item in payload.get("mapping", [])
                     if isinstance(item, dict)}
        for row in rows:
            item = new_by_id.get(row["problem_id"], {})
            row["new_regions"] = [box for raw in (item.get("answer_regions") or [])
                                  if (box := _region(raw, len(pages)))]
            row["new_raw_regions"] = item.get("raw_answer_regions") or []
            row["new_proposed_regions"] = [box for raw in (item.get("proposed_regions") or [])
                                           if (box := _region(raw, len(pages)))]
            row["new_needs_review"] = bool(item.get("needs_review", True))
            row["risks"] = [str(risk)[:180] for risk in (item.get("risks") or [])][:12]
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        gateway_error = "18086 旁路暂不可用；现有作业与成绩未改变"
        for row in rows:
            row["risks"].append(gateway_error)
    totals = compare_rows(rows)
    for row in rows:
        row.pop("problem_text")
    return {"submission_id": submission_id, "read_only": True,
            "pages": [{"page_no": index, "image_url": f"/api/submissions/{submission_id}/pages/{index}/image"}
                      for index in range(1, len(pages) + 1)],
            "rows": rows, "summary": totals, "gateway_error": gateway_error}
