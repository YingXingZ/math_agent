"""Evidence-first grading pipeline for the teacher-facing agent."""
from __future__ import annotations

import base64
from difflib import SequenceMatcher
import io
import json
import os
import shutil
import subprocess
import tempfile
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps

from .config import settings
from .db import connection, normalize_question_type
from .agent_tools import run_tool_use
from .llm_provider import grade_homework, model_runtime, recognize_formula
from .region_sets import expand_regions, public_mapping
from .crop_grading import complete_reference, grade_crops
from .photo_semantic_locator import (orient_locator_pages, restore_region_coordinates,
                                    verify_crop_matches_problem, verify_recognized_work_matches_problem)
from .reference_tools import scope_reference
from .prompt_security import inspect_untrusted_text
from .quality_gates import audit_candidate_breakdown, audit_rubric, gate_unverified_candidate, normalize_step_scores, reconcile_step_scores_to_question_score


def _recognition_is_contaminated(recognized_work: str, reference_answer: str) -> bool:
    """Detect a dangerous VLM failure: copying the supplied answer into OCR text.

    This is intentionally conservative.  A long normalised overlap means the
    display must be withheld and sent to teacher review, rather than allowing
    generated reference prose to become false evidence of student work.
    """
    compact = lambda value: "".join(ch for ch in str(value or "") if not ch.isspace() and ch not in "$\\{}()[]，,。；;：:")
    work, reference = compact(recognized_work), compact(reference_answer)
    return len(work) >= 30 and len(reference) >= 30 and (work in reference or reference in work)


def _recognition_has_runaway_repetition(recognized_work: str) -> bool:
    """Detect OCR/VLM loops without mistaking ordinary repeated math for a loop.

    Some vision responses occasionally repeat the same recognition line until
    their output limit.  That is neither a blank page nor usable evidence.  It
    must never replace a shorter, meaningful first-pass transcription.
    """
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(recognized_work or "").splitlines()]
    lines = [line for line in lines if len(line) >= 12]
    if len(lines) < 5:
        return False
    counts: dict[str, int] = {}
    for line in lines:
        counts[line] = counts.get(line, 0) + 1
    return max(counts.values(), default=0) >= 5


def _collapse_repeated_recognition(recognized_work: str) -> str:
    """Keep the first occurrence of each OCR line for teacher-visible evidence."""
    kept: list[str] = []
    seen: set[str] = set()
    for raw_line in str(recognized_work or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        key = re.sub(r"\s+", "", line)
        if key in seen:
            continue
        seen.add(key)
        kept.append(line)
    return "\n".join(kept).strip()


def _numbered_task_coverage(problem_text: str, recognized_work: str) -> dict[str, Any]:
    """Check whether every explicitly numbered subpart appears in OCR.

    Textbook exports often keep (1)…(12) on one line separated by semicolons,
    so line-start matching silently misses all but the first subpart.  The
    question labels themselves are reliable enough for a completeness gate;
    mathematical correctness remains the grader's separate responsibility.
    """
    text = str(problem_text or "")
    numbers = []
    for match in re.finditer(r"[（(]\s*(\d{1,2})\s*[)）]", text):
        number = match.group(1)
        if number not in numbers:
            numbers.append(number)
    if len(numbers) < 2:
        return {"available": False, "required": 0, "matched": 0, "missing": []}
    recognized_labels = set(re.findall(r"[（(]\s*(\d{1,2})\s*[)）]", str(recognized_work or "")))
    matched_parts = [number for number in numbers if number in recognized_labels]
    missing_parts = [number for number in numbers if number not in recognized_labels]
    return {
        "available": True, "required": len(numbers), "matched": len(matched_parts),
        "matched_parts": matched_parts, "missing": missing_parts,
        "ratio": len(matched_parts) / len(numbers),
    }


def _requested_parameter_coverage(problem_text: str, recognized_work: str) -> dict[str, Any]:
    """Require explicit values for every named constant the prompt asks to find.

    This deliberately handles only a clear, common prompt form ("求常数 a,b 的值")
    rather than guessing variables from arbitrary algebra.  It is a completeness
    gate, not a correctness proof: the grader still has to verify each value.
    """
    text = re.sub(r"[\\$(){}]", "", str(problem_text or ""))
    request = re.search(
        r"(?:求(?:出|解)?|确定)(?:\s*常数)?\s*"
        r"([A-Za-z])\s*[,，、及和与]\s*([A-Za-z])"
        r"\s*(?:的\s*)?(?:值|取值)",
        text,
        flags=re.IGNORECASE,
    )
    if not request:
        return {"available": False, "required": [], "matched": [], "missing": []}

    required = list(dict.fromkeys(letter.lower() for letter in request.groups()))
    work = str(recognized_work or "")
    matched = [letter for letter in required if re.search(
        rf"(?:^|[\s,，;；:：∴∵]){re.escape(letter)}\s*=(?!=)",
        work,
        flags=re.IGNORECASE,
    )]
    missing = [letter for letter in required if letter not in matched]
    return {
        "available": True,
        "required": required,
        "matched": matched,
        "missing": missing,
        "ratio": len(matched) / len(required) if required else 1.0,
    }


def _completion_check(recognized_work: str, model_result: dict[str, Any]) -> dict[str, Any]:
    """Reject obviously unfinished work before it can be auto-accepted.

    This is deliberately a *safety gate*, not a second mathematical grader.
    A complete but unsimplified quotient-rule expression remains valid.  The
    gate only blocks a result when the vision model explicitly saw unfinished
    work, or OCR itself ends in an unfinished operator / delimiter.
    """
    declared = model_result.get("work_complete")
    if isinstance(declared, str):
        declared = declared.strip().lower() in {"true", "1", "yes", "complete", "\u5b8c\u6574"}
    # Completion is audited independently without the answer key. A negative
    # visual verdict must block auto-acceptance even when grading says correct.
    if declared is False:
        reason = str(model_result.get("completion_evidence") or "视觉识别到作答未完成").strip()
        return {"complete": False, "reason": reason, "source": "vision"}

    work = str(recognized_work or "").replace("\u3000", " ").strip()
    if not work:
        return {"complete": False, "reason": "\u672a\u8bc6\u522b\u5230\u53ef\u5b8c\u6574\u5224\u5b9a\u7684\u4f5c\u7b54", "source": "ocr"}

    # A terminal operator is strong objective evidence of a cut-off expression.
    compact = re.sub(r"\s+", "", work)
    terminal_operator = re.search(
        r"(?:[+\-*/=]|\\(?:times|cdot|div|pm|mp)|\u00d7|\u00f7|\uff0b|\uff0d|\uff1d)$",
        compact,
    )
    if terminal_operator:
        return {
            "complete": False,
            "reason": "\u8bc6\u522b\u5230\u4f5c\u7b54\u4ee5\u672a\u5b8c\u6210\u7684\u8fd0\u7b97\u7b26\u53f7\u7ed3\u5c3e",
            "source": "ocr",
        }

    # Count literal delimiters after ignoring escaped braces; this catches
    # malformed OCR such as an unfinished fraction without rejecting normal
    # LaTex commands like \\frac{a}{b}.
    plain = re.sub(r"\\[{}]", "", work)
    pairs = (("(", ")"), ("[", "]"), ("{", "}"))
    for opening, closing in pairs:
        if plain.count(opening) != plain.count(closing):
            return {
                "complete": False,
                "reason": "\u8bc6\u522b\u5230\u4f5c\u7b54\u7684\u62ec\u53f7\u6216\u5206\u7ec4\u7b26\u53f7\u672a\u95ed\u5408",
                "source": "ocr",
            }
    return {"complete": True, "reason": "", "source": "passed"}


def _all_rubric_points_earned(model_result: dict[str, Any]) -> bool:
    """Return true only when the model's structured rubric grants every point."""
    steps = model_result.get("step_scores") or []
    if not isinstance(steps, list) or not steps:
        return False
    try:
        return all(
            float(step.get("max_score") or 0) > 0
            and float(step.get("score") or 0) + 1e-6 >= float(step.get("max_score") or 0)
            for step in steps
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _rubric_coverage(recognized_work: str, effective_rubric: str) -> dict[str, Any]:
    """Check whether every structured rubric criterion has visible work evidence."""
    try:
        items = json.loads(str(effective_rubric or ""))
    except (TypeError, ValueError):
        return {"available": False, "complete": False, "matched": [], "missing": []}
    if not isinstance(items, list) or not items:
        return {"available": False, "complete": False, "matched": [], "missing": []}
    raw_work = str(recognized_work or "")
    compact_work = re.sub(r"\s+", "", raw_work).replace("\\", "")
    matched: list[str] = []
    missing: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            return {"available": False, "complete": False, "matched": [], "missing": []}
        patterns = item.get("patterns")
        if not isinstance(patterns, list) or not patterns:
            return {"available": False, "complete": False, "matched": [], "missing": []}
        found = False
        for value in patterns:
            pattern = str(value or "").strip()
            if not pattern:
                continue
            try:
                if re.search(pattern, raw_work, flags=re.IGNORECASE):
                    found = True
                    break
            except re.error:
                pass
            compact_pattern = re.sub(r"\s+", "", pattern).replace("\\", "")
            if compact_pattern and compact_pattern in compact_work:
                found = True
                break
        label = str(item.get("key") or index)
        (matched if found else missing).append(label)
    return {
        "available": True,
        "complete": not missing,
        "matched": matched,
        "missing": missing,
    }


def _answer_text_matches(recognized_work: str, standard_answer: str) -> bool:
    """High-precision answer safeguard after contamination screening."""
    import re
    def compact(value: str) -> str:
        value = str(value or '').lower()
        for source, target in {'\\left':'', '\\right':'', '\\infty':'∞', '\\leq':'≤', '\\geq':'≥', '（':'(', '）':')', '，':',', '。':'.', '＝':'='}.items():
            value = value.replace(source, target)
        value = re.sub(r'\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}', r'\1/\2', value)
        return re.sub(r'[\s,.:;_\[\]{}$\\]', '', value)
    student, answer = compact(recognized_work), compact(standard_answer)
    return len(student) >= 24 and len(answer) >= 24 and SequenceMatcher(None, student, answer).ratio() >= 0.92


def _math_equal(student_answer: str, standard_answer: str) -> dict[str, Any]:
    """Use the existing symbolic engine as an independent check of Qwen."""
    if not student_answer.strip() or not standard_answer.strip():
        return {"available": False, "equal": None, "confidence": 0, "method": "缺少可判等表达式"}
    try:
        project_root = str(Path(__file__).resolve().parents[2])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from grading_engine import expr_equal

        equal, confidence, method = expr_equal(student_answer, standard_answer)
        if equal:
            return {"available": True, "equal": equal, "confidence": confidence, "method": method}

        # Handwritten calculation work is commonly recognised as a complete
        # equality (for example ``1+0=1``), while the answer key stores only
        # the final value (``1``).  Compare its right-hand side only for a
        # single plain equality.  Do not apply this to inequalities, chained
        # equations, set membership, or prose: those carry mathematical
        # structure that must remain in the normal conservative path.
        raw = student_answer.strip().replace("＝", "=")
        if (raw.count("=") == 1 and not any(token in raw for token in ("<", ">", "≤", "≥", "≠", "∈", "∉"))):
            _left, rhs = raw.split("=", 1)
            if _left.strip() and rhs.strip():
                rhs_equal, rhs_confidence, rhs_method = expr_equal(rhs.strip(), standard_answer)
                if rhs_equal:
                    return {
                        "available": True,
                        "equal": True,
                        "confidence": rhs_confidence,
                        "method": f"等式右端与标准答案一致（{rhs_method}）",
                    }
        return {"available": True, "equal": equal, "confidence": confidence, "method": method}
    except Exception as exc:
        return {"available": False, "equal": None, "confidence": 0, "method": f"判等不可用：{str(exc)[:100]}"}


def _render_pdf_pages(path: Path) -> list[Path]:
    """Render PDF pages into a validated cache without exposing partial PNGs.

    Concurrent retries for the same submission used to write the same
    qwen_pages files at once. A grader could open a page while another worker
    was replacing it and PIL would report an unidentified image. Use a
    per-source interprocess lock, validate the whole cache, render to a private
    temporary directory, then atomically publish complete pages.
    """
    try:
        from pypdf import PdfReader
        page_count = len(PdfReader(str(path)).pages)
    except Exception as exc:
        raise ValueError(f"无法读取 PDF：{str(exc)[:100]}") from exc
    if page_count == 0:
        raise ValueError("PDF 没有可识别页面")

    SAFE_PAGE_CEILING = 80
    if page_count > SAFE_PAGE_CEILING:
        raise ValueError(
            f"PDF 共 {page_count} 页，超过安全上限 {SAFE_PAGE_CEILING} 页，已转入教师复核。"
        )

    rendered_dir = path.parent / f"{path.stem}_qwen_pages"
    lock_path = path.parent / f".{path.stem}_qwen_pages.lock"

    def complete_cache() -> list[Path] | None:
        pages = sorted(rendered_dir.glob("page-*.png"), key=lambda item: int(item.stem.rsplit("-", 1)[-1]))
        if len(pages) != page_count:
            return None
        try:
            for page in pages:
                with Image.open(page) as image:
                    image.verify()
            return pages
        except Exception:
            return None

    try:
        import fcntl
    except ImportError:
        fcntl = None

    with lock_path.open("a+b") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        cached = complete_cache()
        if cached:
            return cached

        rendered_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=f".{path.stem}_render_", dir=str(path.parent)))
        try:
            bundled_renderer = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe"
            renderer = (
                settings.pdf_renderer_path
                or os.environ.get("PDFTOPPM_PATH", "")
                or (str(bundled_renderer) if bundled_renderer.is_file() else "")
                or shutil.which("pdftoppm")
            )
            if not renderer:
                try:
                    import fitz

                    document = fitz.open(str(path))
                    matrix = fitz.Matrix(settings.qwen_pdf_render_dpi / 72, settings.qwen_pdf_render_dpi / 72)
                    for page_index in range(page_count):
                        pixmap = document.load_page(page_index).get_pixmap(matrix=matrix, alpha=False)
                        pixmap.save(str(temp_dir / f"page-{page_index + 1}.png"))
                    document.close()
                except Exception as exc:
                    raise ValueError(f"PDF 渲染组件不可用，已转入教师复核：{str(exc)[:100]}") from exc
            else:
                prefix = temp_dir / "page"
                command = [
                    renderer, "-png", "-r", str(settings.qwen_pdf_render_dpi),
                    "-f", "1", "-l", str(page_count), str(path), str(prefix),
                ]
                try:
                    subprocess.run(command, check=True, capture_output=True, timeout=180)
                except FileNotFoundError as exc:
                    raise ValueError("PDF 渲染组件不可用，已转入教师复核") from exc
                except subprocess.CalledProcessError as exc:
                    raise ValueError(f"PDF 转图片失败：{exc.stderr.decode(errors='replace')[:120]}") from exc
                except subprocess.TimeoutExpired as exc:
                    raise ValueError("PDF 转图片超时，已转入教师复核") from exc

            pages = sorted(temp_dir.glob("page-*.png"), key=lambda item: int(item.stem.rsplit("-", 1)[-1]))
            if len(pages) != page_count:
                raise ValueError(f"PDF 仅生成 {len(pages)}/{page_count} 张完整页图")
            for page in pages:
                with Image.open(page) as image:
                    image.verify()

            expected_names = {page.name for page in pages}
            for page in pages:
                os.replace(page, rendered_dir / page.name)
            for stale in rendered_dir.glob("page-*.png"):
                if stale.name not in expected_names:
                    stale.unlink(missing_ok=True)
            complete = complete_cache()
            if not complete:
                raise ValueError("PDF 页图缓存校验失败")
            return complete
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _submission_page_paths(file_path: str) -> list[Path]:
    """Return the original image page(s) used by grading, never alter the upload."""
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return [path]
    if suffix == ".pdf":
        return _render_pdf_pages(path)
    raise ValueError("暂不支持 Word 自动识别，已转入教师复核，不会误判。")


def _load_images(file_path: str) -> list[str]:
    pages = _submission_page_paths(file_path)
    raster_suffixes = {".jpg", ".jpeg", ".png", ".webp"}
    encoded: list[str] = []
    for page in pages:
        if page.suffix.lower() not in raster_suffixes:
            encoded.append(base64.b64encode(page.read_bytes()).decode("ascii"))
            continue
        # Phone photos frequently store pixels in sensor orientation and rely
        # on EXIF Orientation for display. Browsers/viewers honor that tag, but
        # the locator and cropper use raw pixels. Normalize once at ingestion
        # so all downstream normalized boxes refer to the same upright image.
        with Image.open(page) as source:
            upright = ImageOps.exif_transpose(source).convert("RGB")
            buffer = io.BytesIO()
            upright.save(buffer, format="PNG", optimize=True)
        encoded.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
    return encoded



def _compact_problem_label(value: str) -> str:
    """Normalize printed heading text for deterministic PDF anchor matching."""
    return re.sub(r"[\s·•]+", "", str(value or "")).replace("(", "（").replace(")", "）")


def _infer_pdf_question_regions(file_path: str, questions: list[dict[str, Any]]) -> dict[tuple[int, str, int], dict[str, Any]]:
    """Infer per-question rectangles from the generated PDF's printed headings.

    The crop begins just above its heading and ends just before the next known
    assignment heading on that page. This uses embedded PDF text coordinates,
    never VLM number guessing, and therefore cannot drift to another question.
    """
    path = Path(file_path)
    if path.suffix.lower() != ".pdf":
        return {}
    try:
        import fitz
        document = fitz.open(str(path))
    except Exception:
        return {}
    wanted = {_compact_problem_label(row.get("problem_no")): row for row in questions}
    anchors: list[dict[str, Any]] = []
    try:
        for page_index, page in enumerate(document):
            for block in page.get_text("blocks"):
                block_text = _compact_problem_label(block[4])
                matches = [label for label in wanted if label and block_text.startswith(label)]
                if not matches:
                    continue
                label = max(matches, key=len)
                row = wanted[label]
                anchors.append({
                    "row": row, "page_no": page_index + 1,
                    "y0": float(block[1]), "page_width": float(page.rect.width),
                    "page_height": float(page.rect.height),
                })
    finally:
        document.close()
    by_page: dict[int, list[dict[str, Any]]] = {}
    for anchor in anchors:
        by_page.setdefault(anchor["page_no"], []).append(anchor)
    regions: dict[tuple[int, str, int], dict[str, Any]] = {}
    for page_no, page_anchors in by_page.items():
        page_anchors.sort(key=lambda item: item["y0"])
        for index, anchor in enumerate(page_anchors):
            page_height = anchor["page_height"]
            page_width = anchor["page_width"]
            top = max(0.0, anchor["y0"] - 8.0)
            next_top = page_anchors[index + 1]["y0"] - 8.0 if index + 1 < len(page_anchors) else page_height - 18.0
            bottom = max(top + 48.0, min(page_height, next_top))
            row = anchor["row"]
            key = (int(row["question_id"]), str(row.get("subpart_no") or ""), int(row["sort_order"]))
            regions[key] = {
                "page_no": page_no,
                "x": 42.0 / page_width,
                "y": top / page_height,
                "width": (page_width - 84.0) / page_width,
                "height": (bottom - top) / page_height,
                "auto_pdf_anchor": True,
                "anchor_label": row.get("problem_no"),
            }
    return regions

def _region_boxes_conflict(candidate_box: tuple[float, float, float, float],
                           trusted_box: tuple[float, float, float, float]) -> bool:
    """Reject overlap and near-containment across different assigned questions."""
    ax, ay, aw, ah = candidate_box
    bx, by, bw, bh = trusted_box
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    overlap = ix * iy
    area_a, area_b = aw * ah, bw * bh
    union = area_a + area_b - overlap
    smaller_coverage = overlap / max(min(area_a, area_b), 1e-9)
    # A small handwritten subpart label can be almost contained by a broad
    # verified parent crop while having low IoU; don't reinterpret it as a
    # separate assigned exercise.
    return union > 0 and (overlap / union >= .72 or smaller_coverage >= .85)


async def _florence_page_regions(images: list[str]) -> dict[int, list[dict[str, Any]]]:
    """Request Florence answer-box proposals in bounded batches.

    Florence supplies box geometry while the Qwen/printed-anchor locator keeps
    assigning question identity. Images are normalized to detector-sized JPEGs
    and chunked under the service's 24 MB / 12-page request limits so multi-page
    scans do not silently fall back because a single request was too large.
    """
    if os.environ.get("MATH_FLORENCE_REGION_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return {}
    endpoint = os.environ.get("MATH_FLORENCE_REGION_URL", "http://127.0.0.1:18087/detect-answer-regions")
    prepared: list[str] = []
    for raw in images:
        try:
            with Image.open(io.BytesIO(base64.b64decode(raw))) as source:
                source = source.convert("RGB")
                source.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                source.save(buffer, format="JPEG", quality=88, optimize=True)
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                if len(encoded) > 20_000_000:
                    source.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                    buffer = io.BytesIO()
                    source.save(buffer, format="JPEG", quality=80, optimize=True)
                    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                prepared.append(encoded)
        except Exception:
            prepared.append(raw)
    output: dict[int, list[dict[str, Any]]] = {}
    async with httpx.AsyncClient(timeout=120) as client:
        offset = 0
        while offset < len(prepared):
            batch, size = [], 0
            while offset + len(batch) < len(prepared) and len(batch) < 12:
                candidate = prepared[offset + len(batch)]
                if batch and size + len(candidate) > 20_000_000:
                    break
                batch.append(candidate)
                size += len(candidate)
            try:
                response = await client.post(endpoint, json={"images_base64": batch})
                response.raise_for_status()
                payload = response.json()
            except Exception:
                # Preserve the established safe fallback if the proposal service
                # is unavailable; the worker still requires teacher review.
                return {}
            for page in payload.get("pages") or []:
                local_page_no = int(page.get("page_no") or 0)
                page_no = offset + local_page_no
                boxes = []
                for item in page.get("regions") or []:
                    box = {key: float(item[key]) for key in ("x", "y", "width", "height")}
                    box["page_no"] = page_no
                    if (page_no > 0 and 0 <= box["x"] < 1 and 0 <= box["y"] < 1
                            and 0 < box["width"] <= 1 and 0 < box["height"] <= 1
                            and box["x"] + box["width"] <= 1.001
                            and box["y"] + box["height"] <= 1.001):
                        boxes.append(box)
                output[page_no] = boxes
            offset += len(batch)
    return output


def _refine_regions_with_florence(
    regions: dict[tuple[int, str, int], dict[str, Any]],
    proposals: dict[int, list[dict[str, Any]]],
) -> dict[tuple[int, str, int], dict[str, Any]]:
    """Use Florence geometry only when it overlaps a Qwen-assigned question region.

    Qwen continues to assign question identity. The existing crop identity and
    teacher-review gates still run after this geometry refinement.
    """
    if not proposals:
        return regions
    refined = dict(regions)
    for key, mapping in regions.items():
        source_parts = expand_regions(mapping)
        target_parts = []
        used_florence = False
        for source in source_parts:
            try:
                page_no = int(source.get("page_no") or mapping.get("page_no") or 0)
                x, y, width, height = (float(source[name]) for name in ("x", "y", "width", "height"))
            except (TypeError, ValueError, KeyError):
                target_parts.append(source)
                continue
            best = None
            best_score = 0.0
            for candidate in proposals.get(page_no, []):
                ix = max(0.0, min(x + width, candidate["x"] + candidate["width"]) - max(x, candidate["x"]))
                iy = max(0.0, min(y + height, candidate["y"] + candidate["height"]) - max(y, candidate["y"]))
                intersection = ix * iy
                candidate_area = candidate["width"] * candidate["height"]
                candidate_coverage = intersection / candidate_area if candidate_area else 0.0
                if candidate_coverage >= 0.50 and candidate_coverage > best_score:
                    best, best_score = candidate, candidate_coverage
            if best is None:
                target_parts.append(source)
                continue
            target_parts.append({**best, "page_no": page_no,
                                 "florence_candidate_coverage": round(best_score, 4)})
            used_florence = True
        if used_florence and target_parts:
            refined[key] = {
                **mapping, **target_parts[0], "regions": target_parts,
                "auto_photo_anchor": True,
                "heading_source": "florence2_geometry_qwen_question_map",
            }
    return refined


async def _infer_photo_question_regions(file_path: str, questions: list[dict[str, Any]]) -> dict[tuple[int, str, int], dict[str, Any]]:
    """Conservatively locate handwritten question headings on image-only pages.

    Pixel rows are detected by the vision service before OCR; only unique,
    ordered, exact-number headings can produce a gradeable crop. Missing or
    ambiguous headings remain on the existing review-required path.
    """
    parents: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in questions:
        qid = int(row["question_id"])
        if qid not in seen:
            parents.append(row)
            seen.add(qid)
    numbered = []
    for row in parents:
        raw = str(row.get("original_no") or row.get("problem_no") or "")
        match = re.search(r"(\d+)\s*(?:题)?\s*$", raw)
        if not match:
            return {}
        numbered.append((row, int(match.group(1))))
    if not numbered:
        return {}
    images = _load_images(file_path)
    # For printed worksheets, printed prompt boundaries are stronger than
    # semantic guesses over handwriting. A complete, validated anchor map is
    # segmented deterministically to the next printed problem; incomplete
    # anchors fall through to the existing content locator.
    from .photo_semantic_locator import (
        detect_two_column_regions, locate_by_content, locate_by_printed_prompts,
        map_column_region_to_page,
    )
    locator_images, page_rotations = await orient_locator_pages(images)
    # Whole-page locator requests are deliberately downscaled for model
    # stability. Keep an upright, original-resolution counterpart only for
    # the small two-column window fallback below: tiny handwritten `8.`/`a`
    # labels can disappear at the whole-page resolution even though their
    # local tile is well within the vision endpoint's size limit.
    hires_locator_images: list[str] = []
    for raw_image, angle in zip(images, page_rotations):
        try:
            with Image.open(io.BytesIO(base64.b64decode(raw_image))) as source:
                source = source.convert("RGB")
                if angle:
                    source = source.rotate(-int(angle), expand=True)
                buffer = io.BytesIO()
                source.save(buffer, format="PNG", optimize=True)
                hires_locator_images.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
        except Exception:
            hires_locator_images.append(raw_image)
    florence_boxes = await _florence_page_regions(hires_locator_images)
    printed = await locate_by_printed_prompts(locator_images, parents)
    if printed is not None:
        printed_regions: dict[tuple[int, str, int], dict[str, Any]] = {}
        for part in questions:
            box = printed.get(int(part['question_id']))
            if box:
                key = (int(part['question_id']), str(part.get('subpart_no') or ''), int(part['sort_order']))
                printed_regions[key] = {**box, 'anchor_label': part.get('problem_no')}
        if len(printed_regions) == len(questions):
            return restore_region_coordinates(_refine_regions_with_florence(printed_regions, florence_boxes), page_rotations)
    semantic = await locate_by_content(locator_images, parents)
    semantic_regions: dict[tuple[int, str, int], dict[str, Any]] = {}
    if semantic is not None:
        for part in questions:
            box = semantic.get(int(part['question_id']))
            if box:
                key = (int(part['question_id']), str(part.get('subpart_no') or ''), int(part['sort_order']))
                semantic_regions[key] = {**box, 'anchor_label': part.get('problem_no')}
        if len(semantic_regions) == len(questions):
            return restore_region_coordinates(_refine_regions_with_florence(semantic_regions, florence_boxes), page_rotations)

    # A multi-question prompt can overlook work when pages are dense or the
    # student uses nonstandard numbering. Give each still-missing target one
    # focused content search over every page, then retain it only if that
    # locator's independent task audit passes and it does not overlap another
    # question's already-verified crop.
    known_question_ids = {key[0] for key in semantic_regions}
    fallback_eligible_ids: set[int] = set()
    for target in parents:
        target_id = int(target['question_id'])
        if target_id in known_question_ids:
            continue
        # Dense, multi-page uploads can make a joint scan miss one question.
        # Search each page independently for this one target, then combine
        # only page-local, internally audited regions as a possible continuation.
        page_candidates: list[dict[str, Any]] = []
        page_search_failed = False
        for page_index, page_image in enumerate(locator_images):
            # Tell the focused locator which regions have already been
            # assigned to neighboring questions. Otherwise it may repeatedly
            # return the visible q7/q8 answer for a missing q6, get rejected
            # by overlap checks, and never inspect the intervening page area.
            neighbor_regions = []
            for existing_key, existing in semantic_regions.items():
                if existing_key[0] == target_id:
                    continue
                for region in expand_regions(existing):
                    if int(region.get('page_no') or 0) == page_index + 1:
                        neighbor_regions.append(region)
            try:
                page_map = await locate_by_content(
                    [page_image], [target],
                    exclude_regions={target_id: neighbor_regions} if neighbor_regions else None,
                )
            except Exception:
                page_map = None
            page_candidate = (page_map or {}).get(target_id)
            # A dense two-column scan is an important counterexample to a
            # whole-page semantic request: the locator can recognise the
            # target perfectly once only the relevant column/window is shown,
            # yet omit it from its page-wide response.  Try a few overlapping
            # column windows *only after* that whole-page search failed.  This
            # is still an automatic proposal: the normal image, OCR, scope and
            # completeness gates below must all approve it before scoring.
            existing_area = (
                float(page_candidate.get("width") or 0) * float(page_candidate.get("height") or 0)
                if page_candidate else 0.0
            )
            # A page-wide semantic request may return only a trailing answer
            # line. For an unusually broad candidate, also let a focused
            # column window compete; a materially tighter window is less
            # likely to be a coarse page-level guess. It still has to pass all
            # later identity/completeness gates, so this is never a score-only
            # optimisation.
            if not page_candidate or existing_area >= .10:
                highres_page = hires_locator_images[page_index]
                columns = detect_two_column_regions(highres_page) or []
                try:
                    with Image.open(io.BytesIO(base64.b64decode(highres_page))) as source:
                        source = source.convert("RGB")
                        page_width, page_height = source.size
                        window_candidates: list[tuple[int, dict[str, Any]]] = []
                        for column_index, column in enumerate(columns):
                            # The fourth window deliberately bridges the
                            # middle/lower overlap. On real ruled-paper scans
                            # a short final question can start below .5 and
                            # finish above .68; neither of the two broad bands
                            # alone contains enough of its derivation to prove
                            # identity or requested-parameter completeness.
                            for y_start, y_end in ((.02, .38), (.32, .68), (.44, .82), (.62, .98)):
                                bounds = {
                                    "x": float(column["x"]), "y": y_start,
                                    "width": float(column["width"]), "height": y_end - y_start,
                                }
                                box = (
                                    round(bounds["x"] * page_width),
                                    round(bounds["y"] * page_height),
                                    round((bounds["x"] + bounds["width"]) * page_width),
                                    round((bounds["y"] + bounds["height"]) * page_height),
                                )
                                tile = source.crop(box)
                                buffer = io.BytesIO()
                                tile.save(buffer, format="PNG", optimize=True)
                                tile_payload = base64.b64encode(buffer.getvalue()).decode("ascii")
                                tile_map = await locate_by_content([tile_payload], [target])
                                tile_candidate = (tile_map or {}).get(target_id)
                                if not tile_candidate:
                                    continue
                                mapped_regions = [
                                    map_column_region_to_page(region, bounds, page_index + 1)
                                    for region in expand_regions(tile_candidate)
                                ]
                                if not mapped_regions:
                                    continue
                                mapped_regions.sort(key=lambda region: (region["page_no"], region["y"], region["x"]))
                                first = mapped_regions[0]
                                window_candidates.append((column_index, {
                                    **first,
                                    "regions": mapped_regions,
                                    "auto_photo_anchor": True,
                                    "heading_source": "qwen35_two_column_window",
                                    "location_confidence": tile_candidate.get("location_confidence"),
                                    "location_evidence": tile_candidate.get("location_evidence"),
                                }))
                        # Adjacent overlapping windows are independent views
                        # of the same handwriting. Merge the strongest
                        # connected cluster so a derivation straddling a band
                        # boundary is not cut into an incomplete answer.
                        grouped: dict[int, list[dict[str, Any]]] = {}
                        for column_index, candidate in window_candidates:
                            grouped.setdefault(column_index, []).append(candidate)
                        merged_windows: list[dict[str, Any]] = []
                        for candidates in grouped.values():
                            candidates.sort(key=lambda item: float(item["y"]))
                            cluster: list[dict[str, Any]] = []
                            for candidate in candidates:
                                if not cluster:
                                    cluster.append(candidate)
                                    continue
                                previous_bottom = max(float(item["y"]) + float(item["height"])
                                                      for item in cluster)
                                if float(candidate["y"]) <= previous_bottom - .02:
                                    cluster.append(candidate)
                            if not cluster:
                                continue
                            left = min(float(item["x"]) for item in cluster)
                            top = min(float(item["y"]) for item in cluster)
                            right = max(float(item["x"]) + float(item["width"]) for item in cluster)
                            bottom = max(float(item["y"]) + float(item["height"]) for item in cluster)
                            merged_windows.append({
                                "page_no": page_index + 1, "x": left, "y": top,
                                "width": right - left, "height": bottom - top,
                                "regions": [{"page_no": page_index + 1, "x": left, "y": top,
                                             "width": right - left, "height": bottom - top}],
                                "auto_photo_anchor": True,
                                "heading_source": "qwen35_two_column_window",
                                "location_confidence": min(float(item.get("location_confidence") or 0)
                                                             for item in cluster),
                                "location_evidence": "；".join(
                                    str(item.get("location_evidence") or "") for item in cluster
                                )[:360],
                            })
                        if merged_windows:
                            window_candidate = max(
                                merged_windows,
                                key=lambda item: float(item["width"]) * float(item["height"]),
                            )
                            window_area = float(window_candidate["width"]) * float(window_candidate["height"])
                            starts_materially_earlier = (
                                page_candidate is not None
                                and float(window_candidate["y"]) < float(page_candidate["y"]) - .04
                            )
                            if (not page_candidate or window_area <= existing_area * 1.10
                                    and starts_materially_earlier):
                                page_candidate = window_candidate
                except Exception:
                    # A local visual tile is a recovery path only. Failure
                    # must leave the original full-page/number layout fallbacks
                    # available, never turn an unlocated answer into a blank.
                    if not page_candidate:
                        page_candidate = None
            if page_candidate:
                page_regions = []
                for region in expand_regions(page_candidate):
                    page_regions.append({**region, 'page_no': page_index + 1})
                page_candidates.append({**page_candidate, 'regions': page_regions})
            elif page_map is None or (page_map and target_id not in page_map):
                page_search_failed = True
        focused = ({target_id: {
            **page_candidates[0],
            'regions': [region for candidate_page in page_candidates
                        for region in candidate_page.get('regions', [])][:4],
            'location_confidence': min(float(item.get('location_confidence') or 0)
                                       for item in page_candidates),
            'location_evidence': '；'.join(str(item.get('location_evidence') or '')
                                          for item in page_candidates)[:180],
        }} if page_candidates else (None if page_search_failed else {}))
        candidate = (focused or {}).get(target_id)
        if candidate and candidate.get('regions'):
            first_region = candidate['regions'][0]
            candidate.update({key: first_region[key]
                              for key in ('page_no', 'x', 'y', 'width', 'height')})
        if not candidate:
            # Focused semantic absence is not proof that the work is missing:
            # the live regressions showed valid answers omitted from one-page
            # and multi-page maps. Always give the independent number/layout
            # locator a chance; any resulting crop is still review-only and
            # must pass the image and OCR task-identity gates before scoring.
            fallback_eligible_ids.add(target_id)
            continue
        candidate_regions = expand_regions(candidate)
        conflict = False
        for existing_key, existing in semantic_regions.items():
            if existing_key[0] == target_id:
                continue
            for left in candidate_regions:
                for right in expand_regions(existing):
                    if int(left.get('page_no') or 0) != int(right.get('page_no') or 0):
                        continue
                    ix = max(0.0, min(float(left['x']) + float(left['width']), float(right['x']) + float(right['width']))
                             - max(float(left['x']), float(right['x'])))
                    iy = max(0.0, min(float(left['y']) + float(left['height']), float(right['y']) + float(right['height']))
                             - max(float(left['y']), float(right['y'])))
                    intersection = ix * iy
                    left_area = float(left['width']) * float(left['height'])
                    right_area = float(right['width']) * float(right['height'])
                    union = left_area + right_area - intersection
                    # Separate, independently audited answers can overlap at
                    # their edges. Reject only near-duplicate boxes here;
                    # OCR/task consistency below remains the misassignment gate.
                    if union > 0 and intersection / union >= .72:
                        conflict = True
                        break
                if conflict:
                    break
            if conflict:
                break
        if conflict:
            fallback_eligible_ids.add(target_id)
            continue
        for part in questions:
            if int(part['question_id']) != target_id:
                continue
            key = (target_id, str(part.get('subpart_no') or ''), int(part['sort_order']))
            semantic_regions[key] = {**candidate, 'anchor_label': part.get('problem_no')}
        known_question_ids.add(target_id)

    fallback_numbered = [(row, number) for row, number in numbered
                         if int(row['question_id']) in fallback_eligible_ids]
    if not fallback_numbered:
        return restore_region_coordinates(_refine_regions_with_florence(semantic_regions, florence_boxes), page_rotations)

    endpoint = settings.qwen_grading_url.rsplit("/", 1)[0] + "/locate-homework"
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(endpoint, json={
                "images_base64": locator_images,
                "problem_nos": sorted({str(number) for _, number in fallback_numbered}),
            })
            response.raise_for_status()
            locator_result = response.json()
            candidates = locator_result.get("candidates") or []
            layout_rows = locator_result.get("layout_rows") or []
    except Exception:
        return restore_region_coordinates(_refine_regions_with_florence(semantic_regions, florence_boxes), page_rotations)
    located: list[tuple[int, float, int]] = []
    # The locator can also return a full answer rectangle.  It is still an AI
    # proposal (and therefore always review-required), but it is much more
    # useful than dropping a clearly located answer just because its heading
    # was not read by the narrow left-margin pass.
    direct_regions: dict[int, list[tuple[int, float, float, float, float]]] = {}
    for item in candidates:
        try:
            page = int(item["page_index"]) + 1
            y = float(item["y"])
            number = int(item["number"])
        except (KeyError, ValueError, TypeError):
            continue
        if 1 <= page <= len(locator_images) and .025 < y < .94:
            located.append((page, y, number))
        try:
            x = float(item["x"])
            width = float(item["width"])
            height = float(item["height"])
        except (KeyError, ValueError, TypeError):
            continue
        if (1 <= page <= len(locator_images) and 0.0 <= x < 1.0 and .01 <= y < .98
                and .04 <= width <= 1.0 and .03 <= height <= 1.0
                and x + width <= 1.0001 and y + height <= 1.0001):
            direct_regions.setdefault(number, []).append((page, x, y, width, height))
    located.sort()
    strong = []
    for item in layout_rows:
        try:
            place = (int(item["page_index"]) + 1, float(item["y"]))
        except (KeyError, ValueError, TypeError):
            continue
        if 1 <= place[0] <= len(locator_images) and .025 < place[1] < .94:
            strong.append(place)
    strong.sort()
    # Use an explicit VLM rectangle only when it is unambiguous for a known
    # question number.  These crops are initial-review evidence, never a final
    # mark, and they allow a handwritten answer without a visible left heading
    # to enter the same automatic grading flow as the other questions.
    explicit_by_question: dict[int, tuple[int, float, float, float, float]] = {}
    for row, number in fallback_numbered:
        choices = direct_regions.get(number, [])
        if len(choices) == 1:
            explicit_by_question[int(row["question_id"])] = choices[0]

    matched: list[tuple[dict[str, Any], int, float, str]] = []
    last_position = (0, 0.0)
    for row, number in fallback_numbered:
        if int(row["question_id"]) in explicit_by_question:
            continue
        choices = [(page, y) for page, y, value in located if value == number and (page, y) > last_position]
        if len(choices) > 1:
            choices = [position for position in choices if any(
                position[0] == p and abs(position[1] - sy) <= .015 for p, sy in strong)]
            # A cluster of three reads at one handwritten heading usually
            # includes its preceding section mark and the following step.
            # The middle row is only a review-required layout candidate.
            if len(choices) == 3 and len({page for page, _ in choices}) == 1 and choices[-1][1] - choices[0][1] < .09:
                choices = [choices[1]]
        if len(choices) == 1:
            page, y = choices[0]
            matched.append((row, page, y, "heading_read"))
            last_position = (page, y)
    ordered = []
    previous = (0, 0.0)
    for index, (row, _number) in enumerate(fallback_numbered):
        anchor = next((item for item in matched if int(item[0]["question_id"]) == int(row["question_id"])), None)
        if anchor:
            if (anchor[1], anchor[2]) > previous:
                ordered.append(anchor)
                previous = (anchor[1], anchor[2])
            continue
        next_anchor = next((item for later, _ in fallback_numbered[index + 1:]
                            for item in matched if int(item[0]["question_id"]) == int(later["question_id"])
                            and (item[1], item[2]) > previous), None)
        if previous[0] == 0:
            continue
        options = [position for position in strong
                   if position > (previous[0], previous[1] + .09)
                   and (next_anchor is None or position < (next_anchor[1], next_anchor[2] - .06))]
        if options:
            if ordered and ordered[-1][3] == "layout_inferred" and len(ordered) >= 2:
                earlier = ordered[-2]
                if earlier[1] == previous[0]:
                    gap = previous[1] - earlier[2]
                    if .11 <= gap <= .25:
                        target = previous[1] + gap
                        aligned = [position for position in options
                                   if position[0] == previous[0] and abs(position[1] - target) <= .055]
                        options = sorted(aligned, key=lambda position: abs(position[1] - target))
            if options:
                page, y = options[0]
                ordered.append((row, page, y, "layout_inferred"))
                previous = (page, y)
    matched = ordered
    regions: dict[tuple[int, str, int], dict[str, Any]] = {}
    for row, page, x, y, width, height in (
        (row, *explicit_by_question[int(row["question_id"])])
        for row, _number in numbered
        if int(row["question_id"]) in explicit_by_question
    ):
        for part in questions:
            if int(part["question_id"]) != int(row["question_id"]):
                continue
            key = (int(part["question_id"]), str(part.get("subpart_no") or ""), int(part["sort_order"]))
            regions[key] = {"page_no": page, "x": x, "y": y, "width": width, "height": height,
                            "auto_photo_anchor": True, "heading_source": "vlm_answer_region",
                            "anchor_label": part.get("problem_no")}
    for index, (row, page, y, heading_source) in enumerate(matched):
        next_y = next((later_y for _, later_page, later_y, _ in matched[index + 1:] if later_page == page), .95)
        top = max(0.0, y - .015)
        bottom = min(.97, next_y - .012) if next_y < .95 else .95
        # A very long span probably hides an undetected heading. Do not grade it.
        if bottom - top < .055 or bottom - top > .78:
            continue
        for part in questions:
            if int(part["question_id"]) != int(row["question_id"]):
                continue
            key = (int(part["question_id"]), str(part.get("subpart_no") or ""), int(part["sort_order"]))
            regions[key] = {"page_no": page, "x": .015, "y": top,
                            "width": .97, "height": bottom - top,
                            "auto_photo_anchor": True, "heading_source": heading_source, "anchor_label": part.get("problem_no")}
    # Keep independently verified semantic crops and use the weaker number /
    # layout locator only for still-missing questions. Never let a fallback box
    # overwrite a semantic result or overlap a different question's crop.
    merged = dict(semantic_regions)
    semantic_by_question = {key[0]: value for key, value in semantic_regions.items()}
    for key, candidate in regions.items():
        if key in merged:
            continue
        candidate_page = int(candidate.get('page_no') or 0)
        candidate_box = (float(candidate.get('x') or 0), float(candidate.get('y') or 0),
                         float(candidate.get('width') or 0), float(candidate.get('height') or 0))
        conflict = False
        for other_qid, trusted in semantic_by_question.items():
            if other_qid == key[0]:
                continue
            for trusted_region in expand_regions(trusted):
                if int(trusted_region.get('page_no') or 0) != candidate_page:
                    continue
                trusted_box = (float(trusted_region.get('x') or 0),
                               float(trusted_region.get('y') or 0),
                               float(trusted_region.get('width') or 0),
                               float(trusted_region.get('height') or 0))
                candidate_area = candidate_box[2] * candidate_box[3]
                trusted_area = trusted_box[2] * trusted_box[3]
                # A page-spanning semantic box for a multi-part question must
                # not suppress a smaller, independently numbered question.
                # These heading/layout candidates still pass the normal crop
                # identity and OCR checks and remain teacher-review-only.
                anchored_small_candidate = (
                    candidate.get('heading_source') in {'heading_read', 'layout_inferred', 'vlm_answer_region'}
                    and trusted_area >= .55
                    and candidate_area <= trusted_area * .75
                )
                if anchored_small_candidate:
                    continue
                if _region_boxes_conflict(candidate_box, trusted_box):
                    conflict = True
                    break
            if conflict:
                break
        if not conflict:
            merged[key] = candidate
    return restore_region_coordinates(merged, page_rotations)


def _prior_ai_suggested_regions(
    submission_id: int, questions: list[dict[str, Any]], page_count: int
) -> dict[tuple[int, str, int], dict[str, Any]]:
    """Reuse a prior non-final AI crop proposal when a submission is regraded.

    Proposals remain automatic, low-trust evidence: they are cropped for an
    initial score and always routed to teacher review.  They never become a
    teacher-confirmed mapping and never override a manual rectangle.
    """
    with connection() as conn:
        row = conn.execute(
            """SELECT result_json FROM grading_jobs WHERE submission_id=?
               AND result_json IS NOT NULL ORDER BY id DESC LIMIT 1""",
            (submission_id,),
        ).fetchone()
    if not row or not row["result_json"]:
        return {}
    try:
        results = json.loads(row["result_json"]).get("results") or []
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    allowed = {
        (int(item["question_id"]), str(item.get("subpart_no") or ""), int(item["sort_order"]))
        for item in questions
    }
    regions: dict[tuple[int, str, int], dict[str, Any]] = {}
    for item in results:
        key = (int(item.get("question_id") or 0), str(item.get("subpart_no") or ""), int(item.get("sort_order") or 0))
        raw = (item.get("qwen") or {}).get("suggested_region")
        if key not in allowed or not isinstance(raw, dict):
            continue
        qwen_input = item.get("qwen_input") or {}
        location_audit = (item.get("location_match_audit")
                          or qwen_input.get("location_match_audit") or {})
        # Old proposals may have been produced from raw phone pixels before
        # EXIF orientation was normalized, or may have failed the identity
        # audit. Reusing either can override a fresh, corrected locator result.
        if (qwen_input.get("coordinate_space") != "exif_upright_v1"
                or location_audit.get("matches") is not True
                or not str(item.get("recognized_work") or "").strip()):
            continue
        try:
            page_index = int(raw["page_index"])
            x, y = float(raw["x"]), float(raw["y"])
            width, height = float(raw["width"]), float(raw["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= page_index < page_count and 0 <= x < 1 and 0 <= y < 1 and .04 <= width <= 1 and .03 <= height <= 1 and x + width <= 1.0001 and y + height <= 1.0001):
            continue
        regions[key] = {
            "page_no": page_index + 1,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "auto_photo_anchor": True,
            "heading_source": "prior_ai_suggestion",
            "anchor_label": item.get("problem_no"),
        }
    return regions

def _crop_confirmed_region(file_path: str, mapping: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Crop one teacher-confirmed normalized rectangle without modifying the source page."""
    pages = _submission_page_paths(file_path)
    page_no = int(mapping["page_no"])
    if page_no < 1 or page_no > len(pages):
        raise ValueError("教师确认的页码不存在")
    try:
        with Image.open(pages[page_no - 1]) as source:
            # Match the EXIF-normalized coordinate space used by _load_images.
            image = ImageOps.exif_transpose(source).convert("RGB")
            full_width, full_height = image.size
            left = max(0, min(full_width - 1, round(float(mapping["x"]) * full_width)))
            top = max(0, min(full_height - 1, round(float(mapping["y"]) * full_height)))
            right = max(left + 1, min(full_width, round((float(mapping["x"]) + float(mapping["width"])) * full_width)))
            bottom = max(top + 1, min(full_height, round((float(mapping["y"]) + float(mapping["height"])) * full_height)))
            if right - left < 32 or bottom - top < 32:
                raise ValueError("教师确认的区域过小，无法可靠识别")
            cropped = image.crop((left, top, right, bottom))
            crop_rotation = int(mapping.get('orientation_correction_degrees') or 0)
            if crop_rotation in (90, 180, 270):
                # Candidate boxes are mapped back to original-page coordinates
                # for extraction, but rotated scans still need an upright OCR
                # crop. Rotate only the crop; never modify the stored original.
                cropped = cropped.rotate(-crop_rotation, expand=True)
            coloured_ink_pixels = sum(
                1 for red, green, blue in cropped.getdata()
                if blue - red >= 25 and blue - green >= 10 and blue < 250
            )
            buffer = io.BytesIO()
            cropped.save(buffer, format="PNG", optimize=True)
    except OSError as exc:
        raise ValueError("无法读取教师确认区域所在页图") from exc
    return base64.b64encode(buffer.getvalue()).decode("ascii"), {
        "mode": "auto_pdf_text_crop" if mapping.get("auto_pdf_anchor") else ("auto_photo_heading_crop" if mapping.get("auto_photo_anchor") else "teacher_confirmed_crop"),
        "coordinate_space": "exif_upright_v1",
        "page_no": page_no,
        "heading_source": mapping.get("heading_source"),
        "location_confidence": mapping.get("location_confidence"),
        "location_evidence": mapping.get("location_evidence"),
        "region": {key: float(mapping[key]) for key in ("x", "y", "width", "height")},
        "crop_size": {"width": right - left, "height": bottom - top},
        "orientation_correction_degrees": int(mapping.get('orientation_correction_degrees') or 0),
        "coloured_ink_pixels": coloured_ink_pixels,
    }


async def _extend_auto_crop_for_completion(
    file_path: str,
    row: dict[str, Any],
    problem: dict[str, Any],
    mapping: dict[str, Any],
    reference: dict[str, Any] | None,
    recognized_work: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str] | None, list[dict[str, Any]]]:
    """Recover a clipped parameter answer without admitting a neighbouring task.

    Most short answer-row crops need a small downward extension.  Phone photos
    rotated by 90/270 degrees need the analogous *source-page horizontal*
    extension: after rotation, a narrow full-height source strip is a short
    upright band and can cut off the final parameter line.  Every candidate
    remains one bounded region and must independently prove question identity,
    scope, and all explicitly requested parameter values.  The original upload
    and mapping are never changed here.
    """
    from .photo_semantic_locator import verify_recognized_work_scope

    prompt_text = str(problem.get("problem_text") or row.get("content") or "")
    coverage = _requested_parameter_coverage(prompt_text, recognized_work)
    task_coverage = _numbered_task_coverage(prompt_text, recognized_work)
    missing_parameters = bool(coverage.get("available") and coverage.get("missing"))
    missing_numbered_parts = bool(task_coverage.get("available") and task_coverage.get("missing"))
    attempts: list[dict[str, Any]] = []
    regions = expand_regions(mapping)
    if not ((missing_parameters or missing_numbered_parts) and len(regions) == 1):
        return None, None, None, attempts
    base = dict(regions[0])
    height = float(base.get("height") or 0)
    width = float(base.get("width") or 0)
    top = float(base.get("y") or 0)
    left = float(base.get("x") or 0)
    bottom = top + height
    right = left + width
    candidates: list[tuple[dict[str, Any], dict[str, float | str]]] = []

    # Ordinary short row: extend only downward by a few lines.
    if .04 <= height < .20 and bottom < .96:
        for extra in (.015, .025, .035, .045):
            expanded_bottom = min(.96, bottom + extra)
            candidates.append((
                {**base, "height": expanded_bottom - top},
                {"axis": "down", "bottom": round(expanded_bottom, 4)},
            ))

    # A 90/270-degree phone scan may be returned as a very tall but narrow
    # source-page crop.  In the upright view that is a shallow horizontal band;
    # shift its leading edge slightly past the previous question and grow only
    # toward the continuation.  This avoids blindly submitting the whole page.
    rotation = int(mapping.get("orientation_correction_degrees") or base.get("orientation_correction_degrees") or 0)
    if rotation in (90, 270) and height >= .30 and .04 <= width < .25 and right < .94:
        # Missing numbered parts normally means that a contiguous block of the
        # same answer was cut between its early and late subparts.  Probe the
        # smallest continuation first; the scope gate below rejects a window
        # as soon as it reaches the next numbered exercise.
        horizontal_steps = ((.02, .12), (.025, .14), (.03, .16)) if missing_parameters else ()
        if missing_numbered_parts:
            horizontal_steps += ((.01, .10), (.01, .16), (.015, .22), (.02, .28))
        seen_windows: set[tuple[float, float]] = set()
        for shift, extra in horizontal_steps:
            expanded_left = min(.94, left + shift)
            expanded_right = min(.94, right + extra)
            if expanded_right - expanded_left < width + .04:
                continue
            window_key = (round(expanded_left, 4), round(expanded_right, 4))
            if window_key in seen_windows:
                continue
            seen_windows.add(window_key)
            candidates.append((
                {**base, "x": expanded_left, "width": expanded_right - expanded_left},
                {"axis": "rotated_horizontal", "left": round(expanded_left, 4),
                 "right": round(expanded_right, 4)},
            ))

    if not candidates:
        return None, None, None, attempts
    for expanded, trace in candidates:
        try:
            payload, context = _crop_confirmed_region(file_path, {**mapping, **expanded})
            image_audit = await verify_crop_matches_problem([payload], {"problem_text": prompt_text})
            if not image_audit.get("matches"):
                attempts.append({**trace, "reason": "题区核验失败"})
                continue
            candidate = await grade_crops([payload], problem, reference)
            work = str(candidate.get("recognized_work") or "").strip()
            text_audit = await verify_recognized_work_matches_problem(
                {"problem_text": prompt_text}, work
            ) if work else {"matches": False, "reason": "扩展区域未识别到作答"}
            scope_audit = await verify_recognized_work_scope(
                {"problem_text": prompt_text}, work
            ) if work else {"matches": False, "reason": "扩展区域未识别到作答"}
            extended_coverage = _requested_parameter_coverage(prompt_text, work)
            extended_task_coverage = _numbered_task_coverage(prompt_text, work)
            if (text_audit.get("matches") and scope_audit.get("matches")
                    and not extended_coverage.get("missing")
                    and not extended_task_coverage.get("missing")):
                candidate["location_match_audit"] = image_audit
                candidate["recognition_task_audit"] = text_audit
                candidate["recognition_scope_audit"] = scope_audit
                attempts.append({**trace, "accepted": True})
                candidate["answer_completion_extension"] = attempts
                return candidate, {**context, "regions": [context], "page_nos": [context["page_no"]],
                                   "region_count": 1, "answer_completion_extension": attempts}, [payload], attempts
            attempts.append({**trace,
                             "reason": "作答仍不完整或混入其他题",
                             "coverage": extended_coverage,
                             "task_coverage": extended_task_coverage,
                             "scope_matches": scope_audit.get("matches")})
        except Exception as exc:
            attempts.append({**trace, "reason": type(exc).__name__})
    return None, None, None, attempts


async def _recover_auto_photo_crop_after_identity_failure(
    file_path: str,
    row: dict[str, Any],
    problem: dict[str, Any],
    original_mapping: dict[str, Any],
    reference: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str] | None, dict[str, Any]]:
    """Search a different answer region after OCR is blank or belongs to another task.

    A plausible image-only match is not enough: the replacement must pass both
    crop-to-question and transcription-to-question checks before it can become
    grading evidence. The original answer image is never changed.
    """
    from .photo_semantic_locator import (
        detect_two_column_regions, locate_by_content, map_column_region_to_page,
        verify_recognized_work_scope,
    )

    try:
        source_pages = _load_images(file_path)
        locator_pages, rotations = await orient_locator_pages(source_pages)
        excluded_original = expand_regions(original_mapping)
        # A coarse heading-to-bottom box can contain the actual answer plus a
        # neighbouring question. Masking that entire box would make recovery
        # impossible. It is already rejected by the identity gate, so for a
        # clearly oversized single crop we keep the locator page visible and
        # merely reject an identical replacement later in the normal audits.
        if len(excluded_original) == 1:
            raw = excluded_original[0]
            raw_area = float(raw.get("width") or 0) * float(raw.get("height") or 0)
            if (raw_area >= .26 or float(raw.get("width") or 0) >= .92
                    or float(raw.get("height") or 0) >= .42):
                excluded_original = []
        inverse_rotations = [(-int(angle)) % 360 for angle in rotations]
        excluded_for_locator = []
        if excluded_original:
            excluded_for_locator = restore_region_coordinates(
                {"excluded": {"regions": excluded_original}}, inverse_rotations
            )["excluded"]["regions"]

        # /locate-homework returns its best region for a number. Repeating the
        # same request over unchanged pages therefore tends to return the same
        # bad box. Mask only that rejected rectangle in the *locator payload*
        # and ask again; the stored upload and grading crop remain untouched.
        masked_locator_pages = []
        try:
            from PIL import ImageDraw
            excluded_by_page: dict[int, list[dict[str, Any]]] = {}
            for region in excluded_for_locator:
                excluded_by_page.setdefault(int(region.get("page_no") or 1), []).append(region)
            for page_no, encoded in enumerate(locator_pages, 1):
                with Image.open(io.BytesIO(base64.b64decode(encoded))) as source:
                    page = source.convert("RGB")
                    draw = ImageDraw.Draw(page)
                    page_width, page_height = page.size
                    for region in excluded_by_page.get(page_no, []):
                        left = max(0, int((float(region["x"]) - .008) * page_width))
                        top = max(0, int((float(region["y"]) - .008) * page_height))
                        right = min(page_width - 1, int((float(region["x"]) + float(region["width"]) + .008) * page_width))
                        bottom = min(page_height - 1, int((float(region["y"]) + float(region["height"]) + .008) * page_height))
                        draw.rectangle((left, top, right, bottom), fill="white")
                    buffer = io.BytesIO()
                    page.save(buffer, format="PNG", optimize=True)
                    masked_locator_pages.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
        except Exception:
            # Tests and unsupported image payloads fall back to the unmodified
            # locator request; no error may ever alter the original file.
            masked_locator_pages = locator_pages
        alternative = await locate_by_content(
            masked_locator_pages,
            [row],
            exclude_regions={int(row["question_id"]): excluded_for_locator},
        )
        alternative = restore_region_coordinates(alternative, rotations)
        replacements = []

        def overlaps_original(candidate: dict[str, Any]) -> bool:
            for left in expand_regions(candidate):
                for right in expand_regions(original_mapping):
                    if int(left.get("page_no") or 0) != int(right.get("page_no") or 0):
                        continue
                    ix = max(0.0, min(float(left["x"]) + float(left["width"]),
                                      float(right["x"]) + float(right["width"]))
                             - max(float(left["x"]), float(right["x"])))
                    iy = max(0.0, min(float(left["y"]) + float(left["height"]),
                                      float(right["y"]) + float(right["height"]))
                             - max(float(left["y"]), float(right["y"])))
                    intersection = ix * iy
                    union = (float(left["width"]) * float(left["height"])
                             + float(right["width"]) * float(right["height"]) - intersection)
                    if union > 0 and intersection / union >= .72:
                        return True
            return False

        semantic_replacement = (alternative or {}).get(int(row["question_id"]))
        if semantic_replacement:
            replacements.append((semantic_replacement, "semantic_content"))

        # Whole-page semantic localization can mistake one column for another
        # when printed prompts or handwritten answers share the same sheet.
        # Re-run against independently cropped columns, then map every returned
        # box back to oriented page coordinates before the normal identity gate.
        column_candidate_count = 0
        column_locator_errors = 0
        column_error_types = []
        for page_index, encoded in enumerate(locator_pages):
            columns = detect_two_column_regions(encoded)
            if not columns:
                continue
            try:
                with Image.open(io.BytesIO(base64.b64decode(encoded))) as page:
                    page = page.convert("RGB")
                    page_width, page_height = page.size
                    page_no = page_index + 1
                    for bounds in columns:
                        # Whole-column prompts can contain several exercises;
                        # search overlapping vertical bands as well so one
                        # neighboring answer cannot dominate the entire column.
                        for y_start, y_end in ((.02, .38), (.32, .68), (.62, .98)):
                            tile_bounds = {"x": bounds["x"], "y": y_start,
                                           "width": bounds["width"], "height": y_end-y_start}
                            box = (round(tile_bounds["x"]*page_width), round(tile_bounds["y"]*page_height),
                                   round((tile_bounds["x"]+tile_bounds["width"])*page_width),
                                   round((tile_bounds["y"]+tile_bounds["height"])*page_height))
                            tile = page.crop(box)
                            buffer = io.BytesIO()
                            tile.save(buffer, format="PNG", optimize=True)
                            tile_payload = base64.b64encode(buffer.getvalue()).decode("ascii")
                            column_result = await locate_by_content([tile_payload], [row])
                            column_mapping = (column_result or {}).get(int(row["question_id"]))
                            if not column_mapping:
                                continue
                            mapped_regions = []
                            try:
                                for tile_region in expand_regions(column_mapping):
                                    mapped_regions.append(map_column_region_to_page(
                                        tile_region, tile_bounds, page_no
                                    ))
                            except (TypeError, ValueError, KeyError):
                                continue
                            if not mapped_regions:
                                continue
                            mapped_regions.sort(key=lambda region: (region["page_no"], region["y"], region["x"]))
                            first = mapped_regions[0]
                            candidate = {
                                **first, "regions": mapped_regions,
                                "heading_source": "qwen35_two_column_semantic",
                                "auto_photo_anchor": True,
                                "location_confidence": column_mapping.get("location_confidence"),
                                "location_evidence": column_mapping.get("location_evidence"),
                            }
                            if overlaps_original(candidate):
                                continue
                            candidate = restore_region_coordinates({"candidate": candidate}, rotations)["candidate"]
                            replacements.append((candidate, "two_column_semantic"))
                            column_candidate_count += 1
            except Exception as column_exc:
                column_locator_errors += 1
                column_error_types.append(type(column_exc).__name__ + ": " + str(column_exc)[:140])

        # The semantic model can miss the correct answer after excluding a
        # bad box. Retry using exact handwritten question-number rectangles as
        # independent candidates; do not accept them based on number alone.
        # Each crop must still pass image identity, OCR, and text identity.
        raw_no = str(row.get("original_no") or row.get("problem_no") or "")
        number_match = re.search(r"(\d+)\s*(?:题)?\s*$", raw_no)
        number_candidate_count = 0
        number_candidate_excluded = 0
        layout_candidate_count = 0
        number_locator_error = None
        if number_match:
            endpoint = settings.qwen_grading_url.rsplit("/", 1)[0] + "/locate-homework"
            try:
                async with httpx.AsyncClient(timeout=180) as client:
                    response = await client.post(endpoint, json={
                        "images_base64": masked_locator_pages,
                        "problem_nos": [str(int(number_match.group(1)))],
                    })
                    response.raise_for_status()
                    located = response.json()
                for item in located.get("candidates") or []:
                    try:
                        candidate_number = int(item.get("number", -1))
                    except (TypeError, ValueError):
                        continue
                    if candidate_number != int(number_match.group(1)):
                        continue
                    number_candidate_count += 1
                    try:
                        region = {
                            "page_no": int(item["page_index"]) + 1,
                            "x": float(item["x"]), "y": float(item["y"]),
                            "width": float(item["width"]), "height": float(item["height"]),
                        }
                    except (KeyError, TypeError, ValueError):
                        continue
                    candidate = {"regions": [region], **region,
                                 "heading_source": "retry_numbered_answer_region",
                                 "auto_photo_anchor": True}
                    # Exclude near-identical locations; the original box was
                    # already rejected and must not simply be re-used.
                    candidate = restore_region_coordinates({"candidate": candidate}, rotations)["candidate"]
                    if overlaps_original(candidate):
                        number_candidate_excluded += 1
                        continue
                    replacements.append((candidate, "numbered_layout"))
                # Older number/layout responses contain only the detected row
                # (page + y), not an answer rectangle. Use the next physical
                # row as a bounded vertical stop instead of silently throwing
                # these genuine anchors away. When a central page gutter is
                # visible, restrict the crop to the scanned left column; the
                # OCR/task gate still decides whether it is the right answer.
                for item in located.get("candidates") or []:
                    try:
                        candidate_number = int(item.get("number", -1))
                        page_index = int(item["page_index"])
                        heading_y = float(item["y"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if candidate_number != int(number_match.group(1)) or not 0 <= page_index < len(locator_pages):
                        continue
                    if all(key in item for key in ("x", "y", "width", "height")):
                        continue
                    page_no = page_index + 1
                    rows = sorted(float(row_item["y"]) for row_item in located.get("layout_rows") or []
                                  if int(row_item.get("page_index", -1)) == page_index
                                  and float(row_item.get("y", 0)) > heading_y + .04)
                    top = max(0.0, heading_y - .025)
                    bottom = (max(top + .06, rows[0] - .015) if rows else min(.96, top + .42))
                    bottom = min(bottom, top + .46, .98)
                    if bottom - top < .055:
                        continue
                    x, width = .02, .96
                    try:
                        with Image.open(io.BytesIO(base64.b64decode(locator_pages[page_index]))) as page:
                            grey = page.convert("L")
                            page_width, page_height = grey.size
                            center_left, center_right = int(page_width * .40), int(page_width * .60)
                            densities = []
                            for col in range(center_left, max(center_left + 1, center_right)):
                                sample = grey.crop((col, 0, col + 1, page_height))
                                densities.append(sum(1 for value in sample.getdata() if value < 205) / page_height)
                            if densities and min(densities) < .012:
                                x, width = .02, .45
                    except Exception:
                        pass
                    candidate = {"page_no": page_no, "x": x, "y": top,
                                 "width": width, "height": bottom - top,
                                 "regions": [{"page_no": page_no, "x": x, "y": top,
                                              "width": width, "height": bottom - top}],
                                 "heading_source": "retry_numbered_layout_rows",
                                 "auto_photo_anchor": True}
                    if overlaps_original(candidate):
                        number_candidate_excluded += 1
                        continue
                    replacements.append((candidate, "numbered_layout_rows"))
                    layout_candidate_count += 1
            except Exception as locator_exc:
                number_locator_error = type(locator_exc).__name__
        if not replacements:
            return None, None, None, {
                "attempted": True, "reason": "未找到不同的替代作答区域",
                "numbered_candidates": number_candidate_count,
                "numbered_candidates_excluded_as_duplicate": number_candidate_excluded,
                "layout_row_candidates": layout_candidate_count,
                "two_column_candidates": column_candidate_count,
                "two_column_errors": column_locator_errors,
                "two_column_error_types": column_error_types[:3],
                "number_locator_error": number_locator_error,
            }

        failures = []
        verified_candidates = []
        for replacement, replacement_source in replacements:
            try:
                pieces = [
                    _crop_confirmed_region(file_path, {**replacement, **region})
                    for region in expand_regions(replacement)
                ]
                payloads = [piece[0] for piece in pieces]
                image_audit = await verify_crop_matches_problem(
                    payloads, {"problem_text": str(problem.get("problem_text") or row.get("content") or "")}
                )
                if not image_audit.get("matches"):
                    failures.append({"source": replacement_source, "reason": "题区核验失败", "image_audit": image_audit})
                    continue

                candidate = await grade_crops(payloads, problem, reference)
                recognized = str(candidate.get("recognized_work") or "").strip()
                if not recognized:
                    failures.append({"source": replacement_source, "reason": "替代区域仍无法识别", "image_audit": image_audit})
                    continue
                text_audit = await verify_recognized_work_matches_problem(
                    {"problem_text": str(problem.get("problem_text") or row.get("content") or "")},
                    recognized,
                )
                if not text_audit.get("matches"):
                    failures.append({"source": replacement_source, "reason": "转写与题干不一致",
                                     "image_audit": image_audit, "text_audit": text_audit})
                    continue
                scope_audit = await verify_recognized_work_scope(
                    {"problem_text": str(problem.get("problem_text") or row.get("content") or "")},
                    recognized,
                )
                if not scope_audit.get("matches"):
                    failures.append({"source": replacement_source,
                                     "reason": "候选区域可能混入其他题，保留待复核",
                                     "image_audit": image_audit, "text_audit": text_audit,
                                     "scope_audit": scope_audit})
                    continue
                contexts = [piece[1] for piece in pieces]

                # A short answer-row crop may stop just before the requested
                # final value.  Search a few small downward extensions only
                # when the prompt explicitly asks for named parameters and
                # the OCR has not found all of them.  Each extension repeats
                # image identity, transcript identity and mixed-task checks.
                # A wider crop is never used merely because it produced a
                # convenient answer.
                parameter_coverage = _requested_parameter_coverage(
                    str(problem.get("problem_text") or row.get("content") or ""), recognized
                )
                expansion_attempts = []
                if (parameter_coverage.get("available") and parameter_coverage.get("missing")
                        and len(pieces) == 1):
                    base_region = dict(expand_regions(replacement)[0])
                    base_height = float(base_region.get("height") or 0)
                    base_bottom = float(base_region.get("y") or 0) + base_height
                    if .04 <= base_height < .20 and base_bottom < .96:
                        for extra in (.015, .025, .035, .045):
                            bottom = min(.96, base_bottom + extra)
                            if bottom <= base_bottom + .009:
                                continue
                            expanded_region = {**base_region, "height": bottom - float(base_region["y"])}
                            try:
                                expanded_piece = _crop_confirmed_region(
                                    file_path, {**replacement, **expanded_region}
                                )
                                expanded_payload = expanded_piece[0]
                                expanded_image_audit = await verify_crop_matches_problem(
                                    [expanded_payload],
                                    {"problem_text": str(problem.get("problem_text") or row.get("content") or "")},
                                )
                                if not expanded_image_audit.get("matches"):
                                    expansion_attempts.append({"bottom": round(bottom, 4), "reason": "题区核验失败"})
                                    continue
                                expanded_candidate = await grade_crops([expanded_payload], problem, reference)
                                expanded_work = str(expanded_candidate.get("recognized_work") or "").strip()
                                expanded_text_audit = await verify_recognized_work_matches_problem(
                                    {"problem_text": str(problem.get("problem_text") or row.get("content") or "")},
                                    expanded_work,
                                ) if expanded_work else {"matches": False, "reason": "替代区域仍无法识别"}
                                expanded_scope_audit = await verify_recognized_work_scope(
                                    {"problem_text": str(problem.get("problem_text") or row.get("content") or "")},
                                    expanded_work,
                                ) if expanded_work else {"matches": False, "reason": "替代区域仍无法识别"}
                                expanded_coverage = _requested_parameter_coverage(
                                    str(problem.get("problem_text") or row.get("content") or ""), expanded_work
                                )
                                if (expanded_text_audit.get("matches") and expanded_scope_audit.get("matches")
                                        and not expanded_coverage.get("missing")):
                                    replacement = {**replacement, **expanded_region, "regions": [expanded_region]}
                                    replacement_source += "_complete_extension"
                                    payloads, contexts = [expanded_payload], [expanded_piece[1]]
                                    candidate, recognized = expanded_candidate, expanded_work
                                    image_audit = expanded_image_audit
                                    text_audit, scope_audit = expanded_text_audit, expanded_scope_audit
                                    parameter_coverage = expanded_coverage
                                    expansion_attempts.append({"bottom": round(bottom, 4), "accepted": True})
                                    break
                                expansion_attempts.append({
                                    "bottom": round(bottom, 4),
                                    "reason": "作答仍不完整或混入其他题",
                                    "coverage": expanded_coverage,
                                    "scope_matches": expanded_scope_audit.get("matches"),
                                })
                            except Exception as expansion_exc:
                                expansion_attempts.append({"bottom": round(bottom, 4),
                                                           "reason": type(expansion_exc).__name__})
                candidate["recognition_task_audit"] = text_audit
                candidate["location_match_audit"] = image_audit
                candidate["recognition_scope_audit"] = scope_audit
                if expansion_attempts:
                    candidate["answer_completion_extension"] = expansion_attempts
                candidate["previous_candidate_rejected"] = True
                context = {
                    **contexts[0], "regions": contexts,
                    "page_nos": [item["page_no"] for item in contexts],
                    "region_count": len(contexts),
                    "previous_candidate_rejected": True,
                    "previous_candidate_identity_failed": True,
                    "location_match_audit": image_audit,
                    "recognition_task_audit": text_audit,
                    "recognition_scope_audit": scope_audit,
                    "replacement_source": replacement_source,
                }
                verified_candidates.append((candidate, context, payloads, image_audit,
                                            text_audit, scope_audit, replacement_source))
                # Keep searching. A parent question can continue below the
                # first recognized subpart, including in neighboring bands.
                continue
            except Exception as retry_exc:
                failures.append({"source": replacement_source,
                                 "reason": type(retry_exc).__name__ + ": " + str(retry_exc)[:140]})
        if verified_candidates:
            if len(verified_candidates) == 1:
                candidate, context, payloads, image_audit, text_audit, scope_audit, source = verified_candidates[0]
                return candidate, context, payloads, {
                    "attempted": True, "recovered": True, "source": source,
                    "image_audit": image_audit, "text_audit": text_audit,
                    "scope_audit": scope_audit,
                }
            combined_contexts = []
            combined_payloads = []
            selected_regions = []
            selected_image_audits = []
            for candidate, context, payloads, image_audit, text_audit, scope_audit, source in verified_candidates:
                candidate_regions = context.get("regions") or []
                added = False
                for region, payload in zip(candidate_regions, payloads):
                    geometry = region.get("region") or region
                    area = max(1e-9, float(geometry["width"])*float(geometry["height"]))
                    duplicate = False
                    for existing in selected_regions:
                        if int(geometry.get("page_no") or region.get("page_no") or 0) != int(existing.get("page_no") or 0):
                            continue
                        ix = max(0.0, min(float(geometry["x"])+float(geometry["width"]),
                                          float(existing["x"])+float(existing["width"]))
                                 - max(float(geometry["x"]), float(existing["x"])))
                        iy = max(0.0, min(float(geometry["y"])+float(geometry["height"]),
                                          float(existing["y"])+float(existing["height"]))
                                 - max(float(geometry["y"]), float(existing["y"])))
                        existing_area = max(1e-9, float(existing["width"])*float(existing["height"]))
                        if ix*iy/min(area, existing_area) > .72:
                            duplicate = True
                            break
                    if duplicate:
                        continue
                    combined_contexts.append(region)
                    combined_payloads.append(payload)
                    selected_regions.append({**geometry, "page_no":
                                             geometry.get("page_no") or region.get("page_no")})
                    added = True
                if added:
                    selected_image_audits.append(image_audit)
            if combined_payloads:
                try:
                    combined_candidate = await grade_crops(combined_payloads, problem, reference)
                    combined_work = str(combined_candidate.get("recognized_work") or "").strip()
                    combined_text_audit = await verify_recognized_work_matches_problem(
                        {"problem_text": str(problem.get("problem_text") or row.get("content") or "")},
                        combined_work,
                    )
                    combined_scope_audit = await verify_recognized_work_scope(
                        {"problem_text": str(problem.get("problem_text") or row.get("content") or "")},
                        combined_work,
                    )
                    if combined_text_audit.get("matches") and combined_scope_audit.get("matches"):
                        combined_context = {
                            **combined_contexts[0], "regions": combined_contexts,
                            "page_nos": sorted({item["page_no"] for item in combined_contexts}),
                            "region_count": len(combined_contexts),
                            "previous_candidate_rejected": True,
                            "previous_candidate_identity_failed": True,
                            "location_match_audit": selected_image_audits,
                            "recognition_task_audit": combined_text_audit,
                            "recognition_scope_audit": combined_scope_audit,
                            "replacement_source": "verified_multi_crop",
                        }
                        combined_candidate["location_match_audit"] = selected_image_audits
                        combined_candidate["recognition_task_audit"] = combined_text_audit
                        combined_candidate["recognition_scope_audit"] = combined_scope_audit
                        combined_candidate["previous_candidate_rejected"] = True
                        return combined_candidate, combined_context, combined_payloads, {
                            "attempted": True, "recovered": True,
                            "source": "verified_multi_crop",
                            "region_count": len(combined_contexts),
                            "image_audits": selected_image_audits,
                            "text_audit": combined_text_audit,
                            "scope_audit": combined_scope_audit,
                        }
                    failures.append({"source": "verified_multi_crop",
                                     "reason": "合并区域未通过题目或混题核验",
                                     "text_audit": combined_text_audit,
                                     "scope_audit": combined_scope_audit})
                except Exception as combine_exc:
                    failures.append({"source": "verified_multi_crop",
                                     "reason": type(combine_exc).__name__ + ": " + str(combine_exc)[:140]})
        return None, None, None, {
            "attempted": True, "reason": "所有替代区域均未通过身份与OCR核验",
            "two_column_candidates": column_candidate_count,
            "two_column_errors": column_locator_errors,
            "two_column_error_types": column_error_types[:3],
            "failures": failures,
        }
    except Exception as exc:
        return None, None, None, {
            "attempted": True,
            "reason": "替代定位/识别失败：" + type(exc).__name__ + ": " + str(exc)[:160],
        }


async def grade_submission(submission_id: int, only_question_id: int | None = None) -> dict[str, Any]:
    """Retrieve assignment evidence, call Qwen, then cross-check each calc answer."""
    with connection() as conn:
        submission = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if not submission:
            raise ValueError("提交不存在")
        parents = [dict(row) for row in conn.execute(
            """SELECT aq.question_id, aq.sort_order, aq.score AS max_score, aq.original_no,
                      q.*,ro.rubric AS assignment_rubric
               FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id
               LEFT JOIN assignment_question_rubric_overrides ro
                 ON ro.assignment_id=aq.assignment_id AND ro.question_id=aq.question_id
               WHERE aq.assignment_id=? ORDER BY aq.sort_order""",
            (submission["assignment_id"],),
        ).fetchall()]
        selected_parts = [dict(row) for row in conn.execute(
            """SELECT question_id,subpart_no,part_order,content,answer,rubric,score
               FROM assignment_question_parts WHERE assignment_id=?
               ORDER BY question_id,part_order""",
            (submission["assignment_id"],),
        ).fetchall()]

    if only_question_id is not None:
        parents = [row for row in parents if int(row["question_id"]) == int(only_question_id)]
        selected_parts = [row for row in selected_parts if int(row["question_id"]) == int(only_question_id)]
        if not parents:
            raise ValueError("该作业未关联指定题目")

    if not parents:
        raise ValueError("该作业未关联题目")
    parts_by_parent: dict[int, list[dict]] = {}
    for part in selected_parts:
        parts_by_parent.setdefault(part["question_id"], []).append(part)
    questions: list[dict] = []
    for parent in parents:
        if parent.get("assignment_rubric"):
            parent["rubric"] = parent["assignment_rubric"]
        parts = parts_by_parent.get(parent["question_id"], [])
        if not parts:
            parent["grading_key"] = str(parent["question_id"])
            parent["subpart_no"] = None
            parent["problem_no"] = str(parent["original_no"] or parent["sort_order"])
            questions.append(parent)
            continue
        for part in parts:
            questions.append({
                **parent, **part,
                "max_score": float(part["score"]),
                "grading_key": f"{parent['question_id']}:{part['subpart_no']}",
                "problem_no": f"{parent['original_no'] or parent['sort_order']}（{part['subpart_no']}）",
            })
    rubric_audits: dict[str, dict[str, Any]] = {}
    for row in questions:
        row['answer'] = scope_reference(row.get('answer'), row.get('problem_no'))
        row['rubric'] = scope_reference(row.get('rubric'), row.get('problem_no'))
        rubric_audits[row["grading_key"]] = audit_rubric(
            row["content"], row["answer"] or "", row["rubric"] or "", float(row["max_score"])
        )
    problems = [
        {
            "problem_id": row["grading_key"],
            "problem_no": row["problem_no"],
            "problem_text": row["content"],
            "question_type": normalize_question_type(row["question_type"]),
            "std_answer": row["answer"] or "",
            "full_solution": rubric_audits[row["grading_key"]]["effective_solution"],
            "max_score": float(row["max_score"]),
        }
        for row in questions
    ]

    # A manual rectangle is strongest evidence, but an AI-detected photo
    # rectangle is enough for an initial candidate score.  Both remain subject
    # to the same teacher-review gate before any result is released.
    with connection() as conn:
        region_rows = [dict(item) for item in conn.execute(
            """SELECT question_id,subpart_no,sort_order,page_no,x,y,width,height,confirmed_at,regions_json
               FROM submission_question_regions WHERE submission_id=?""", (submission_id,)
        ).fetchall()]
    regions = {(item["question_id"], item["subpart_no"] or "", item["sort_order"]): public_mapping(item) for item in region_rows}
    with connection() as conn:
        exclusion_rows = conn.execute(
            "SELECT question_id,subpart_no,sort_order FROM submission_region_exclusions WHERE submission_id=?",
            (submission_id,),
        ).fetchall()
    excluded_region_keys = {(int(item["question_id"]), str(item["subpart_no"] or ""), int(item["sort_order"])) for item in exclusion_rows}
    locator_questions = [
        item for item in questions
        if (int(item["question_id"]), str(item.get("subpart_no") or ""), int(item["sort_order"])) not in regions
        and (int(item["question_id"]), str(item.get("subpart_no") or ""), int(item["sort_order"])) not in excluded_region_keys
    ]
    auto_pdf_regions = _infer_pdf_question_regions(submission["file_path"], locator_questions) if locator_questions else {}
    # A PDF text layer may expose only one or two question anchors.  Treat that
    # as a useful first pass, not a reason to skip photo/VLM segmentation for
    # every remaining handwritten answer.
    expected_question_ids = {int(item["question_id"]) for item in locator_questions}
    covered_question_ids = {int(key[0]) for key in auto_pdf_regions}
    auto_photo_regions: dict[tuple[int, str, int], dict[str, Any]] = {}
    florence_enabled = os.environ.get("MATH_FLORENCE_REGION_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    if locator_questions and (covered_question_ids != expected_question_ids or florence_enabled):
        auto_photo_regions = await _infer_photo_question_regions(submission["file_path"], locator_questions)
    auto_regions = dict(auto_pdf_regions)
    for key, mapping in auto_photo_regions.items():
        # Florence may refine the boundary of a Qwen-assigned question even
        # when a PDF text anchor exists; otherwise retain the deterministic PDF box.
        if mapping.get("heading_source") == "florence2_geometry_qwen_question_map":
            auto_regions[key] = mapping
        else:
            auto_regions.setdefault(key, mapping)
    prior_ai_regions = _prior_ai_suggested_regions(
        submission_id, questions, len(_submission_page_paths(submission["file_path"]))
    )
    for key in excluded_region_keys:
        prior_ai_regions.pop(key, None)
        auto_regions.pop(key, None)
    for key, mapping in auto_regions.items():
        existing = regions.get(key)
        if not existing:
            regions[key] = mapping
    for key, mapping in prior_ai_regions.items():
        if key not in regions:
            regions[key] = mapping
    is_photo_locator = any(
        item.get("auto_photo_anchor") for item in (*auto_photo_regions.values(), *prior_ai_regions.values())
    )
    qwen_by_question: dict[str, dict[str, Any]] = {}
    qwen_context_by_question: dict[str, dict[str, Any]] = {}
    crop_payload_by_question: dict[str, list[str]] = {}
    crop_errors: dict[str, str] = {}
    qwen_errors: list[str] = []
    unconfirmed: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def request_qwen(images: list[str], requested_problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return await grade_homework(images, requested_problems)

    for row, problem in zip(questions, problems):
        qid = row["grading_key"]
        region_key = (row["question_id"], row.get("subpart_no") or "", row["sort_order"])
        mapping = regions.get(region_key)
        # A default full-page rectangle is only a UI placeholder, not proof
        # that the teacher identified this particular question.  Crop only a
        # genuinely boxed region; otherwise preserve the full-page locator.
        is_confirmed_crop = bool(mapping) and (mapping.get('confirmed_at') or mapping.get('regions') or
            float(mapping.get("width", 1)) < 0.999 or float(mapping.get("height", 1)) < 0.999
        )
        if not is_confirmed_crop:
            unconfirmed.append((row, problem))
            continue
        try:
            pieces = [_crop_confirmed_region(submission['file_path'], {**mapping, **region})
                      for region in expand_regions(mapping)]
            crop_payloads = [piece[0] for piece in pieces]
            crop_payload = crop_payloads[0]
            crop_context = {**pieces[0][1], 'regions': [piece[1] for piece in pieces],
                            'page_nos': [piece[1]['page_no'] for piece in pieces],
                            'region_count': len(pieces)}
            if mapping.get('auto_photo_anchor'):
                # A plausible box/heading is not proof that the handwriting is
                # for this task. Verify task-specific content before OCR or
                # grading; uncertainty must remain review-only.
                match_audit = await verify_crop_matches_problem(crop_payloads, problem)
                crop_context['location_match_audit'] = match_audit
                if not match_audit.get('matches'):
                    reference = await complete_reference(problem, rubric_audits[qid])
                    replacement, replacement_context, replacement_payloads, retry_audit = (
                        await _recover_auto_photo_crop_after_identity_failure(
                            submission['file_path'], row, problem, mapping, reference
                        )
                    )
                    if replacement is not None:
                        replacement['alternative_search_audit'] = retry_audit
                        replacement['previous_location_match_audit'] = match_audit
                        qwen_by_question[qid] = replacement
                        qwen_context_by_question[qid] = replacement_context or crop_context
                        crop_payload_by_question[qid] = replacement_payloads or crop_payloads
                    else:
                        qwen_by_question[qid] = {
                            'score': 0, 'max_score': float(row['max_score']),
                            'correct': None, 'answer_present': None,
                            'recognized_work': '', 'need_review': True, 'confidence': 0,
                            'feedback': '候选题区与题干不一致，已排除该框并尝试语义及题号定位；仍无通过OCR核验的作答区。',
                            'location_match_rejected': True,
                            'alternative_search_audit': retry_audit,
                        }
                        crop_context['alternative_search_audit'] = retry_audit
                        qwen_context_by_question[qid] = crop_context
                        crop_payload_by_question[qid] = crop_payloads
                    continue
            crop_notice = ("【照片题号自动定位：此图是当前题的候选作答区，须识别实际笔迹，不能凭参考答案补写。】\n"
                           if mapping.get("auto_photo_anchor") else
                           "【教师已确认：本图片仅包含当前题目/小问的作答区域；不要再猜测或匹配其他题号。】\n")
            confirmed_problem = {
                **problem,
                "problem_text": crop_notice + str(problem.get("problem_text") or ""),
            }
            # Teacher crops bypass a second locator. OCR is independent of the
            # reference and the fresh score always uses this exact OCR evidence.
            if mapping.get('confirmed_at') or len(crop_payloads) > 1 or mapping.get('auto_photo_anchor'):
                reference = await complete_reference(problem, rubric_audits[qid])
                fresh = await grade_crops(crop_payloads, problem, reference)
                if mapping.get('auto_photo_anchor'):
                    first_work = str(fresh.get('recognized_work') or '').strip()
                    first_text_audit = None
                    first_scope_audit = None
                    if first_work:
                        first_text_audit = await verify_recognized_work_matches_problem(
                            {"problem_text": str(row.get("content") or "")}, first_work
                        )
                        from .photo_semantic_locator import verify_recognized_work_scope
                        first_scope_audit = await verify_recognized_work_scope(
                            {"problem_text": str(row.get("content") or "")}, first_work
                        )
                    if (first_work and (first_text_audit or {}).get('matches')
                            and (first_scope_audit or {}).get('matches')):
                        extended, extended_context, extended_payloads, extension_attempts = (
                            await _extend_auto_crop_for_completion(
                                submission['file_path'], row, problem, mapping, reference, first_work
                            )
                        )
                        if extended is not None:
                            fresh = extended
                            crop_context = extended_context or crop_context
                            crop_payloads = extended_payloads or crop_payloads
                            crop_payload = crop_payloads[0]
                            first_work = str(fresh.get('recognized_work') or '').strip()
                            first_text_audit = fresh.get('recognition_task_audit')
                            first_scope_audit = fresh.get('recognition_scope_audit')
                        elif extension_attempts:
                            fresh['answer_completion_extension'] = extension_attempts
                    replacement, replacement_context, replacement_payloads, retry_audit = (None, None, None, {})
                    if (not first_work or not (first_text_audit or {}).get('matches')
                            or not (first_scope_audit or {}).get('matches')):
                        replacement, replacement_context, replacement_payloads, retry_audit = (
                            await _recover_auto_photo_crop_after_identity_failure(
                                submission['file_path'], row, problem, mapping, reference
                            )
                        )
                    if retry_audit:
                        fresh['alternative_search_audit'] = retry_audit
                        fresh['recognition_task_audit'] = first_text_audit
                        fresh['recognition_scope_audit'] = first_scope_audit
                    if replacement is not None:
                        fresh = replacement
                        crop_context = replacement_context or crop_context
                        crop_payloads = replacement_payloads or crop_payloads
                        crop_payload = crop_payloads[0]
                    elif retry_audit:
                        fresh['score'] = 0
                        fresh['step_scores'] = []
                        fresh['correct'] = False if first_work else None
                        fresh['answer_present'] = None
                        fresh['need_review'] = True
                        fresh['confidence'] = min(float(fresh.get('confidence') or 0), 0.60)
                        if first_work:
                            fresh['recognized_work'] = ''
                            fresh['location_match_rejected'] = True
                        fresh.setdefault('risks', []).append(
                            '初始自动题区为空或与题目不符，已排除原框搜索替代区；仍需教师核对'
                        )
                qwen_by_question[qid] = fresh
                qwen_context_by_question[qid] = crop_context
                crop_payload_by_question[qid] = crop_payloads
                continue
            qwen_items = await request_qwen(crop_payloads, [confirmed_problem])
            qwen_by_question[qid] = next(
                (item for item in qwen_items if str(item.get("problem_id")) == qid), {}
            )
            candidate = qwen_by_question[qid]
            candidate_work = str(candidate.get("recognized_work") or "")
            if not candidate_work.strip() or _recognition_is_contaminated(candidate_work, str(row.get("answer") or "")):
                # Recompute the score, not merely its displayed transcription.
                reference = await complete_reference(problem, rubric_audits[qid])
                candidate = await grade_crops(crop_payloads, problem, reference)
                qwen_by_question[qid] = candidate
                candidate_work = str(candidate.get('recognized_work') or '')
            if not candidate_work.strip():
                try:
                    endpoint = settings.qwen_grading_url.rsplit("/", 1)[0] + "/read-work-crop"
                    async with httpx.AsyncClient(timeout=120) as client:
                        ocr_response = await client.post(endpoint, json={"image_base64": crop_payload})
                        ocr_response.raise_for_status()
                        independent_work = str(ocr_response.json().get("text") or "").strip()
                    if (
                        len(independent_work) >= 12
                        and not _recognition_has_runaway_repetition(independent_work)
                        and not any(
                            marker in independent_work[:40] for marker in ("无法识别", "没有作答", "未见作答", "无法辨认")
                        )
                    ):
                        candidate["recognized_work"] = independent_work
                        candidate["independent_ocr"] = True
                        candidate["need_review"] = True
                        candidate.setdefault("risks", []).append("已使用不含参考答案的独立笔迹转写；请结合原图核对")
                    elif _recognition_has_runaway_repetition(independent_work):
                        collapsed_work = _collapse_repeated_recognition(independent_work)
                        if len(collapsed_work) >= 12:
                            # Preserve the non-repeated portion as teacher-facing
                            # OCR evidence, but keep the result under review and
                            # never infer a score from the unstable continuation.
                            candidate["recognized_work"] = collapsed_work
                            candidate["independent_ocr"] = True
                            candidate["need_review"] = True
                        candidate.setdefault("risks", []).append(
                            "独立笔迹转写出现重复输出，已去重保留可读片段；本题分数须教师结合原图复核"
                        )
                except Exception:
                    candidate.setdefault("risks", []).append("独立笔迹重读失败，仍需结合原图复核")
            qwen_context_by_question[qid] = crop_context
            crop_payload_by_question[qid] = crop_payloads
        except Exception as exc:
            crop_errors[qid] = str(exc)[:180]
            if mapping and mapping.get('confirmed_at'):
                qwen_by_question[qid] = {'score': 0, 'need_review': True, 'answer_present': None,
                    'recognized_work': '', 'feedback': '已确认题区识别/评分失败，请重试或由教师裁定。'}
                qwen_context_by_question[qid] = {'mode': 'teacher_confirmed_crop', 'error': str(exc)[:180]}
                continue
            # Keep the original full-page candidate route as a visible fallback,
            # never replace it silently with a cropped result.
            unconfirmed.append((row, problem))

    if unconfirmed and is_photo_locator:
        # Full-page grading is discarded by the location guard anyway. Avoid a
        # slow, misleading model pass; unresolved photo questions stay pending.
        for row, _problem in unconfirmed:
            qid = row["grading_key"]
            qwen_by_question[qid] = {}
            qwen_context_by_question[qid] = {"mode": "full_page_locator", "detail": "题区未可靠定位"}
    elif unconfirmed:
        try:
            image_payload = _load_images(submission["file_path"])
            qwen_items = await request_qwen(image_payload, [item[1] for item in unconfirmed])
            fallback_by_question = {str(item.get("problem_id")): item for item in qwen_items}
            for row, _problem in unconfirmed:
                qid = row["grading_key"]
                qwen_by_question[qid] = fallback_by_question.get(qid, {})
                qwen_context_by_question[qid] = {
                    "mode": "full_page_locator_fallback" if qid in crop_errors else "full_page_locator",
                    "page_count": len(image_payload),
                }
        except Exception as exc:
            message = str(exc)[:180]
            qwen_errors.append(message)
            for row, _problem in unconfirmed:
                qid = row["grading_key"]
                qwen_by_question.setdefault(qid, {})
                qwen_context_by_question.setdefault(qid, {
                    "mode": "qwen_unavailable",
                    "detail": message,
                })

    qwen_error = "；".join(qwen_errors)
    formula_by_question: dict[str, dict[str, Any]] = {}
    for row in questions:
        qid = row["grading_key"]
        crop = crop_payload_by_question.get(qid)
        candidate = qwen_by_question.get(qid, {})
        # FormulaNet is supporting evidence for a failed transcription, never
        # an independent student answer or an automatic score.
        if crop and not str(candidate.get("recognized_work") or "").strip():
            formula_by_question[qid] = await recognize_formula(crop)
    coloured_ink_reference_present = any(
        context.get("mode") == "auto_pdf_text_crop"
        and int(context.get("coloured_ink_pixels") or 0) >= 80
        for context in qwen_context_by_question.values()
    )

    results: list[dict[str, Any]] = []
    for row in questions:
        qid = row["grading_key"]
        qwen = qwen_by_question.get(qid, {})
        formula_evidence = formula_by_question.get(qid, {"available": False})
        qwen_input = qwen_context_by_question.get(qid, {"mode": "unknown"})
        if qwen_input.get("mode") == "auto_photo_heading_crop" and str(qwen.get("recognized_work") or "").strip():
            first_audit = await verify_recognized_work_matches_problem(
                {"problem_text": str(row.get("content") or "")}, str(qwen.get("recognized_work") or ""),
            )
            qwen["recognition_task_audit"] = first_audit
            if not first_audit.get("matches"):
                # OCR can reveal that an image-only locator accepted a nearby
                # task. Search again excluding that exact crop, then require
                # both independent image and OCR task checks before replacing it.
                try:
                    from .photo_semantic_locator import locate_by_content
                    excluded = []
                    for piece in qwen_input.get("regions") or []:
                        region = piece.get("region") or {}
                        if piece.get("page_no") and all(key in region for key in ("x", "y", "width", "height")):
                            excluded.append({"page_no": piece["page_no"], **region})
                    if not excluded and qwen_input.get("page_no") and qwen_input.get("region"):
                        excluded = [{"page_no": qwen_input["page_no"], **qwen_input["region"]}]
                    full_pages = _load_images(submission["file_path"])
                    locator_pages, retry_rotations = await orient_locator_pages(full_pages)
                    excluded_box = restore_region_coordinates(
                        {"excluded": {"regions": excluded}},
                        [(-angle) % 360 for angle in retry_rotations],
                    )["excluded"]["regions"] if excluded else []
                    alternate = await locate_by_content(
                        locator_pages, [row],
                        exclude_regions={int(row["question_id"]): excluded_box} if excluded_box else None,
                    )
                    alternate = restore_region_coordinates(alternate, retry_rotations)
                    replacement = (alternate or {}).get(int(row["question_id"]))
                    if replacement:
                        replacement_pieces = [
                            _crop_confirmed_region(submission["file_path"], {**replacement, **region})
                            for region in expand_regions(replacement)
                        ]
                        replacement_images = [piece[0] for piece in replacement_pieces]
                        image_audit = await verify_crop_matches_problem(replacement_images, {
                            "problem_text": str(row.get("content") or "")
                        })
                        if image_audit.get("matches"):
                            target_problem = next((item for item in problems if str(item.get("problem_id")) == str(qid)), None)
                            if target_problem:
                                reference = await complete_reference(target_problem, rubric_audits[qid])
                                replacement_grade = await grade_crops(replacement_images, target_problem, reference)
                                replacement_work = str(replacement_grade.get("recognized_work") or "")
                                text_audit = await verify_recognized_work_matches_problem(
                                    {"problem_text": str(row.get("content") or "")}, replacement_work,
                                )
                                if text_audit.get("matches"):
                                    qwen = replacement_grade
                                    qwen["recognition_task_audit"] = text_audit
                                    qwen["location_match_audit"] = image_audit
                                    qwen["previous_candidate_rejected"] = True
                                    qwen_by_question[qid] = qwen
                                    qwen_input = {
                                        **replacement_pieces[0][1],
                                        "regions": [piece[1] for piece in replacement_pieces],
                                        "page_nos": [piece[1]["page_no"] for piece in replacement_pieces],
                                        "region_count": len(replacement_pieces),
                                        "previous_candidate_rejected": True,
                                    }
                                    qwen_context_by_question[qid] = qwen_input
                                    crop_payload_by_question[qid] = replacement_images
                                else:
                                    qwen["alternative_search_audit"] = text_audit
                except Exception as retry_exc:
                    qwen["alternative_search_error"] = type(retry_exc).__name__
        standard_answer = row["answer"] or ""
        is_proof = normalize_question_type(row["question_type"]) == "proof"
        rubric_audit = rubric_audits[qid]
        trusted_reference = bool(standard_answer) and bool(rubric_audit["valid"])
        step_score_audit = normalize_step_scores(qwen, float(row["max_score"]))
        # The structured rubric is the most specific scoring evidence. If the
        # model says the answer is correct and every rubric point is awarded,
        # a stale aggregate score or conservative confidence must not reduce
        # the grade or create manual-review work.
        step_full_credit = _all_rubric_points_earned(qwen)
        # Complete structured scoring evidence is stronger than an internally
        # contradictory aggregate/boolean returned by the vision model.
        rubric_full_credit = bool(trusted_reference and step_full_credit
                                  and qwen.get("answer_present") is True)
        if rubric_full_credit:
            qwen["score"] = float(row["max_score"])
            qwen["max_score"] = float(row["max_score"])
            qwen["confidence"] = max(float(qwen.get("confidence") or 0), 0.90)
            qwen["need_review"] = False
            qwen["score_reconciled_from_rubric"] = True
        recognized = str(qwen.get("recognized_work") or "")
        recognition_task_audit = qwen.get("recognition_task_audit")
        if qwen_input.get("mode") == "auto_photo_heading_crop" and recognized.strip():
            if recognition_task_audit is None:
                recognition_task_audit = await verify_recognized_work_matches_problem(
                    {"problem_text": str(row.get("content") or "")}, recognized,
                )
            qwen["recognition_task_audit"] = recognition_task_audit
            if not recognition_task_audit.get("matches"):
                # Keep wrong-task OCR from becoming a candidate mark, even if
                # the image-only locator or grader was overconfident.
                qwen["location_match_rejected"] = True
                qwen["score"] = 0.0
                qwen["step_scores"] = []
                qwen["correct"] = False
                qwen["answer_present"] = None
                qwen["work_complete"] = False
                qwen["need_review"] = True
                qwen["confidence"] = min(float(qwen.get("confidence") or 0), 0.60)
                qwen["recognized_work"] = ""
                recognized = ""
                rubric_full_credit = False
                step_full_credit = False
        task_coverage = _numbered_task_coverage(str(row.get("content") or ""), recognized)
        numbered_coverage_only = False
        if task_coverage.get("available") and task_coverage["matched"] < task_coverage["required"]:
            # OCR can miss handwritten labels even when the work is present.
            # Treat label coverage as a review signal, not proportional evidence
            # that the student left those parts unanswered.
            rubric_full_credit = False
            answer_text_full_credit = False
            numbered_coverage_only = qwen.get("work_complete") is not False
            qwen["work_complete"] = False
            qwen["completion_evidence"] = (
                f"编号小问仅识别到 {task_coverage['matched']}/{task_coverage['required']} 个标签，"
                f"未识别标签：{'、'.join(task_coverage['missing'])}；需教师核对原图"
            )
        answer_coverage = _requested_parameter_coverage(str(row.get("content") or ""), recognized)
        if answer_coverage.get("available") and answer_coverage["missing"]:
            # A model's full-credit claim cannot prove that every requested
            # value was present in the student's independently read work.
            numbered_coverage_only = False
            rubric_full_credit = False
            answer_text_full_credit = False
            qwen["work_complete"] = False
            qwen["completion_evidence"] = (
                "题目要求给出参数 " + "、".join(answer_coverage["required"])
                + " 的值，但识别文本未能确认：" + "、".join(answer_coverage["missing"])
            )
            qwen["need_review"] = True
            qwen["score"] = min(
                float(qwen.get("score") or 0),
                float(row["max_score"]) * float(answer_coverage["ratio"]),
            )
        recognized_compact_length = len(re.sub(r"\s+", "", recognized))
        dense_ink_low_ocr_coverage = (
            qwen_input.get("mode") == "auto_pdf_text_crop"
            and int(qwen_input.get("coloured_ink_pixels") or 0) >= 10000
            and recognized_compact_length < 40
        )
        if dense_ink_low_ocr_coverage:
            qwen["work_complete"] = False
            qwen["completion_evidence"] = "题区笔迹较多但识别文本过短，无法确认完整作答"
        risks = list(qwen.get("risks") or [])
        input_guard = inspect_untrusted_text(str(row["content"] or ""))
        recognition_guard = inspect_untrusted_text(recognized)
        if input_guard.suspicious:
            risks.append("题目文本含疑似提示词注入内容：" + "、".join(input_guard.reasons))
        if recognition_guard.suspicious:
            risks.append("学生识别文本含疑似提示词注入内容：" + "、".join(recognition_guard.reasons))
        contaminated_recognition = (_recognition_is_contaminated(recognized, standard_answer)
                                    and not qwen.get("independent_ocr"))
        # A valid short answer such as 0 or x=1 is not failed OCR.
        recognized_too_short = not recognized.strip() if qwen.get('independent_ocr') else len(re.sub(r"\s+", "", recognized)) < 8
        if contaminated_recognition or recognized_too_short:
            # Candidate points are never independent evidence.  A crop may be
            # correct while Qwen still fails to transcribe it; in that case the
            # safe outcome is 0 pending teacher review, not a fabricated score
            # inferred from the supplied reference answer or rubric.
            recognized = ""
            qwen["recognized_work"] = ""
            qwen["score"] = 0.0
            qwen["step_scores"] = []
            qwen["correct"] = False
            qwen["answer_present"] = None
            qwen["work_complete"] = False
            qwen["need_review"] = True
            qwen["confidence"] = min(float(qwen.get("confidence") or 0), 0.60)
            rubric_full_credit = False
            step_full_credit = False
            reason = "识别结果疑似受参考答案污染" if contaminated_recognition else "未识别到足够的学生作答"
            qwen["completion_evidence"] = reason
            risks.append(reason + "：候选评分点已清空，待教师结合原图复核")
        answer_text_full_credit = bool(trusted_reference and qwen.get("answer_present") is True
                                       and _answer_text_matches(recognized, standard_answer))
        if answer_text_full_credit:
            qwen["score"] = float(row["max_score"])
            qwen["max_score"] = float(row["max_score"])
            qwen["correct"] = True
            qwen["score_reconciled_from_answer_text"] = True
            risks.append("系统校验：作答文本与标准答案高度一致，候选分已修正为满分")
        tool_use = run_tool_use(
            question_id=int(row["question_id"]),
            question_type=normalize_question_type(row["question_type"]),
            problem_text=str(row["content"] or ""),
            recognized_work=recognized,
            standard_answer=standard_answer,
        )
        equivalent = tool_use["math_equivalence"]
        review_reasons: list[str] = []
        answer_present = qwen.get("answer_present")
        deterministic_blank = (
            qwen_input.get("mode") == "auto_pdf_text_crop"
            and coloured_ink_reference_present
            and int(qwen_input.get("coloured_ink_pixels") or 0) < 20
        )
        if deterministic_blank:
            answer_present = False
            qwen["answer_present"] = False
            qwen["work_complete"] = False
            qwen["completion_evidence"] = "当前题区未检测到学生彩色手写墨迹"
            risks.append("确定性空白检测")
        if answer_present is False:
            qwen["correct"] = False
            qwen["score"] = 0.0
            qwen["need_review"] = False
            qwen["confidence"] = max(float(qwen.get("confidence") or 0), 0.95)
        feedback_text = str(qwen.get("feedback") or "")
        rubric_coverage = _rubric_coverage(recognized, rubric_audit["effective_solution"])
        early_completion = _completion_check(recognized, qwen)
        if (
            trusted_reference
            and normalize_question_type(row["question_type"]) == "calc"
            and answer_present is True
            and rubric_coverage["available"]
            and rubric_coverage["complete"]
            and early_completion["complete"]
            and not (task_coverage.get("available") and task_coverage["matched"] < task_coverage["required"])
        ):
            qwen["score"] = float(row["max_score"])
            qwen["max_score"] = float(row["max_score"])
            qwen["correct"] = True
            qwen["confidence"] = max(float(qwen.get("confidence") or 0), 0.90)
            qwen["need_review"] = False
            qwen["score_reconciled_from_rubric_coverage"] = True
        # A model result that explicitly says the answer is correct must not
        # retain an arbitrary partial score and create a teacher-review task.
        # Strong positive feedback is treated the same way when the provider's
        # boolean flag is internally inconsistent.
        model_affirms_correct = bool(trusted_reference and qwen.get("correct")) and answer_present is True
        if model_affirms_correct:
            qwen["correct"] = True
            qwen["score"] = float(row["max_score"])
            qwen["max_score"] = float(row["max_score"])
            qwen["need_review"] = False
        if qwen_error:
            review_reasons.append("Qwen 识别失败")
        if qid in crop_errors:
            review_reasons.append(f"已确认区域处理失败：{crop_errors[qid]}")
        if answer_present is None:
            review_reasons.append("纸面作答存在性未能确认")
        if not standard_answer:
            review_reasons.append("缺少标准答案")
        if qwen.get('provisional_reference'):
            review_reasons.append('使用模型独立补全参考解，建议分须教师核对')
        if qwen.get('grading_method') == 'independent_crop_ocr_v2':
            review_reasons.append('已按最新切分独立识别并重新逐小问评分，待教师复核')
        if qwen.get('location_match_rejected'):
            match_audit = qwen.get('recognition_task_audit') or qwen_input.get('location_match_audit') or {}
            review_reasons.append('自动题区/识别文本与目标题干一致性未通过，已阻止评分：' + str(match_audit.get('reason') or '证据不足'))
        if not rubric_audit["valid"]:
            review_reasons.append("评分标准异常，已回退标准答案：" + "；".join(rubric_audit["issues"]))
        if normalize_question_type(row["question_type"]) != "calc":
            review_reasons.append("证明/非计算题需教师复核")
        if not model_affirms_correct and float(qwen.get("confidence", 0) or 0) < 0.85:
            review_reasons.append("Qwen 置信度不足")
        if not model_affirms_correct and qwen.get("need_review", True):
            review_reasons.append("Qwen 标记为需复核")
        completion = ({"complete": True, "reason": "视觉审计确认未作答，自动记零分", "source": "vision"}
                      if answer_present is False else _completion_check(recognized, qwen))
        if not completion["complete"] and not (rubric_full_credit or answer_text_full_credit):
            review_reasons.append("作答疑似未完成：" + completion["reason"])
            risks.append("作答完整性拦截：" + completion["reason"])
            # Never expose an unfinished expression as an automatically
            # accepted full-credit answer.  A teacher can still override this
            # candidate after looking at the original image.
            qwen["need_review"] = True
            if not numbered_coverage_only:
                qwen["correct"] = False
                qwen["confidence"] = min(float(qwen.get("confidence", 0) or 0), 0.60)
                qwen["score"] = min(
                    float(qwen.get("score", 0) or 0),
                    float(row["max_score"]) * 0.60,
                )
        if answer_coverage.get("available") and answer_coverage["missing"]:
            review_reasons.append("所求参数不完整：缺少 " + "、".join(answer_coverage["missing"]) + " 的明确取值")
            risks.append("参数答案覆盖门槛未通过")
        if any("参考答案污染" in str(risk) for risk in risks):
            review_reasons.append("识别文本疑似参考答案污染")
        if input_guard.suspicious or recognition_guard.suspicious:
            review_reasons.append("检测到疑似提示词注入内容，已保留证据并转教师复核")
        # A symbolic parser may be unable to parse perfectly valid handwritten
        # OCR (for example y'=3x^2+3/(2√x)). That is inconclusive evidence,
        # not a mathematical contradiction. Only a high-confidence negative
        # symbolic verdict may override a high-confidence VLM assessment.
        math_contradiction = (
            bool(equivalent.get("available"))
            and equivalent.get("equal") is False
            and float(equivalent.get("confidence") or 0) >= 0.85
            and qwen.get("correct") is not None
            and bool(qwen.get("correct")) is True
        )
        if math_contradiction:
            review_reasons.append("Qwen 与高置信数学判等结论不一致")
        # A result cannot safely be auto-accepted when the model says
        # "correct" but assigns fewer than the available points.
        model_score = float(qwen.get("score") or 0)
        model_max_score = float(qwen.get("max_score") or 0)
        if qwen.get("correct") is True and model_max_score > 0 and model_score + 1e-6 < model_max_score:
            review_reasons.append("Qwen 正确结论与得分不一致")

        # A whole-page request is useful only for locating a likely question; it
        # is not evidence that the located handwriting belongs to this question.
        # Never award points from that unconfirmed locator.  This prevents work
        # written for (for example) Ex1.3 from being copied into an unanswered
        # Ex1.2 item merely because both appear on the same uploaded page.
        if qwen_input.get("mode") == "auto_photo_heading_crop":
            review_reasons.append("照片题号自动定位，需教师结合原图核对")
        unconfirmed_full_page = str(qwen_input.get("mode") or "").startswith("full_page_locator")
        if unconfirmed_full_page:
            recognized = ""
            qwen["recognized_work"] = ""
            qwen["score"] = 0.0
            qwen["max_score"] = float(row["max_score"])
            qwen["correct"] = False
            qwen["answer_present"] = None
            answer_present = None
            qwen["work_complete"] = False
            qwen["need_review"] = True
            qwen["confidence"] = min(float(qwen.get("confidence") or 0), 0.45)
            qwen["feedback"] = "题区尚未确认，整页识别文字不作为本题作答证据；请框选本题区域后重新初评，或由教师直接裁定。"
            completion = {"complete": False, "reason": "本题作答区域尚未确认，不能采用整页定位文字计分", "source": "location_guard"}
            review_reasons.append("题区尚未确认：整页定位结果不计分，需框选本题区域或由教师裁定")
            risks.append("题号/小问区域未确认：已阻止同页其他题作答被误计入本题")

        if gate_unverified_candidate(qwen, answer_present=answer_present,
                                     trusted_reference=trusted_reference):
            review_reasons.append("缺少可自动采纳的纸面作答与教师评分依据")
            risks.append("已拦截无依据满分；模型候选仅保留作复核证据")

        score_status = str(qwen.get("score_status") or ("valid" if qwen.get("score") is not None else "invalid"))
        if score_status != "valid":
            score_status = "invalid"
            qwen["score_status"] = score_status
            qwen["score"] = 0.0
            qwen["max_score"] = float(row["max_score"])
            qwen["correct"] = None
            qwen["need_review"] = True
            review_reasons.append("无有效评分：Qwen 请求重试后仍未返回可用评分，请重试或教师裁定")
            risks.append(str(qwen.get("grading_error") or "本题没有可采纳的模型评分"))

        # Deterministic checks above may correct the aggregate score after the
        # VLM supplied step scores. Reconcile the visible evidence last so the
        # teacher never sees a step total that contradicts this question score.
        step_score_audit = normalize_step_scores(qwen, float(row["max_score"]))
        step_score_reconciliation = reconcile_step_scores_to_question_score(qwen, float(row["max_score"]))
        if step_score_reconciliation.get("reconciled"):
            qwen["step_score_reconciliation"] = step_score_reconciliation
            risks.append("评分点候选已按本题最终建议分自动对账")
        candidate_breakdown = audit_candidate_breakdown(qwen, float(row["max_score"]))
        qwen["candidate_breakdown"] = candidate_breakdown
        if not candidate_breakdown["valid"]:
            review_reasons.append("评分点候选需复核：" + "；".join(candidate_breakdown["issues"]))
            risks.append("评分点候选未通过本题分值一致性校验，不能自动采纳")
        needs_review = bool(review_reasons)
        results.append({
            "question_id": row["question_id"],
            "question_type": normalize_question_type(row["question_type"]),
            "subpart_no": row.get("subpart_no"),
            "problem_no": row["problem_no"],
            "sort_order": row["sort_order"],
            "score_status": score_status,
            "score_valid": score_status == "valid",
            "score": float(qwen.get("score", 0) or 0),
            "max_score": float(row["max_score"]),
            "correct": qwen.get("correct"),
            "answer_present": answer_present,
            "work_complete": qwen.get("work_complete"),
            "confidence": float(qwen.get("confidence", 0) or 0),
            "recognized_work": recognized,
            "feedback": str(qwen.get("feedback") or "待教师查看识别结果"),
            "completion": completion,
            "task_coverage": task_coverage,
            "answer_coverage": answer_coverage,
            "rubric_audit": rubric_audit,
            "rubric_coverage": rubric_coverage,
            "step_score_audit": step_score_audit,
            "candidate_breakdown": candidate_breakdown,
            "needs_review": needs_review,
            "review_reasons": review_reasons,
            "qwen": qwen,
            "qwen_input": qwen_input,
            "math_equivalence": equivalent,
            "answer_evidence": tool_use["answer_evidence"],
            "formula_reference": tool_use["formula_reference"],
            "formula_ocr": formula_evidence,
            "tool_trace": tool_use["tool_trace"],
            "tool_router_version": tool_use["tool_router_version"],
            "handwriting_score": qwen.get("handwriting_score"),
            "evidence": {
                "source_problem_id": row["source_problem_id"],
                "standard_answer": standard_answer,
                "rubric": rubric_audit["effective_solution"],
                "rubric_source": row["rubric"] or "",
                "source": json.loads(row["source_evidence_json"] or "{}"),
            },
            "risks": risks,
            "prompt_security": {
                "input": input_guard.trace(),
                "recognized_work": recognition_guard.trace(),
            },
        })

    # Teacher-confirmed page/region mappings survive a regrade and remain
    # visible as evidence even though the original upload is never altered.
    for item in results:
        key = (item["question_id"], item.get("subpart_no") or "", item["sort_order"])
        if key in regions:
            item["teacher_page_mapping"] = regions[key]

    needs_review = any(item["needs_review"] for item in results)
    quality_score = round(sum(float(item.get("score") or 0) for item in results), 1)
    quality_max_score = round(sum(item["max_score"] for item in results), 1)
    with connection() as conn:
        score_meta = conn.execute(
            "SELECT score_policy,completion_points,total_score FROM assignments WHERE id=?",
            (submission["assignment_id"],),
        ).fetchone()
    score_policy = str(score_meta["score_policy"] or "legacy") if score_meta else "legacy"
    completion_score = float(score_meta["completion_points"] or 0) if score_meta else 0.0
    total_score = round(quality_score + completion_score, 1)
    max_score = float(score_meta["total_score"] or quality_max_score) if score_meta else quality_max_score
    hw_scores = [float(item["handwriting_score"]) for item in results
                 if item.get("handwriting_score") is not None]
    handwriting_score = round(sum(hw_scores) / len(hw_scores), 1) if hw_scores else None
    return {
        "submission_id": submission_id,
        "total_score": total_score,
        "max_score": max_score,
        "quality_score": quality_score,
        "quality_max_score": quality_max_score,
        "completion_score": completion_score,
        "score_policy": score_policy,
        "model_runtime": model_runtime(),
        "handwriting_score": handwriting_score,
        "needs_review": needs_review,
        "qwen_error": qwen_error,
        "results": results,
    }


async def run_grading_job(job_id: int) -> None:
    """Claim and persist one job; this is safe for web background tasks or worker.py."""
    with connection() as conn:
        job = conn.execute("SELECT * FROM grading_jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["status"] != "queued":
            return
        changed = conn.execute("UPDATE grading_jobs SET status='running' WHERE id=? AND status='queued'", (job_id,)).rowcount
        if not changed:
            return
    try:
        result = await grade_submission(job["submission_id"])
        with connection() as conn:
            conn.execute("UPDATE grading_jobs SET status='completed', result_json=? WHERE id=?", (json.dumps(result, ensure_ascii=False), job_id))
            conn.execute(
                "UPDATE submissions SET status=?, score=?, feedback=?, needs_review=?, handwriting_score=?, completion_score=?, quality_score=?, quality_max_score=?, score_policy_version=? WHERE id=?",
                ("review_required" if result["needs_review"] else "graded", result["total_score"],
                 "AI 初评完成；请查看评分证据。", int(result["needs_review"]),
                 result.get("handwriting_score"), result.get("completion_score", 0),
                 result.get("quality_score"), result.get("quality_max_score"), result.get("score_policy", "legacy"),
                 job["submission_id"]),
            )
    except Exception as exc:
        with connection() as conn:
            conn.execute("UPDATE grading_jobs SET status='failed', result_json=? WHERE id=?", (json.dumps({"error": str(exc)}, ensure_ascii=False), job_id))
            conn.execute("UPDATE submissions SET status='review_required', needs_review=1, feedback=? WHERE id=?", ("初评任务失败，已转入教师复核。", job["submission_id"]))
