"""手写答案感知 Skill：只做视觉识别，不接收标准答案、不决定对错。"""
from __future__ import annotations

import json
import os
import urllib.request

from .registry import registry
from .schemas import AnswerPerceptionInput, AnswerPerceptionResult, FormulaRegion


@registry.register("answer_perception")
def answer_perception(payload: AnswerPerceptionInput, *, timeout_seconds: int = 600) -> AnswerPerceptionResult:
    request_body = {
        "images_base64": [payload.image_base64],
        # 刻意不包含 std_answer / full_solution：感知模型不应根据答案“看图猜对错”。
        "problems": [{"problem_id": payload.problem_id, "problem_text": payload.problem_text}],
    }
    url = os.environ.get("MATH_VLM_URL", "http://127.0.0.1:18080").rstrip("/") + "/grade-homework"
    try:
        request = urllib.request.Request(
            url, data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = json.loads(response.read().decode("utf-8"))
        item = (raw.get("results") or [{}])[0] if isinstance(raw, dict) else {}
        recognized = str(item.get("recognized_work") or "").strip()
        confidence = item.get("recognition_confidence", item.get("confidence"))
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        raw_regions = item.get("formula_regions") or item.get("regions") or []
        regions = [
            FormulaRegion(label=str(region.get("label") or "formula"),
                          bbox=list(region.get("bbox") or []),
                          confidence=region.get("confidence"))
            for region in raw_regions if isinstance(region, dict)
        ]
        if not recognized:
            return AnswerPerceptionResult(
                success=False, confidence=confidence, provider=str(raw.get("model") or "local-qwen"),
                formula_regions=regions, raw_response=raw,
                warnings=["未能从手写图片中识别出可用数学答案。"],
                error_code="EMPTY_OCR_RESULT",
            )
        warnings = [] if regions else ["视觉服务未返回公式区域；当前仅记录整张图片的识别结果。"]
        return AnswerPerceptionResult(
            success=True, confidence=confidence, recognized_work=recognized,
            formula_regions=regions, provider=str(raw.get("model") or "local-qwen"),
            raw_response=raw, warnings=warnings,
        )
    except TimeoutError:
        return AnswerPerceptionResult(success=False, confidence=0.0, error_code="PERCEPTION_TIMEOUT",
                                      warnings=["手写识别超时。"])
    except Exception as exc:
        return AnswerPerceptionResult(success=False, confidence=0.0, error_code="PERCEPTION_UNAVAILABLE",
                                      warnings=["手写识别服务不可用。"], raw_response={"detail": str(exc)[:200]})
