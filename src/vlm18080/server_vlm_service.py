"""Private GPU vision-model service for math answer review."""

from __future__ import annotations

import base64
import ast
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from threading import Lock

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from PIL import Image
from qwen_vl_utils import process_vision_info
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration


MODEL_PATH = os.environ.get("MATH_VLM_MODEL", "/opt/math-vlm/models/Qwen2.5-VL-3B-Instruct")
MODEL_DEVICE_MAP = os.environ.get("MATH_VLM_DEVICE_MAP", "auto")
FORMULA_OCR_ENABLED = os.environ.get("FORMULA_OCR_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
FORMULA_OCR_MODEL_DIR = os.environ.get("FORMULA_OCR_MODEL_DIR", "/opt/math-vlm/models/PP-FormulaNet_plus-L")
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
    question_type: str = "计算题"
    max_score: float = 10.0


class GradeHomeworkRequest(BaseModel):
    images_base64: list[str]
    problems: list[GradeProblem]


class FormulaRecognitionRequest(BaseModel):
    image_base64: str


class LocateHomeworkRequest(BaseModel):
    images_base64: list[str]
    problem_nos: list[str]


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
            device_map=MODEL_DEVICE_MAP,
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
        "device_map": MODEL_DEVICE_MAP,
        "formula_ocr_enabled": FORMULA_OCR_ENABLED,
    }


@app.post("/recognize-formula")
def recognize_formula(req: FormulaRecognitionRequest):
    """Run FormulaNet on one isolated crop; it returns evidence, never a score."""
    if not FORMULA_OCR_ENABLED:
        raise HTTPException(503, "formula OCR is disabled")
    try:
        image = Image.open(io.BytesIO(base64.b64decode(req.image_base64))).convert("RGB")
    except Exception as exc:
        raise HTTPException(400, "invalid formula image") from exc
    try:
        with tempfile.TemporaryDirectory(prefix="math_formula_") as directory:
            source = os.path.join(directory, "formula.png")
            output_dir = os.path.join(directory, "out")
            image.save(source, format="PNG")
            completed = subprocess.run(
                [sys.executable, "-m", "paddleocr", "formula_recognition", "-i", source, "--model_name", "PP-FormulaNet_plus-L", "--model_dir", FORMULA_OCR_MODEL_DIR, "--device", "gpu:0", "--save_path", output_dir],
                capture_output=True, text=True, timeout=90, check=False,
            )
            output = (completed.stdout or "") + "\n" + (completed.stderr or "")
            match = re.search(r"(\{'res':\s*\{.*?\}\})", output, flags=re.DOTALL)
            payload = ast.literal_eval(match.group(1)) if match else {}
            latex = str((payload.get("res") or {}).get("rec_formula") or "").strip()
    except Exception as exc:
        raise HTTPException(502, f"formula OCR failed: {str(exc)[:120]}") from exc
    if not latex:
        raise HTTPException(422, "formula OCR returned no formula")
    return {"provider": "PP-FormulaNet_plus-L", "latex_text": latex}


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


def _normalize_answer_region(raw: object, page_indices: list[int]) -> dict | None:
    """Accept only conservative, normalized VLM region proposals.

    These coordinates are *suggestions* for the teacher UI, never grading
    evidence by themselves. Reject malformed, implausibly tiny, or
    page-mismatched rectangles before they reach the application.
    """
    if not isinstance(raw, dict):
        return None
    try:
        page_index = int(raw.get("page_index"))
        x = float(raw.get("x"))
        y = float(raw.get("y"))
        width = float(raw.get("width"))
        height = float(raw.get("height"))
    except (TypeError, ValueError):
        return None
    if page_index not in page_indices:
        return None
    if not (0.0 <= x < 1.0 and 0.0 <= y < 1.0 and 0.04 <= width <= 1.0 and 0.03 <= height <= 1.0):
        return None
    if x + width > 1.0001 or y + height > 1.0001:
        return None
    return {
        "page_index": page_index,
        "x": round(x, 4),
        "y": round(y, 4),
        "width": round(width, 4),
        "height": round(height, 4),
    }

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
图片按 0 起序号（第 0 张为第一页）。同时为每个确实找到作答的题给出“学生作答区域”的建议框，不要框题干、其他题或空白。坐标必须是 0 到 1 的比例：x/y 为左上角，width/height 为宽高；不确定时 answer_region 为 null。
只输出严格 JSON，不要 Markdown。格式示例（<...> 为需你填写的字段，输出时不要保留尖括号）：
{{"mapping":[{{"problem_no":"<题号>","page_indices":[<图片序号>],"snippet":"<题干开头片段>","answer_region":{{"page_index":0,"x":0.08,"y":0.20,"width":0.84,"height":0.30}}}}]}}
若某题号在图片中找不到，page_indices 留空数组 [] 且 answer_region 为 null。严禁照抄占位文字。"""
    content = [{"type": "image", "image": image} for image in images]
    content.append({"type": "text", "text": locator_prompt})
    raw = generate([{"role": "user", "content": content}], 900)
    locator = try_parse_json(raw)
    mapping: dict[str, dict] = {}
    if locator and isinstance(locator.get("mapping"), list):
        for item in locator["mapping"]:
            pn = str(item.get("problem_no") or "").strip()
            pidx = item.get("page_indices") or []
            page_indices = [int(x) for x in pidx if str(x).strip().isdigit()]
            mapping[pn] = {
                "page_indices": page_indices,
                "snippet": str(item.get("snippet") or ""),
            }
            answer_region = _normalize_answer_region(item.get("answer_region"), page_indices)
            if answer_region:
                mapping[pn]["answer_region"] = answer_region
    return mapping


def _grading_template(question_type: str) -> str:
    """Return the evidence requirements for the requested question type."""
    label = str(question_type or "").strip().lower()
    if any(token in label for token in ("证明", "proof")):
        return "证明题：评分点必须覆盖前提/定义或定理的正确使用、关键推理链、结论；不能把重复抄题或无关公式计分。"
    if any(token in label for token in ("选择", "单选", "多选", "choice")):
        return "选择题：先核对学生实际选择的选项及理由（如有）；仅对正确选项或明确等价判断计分，不能按长篇无关推导给分。"
    if any(token in label for token in ("填空", "fill", "填")):
        return "填空题：核对最终填入结果、符号、条件与必要单位；没有明确结果不得给分，过程仅可作为核对依据。"
    return "计算题：评分点必须覆盖方法选择、关键计算/变形、最终结论及必要条件；允许等价步骤，但不能给未出现的步骤计分。"

@app.post("/locate-homework")
def locate_homework(req: LocateHomeworkRequest):
    """Find physical heading rows first, then read only their left-edge digits.

    The model never invents coordinates: pixel rows come from dark-ink projection.
    Ambiguous rows are omitted so grading cannot treat a guessed heading as evidence.
    """
    ensure_model()
    import numpy as np
    from PIL import ImageEnhance

    try:
        pages = [Image.open(io.BytesIO(base64.b64decode(value))).convert("RGB") for value in req.images_base64]
    except Exception as exc:
        raise HTTPException(400, "invalid student work image") from exc
    allowed = {int(value) for value in req.problem_nos if str(value).strip().isdigit()}
    candidates = []
    layout_rows = []
    # First ask the VLM to inspect the complete submission and propose an
    # answer rectangle for every visible requested question.  The former
    # left-margin digit scan is retained below as a precision fallback, but it
    # misses perfectly valid work when students omit a large handwritten
    # heading or start in the middle of a page.
    semantic_numbers = set()
    try:
        semantic = _locate_problems(pages, [str(value) for value in sorted(allowed)])
        for raw_number, located in semantic.items():
            try:
                number = int(raw_number)
            except (TypeError, ValueError):
                continue
            if number not in allowed:
                continue
            region = located.get("answer_region") if isinstance(located, dict) else None
            if not isinstance(region, dict):
                continue
            try:
                page_index = int(region["page_index"])
                x = float(region["x"])
                y = float(region["y"])
                width = float(region["width"])
                height = float(region["height"])
            except (KeyError, TypeError, ValueError):
                continue
            if (0 <= page_index < len(pages) and 0 <= x < 1 and 0 <= y < 1
                    and width >= .04 and height >= .03 and x + width <= 1.0001
                    and y + height <= 1.0001):
                candidates.append({"page_index": page_index, "number": number,
                                   "x": round(x, 4), "y": round(y, 4),
                                   "width": round(width, 4), "height": round(height, 4),
                                   "source": "full_page_vlm"})
                semantic_numbers.add(number)
    except Exception:
        # A failed semantic pass must not prevent the older physical locator
        # from returning its useful high-confidence headings.
        pass
    if semantic_numbers == allowed and allowed:
        return {"candidates": candidates, "layout_rows": layout_rows,
                "page_count": len(pages), "strategy": "full_page_vlm"}
    for page_index, page in enumerate(pages):
        width, height = page.size
        grey = np.asarray(page.convert("L"))
        left = grey[:, max(0, int(width * .01)):max(1, int(width * .24))]
        ink = (left < 100).sum(axis=1)
        active = np.flatnonzero(ink >= 4)
        groups = []
        for y in active:
            if not groups or int(y) - groups[-1][-1] > 3:
                groups.append([int(y)])
            else:
                groups[-1].append(int(y))
        margin = grey[:, max(0, int(width * .01)):max(1, int(width * .12))]
        strong_ink = (margin < 100).sum(axis=1)
        strong_active = np.flatnonzero(strong_ink >= 4)
        strong_groups = []
        for marker_y in strong_active:
            if not strong_groups or int(marker_y) - strong_groups[-1][-1] > 3:
                strong_groups.append([int(marker_y)])
            else:
                strong_groups[-1].append(int(marker_y))
        for strong_group in strong_groups:
            if len(strong_group) >= 6 and height * .025 < strong_group[0] < height * .94:
                layout_rows.append({"page_index": page_index, "y": round((strong_group[0] + strong_group[-1]) / (2 * height), 4)})
        for group in groups:
            if len(group) < 5 or group[0] < height * .025 or group[0] > height * .94:
                continue
            y = (group[0] + group[-1]) / 2
            top = max(0, int(y - height * .028))
            bottom = min(height, int(y + height * .040))
            crop = page.crop((0, top, int(width * .32), bottom))
            crop = ImageEnhance.Contrast(crop).enhance(1.8)
            prompt = ("只辨认这张手写作业行左侧开头的大题题号。"
                      "必须是行首独立的大题编号，不是公式、页码、章节号、小问或解题步骤。"
                      "可能的题号：" + "、".join(map(str, sorted(allowed))) + "。"
                      "只输出一个阿拉伯数字；没有清楚的大题号就输出无。")
            raw = generate([{"role": "user", "content": [{"type": "image", "image": crop},
                     {"type": "text", "text": prompt}]}], 24).strip()
            if not re.fullmatch(r"\d{1,2}", raw):
                continue
            number = int(raw)
            if number not in allowed:
                continue
            candidates.append({"page_index": page_index, "number": number,
                               "y": round(y / height, 4), "raw": raw})
    return {"candidates": candidates, "layout_rows": layout_rows,
            "page_count": len(pages), "strategy": "full_page_vlm+heading_fallback"}
@app.post("/read-heading-crops")
def read_heading_crops(req: LocateHomeworkRequest):
    ensure_model()
    outputs = []
    for encoded in req.images_base64:
        image = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")
        options = "、".join(req.problem_nos)
        prompt = f"这张小图是手写作业某一行的左侧。请仔细辨认最左边的大题题号；可能的题号有：{options}。只输出你实际看到的阿拉伯数字；没有大题题号就输出无。"
        raw = generate([{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": prompt}]}], 40)
        outputs.append(raw.strip())
    return {"outputs": outputs}

@app.post("/read-work-crop")
def read_work_crop(req: SolveImageRequest):
    """Transcribe an isolated student answer without the grading key."""
    ensure_model()
    try:
        image = Image.open(io.BytesIO(base64.b64decode(req.image_base64))).convert("RGB")
    except Exception as exc:
        raise HTTPException(400, "invalid crop image") from exc
    prompt = "只逐行转写图片中学生亲笔写下的数学作答。保留公式和证明步骤；不要解题，不要补写，不要给分；看不清写[不清]。不要描述图片。"
    raw = generate([{"role": "user", "content": [{"type": "image", "image": image},
                    {"type": "text", "text": prompt}]}], 1200).strip()
    return {"text": raw}


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
    isolated_crop = len(images) == 1 and len(req.problems) == 1 and any(
        str(req.problems[0].problem_text).startswith(prefix)
        for prefix in ("【教师已确认", "【照片题号自动定位")
    )
    locator = {} if isolated_crop else _locate_problems(images, problem_nos)

    results = []
    for problem in req.problems:
        located = locator.get(str(problem.problem_no), {})
        located_pages = located.get("page_indices", [])
        located_snippet = located.get("snippet", "")
        suggested_region = located.get("answer_region")
        scope_instruction = ("这是一张已经裁切出的单题作答图片。不要再搜索其他页或要求图中出现完整教材题号；先逐行识别实际笔迹，再给候选评分。"
                             if isolated_crop else "所给多张图片是一名学生的整份手写作业。请定位指定题号的学生作答。")
        prompt = f"""你是严谨的高等数学阅卷教师。{scope_instruction}
只批改题号 {problem.problem_no}；不可把其他题或参考答案当作学生作答。
题号定位提示：该题应出现在图片页 {located_pages if located_pages else '未知'}（0 起序号）；题干开头片段参考：{located_snippet or '未提供'}；题区建议坐标仅供辅助定位：{suggested_region or '未提供'}。题目：{problem.problem_text or '题目文字缺失，请结合题号和标准答案定位'}
标准答案：{problem.std_answer}
参考解答：{problem.full_solution or '未提供'}
题型：{problem.question_type}
本题评分模板：{_grading_template(problem.question_type)}
满分：{problem.max_score}

请逐步骤比较学生过程与参考解答。允许等价解法；不要因为书写形式不同扣分。若找不到作答、题号错配、图片不清、识别不确定或证明过程需教师判断，need_review 必须为 true。
step_scores 必须列出 1–6 个实际可核对的评分点；每项须有明确 step、score、max_score，所有 max_score 合计必须严格等于本题满分，所有 score 合计必须严格等于 score。若没有可识别作答，返回 score=0 且 step_scores=[]，并 need_review=true；禁止把参考答案或其他题的文字当作学生作答计分。
你必须真实阅读图片中学生的实际作答，据此给出分数与中文反馈；严禁照抄下面的格式示例里的占位文字。
只输出严格 JSON，不要 Markdown。格式示例（<...> 为需你填写的字段，输出时不要保留尖括号）：
{{"located_problem_no":"<你实际定位到的题号，应等于 {problem.problem_no}>","located_problem_text":"<你实际看到的该题题干片段>","score": <数字>, "max_score": {problem.max_score}, "correct": true或false, "confidence": <0到1之间的数字>, "feedback": "<给学生的中文反馈>", "need_review": true或false, "recognized_work": "<识别到的学生步骤>", "matched_image_indices": [<图片序号>], "step_scores": [{{"step": "<步骤说明>", "score": <数字>, "max_score": <数字>}}], "handwriting_score": <0到100整数>, "handwriting_note": "<书写整洁度评价>", "risks": [<风险描述>]}}
批改结束后请自检：located_problem_no 是否确实等于 {problem.problem_no}？若不等或无法确定，need_review 必须为 true 并在 risks 写明"题号定位可能错配"。
"""
        content = [{"type": "image", "image": image} for image in images]
        content.append({"type": "text", "text": prompt})
        raw = generate([{"role": "user", "content": content}], 1400)
        parsed = try_parse_json(raw)
        if not parsed:
            repair = f"""把以下阅卷草稿整理成严格 JSON，只输出 JSON。数学反斜杠须正确转义。
格式：{{"located_problem_no":"{problem.problem_no}","score":0.0,"max_score":{problem.max_score},"correct":false,"confidence":0.0,"feedback":"...","need_review":true,"recognized_work":"...","matched_image_indices":[],"step_scores":[],"handwriting_score":60,"handwriting_note":"","risks":[]}}
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
        if isolated_crop and not str(parsed.get("recognized_work") or "").strip():
            # Retry transcription without exposing the standard answer. A failed
            # first grading pass must not turn a visibly written crop into a
            # fabricated blank answer. Any recovered text stays review-only.
            ocr_prompt = "只转写这张裁切图片中学生实际写下的数学作答，逐行保留公式和文字；不要解题、不要补写、不要输出参考答案。看不清的地方写[不清]。"
            ocr_raw = generate([{"role": "user", "content": [
                {"type": "image", "image": images[0]}, {"type": "text", "text": ocr_prompt}]}], 650).strip()
            if len(ocr_raw) >= 8 and not any(word in ocr_raw[:30] for word in ("无法识别", "没有作答", "未见作答")):
                parsed["recognized_work"] = ocr_raw
                parsed["independent_ocr"] = True
                parsed["need_review"] = True
                parsed["score"] = 0.0
                parsed["step_scores"] = []
                parsed["risks"] = list(parsed.get("risks") or []) + ["初次识别失败；已独立重读笔迹，评分需教师确认"]
        score = max(0.0, min(float(problem.max_score), float(parsed.get("score", 0) or 0)))
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0) or 0)))
        located_no = str(problem.problem_no) if isolated_crop else str(parsed.get("located_problem_no") or "").strip()
        located_text = str(parsed.get("located_problem_text") or "")
        mislocated = (not isolated_crop) and bool(located_no) and located_no != str(problem.problem_no)
        risks = list(parsed.get("risks", []) or [])
        if mislocated or not located_no:
            risks.append(f"题号定位可能错配：模型定位到 {located_no or '未知'}，期望 {problem.problem_no}")
        try:
            handwriting_score = int(parsed.get("handwriting_score")) if parsed.get("handwriting_score") is not None else None
            if handwriting_score is not None:
                handwriting_score = max(0, min(100, handwriting_score))
        except (TypeError, ValueError):
            handwriting_score = None
        need_review = bool(parsed.get("need_review", True)) or confidence < 0.85 or mislocated
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
            "recognized_work": str(parsed.get("recognized_work", "")),
            "matched_image_indices": parsed.get("matched_image_indices", []),
            # UI-only proposal. It becomes grading evidence only after the
            # teacher explicitly accepts/saves this rectangle in the mapper.
            "suggested_region": located.get("answer_region"),
            "step_scores": parsed.get("step_scores", []),
            "handwriting_score": handwriting_score,
            "handwriting_note": str(parsed.get("handwriting_note", "")),
            "risks": risks,
            "detail": {"grader": "Qwen2.5-VL-3B-Instruct", "vision": True},
        })
    return {"results": results, "model": "Qwen2.5-VL-3B-Instruct"}
