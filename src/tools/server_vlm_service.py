"""Private GPU vision-model service for math answer review."""

from __future__ import annotations

import base64
import io
import json
import os
import re
from threading import Lock

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from PIL import Image
from qwen_vl_utils import process_vision_info
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration


MODEL_PATH = os.environ.get("MATH_VLM_MODEL", "/opt/math-vlm/models/Qwen2.5-VL-3B-Instruct")
app = FastAPI(title="Private Math VLM Review")
model = None
processor = None
load_lock = Lock()
inference_lock = Lock()


class ReviewRequest(BaseModel):
    image_base64: str
    images_base64: list[str] = []
    problem_text: str = ""
    ocr_text: str = ""
    section_no: str = ""
    problem_no: str = ""
    subquestion_count: int = 0


def _detect_subquestion_count(images: list[Image.Image], req: ReviewRequest) -> int:
    """Ask Qwen for a count only when the teacher did not explicitly set one."""
    prompt = f"""判断高等数学第 {req.section_no} 章第 {req.problem_no} 题有多少个明确编号的小问。
题目图或答案图中出现的 (1)、(2)、… 才算小问；解题步骤、分情况不算。题目文本仅供参考：{req.problem_text or '未提供'}。
只输出一个 0 到 30 的阿拉伯数字；若没有小问输出 0。"""
    content = [{"type": "image", "image": image} for image in images]
    content.append({"type": "text", "text": prompt})
    raw = generate([{"role": "user", "content": content}], 80).strip()
    match = re.search(r"\b([0-9]|[12][0-9]|30)\b", raw)
    return int(match.group(1)) if match else 0


class GradeProblem(BaseModel):
    problem_id: str
    problem_no: str
    problem_text: str = ""
    std_answer: str
    full_solution: str = ""
    max_score: float = 10.0


class GradeHomeworkRequest(BaseModel):
    images_base64: list[str]
    problems: list[GradeProblem]


class SolveRequest(BaseModel):
    problem_text: str
    section_no: str = ""
    problem_no: str = ""


class SolveImageRequest(BaseModel):
    image_base64: str
    section_no: str = ""
    problem_no: str = ""


def ensure_model():
    global model, processor
    if model is not None:
        return
    with load_lock:
        if model is not None:
            return
        processor = AutoProcessor.from_pretrained(
            MODEL_PATH, min_pixels=256 * 28 * 28, max_pixels=1280 * 28 * 28
        )
        model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            MODEL_PATH,
            torch_dtype=torch.bfloat16,
            device_map="auto",
            attn_implementation="sdpa",
        ).eval()


def _escape_math_backslashes(candidate: str) -> str:
    """Escape LaTeX slashes inside JSON strings without breaking escaped quotes."""
    output = []
    in_string = False
    index = 0
    while index < len(candidate):
        char = candidate[index]
        if char == '"':
            backslashes = 0
            cursor = index - 1
            while cursor >= 0 and candidate[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                in_string = not in_string
            output.append(char)
            index += 1
            continue
        if in_string and char == "\\":
            following = candidate[index + 1] if index + 1 < len(candidate) else ""
            if following in {'"', "\\", "/"}:
                output.extend((char, following))
                index += 2
                continue
            if following == "u" and re.match(r"^[0-9a-fA-F]{4}$", candidate[index + 2:index + 6]):
                output.append(candidate[index:index + 6])
                index += 6
                continue
            # JSON treats \frac, \to, \neq, etc. as invalid escapes (or
            # silently interprets their first letter). Preserve them as LaTeX.
            output.append("\\\\")
            index += 1
            continue
        if in_string and char in {"\n", "\r", "\t"}:
            output.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[char])
            index += 1
            continue
        output.append(char)
        index += 1
    return "".join(output)


def try_parse_json(text: str):
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except (json.JSONDecodeError, TypeError):
            try:
                # Qwen often emits valid LaTeX but invalid JSON string escapes.
                value = json.loads(_escape_math_backslashes(candidate))
                if isinstance(value, dict):
                    return value
            except (json.JSONDecodeError, TypeError):
                pass
    return None


def _generate_unlocked(messages, max_new_tokens=2400):
    chat_text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs = process_vision_info(messages)
    inputs = processor(
        text=[chat_text], images=image_inputs, videos=video_inputs,
        padding=True, return_tensors="pt",
    ).to(model.device)
    with torch.inference_mode():
        generated = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    trimmed = [out[len(inp):] for inp, out in zip(inputs.input_ids, generated)]
    return processor.batch_decode(trimmed, skip_special_tokens=True)[0]


def generate(messages, max_new_tokens=2400):
    # Transformers generation on one shared model is not thread-safe. Queue
    # simultaneous browser requests instead of letting them corrupt each other.
    with inference_lock:
        return _generate_unlocked(messages, max_new_tokens)


@app.get("/health")
def health():
    return {
        "ok": True,
        "model_path": MODEL_PATH,
        "model_loaded": model is not None,
        "cuda": torch.cuda.is_available(),
        "gpu_count": torch.cuda.device_count(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    }


@app.post("/review")
def review(req: ReviewRequest):
    ensure_model()
    try:
        encoded_images = req.images_base64 or [req.image_base64]
        images = [Image.open(io.BytesIO(base64.b64decode(value))).convert("RGB")
                  for value in encoded_images]
    except Exception as exc:
        raise HTTPException(400, "invalid image") from exc

    detected_subquestion_count = int(req.subquestion_count or 0)
    auto_detected = False
    if detected_subquestion_count == 0:
        detected_subquestion_count = _detect_subquestion_count(images, req)
        auto_detected = detected_subquestion_count > 0

    if detected_subquestion_count:
        sub_answers = []
        risks = []
        for number in range(1, detected_subquestion_count + 1):
            sub_prompt = f"""你是中国大学高等数学教师。多张图片共同属于章节 {req.section_no} 第 {req.problem_no} 题。
本次只识别其中第 ({number}) 小问。请在所有图片中寻找该小问的题目和解答，不要混入其他小问。
题目正文参考：{req.problem_text or '未提供'}
只输出一个简短且完整的严格 JSON，不要 Markdown：
{{"ptype":"calc或proof","std_answer":"最终答案","full_solution":"必要的完整步骤","confidence":0.0,"risks":[]}}
注意：若答案先得到通解/函数表达式，随后代入初值或题设时间、位置等条件求得题目真正所问的数值，std_answer 必须填写该最终数值结论（含单位）；通解保留在 full_solution，不能替代最终答案。
若题目要求“表达式及定义域/值域/单调性/极值”等多个结论，std_answer 必须逐项完整列出，例如“表达式：…；定义域：…”。只写中间表达式或只写其中一项视为不完整，必须在 risks 中说明。
如果图片中确实找不到第 ({number}) 小问，也要返回 JSON，并在 risks 中写明缺少对应图片，禁止猜测。
"""
            content = [{"type": "image", "image": image} for image in images]
            content.append({"type": "text", "text": sub_prompt})
            raw_sub = generate([{"role": "user", "content": content}], 1000)
            parsed = try_parse_json(raw_sub)
            if not parsed:
                repair = f"""把下面草稿整理成严格 JSON，只输出 JSON。数学反斜杠必须正确转义。
格式：{{"ptype":"calc","std_answer":"...","full_solution":"...","confidence":0.0,"risks":[]}}
草稿：{raw_sub[:4000]}"""
                parsed = try_parse_json(generate(
                    [{"role": "user", "content": [{"type": "text", "text": repair}]}], 700
                ))
            if not parsed or not str(parsed.get("std_answer", "")).strip():
                parsed = {
                    "ptype": "calc",
                    "std_answer": "未识别，请补充对应答案图片或人工填写",
                    "full_solution": "",
                    "confidence": 0.0,
                    "risks": [f"未能可靠识别第 ({number}) 小问"],
                }
            sub_risks = parsed.get("risks") if isinstance(parsed.get("risks"), list) else []
            risks.extend([f"({number}) {value}" for value in sub_risks])
            sub_answers.append({
                "sub_no": str(number),
                "std_answer": str(parsed.get("std_answer", "")),
                "full_solution": str(parsed.get("full_solution", "")),
                "confidence": max(0.0, min(1.0, float(parsed.get("confidence", 0) or 0))),
                "ptype": parsed.get("ptype") if parsed.get("ptype") in {"calc", "proof"} else "calc",
            })
        confidence = sum(item["confidence"] for item in sub_answers) / len(sub_answers)
        return {
            "ptype": "proof" if all(item["ptype"] == "proof" for item in sub_answers) else "calc",
            "std_answer": "\n".join(f"({item['sub_no']}) {item['std_answer']}" for item in sub_answers),
            "full_solution": "\n\n".join(f"({item['sub_no']}) {item['full_solution']}" for item in sub_answers),
            "sub_answers": sub_answers,
            "confidence": confidence,
            "risks": risks,
            "model": "Qwen2.5-VL-3B-Instruct",
            "detected_subquestion_count": detected_subquestion_count,
            "subquestion_count_source": "teacher" if req.subquestion_count else ("auto" if auto_detected else "none"),
        }

    prompt = f"""你是一名严谨的中国大学高等数学教师。请查看答案书原始页，并结合题号定位目标答案。
目标：章节 {req.section_no}，第 {req.problem_no} 题。
本题小问数量：未知。请自行识别明确编号的 (1)(2)… 小问；若有小问，sub_answers 必须逐问输出；若没有，sub_answers 为空。解题步骤、分情况不算小问。
题目文本（可能缺失或含 OCR 错误）：{req.problem_text or '未提供'}
已有 OCR 候选（仅供参考，可能严重错误）：{req.ocr_text or '未提供'}

请完成：
1. 判断题型，只能是 calc（计算题）或 proof（证明题）；
2. 从图片准确定位本题答案，不要混入相邻题；
3. 给出可用于教师答案库的标准答案和完整解答；若先得到通解再代入题设求最终量，std_answer 必须是题目最终所问的数值/结论，通解写入 full_solution；如果母题含多个小问，必须逐一识别所有小问，不能合并或遗漏；
4. 指出题号错配、图像不清或公式不确定等风险；
5. 给出 0 到 1 的置信度，禁止猜测。
只输出一个严格 JSON 对象，不要输出 Markdown 或解释文字：
若图片明确存在小问，sub_answers 必须按图片中的小问编号输出；否则为空数组。禁止把解题步骤误作小问。
{{"ptype":"calc或proof","std_answer":"按小问编号汇总的答案","full_solution":"按小问编号汇总的解答","sub_answers":[{{"sub_no":"1","std_answer":"...","full_solution":"...","confidence":0.0}}],"confidence":0.0,"risks":["..."]}}
"""
    content = [{"type": "image", "image": image} for image in images]
    content.append({"type": "text", "text": prompt})
    messages = [{"role": "user", "content": content}]
    raw = generate(messages, 2600)
    result = try_parse_json(raw)

    if not result or result.get("ptype") not in {"calc", "proof"} or not result.get("std_answer"):
        repair_prompt = f"""把下面的模型草稿整理成严格 JSON。只输出 JSON，不要 Markdown，不要解释。
ptype 只能是 calc 或 proof；std_answer 不能为空；confidence 必须是 0 到 1 的数字；risks 必须是字符串数组。
格式：{{"ptype":"calc","std_answer":"...","full_solution":"...","sub_answers":[{{"sub_no":"1","std_answer":"...","full_solution":"...","confidence":0.0}}],"confidence":0.0,"risks":[]}}
模型草稿：
{raw[:6000]}
"""
        repaired = generate([{"role": "user", "content": [{"type": "text", "text": repair_prompt}]}], 2000)
        result = try_parse_json(repaired)
        raw = repaired

    if not result:
        compact_prompt = f"""你是中国大学高等数学教师。识别图片中章节 {req.section_no} 第 {req.problem_no} 题的答案。
上一次输出过长导致格式截断。这次只给标准答案和最必要的步骤，全文不超过 700 个汉字。
只输出严格 JSON，不要 Markdown：
{{"ptype":"calc或proof","std_answer":"标准答案","full_solution":"简要必要步骤","sub_answers":[],"confidence":0.0,"risks":[]}}
"""
        compact_content = [{"type": "image", "image": image} for image in images]
        compact_content.append({"type": "text", "text": compact_prompt})
        compact_raw = generate([{"role": "user", "content": compact_content}], 1200)
        result = try_parse_json(compact_raw)
        raw = compact_raw
    if not result:
        # Preserve the recognition as a low-confidence editable candidate.
        # The source images remain the authority and nothing is auto-adopted.
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
        result = {
            "ptype": "proof" if "证明" in cleaned else "calc",
            "std_answer": "服务器已识别，但结构化结果需人工整理",
            "full_solution": cleaned[:6000],
            "sub_answers": [],
            "confidence": 0.15,
            "risks": ["模型输出格式异常，已保留识别草稿供人工复核"],
        }
    risks = result.get("risks") if isinstance(result.get("risks"), list) else [str(result.get("risks", ""))]
    normalized_with_loss = False
    if result.get("ptype") not in {"calc", "proof"}:
        result["ptype"] = "proof" if "证明" in str(result) else "calc"
        risks.append("模型题型字段异常，已自动归一化")
    std_answer = result.get("std_answer", "")
    if isinstance(std_answer, list):
        std_answer = "\n".join(f"({number}) {value}" for number, value in enumerate(std_answer, 1))
    elif isinstance(std_answer, dict):
        std_answer = "\n".join(f"{key}: {value}" for key, value in std_answer.items())
    if not str(std_answer).strip():
        std_answer = "服务器已识别，但标准答案字段需人工整理"
        risks.append("标准答案字段为空或格式异常")
        normalized_with_loss = True
    result["std_answer"] = str(std_answer)
    full_solution = result.get("full_solution", "")
    if isinstance(full_solution, list):
        full_solution = "\n\n".join(f"({number}) {value}" for number, value in enumerate(full_solution, 1))
    elif isinstance(full_solution, dict):
        full_solution = "\n".join(f"{key}: {value}" for key, value in full_solution.items())
    result["full_solution"] = str(full_solution or "")
    try:
        confidence = float(result.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.2
        risks.append("置信度字段异常，已降级为人工复核")
    result["confidence"] = max(0.0, min(1.0, confidence))
    if normalized_with_loss:
        result["confidence"] = min(result["confidence"], 0.2)
    result["risks"] = [str(value) for value in risks if str(value).strip()]
    if not isinstance(result.get("sub_answers"), list):
        result["sub_answers"] = []
    # The multi-question branch above has already created exactly the teacher
    # requested entries.  Do not run the generic padding block a second time.
    if req.subquestion_count and not detected_subquestion_count:
        indexed = {str(item.get("sub_no", "")): item for item in result["sub_answers"] if isinstance(item, dict)}
        result["sub_answers"] = [indexed.get(str(number), {
            "sub_no": str(number),
            "std_answer": "未识别，请补充对应答案图片或人工填写",
            "full_solution": "",
            "confidence": 0.0,
        }) for number in range(1, req.subquestion_count + 1)]
    else:
        # A teacher-entered zero explicitly means one unsplit problem.  Model
        # reasoning branches (cases, sequences, proof steps) are not subparts.
        result["sub_answers"] = []
    result["model"] = "Qwen2.5-VL-3B-Instruct"
    return result


@app.post("/solve")
def solve_without_answer_book(req: SolveRequest):
    """Solve from the problem statement only for independent cross-checking."""
    ensure_model()
    if not req.problem_text.strip():
        raise HTTPException(400, "problem_text is required for independent solving")
    prompt = f"""你是严谨的中国大学高等数学教师。请只根据题目独立解题，不得参考答案书或 OCR 候选。
章节：{req.section_no}；题号：{req.problem_no}
题目：{req.problem_text}
只输出严格 JSON，不要 Markdown：
{{"ptype":"calc或proof","std_answer":"最终答案","full_solution":"必要推导","confidence":0.0,"risks":[]}}
若题目正文残缺或无法唯一确定，禁止猜测，降低 confidence 并在 risks 说明。
"""
    raw = generate([{"role": "user", "content": [{"type": "text", "text": prompt}]}], 1800)
    result = try_parse_json(raw)
    if not result or not str(result.get("std_answer", "")).strip():
        raise HTTPException(422, "independent solve did not return a usable answer")
    result["ptype"] = result.get("ptype") if result.get("ptype") in {"calc", "proof"} else "calc"
    result["std_answer"] = str(result.get("std_answer") or "")
    result["full_solution"] = str(result.get("full_solution") or "")
    try:
        result["confidence"] = max(0.0, min(1.0, float(result.get("confidence") or 0)))
    except Exception:
        result["confidence"] = 0.0
    result["risks"] = result.get("risks") if isinstance(result.get("risks"), list) else [str(result.get("risks") or "")]
    result["model"] = "Qwen2.5-VL-3B-Instruct"
    return result


@app.post("/solve-from-image")
def solve_from_problem_image(req: SolveImageRequest):
    """Transcribe a question crop and solve it without an answer-book image.

    No longer raises 422 when only one of (problem_text, std_answer) is readable.
    Instead it returns HTTP 200 with ``partial: true`` and a ``missing`` list so the
    caller (the Agent) can park the partial recognition as a teacher-review candidate
    instead of dropping the whole result. A targeted single-field retry is attempted
    first to recover whatever the model missed on the first pass.
    """
    ensure_model()
    try:
        image = Image.open(io.BytesIO(base64.b64decode(req.image_base64))).convert("RGB")
    except Exception as exc:
        raise HTTPException(400, "invalid problem image") from exc
    prompt = f"""你是一名严谨的中国大学高等数学教师。图片是教材中的原题截图，不是答案页。
请先忠实转写题目，再只根据该题目独立求解；不要猜测看不清的符号或缺失条件。
章节：{req.section_no}；题号：{req.problem_no}
只输出严格 JSON，不要 Markdown：
{{"problem_text":"完整题干","ptype":"calc或proof","std_answer":"最终答案","full_solution":"必要推导","confidence":0.0,"risks":[]}}
若题目要求多个结论（如表达式和定义域），std_answer 必须逐项完整列出；若题图不可读，降低 confidence 并在 risks 说明。"""
    raw = generate([{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": prompt}]}], 2200)
    result = try_parse_json(raw)
    if result is None:
        result = {}

    stem_missing = not str(result.get("problem_text", "")).strip()
    answer_missing = not str(result.get("std_answer", "")).strip()

    # Targeted single-field recovery before giving up on a field.
    if stem_missing and not answer_missing:
        result = _recover_stem_only(image, req, result)
    elif answer_missing and not stem_missing:
        result = _recover_answer_only(image, req, result)
    elif stem_missing and answer_missing:
        result = _recover_stem_only(image, req, result)
        if not str(result.get("std_answer", "")).strip():
            result = _recover_answer_only(image, req, result)

    stem_missing = not str(result.get("problem_text", "")).strip()
    answer_missing = not str(result.get("std_answer", "")).strip()

    result["problem_text"] = str(result.get("problem_text") or "")
    result["ptype"] = result.get("ptype") if result.get("ptype") in {"calc", "proof"} else "calc"
    result["std_answer"] = str(result.get("std_answer") or "")
    result["full_solution"] = str(result.get("full_solution") or "")
    try:
        result["confidence"] = max(0.0, min(1.0, float(result.get("confidence") or 0)))
    except Exception:
        result["confidence"] = 0.0
    if stem_missing or answer_missing:
        # partial recognition is never trustworthy enough for auto-publish
        result["confidence"] = min(result["confidence"], 0.45)
    risks = result.get("risks") if isinstance(result.get("risks"), list) else [str(result.get("risks") or "")]
    if stem_missing:
        risks.append("题干转写失败，已保留部分识别供人工复核")
    if answer_missing:
        risks.append("答案未识别，已保留部分识别供人工复核")
    result["risks"] = [str(v) for v in risks if str(v).strip()]
    result["model"] = "Qwen2.5-VL-3B-Instruct"
    result["partial"] = bool(stem_missing or answer_missing)
    missing = []
    if stem_missing:
        missing.append("problem_text")
    if answer_missing:
        missing.append("std_answer")
    result["missing"] = missing
    return result


def _recover_stem_only(image, req, prev: dict) -> dict:
    """Retry with a prompt that asks only for the stem transcription."""
    prompt = f"""图片是教材原题截图（章节 {req.section_no}，第 {req.problem_no} 题）。
请只忠实转写题干，包括题号、所有小问编号和公式；不要解题、不要给答案。
只输出严格 JSON，不要 Markdown：
{{"problem_text":"完整题干文本（含公式）","confidence":0.0,"risks":[]}}
若题图模糊或公式无法辨认，尽量转写可见文字，缺失处留空，不要编造。"""
    try:
        raw = generate([{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": prompt}]}], 2200)
        rec = try_parse_json(raw)
        if rec and str(rec.get("problem_text", "")).strip():
            merged = dict(prev)
            merged["problem_text"] = str(rec["problem_text"])
            try:
                merged["confidence"] = min(float(prev.get("confidence") or 1.0), float(rec.get("confidence") or 1.0))
            except Exception:
                pass
            if rec.get("risks"):
                extra = rec["risks"] if isinstance(rec["risks"], list) else [str(rec["risks"])]
                merged["risks"] = list(prev.get("risks") or []) + extra
            return merged
    except Exception:
        pass
    return prev


def _recover_answer_only(image, req, prev: dict) -> dict:
    """Retry with a prompt that asks only for the answer (stem already known)."""
    prompt = f"""图片是教材原题截图（章节 {req.section_no}，第 {req.problem_no} 题）。
请只独立求解本题给出最终答案，不要转写题干。
只输出严格 JSON，不要 Markdown：
{{"std_answer":"最终答案","full_solution":"必要推导","ptype":"calc或proof","confidence":0.0,"risks":[]}}"""
    try:
        raw = generate([{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": prompt}]}], 2200)
        rec = try_parse_json(raw)
        if rec and str(rec.get("std_answer", "")).strip():
            merged = dict(prev)
            merged["std_answer"] = str(rec["std_answer"])
            merged["full_solution"] = str(rec.get("full_solution") or prev.get("full_solution") or "")
            merged["ptype"] = rec.get("ptype") if rec.get("ptype") in {"calc", "proof"} else prev.get("ptype", "calc")
            try:
                merged["confidence"] = min(float(prev.get("confidence") or 1.0), float(rec.get("confidence") or 1.0))
            except Exception:
                pass
            if rec.get("risks"):
                extra = rec["risks"] if isinstance(rec["risks"], list) else [str(rec["risks"])]
                merged["risks"] = list(prev.get("risks") or []) + extra
            return merged
    except Exception:
        pass
    return prev


def _locate_problems(images: list[Image.Image], problem_nos: list[str]) -> dict[str, dict]:
    """Pass 1 — cheap single call that maps each problem_no to the page(s) it appears on.

    Returns ``{problem_no: {"page_indices": [...], "snippet": "..."}}``.  This lets the
    per-question grading pass look at the right page and later cross-check that the model
    actually graded the requested problem number (the 3B model occasionally misaligns).
    """
    if not problem_nos:
        return {}
    locator_prompt = f"""你是高等数学阅卷助手。所给多张图片是一名学生的整份手写作业。
请逐页扫描，找出下列各题号分别出现在哪些图片页上，并摘录该题可见的题干开头片段。
待定位题号：{", ".join(problem_nos)}
图片按 0 起序号（第 0 张为第一页）。只输出严格 JSON，不要 Markdown。格式示例（<...> 为需你填写的字段，输出时不要保留尖括号）：
{{"mapping":[{{"problem_no":"<题号>","page_indices":[<图片序号>],"snippet":"<题干开头片段>"}}]}}
若某题号在图片中找不到，page_indices 留空数组 []。严禁照抄占位文字。"""
    content = [{"type": "image", "image": image} for image in images]
    content.append({"type": "text", "text": locator_prompt})
    raw = generate([{"role": "user", "content": content}], 900)
    locator = try_parse_json(raw)
    mapping: dict[str, dict] = {}
    if locator and isinstance(locator.get("mapping"), list):
        for item in locator["mapping"]:
            pn = str(item.get("problem_no") or "").strip()
            pidx = item.get("page_indices") or []
            mapping[pn] = {
                "page_indices": [int(x) for x in pidx if str(x).strip().isdigit()],
                "snippet": str(item.get("snippet") or ""),
            }
    return mapping


def _audit_completion_for_full_credit(
    images: list[Image.Image],
    problem_no: str,
    page_indices: list[int],
) -> dict | None:
    """Run a narrow visual audit before accepting a handwritten full-score result.

    The primary grader has to solve mathematics and locate a problem at once.
    This second, short pass deliberately ignores correctness and inspects only
    whether the handwriting visibly ends as a finished answer.
    """
    selected = [images[index] for index in page_indices if 0 <= index < len(images)] or images
    prompt = f"""\u4f60\u662f\u4e00\u540d\u4e25\u683c\u7684\u8bd5\u5377\u5b8c\u6574\u6027\u5ba1\u8ba1\u5458\u3002\u53ea\u68c0\u67e5\u9898\u53f7 {problem_no} \u7684\u5b66\u751f\u624b\u5199\u4f5c\u7b54\u662f\u5426\u5728\u7eb8\u9762\u4e0a\u771f\u6b63\u6536\u5c3e\uff0c\u4e0d\u5224\u65ad\u6570\u5b66\u8ba1\u7b97\u6b63\u8bef\u3002
\u82e5\u7b97\u5f0f\u6700\u540e\u4e00\u884c\u6709\u5b64\u7acb\u7684 +\u3001-\u3001=\u3001\u4e58\u9664\u53f7\uff0c\u672a\u95ed\u5408\u7684\u5206\u5f0f/\u62ec\u53f7\uff0c\u6216\u660e\u663e\u8fd8\u5728\u5199\u63a8\u5bfc\u4f46\u672a\u5199\u5b8c\u5f53\u524d\u5f0f\u5b50\uff0c\u5fc5\u987b\u5224\u5b9a work_complete=false\u3002\u5b8c\u6574\u4f46\u672a\u5316\u7b80\u7684\u5546\u6cd5\u5219\u516c\u5f0f\u4e0d\u7b97\u672a\u5b8c\u6210\u3002\u770b\u4e0d\u6e05\u65f6\u4e5f\u5224\u5b9a false\u3002
\u4ec5\u8f93\u51fa\u4e25\u683c JSON\uff1a{{"work_complete":true\u6216false,"completion_evidence":"\u4e0d\u8d85\u8fc730\u5b57\u7684\u89c6\u89c9\u8bc1\u636e"}}\u3002"""
    content = [{"type": "image", "image": image} for image in selected]
    content.append({"type": "text", "text": prompt})
    audit = try_parse_json(generate([{"role": "user", "content": content}], 280))
    if not isinstance(audit, dict) or not isinstance(audit.get("work_complete"), bool):
        return None
    return {
        "work_complete": bool(audit["work_complete"]),
        "completion_evidence": str(audit.get("completion_evidence") or ""),
    }


@app.post("/grade-homework")
def grade_homework(req: GradeHomeworkRequest):
    """Match handwritten pages to questions and grade each question independently.

    Two-pass design for precise problem-number locating:
      * Pass 1 (locator): one cheap call maps every problem_no to the page(s) it is on.
      * Pass 2 (grading): each question is graded on its located page, and the model must
        echo the problem number it actually matched (``located_problem_no``).  A mismatch
        forces ``need_review=True`` with a "题号定位可能错配" risk, so a misaligned grade
        is never silently accepted.
    Handwriting neatness is also scored (0-100) as an independent evaluation dimension.
    """
    ensure_model()
    try:
        images = [Image.open(io.BytesIO(base64.b64decode(value))).convert("RGB")
                  for value in req.images_base64]
    except Exception as exc:
        raise HTTPException(400, "invalid student work image") from exc

    problem_nos = [str(p.problem_no) for p in req.problems]
    locator = _locate_problems(images, problem_nos)

    results = []
    for problem in req.problems:
        located = locator.get(str(problem.problem_no), {})
        located_pages = located.get("page_indices", [])
        located_snippet = located.get("snippet", "")
        prompt = f"""你是严谨的高等数学阅卷教师。所给多张图片是一名学生的整份手写作业。
请在所有图片中定位题号 {problem.problem_no} 的学生作答，只批改这一题；这一步同时完成切题、题号匹配和手写识别。
题号定位提示：该题应出现在图片页 {located_pages if located_pages else '未知'}（0 起序号）；题干开头片段参考：{located_snippet or '未提供'}。
题目：{problem.problem_text or '题目文字缺失，请结合题号和标准答案定位'}
标准答案：{problem.std_answer}
参考解答：{problem.full_solution or '未提供'}
满分：{problem.max_score}

请逐步骤比较学生过程与参考解答。允许等价解法；不要因为书写形式不同扣分。若找不到作答、题号错配、图片不清、识别不确定或证明过程需教师判断，need_review 必须为 true。
你必须真实阅读图片中学生的实际作答，据此给出分数与中文反馈；严禁照抄下面的格式示例里的占位文字。
只输出严格 JSON，不要 Markdown。格式示例（<...> 为需你填写的字段，输出时不要保留尖括号）：
{{"located_problem_no":"<你实际定位到的题号，应等于 {problem.problem_no}>","located_problem_text":"<你实际看到的该题题干片段>","score": <数字>, "max_score": {problem.max_score}, "correct": true或false, "confidence": <0到1之间的数字>, "feedback": "<给学生的中文反馈>", "need_review": true或false, "work_complete": true或false, "completion_evidence": "<作答末尾是否完整的图像证据>", "recognized_work": "<识别到的学生步骤>", "matched_image_indices": [<图片序号>], "step_scores": [{{"step": "<步骤说明>", "score": <数字>, "max_score": <数字>}}], "handwriting_score": <0到100整数>, "handwriting_note": "<书写整洁度评价>", "risks": [<风险描述>]}}

首先核查作答是否真正完成：若图中算式末尾留有孤立的 +、-、=、乘除号，未写完的分式/括号，或只停在中间步骤而没有写完当前算式，work_complete 必须为 false，need_review 必须为 true，不得给满分或 correct=true。
一个完整的、未化简的正确求导/商法则算式仍可以视为完成；只有明显截断或缺步时才拦截。若作答完整性无法从图中确认，按未完成处理。
批改结束后请自检：located_problem_no 是否确实等于 {problem.problem_no}？若不等或无法确定，need_review 必须为 true 并在 risks 写明"题号定位可能错配"。
"""
        content = [{"type": "image", "image": image} for image in images]
        content.append({"type": "text", "text": prompt})
        raw = generate([{"role": "user", "content": content}], 1400)
        parsed = try_parse_json(raw)
        if not parsed:
            repair = f"""把以下阅卷草稿整理成严格 JSON，只输出 JSON。数学反斜杠须正确转义。
格式：{{"located_problem_no":"{problem.problem_no}","score":0.0,"max_score":{problem.max_score},"correct":false,"confidence":0.0,"feedback":"...","need_review":true,"work_complete":false,"completion_evidence":"无法从草稿确认完整性","recognized_work":"...","matched_image_indices":[],"step_scores":[],"handwriting_score":60,"handwriting_note":"","risks":[]}}
草稿：{raw[:5000]}"""
            parsed = try_parse_json(generate(
                [{"role": "user", "content": [{"type": "text", "text": repair}]}], 900
            ))
        if not parsed:
            parsed = {"score": 0, "correct": None, "confidence": 0,
                      "feedback": "AI 返回格式异常，请教师复核", "need_review": True,
                      "recognized_work": "", "matched_image_indices": [],
                      "step_scores": [], "handwriting_score": None,
                      "handwriting_note": "", "risks": ["模型结果无法解析"]}
        score = max(0.0, min(float(problem.max_score), float(parsed.get("score", 0) or 0)))
        # A separately prompted visual check prevents a correct intermediate
        # formula from silently becoming a full-credit result when the final
        # line visibly trails off.
        if parsed.get("correct") is True and score >= float(problem.max_score) - 1e-6:
            completion_audit = _audit_completion_for_full_credit(
                images, str(problem.problem_no), located_pages
            )
            if completion_audit is not None:
                parsed["work_complete"] = completion_audit["work_complete"]
                parsed["completion_evidence"] = completion_audit["completion_evidence"]
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0) or 0)))
        located_no = str(parsed.get("located_problem_no") or "").strip()
        located_text = str(parsed.get("located_problem_text") or "")
        mislocated = bool(located_no) and located_no != str(problem.problem_no)
        risks = list(parsed.get("risks", []) or [])
        if mislocated or not located_no:
            risks.append(f"题号定位可能错配：模型定位到 {located_no or '未知'}，期望 {problem.problem_no}")
        try:
            handwriting_score = int(parsed.get("handwriting_score")) if parsed.get("handwriting_score") is not None else None
            if handwriting_score is not None:
                handwriting_score = max(0, min(100, handwriting_score))
        except (TypeError, ValueError):
            handwriting_score = None
        work_complete = parsed.get("work_complete")
        if isinstance(work_complete, str):
            work_complete = work_complete.strip().lower() in {"true", "1", "yes", "complete", "完整"}
        if work_complete is False:
            score = min(score, float(problem.max_score) * 0.60)
            confidence = min(confidence, 0.60)
            risks.append("作答疑似未完成：" + str(parsed.get("completion_evidence") or "视觉识别结果"))
        need_review = bool(parsed.get("need_review", True)) or confidence < 0.85 or mislocated or work_complete is False
        results.append({
            "problem_id": problem.problem_id,
            "problem_no": problem.problem_no,
            "located_problem_no": located_no,
            "located_problem_text": located_text,
            "score": score,
            "max_score": float(problem.max_score),
            "correct": parsed.get("correct"),
            "confidence": confidence,
            "feedback": str(parsed.get("feedback", "")),
            "need_review": need_review,
            "work_complete": work_complete,
            "completion_evidence": str(parsed.get("completion_evidence", "")),
            "recognized_work": str(parsed.get("recognized_work", "")),
            "matched_image_indices": parsed.get("matched_image_indices", []),
            "step_scores": parsed.get("step_scores", []),
            "handwriting_score": handwriting_score,
            "handwriting_note": str(parsed.get("handwriting_note", "")),
            "risks": risks,
            "detail": {"grader": "Qwen2.5-VL-3B-Instruct", "vision": True},
        })
    return {"results": results, "model": "Qwen2.5-VL-3B-Instruct"}
