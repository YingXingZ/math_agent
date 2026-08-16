"""Deterministic publication gate for OCR/MinerU mathematics questions.

The validator is intentionally side-effect free.  It never edits text, runs OCR,
or writes a database.  Callers may use ``needs_formula_rescue`` to request one
Pix2Text attempt from the original crop, then validate the returned candidate
again with ``source_type='pix2text'``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


PASS_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.70


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    severity: str
    message: str
    position: int | None = None
    fragment: str = ""
    formula_rescue: bool = False


_SOURCE_DEFAULTS = {
    "manual": 1.0,
    "mineru": 0.90,
    "ocr": 0.78,
    "qwen": 0.80,
    "pix2text": 0.45,
    "unknown": 0.75,
}

_FORMULA_RESCUE_CODES = {
    "MATH_DELIMITER_UNCLOSED",
    "GROUP_BRACE_UNCLOSED",
    "GROUP_BRACE_ORPHAN_CLOSE",
    "LATEX_ENV_UNCLOSED",
    "LATEX_ENV_MISMATCH",
    "LATEX_ENV_ORPHAN_END",
    "LATEX_COMMAND_MISSING_ARGUMENT",
    "PAREN_UNCLOSED",
    "BRACKET_UNCLOSED",
    "BRACKET_MISMATCH",
    "OCR_REPLACEMENT_CHARACTER",
    "OCR_VERTICAL_FRAGMENT",
    "OCR_SUSPICIOUS_FORMULA",
    "OCR_SUSPICIOUS_LATIN_RUN",
}


def _issue(code: str, severity: str, message: str, position: int | None = None,
           fragment: str = "") -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity=severity,
        message=message,
        position=position,
        fragment=fragment,
        formula_rescue=code in _FORMULA_RESCUE_CODES,
    )


def _check_math_delimiters(text: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    stack: list[tuple[str, int]] = []
    pairs = {r"\(": r"\)", r"\[": r"\]"}
    reverse = {value: key for key, value in pairs.items()}
    index = 0
    dollar_open: tuple[str, int] | None = None
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            token = text[index:index + 2]
            if token in pairs:
                stack.append((token, index))
                index += 2
                continue
            if token in reverse:
                if not stack or stack[-1][0] != reverse[token]:
                    issues.append(_issue("MATH_DELIMITER_MISMATCH", "error", "数学定界符不匹配", index, token))
                else:
                    stack.pop()
                index += 2
                continue
            index += 2
            continue
        if text[index] == "$":
            token = "$$" if text[index:index + 2] == "$$" else "$"
            if dollar_open is None:
                dollar_open = (token, index)
            elif dollar_open[0] == token:
                dollar_open = None
            else:
                issues.append(_issue("MATH_DELIMITER_MISMATCH", "error", "美元数学定界符不匹配", index, token))
                dollar_open = None
            index += len(token)
            continue
        index += 1
    for token, position in stack:
        issues.append(_issue("MATH_DELIMITER_UNCLOSED", "error", "数学定界符未闭合", position, token))
    if dollar_open:
        issues.append(_issue("MATH_DELIMITER_UNCLOSED", "error", "美元数学定界符未闭合", dollar_open[1], dollar_open[0]))
    return issues


def _is_interval_mixed_pair(text: str, open_index: int, close_index: int) -> bool:
    fragment = text[open_index:close_index + 1]
    return "," in fragment or "，" in fragment


def _check_round_square_brackets(text: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    stack: list[tuple[str, int]] = []
    opening = {"(": ")", "[": "]", "（": "）", "【": "】"}
    closing = {value: key for key, value in opening.items()}
    for index, char in enumerate(text):
        escaped = index > 0 and text[index - 1] == "\\"
        if escaped:
            continue
        if char in opening:
            stack.append((char, index))
        elif char in closing:
            if not stack:
                issues.append(_issue("BRACKET_ORPHAN_CLOSE", "warning", "存在孤立的右括号", index, char))
                continue
            opened, position = stack[-1]
            if opening[opened] == char:
                stack.pop()
            elif opened in "([" and char in ")]" and _is_interval_mixed_pair(text, position, index):
                # Half-open intervals such as (a,b] and [a,b) are valid.
                stack.pop()
            else:
                stack.pop()
                issues.append(_issue("BRACKET_MISMATCH", "error", "圆括号或方括号不匹配", index, text[position:index + 1]))
    for opened, position in stack:
        code = "PAREN_UNCLOSED" if opened in "(（" else "BRACKET_UNCLOSED"
        issues.append(_issue(code, "error", "圆括号未闭合" if code == "PAREN_UNCLOSED" else "方括号未闭合", position, opened))
    return issues


def _check_group_braces(text: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    stack: list[int] = []
    for index, char in enumerate(text):
        escaped = index > 0 and text[index - 1] == "\\"
        if char == "{" and not escaped:
            stack.append(index)
        elif char == "}" and not escaped:
            if stack:
                stack.pop()
            else:
                issues.append(_issue("GROUP_BRACE_ORPHAN_CLOSE", "error", "存在孤立的 LaTeX 右花括号", index, "}"))
    for position in stack:
        issues.append(_issue("GROUP_BRACE_UNCLOSED", "error", "LaTeX 花括号未闭合", position, "{"))
    return issues


def _check_environments(text: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    stack: list[tuple[str, int]] = []
    for match in re.finditer(r"\\(begin|end)\s*\{([^{}]+)\}", text):
        action, name = match.group(1), match.group(2).strip()
        if action == "begin":
            stack.append((name, match.start()))
        elif not stack:
            issues.append(_issue("LATEX_ENV_ORPHAN_END", "error", f"存在孤立的 \\end{{{name}}}", match.start(), match.group(0)))
        elif stack[-1][0] != name:
            opened, _ = stack.pop()
            issues.append(_issue("LATEX_ENV_MISMATCH", "error", f"LaTeX 环境不匹配：{opened} / {name}", match.start(), match.group(0)))
        else:
            stack.pop()
    for name, position in stack:
        issues.append(_issue("LATEX_ENV_UNCLOSED", "error", f"LaTeX 环境 {name} 未闭合", position, name))
    return issues


def _consume_group(text: str, position: int, opener: str = "{", closer: str = "}") -> int | None:
    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text) or text[position] != opener:
        return None
    depth = 0
    for index in range(position, len(text)):
        if text[index] == opener and (index == 0 or text[index - 1] != "\\"):
            depth += 1
        elif text[index] == closer and (index == 0 or text[index - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _check_command_arguments(text: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    required = {"frac": 2, "dfrac": 2, "tfrac": 2, "sqrt": 1,
                "overline": 1, "underline": 1, "vec": 1}
    for match in re.finditer(r"\\([A-Za-z]+)", text):
        command = match.group(1)
        count = required.get(command)
        if not count:
            continue
        position = match.end()
        if command == "sqrt":
            optional = _consume_group(text, position, "[", "]")
            if optional is not None:
                position = optional
        ok = True
        for _ in range(count):
            next_position = _consume_group(text, position)
            if next_position is None:
                ok = False
                break
            position = next_position
        if not ok:
            issues.append(_issue(
                "LATEX_COMMAND_MISSING_ARGUMENT",
                "error",
                f"LaTeX 命令 \\{command} 缺少必需参数",
                match.start(),
                match.group(0),
            ))
    return issues


def _check_text_quality(text: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    stripped = text.strip()
    if len(stripped) < 10:
        issues.append(_issue("QUESTION_TOO_SHORT", "block", "题干缺失或过短"))
        return issues
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    one_char_lines = sum(len(line) <= 1 for line in lines)
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", stripped))
    if "�" in stripped:
        issues.append(_issue("OCR_REPLACEMENT_CHARACTER", "block", "题干含无法解码字符", stripped.index("�"), "�"))
    if len(lines) >= 7 and one_char_lines / len(lines) >= 0.45:
        issues.append(_issue("OCR_VERTICAL_FRAGMENT", "error", "题干是竖排公式碎片"))
    if cjk_count < 3 and len(stripped) > 20 and not re.search(r"\\[A-Za-z]+", stripped):
        issues.append(_issue("TEXT_READABILITY_LOW", "error", "题干缺少可读文字"))
    garbled = re.search(r"[←ζ]|(?:[一二三四五六七八九]oo)|(?:[VJH][0-9])|(?:[一丨川]（)|例\s*\d", stripped)
    if garbled:
        issues.append(_issue("OCR_SUSPICIOUS_FORMULA", "error", "题干含数学 OCR 污染", garbled.start(), garbled.group(0)))
    latin_run = re.search(r"\b[JHVUR]{3,}\b", stripped)
    if latin_run:
        issues.append(_issue("OCR_SUSPICIOUS_LATIN_RUN", "warning", "题干含可疑连续大写变量", latin_run.start(), latin_run.group(0)))
    if re.search(r"[（(][^）)]{0,80}[）)]", stripped):
        full = stripped.count("（") + stripped.count("）")
        half = stripped.count("(") + stripped.count(")")
        if full and half:
            issues.append(_issue("MIXED_WIDTH_BRACKET", "warning", "题干混用全角与半角圆括号"))
    return issues


def validate_question(
    content_text: str,
    *,
    source_type: str = "unknown",
    source_confidence: float | None = None,
    crop_image_path: str | None = None,
    compile_status: str = "not_checked",
) -> dict[str, Any]:
    """Return a structured validation report and a hard publication decision."""
    text = str(content_text or "")
    source = source_type if source_type in _SOURCE_DEFAULTS else "unknown"
    confidence = _SOURCE_DEFAULTS[source] if source_confidence is None else max(0.0, min(1.0, float(source_confidence)))
    issues = [
        *_check_text_quality(text),
        *_check_math_delimiters(text),
        *_check_round_square_brackets(text),
        *_check_group_braces(text),
        *_check_environments(text),
        *_check_command_arguments(text),
    ]
    # De-duplicate identical findings while preserving the first useful position.
    unique: list[ValidationIssue] = []
    seen: set[tuple[str, int | None, str]] = set()
    for item in issues:
        key = (item.code, item.position, item.fragment)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    issues = unique

    block_count = sum(item.severity == "block" for item in issues)
    error_count = sum(item.severity == "error" for item in issues)
    warning_count = sum(item.severity == "warning" for item in issues)
    structural_score = max(0.0, 1.0 - block_count - 0.24 * error_count - 0.08 * warning_count)
    final_confidence = round(min(confidence, structural_score), 2)
    if block_count:
        decision = "block"
    elif error_count or final_confidence < PASS_THRESHOLD:
        decision = "review" if final_confidence >= REVIEW_THRESHOLD or error_count else "block"
    else:
        decision = "pass"
    needs_rescue = any(item.formula_rescue for item in issues) and bool(crop_image_path)
    publish_allowed = decision == "pass" and not issues and final_confidence >= PASS_THRESHOLD
    return {
        "schema_version": "question-validation/v1",
        "decision": decision,
        "valid": not any(item.severity in {"block", "error"} for item in issues),
        "publish_allowed": publish_allowed,
        "score": final_confidence,
        "source_type": source,
        "source_confidence": round(confidence, 2),
        "issues": [asdict(item) for item in issues],
        "checks": {
            "text_readable": not any(item.code.startswith(("QUESTION_", "TEXT_", "OCR_")) for item in issues),
            "delimiter_balanced": not any(item.code.startswith("MATH_DELIMITER_") for item in issues),
            "group_braces_balanced": not any(item.code.startswith("GROUP_BRACE_") for item in issues),
            "environment_balanced": not any(item.code.startswith("LATEX_ENV_") for item in issues),
            "command_arguments_complete": not any(item.code == "LATEX_COMMAND_MISSING_ARGUMENT" for item in issues),
            "latex_compile": compile_status,
        },
        "needs_formula_rescue": needs_rescue,
        "rescue_reason": next((item.code for item in issues if item.formula_rescue), None),
        "crop_image_available": bool(crop_image_path),
    }


def first_issue_message(report: dict[str, Any]) -> str | None:
    issues = report.get("issues") or []
    return str(issues[0].get("message")) if issues else None
