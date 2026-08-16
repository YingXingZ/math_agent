"""Generate a student homework PDF and a LaTeX-source export from an assignment.

This module reuses the A4 layout engine in
``D:/workbuddy/2026-08-06-15-31-48/build_worksheet.py`` (``WorksheetBuilder``):
it already implements the exact "original screenshot fidelity + dashed answer
blank" design (设计二：留答题空白).  We drive it with one rendered image per
selected problem so that:

* the original textbook problem number is preserved (设计二：保持原本题号);
* a dashed answer box is drawn under every problem;
* the whole sheet is a real, printable A4 PDF (设计一.1：一键发布 PDF).

Problems are rendered from their (LaTeX-bearing) text.  When a local crop image
is available it can be passed in via ``img_path`` and embedded verbatim.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import fitz

# --- reuse the workspace worksheet engine -----------------------------------
_WORKSPACE = Path(r"D:/workbuddy/2026-08-06-15-31-48")
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from build_worksheet import WorksheetBuilder  # noqa: E402  (workspace engine)

FONT_SONG = r"C:/Windows/Fonts/simsun.ttc"
FONT_HEI = r"C:/Windows/Fonts/simhei.ttf"
CONTENT_W = 595.28 - 56.7 - 45.4  # matches build_worksheet.py CONTENT_W


def _answer_space_for(question_type: str, content: str) -> int:
    """Heuristic answer-box height (pt). Proofs need more room than calculations."""
    if question_type == "证明题":
        return 165
    if "证明" in (content or ""):
        return 150
    if question_type in ("应用题", "综合题"):
        return 130
    return 110


def render_problem_image(content: str, original_no: str, out_path: str | Path,
                         font_size: float = 11.5) -> str:
    """Render one problem (original number + text) to a cropped PNG for the engine.

    The engine scales the PNG back to ``CONTENT_W`` at 300 DPI, so we render at
    that DPI and crop to the text height to avoid empty trailing space.
    """
    doc = fitz.open()
    page = doc.new_page(width=CONTENT_W, height=2000)
    page.insert_font(fontname="song", fontfile=FONT_SONG)
    page.insert_font(fontname="hei", fontfile=FONT_HEI)
    margin = 6.0
    label = f"{original_no}. " if original_no else ""
    body = (content or "").replace("\r", "").lstrip()
    # 8014 content_text already contains the textbook number (e.g. "10. ...").
    # Strip it before prepending the *canonical* original number so we do not
    # print "10. 10. ..." on the worksheet.
    body = re.sub(rf"^{re.escape(original_no)}\.\s*", "", body)
    full = label + body
    rect = fitz.Rect(margin, margin, CONTENT_W - margin, 1990)
    leftover = page.insert_textbox(
        rect, full, fontname="song", fontsize=font_size, lineheight=1.55, align=0,
    )
    used = (1990 - margin) - (leftover if leftover > 0 else 0)
    used_h = max(margin * 2 + font_size, used + margin)
    clip = fitz.Rect(0, 0, CONTENT_W, used_h)
    pix = page.get_pixmap(dpi=300, clip=clip)
    pix.save(str(out_path))
    doc.close()
    return str(out_path)


def _items_to_render(assignment: dict[str, Any], items: list[dict[str, Any]],
                     workdir: Path) -> list[dict[str, Any]]:
    """Normalise DB rows into render descriptors (with temp images if needed)."""
    descriptors: list[dict[str, Any]] = []
    for idx, row in enumerate(items):
        original_no = str(row.get("original_no") or row.get("sort_order") or (idx + 1))
        content = (row.get("content") or "").strip()
        img_path = row.get("img_path")
        if not img_path:
            tmp = workdir / f"_prob_{original_no}.png"
            render_problem_image(content, original_no, tmp)
            img_path = str(tmp)
        descriptors.append({
            "original_no": original_no,
            "content": content,
            "img_path": img_path,
            "answer_space": _answer_space_for(str(row.get("question_type") or ""), content),
        })
    return descriptors


def _format_due(value: Any) -> str:
    """Turn an ISO datetime string into a Chinese-readable date/time."""
    text = str(value or "").strip()
    if not text:
        return ""
    # Accept 2026-08-21T23:59:00+00:00 or 2026-08-21T23:59:00
    for pat in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return __import__("datetime").datetime.strptime(text, pat).strftime("%Y年%m月%d日 %H:%M")
        except ValueError:
            continue
    return text


def build_assignment_pdf(assignment: dict[str, Any], items: list[dict[str, Any]],
                         out_path: str | Path) -> tuple[str, int]:
    """Build the printable A4 homework PDF. Returns (path, page_count)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        descriptors = _items_to_render(assignment, items, workdir)
        wb = WorksheetBuilder(
            title=assignment.get("title") or "高等数学作业",
            subtitle=f"章节 {assignment.get('chapter') or ''} · 课后作业",
            # 班级/学号/姓名留空给学生填写；右上 meta 只放截止时间和教材信息。
            meta_line=f"截止时间：{_format_due(assignment.get('due_at'))}　教材：李继成《高等数学》第二版",
        )
        for d in descriptors:
            wb.add_problem(d["img_path"], d["answer_space"])
        path, n = wb.save(str(out_path))
    return path, n


def export_latex_source(assignment: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Machine-readable LaTeX source for every selected problem (设计二：含 latex 源码)."""
    return {
        "title": assignment.get("title"),
        "chapter": assignment.get("chapter"),
        "class_name": assignment.get("class_name"),
        "due_at": assignment.get("due_at"),
        "problems": [
            {
                "original_no": str(row.get("original_no") or row.get("sort_order")),
                "latex": (row.get("content") or "").strip(),
                "question_type": row.get("question_type"),
            }
            for row in items
        ],
    }


def latex_document(assignment: dict[str, Any], items: list[dict[str, Any]]) -> str:
    """Wrap the LaTeX source into a minimal, compilable ``article`` document."""
    data = export_latex_source(assignment, items)
    body = []
    for p in data["problems"]:
        body.append(f"\\subsection*{{第 {p['original_no']} 题}}\n{p['latex']}\n\\vspace{{3cm}}\n")
    problems = "\n".join(body)
    return (
        "\\documentclass[12pt]{{article}}\n"
        "\\usepackage[UTF8]{{ctex}}\n"
        "\\usepackage{{amsmath,amssymb}}\n"
        "\\begin{{document}}\n"
        f"\\title{{{data['title'] or '高等数学作业'}}}\n"
        f"\\author{{章节 {data['chapter'] or ''} \\quad 班级 {data['class_name'] or ''}}}\n"
        "\\maketitle\n"
        f"\\noindent 姓名：\\underline{{\\hspace{{4cm}}}} \\quad 学号：\\underline{{\\hspace{{3cm}}}} \\quad 成绩：\\underline{{\\hspace{{2cm}}}}\n\n"
        f"{problems}"
        "\\end{{document}}\n"
    )
