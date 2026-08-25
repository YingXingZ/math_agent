"""Generate a student homework PDF and a LaTeX-source export from an assignment.

This module reuses the A4 layout engine in
the repository's ``src/tools/build_worksheet.py`` (``WorksheetBuilder``):
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

import io
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import fitz

# --- reuse the workspace worksheet engine -----------------------------------
TOOLS_ROOT = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from build_worksheet import WorksheetBuilder  # noqa: E402  (workspace engine)

FONT_SONG = r"C:/Windows/Fonts/simsun.ttc"
FONT_HEI = r"C:/Windows/Fonts/simhei.ttf"
CONTENT_W = 595.28 - 56.7 - 45.4  # matches build_worksheet.py CONTENT_W

# Pre-compile the LaTeX detector.  We only treat tokens that look like real
# LaTeX commands (\frac, \lim, \sum, …) as formulae, never plain text.  The
# regex below matches either a backslash-letter command or a backslash symbol
# (e.g. \le, \ge, \to, \infty) followed by an optional braced group.
_LATEX_SCAN = re.compile(
    r"\\[A-Za-z]+(?:\s*\{[^{}]*\})*"
    r"|\\\([^()]*\)"
    r"|\\\[^[\]]*\]"
)

# Match the textbook-standard math delimiters: $$...$$ (display) first, then
# $...$ (inline).  Everything between the delimiters is a single formula span
# that mathtext should render as one glyph run, not token-by-token.
_MATH_DOLLAR = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$")


def _normalise_mathtext(tex: str) -> str:
    """Map textbook macros onto what matplotlib mathtext understands.

    mathtext is a deliberately small subset of TeX: it has no ``\\operatorname``
    and renders ``\\text`` poorly, so we swap those for ``\\mathrm``.  We also
    normalise the fraction/style macros.  Applied to the *inner* content of a
    $...$ span (delimiters already stripped by the caller).
    """
    return (
        tex.replace("\\operatorname", "\\mathrm")
           .replace("\\dfrac", "\\frac")
           .replace("\\tfrac", "\\frac")
           .replace("\\text{", "\\mathrm{")
           .replace("\\text ", "\\mathrm ")
    )


def _answer_space_for(question_type: str, content: str) -> int:
    """Heuristic answer-box height (pt). Proofs need more room than calculations."""
    if question_type == "证明题":
        return 165
    if "证明" in (content or ""):
        return 150
    if question_type in ("应用题", "综合题"):
        return 130
    return 110


# ---------------------------------------------------------------------------
# LaTeX rendering helpers (matplotlib mathtext → inline PNG)
# ---------------------------------------------------------------------------
def _has_matplotlib() -> bool:
    try:
        import matplotlib  # noqa: F401
        return True
    except Exception:
        return False


def _render_latex_png(tex: str, *, fontsize: float = 14.0) -> bytes | None:
    """Render a single LaTeX fragment to a tight PNG via matplotlib mathtext.

    Returns ``None`` when matplotlib is missing or the fragment is not
    renderable.  mathtext is intentionally preferred over a full TeX install:
    it covers the textbook formula vocabulary (\\frac, \\sqrt, \\lim, \\to,
    Greek letters, sums, integrals) without bringing in a 200+ MB TeX tree.
    """
    if not _has_matplotlib() or not tex.strip():
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as _plt

        # Normalise the textbook-style macros that mathtext does not recognise.
        # We swap them for an equivalent that mathtext renders cleanly, while
        # leaving everything else untouched.
        normalised = _normalise_mathtext(tex)

        fig = _plt.figure(figsize=(0.1, 0.1))
        try:
            fig.text(0, 0, "$" + normalised + "$", fontsize=fontsize)
            buf = io.BytesIO()
            _plt.savefig(
                buf, format="png", bbox_inches="tight", pad_inches=0.05,
                dpi=300, transparent=True,
            )
            return buf.getvalue()
        finally:
            _plt.close(fig)
    except Exception:
        return None


def render_problem_image(content: str, original_no: str, out_path: str | Path,
                         font_size: float = 11.5) -> str:
    """Render one problem (original number + text) to a cropped PNG for the engine.

    The engine scales the PNG back to ``CONTENT_W`` at 300 DPI, so we render at
    that DPI and crop to the text height to avoid empty trailing space.

    LaTeX-bearing substrings (``\\frac{...}``, ``\\lim``, etc.) are detected
    inline and rendered as proper formula glyphs via matplotlib mathtext; the
    rest of the line is drawn with the system Chinese font.  This makes the
    printable PDF match the textbook layout the teacher expects, instead of
    showing raw ``\\frac{1}{2}gt^2`` on the worksheet.
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

    y = margin
    max_x = CONTENT_W - margin
    line_h = font_size * 1.95
    cursor_x = margin

    def ew(text: str) -> float:
        """Rough width for a CJK+mixed string in simsun at ``font_size``.

        fitz's ``get_text_length`` rejects page-registered fonts, so we use a
        heuristic: CJK glyphs are full-width, Latin/digit/punct are about half.
        """
        w = 0.0
        for ch in text:
            if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f":
                w += font_size  # full-width CJK
            else:
                w += font_size * 0.55
        return w

    def newline():
        nonlocal y, cursor_x
        y += line_h
        cursor_x = margin

    def draw_literal(text: str):
        nonlocal cursor_x
        if not text:
            return
        w = ew(text)
        if cursor_x + w > max_x:
            newline()
        page.insert_text(
            fitz.Point(cursor_x, y + font_size),
            text, fontname="song", fontsize=font_size,
        )
        cursor_x += w + 1.5

    def draw_math(tex: str, *, from_text_span: bool = False):
        nonlocal cursor_x
        inner = tex.strip()
        if not inner:
            return
        png = _render_latex_png(inner, fontsize=font_size + 2.0)
        if png is None:
            # mathtext could not render the whole span (some mixes of `\le`,
            # `\in`, unmatched bars, etc. are out of mathtext's grammar).  Fall
            # back to per-token rendering so known `\cmd` fragments become
            # real glyphs while the rest of the line is drawn as text.  The
            # ``from_text_span`` guard breaks the cycle: a token called from
            # ``draw_text_span`` only falls back to literal text (not another
            # text-span pass) to avoid infinite recursion.
            if from_text_span:
                draw_literal(inner)
            else:
                draw_text_span(inner)
            return
        tmp = Path(tempfile.gettempdir()) / "_latex_seg.png"
        tmp.write_bytes(png)
        with fitz.open(str(tmp)) as png_doc:
            pix = png_doc[0].get_pixmap()
        aspect = pix.height / max(pix.width, 1)
        target_h = line_h * 0.95
        target_w = target_h / max(aspect, 0.01)
        if cursor_x + target_w > max_x:
            newline()
        img_rect = fitz.Rect(
            cursor_x, y + (line_h - target_h) * 0.45,
            cursor_x + target_w, y + (line_h + target_h) * 0.45,
        )
        page.insert_image(img_rect, filename=str(tmp))
        cursor_x += target_w + 2.0
        try:
            tmp.unlink()
        except Exception:
            pass

    def draw_text_span(text: str):
        """Draw a plain-text span, rendering any bare \\cmd tokens as math."""
        last = 0
        for m in _LATEX_SCAN.finditer(text):
            pre = text[last:m.start()]
            if pre:
                draw_literal(pre)
            draw_math(m.group(0), from_text_span=True)
            last = m.end()
        tail = text[last:]
        if tail:
            draw_literal(tail)

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if not line:
            y += line_h * 0.6
            continue
        cursor_x = margin
        # Tokenize on $ delimiters: text gaps are drawn with the CJK font, math
        # spans are rendered as one glyph run.  The $ delimiters themselves are
        # consumed as boundaries and never drawn.
        last = 0
        for m in _MATH_DOLLAR.finditer(line):
            text_seg = line[last:m.start()]
            if text_seg:
                draw_text_span(text_seg)
            inner = m.group(1) if m.group(1) is not None else m.group(2)
            draw_math(inner)
            last = m.end()
        tail = line[last:]
        if tail:
            draw_text_span(tail)
        y += line_h

    used_h = max(margin * 2 + font_size, y + margin)
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
