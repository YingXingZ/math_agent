"""answer_pdf_mineru_pipeline.py
=================================

Prototype pipeline (阶段 B 验证版):

    上传答案 PDF  ->  MinerU 解析(OCR+版面+公式)  ->  定位目标小节(如 1.1)
    ->  抽取每题答案(题号/小题/答案文本/页码/bbox)  ->  输出结构化 JSON

设计原则
--------
1. **解析器可插拔**: 优先用 MinerU(对扫描版 PDF 必需); 若 PDF 带文字层, 自动回退
   PyMuPDF(更快)。两种解析器都输出统一的 `Span`(页码 + 文本 + bbox)。
2. **绝不写入正式数据库**: 本模块不含任何 sqlite / DB 连接代码, 结果只写到
   `drafts/` 下的 JSON 草稿, 由人工/后续 Agent 阶段再决定如何入库。
3. **小节与目标题号可配置**: 默认定位 "1-1"(同济答案书里的 "习题 1-1", 与教材
   第 1 章第 1 节对应); 同时接受 "1.1" / "1-1" / "习题 1-1" 等写法。

用法
----
    # 直接处理整本(慢, 395 页)
    python answer_pdf_mineru_pipeline.py --pdf "同济高数8版-答案-上册(1).pdf" --section 1-1

    # 只解析前 60 页(快, 用于验证 §1.1)
    python answer_pdf_mineru_pipeline.py --pdf ... --section 1-1 --pages 60

    # 指定 MinerU 可执行文件(venv 内)
    python answer_pdf_mineru_pipeline.py --pdf ... --mineru-exe mineruenv/Scripts/mineru.exe
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------
@dataclass
class Span:
    """解析后的最小文本/公式单元: 页码 + 文本 + bbox(PDF 坐标, 点)。"""
    page_no: int
    text: str
    bbox: list            # [x0, y0, x1, y1]
    kind: str = "text"    # "text" | "formula"(公式图块)


@dataclass
class Problem:
    problem_no: str
    answer_text: str
    sub_items: list = field(default_factory=list)  # [{"sub_no","answer_text"}]
    source_page: int = 0
    bbox: list = field(default_factory=list)       # 该题起始行的 bbox


# --------------------------------------------------------------------------
# 解析器
# --------------------------------------------------------------------------
class BaseParser:
    name = "base"

    def parse(self, pdf: Path) -> list[Span]:
        raise NotImplementedError


class PyMuPDFParser(BaseParser):
    """适用于带文字层的数字版 PDF(本项目 4 本答案书均为扫描版, 此解析器仅作兜底)。"""
    name = "pymupdf"

    def parse(self, pdf: Path) -> list[Span]:
        import fitz  # pymupdf
        spans: list[Span] = []
        doc = fitz.open(str(pdf))
        for pi, page in enumerate(doc):
            words = page.get_text("words")
            if not words:
                continue
            groups = {}
            for w in words:
                x0, y0, x1, y1, t, b, ln, wn = w[:8]
                t = str(t).strip()
                if not t:
                    continue
                groups.setdefault((b, ln), []).append((x0, y0, x1, y1, t, wn))
            for items in groups.values():
                items.sort(key=lambda i: (i[0], i[5]))
                txt = " ".join(i[4] for i in items)
                spans.append(Span(
                    page_no=pi + 1,
                    text=txt,
                    bbox=[min(i[0] for i in items), min(i[1] for i in items),
                          max(i[2] for i in items), max(i[3] for i in items)],
                ))
        return spans


class MinerUParser(BaseParser):
    """MinerU: 扫描版 PDF 的 OCR + 版面分析 + 公式识别。产出 middle.json 供抽取。"""
    name = "mineru"

    def __init__(self, mineru_exe: Optional[str] = None):
        self.mineru_exe = mineru_exe or self._find_exe()

    @staticmethod
    def _find_exe() -> str:
        # 1) PATH
        found = shutil.which("mineru")
        if found:
            return found
        # 2) 本项目已知 venv
        for cand in (Path("mineruenv/Scripts/mineru.exe"),
                     Path("mineruenv/bin/mineru"),
                     Path("../mineruenv/Scripts/mineru.exe")):
            if cand.exists():
                return str(cand)
        raise RuntimeError(
            "未找到 MinerU 可执行文件。请先安装: pip install mineru "
            "(或用 --mineru-exe 指定路径)。"
        )

    def parse(self, pdf: Path, workdir: Path) -> list[Span]:
        if not self.mineru_exe:
            raise RuntimeError("MinerU 不可用")
        # 全部转成 Windows 原生绝对路径(避免 Git-Bash 的 /d/... 形式被 CreateProcess 拒绝)
        exe = str(Path(self.mineru_exe).resolve())
        pdf_arg = str(Path(pdf).resolve())
        out_arg = str(Path(workdir).resolve())
        workdir.mkdir(parents=True, exist_ok=True)
        # 扫描版数学答案书: 用 pipeline 后端 + ocr 方法最稳(避免 heavy VLM/hybrid)
        cmd = [exe, "-p", pdf_arg, "-o", out_arg,
               "--method", "ocr", "--backend", "pipeline"]
        print(f"[mineru] 运行: {' '.join(cmd)}", file=sys.stderr)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stdout, file=sys.stderr)
            print(proc.stderr, file=sys.stderr)
            raise RuntimeError(f"MinerU 解析失败 (exit={proc.returncode})")

        # 定位 middle.json
        middle = list(workdir.rglob("*_middle.json"))
        if not middle:
            raise RuntimeError(f"MinerU 未产出 middle.json, 检查 {workdir}")
        return self._read_middle(middle[0])

    @staticmethod
    def _read_middle(path: Path) -> list[Span]:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        spans: list[Span] = []
        pdf_info = data.get("pdf_info", [])
        for page in pdf_info:
            page_no = int(page.get("page_idx", 0)) + 1
            for block in page.get("para_blocks", []):
                btype = block.get("type")
                bbox = block.get("bbox") or [0, 0, 0, 0]
                if btype == "text":
                    for line in block.get("lines", []):
                        lbbox = line.get("bbox") or bbox
                        txt = ""
                        for sp in line.get("spans", []):
                            if sp.get("type") in (None, "text"):
                                txt += sp.get("content", "")
                        if txt.strip():
                            spans.append(Span(page_no, txt.strip(), lbbox, "text"))
                elif btype == "image":
                    # 公式以图块形式存在, 保留位置但文本留占位
                    spans.append(Span(page_no, "<formula>", bbox, "formula"))
        return spans


# --------------------------------------------------------------------------
# 小节定位
# --------------------------------------------------------------------------
SECTION_RE = re.compile(r"习题\s*([0-9]+)\s*[-.．]\s*([0-9]+)")


def normalize_section(s: str) -> str:
    """'1.1' -> '1-1' ; '1-1' -> '1-1' ; 兼容全/半角点号与空格。"""
    s = s.replace("．", ".").replace("·", ".").replace(" ", "")
    s = s.replace(".", "-")
    return s


def locate_section(spans: list[Span], target: str):
    """在 spans 中找到目标小节的文本区间。返回 dict 或 None。"""
    target = normalize_section(target)
    start_idx = None
    title = None
    for i, sp in enumerate(spans):
        m = SECTION_RE.search(sp.text)
        if m and f"{m.group(1)}-{m.group(2)}" == target:
            start_idx = i
            title = m.group(0)
            break
    if start_idx is None:
        return None
    end_idx = len(spans)
    for j in range(start_idx + 1, len(spans)):
        m = SECTION_RE.search(spans[j].text)
        if m and f"{m.group(1)}-{m.group(2)}" != target:
            end_idx = j
            break
    seg = spans[start_idx:end_idx]
    return {
        "title": title,
        "start_page": seg[0].page_no,
        "end_page": seg[-1].page_no,
        "spans": seg,
    }


# --------------------------------------------------------------------------
# 题目抽取
# --------------------------------------------------------------------------
# 顶层题号: 数字 + 分隔符(. ． 、 )) + 空白或行尾(避免把 "12.5" 这类小数误判为题号)
PROBLEM_RE = re.compile(r"^\s*([0-9]{1,3})\s*[\.．、)](?=\s|$)")
SUB_RE = re.compile(r"\(([0-9]+)\)")                       # 小题 "(1)" "(2)"


def _finalize_problem(raw_lines: list[str], problem_no: str,
                      source_page: int, bbox: list) -> Optional[Problem]:
    """把一题的若干原始行合并, 再按 '(n)' 切成小题。"""
    text = " ".join(l.strip() for l in raw_lines if l.strip())
    if not text:
        return None
    parts = re.split(r"(\(\d+\))", text)        # 保留分隔符
    answer_text = parts[0].strip()
    subs: list[dict] = []
    i = 1
    while i + 1 < len(parts):
        marker, content = parts[i], parts[i + 1]
        num = SUB_RE.search(marker)
        if num:
            subs.append({"sub_no": num.group(1), "answer_text": content.strip()})
        i += 2
    return Problem(problem_no=problem_no, answer_text=answer_text,
                   sub_items=subs, source_page=source_page, bbox=bbox)


def extract_problems(section_spans: list[Span]) -> list[Problem]:
    problems: list[Problem] = []
    cur_no: Optional[str] = None
    cur_page: int = 0
    cur_bbox: list = []
    raw_lines: list[str] = []

    def flush():
        nonlocal cur_no, cur_page, cur_bbox, raw_lines
        if cur_no is not None:
            p = _finalize_problem(raw_lines, cur_no, cur_page, cur_bbox)
            if p is not None:
                problems.append(p)
        cur_no = None
        raw_lines = []

    for sp in section_spans:
        txt = sp.text.strip()
        if not txt:
            continue
        m = PROBLEM_RE.match(txt)
        if m:                       # 新的顶层题号 -> 收尾上一题
            flush()
            cur_no = m.group(1)
            cur_page = sp.page_no
            cur_bbox = list(sp.bbox)
            raw_lines = [txt[m.end():].strip()]
            continue
        if cur_no is not None:      # 续行(含单独成行的 "(1) ...")
            raw_lines.append(txt)
    flush()
    return problems


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def build_subset_pdf(pdf: Path, pages: int, out: Path) -> Path:
    """抽取前 N 页生成临时 PDF, 加速 MinerU 演示(§1.1 在前部)。"""
    import fitz
    doc = fitz.open(str(pdf))
    n = min(pages, len(doc))
    sub = fitz.open()
    sub.insert_pdf(doc, from_page=0, to_page=n - 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    sub.save(str(out))
    sub.close()
    doc.close()
    return out


def run_pipeline(pdf: Path, target_section: str = "1-1",
                 parser_name: str = "auto", mineru_exe: Optional[str] = None,
                 pages: Optional[int] = None,
                 workdir: Optional[Path] = None) -> dict:
    pdf = Path(pdf)
    if not pdf.exists():
        raise FileNotFoundError(pdf)

    # 解析器选择
    if parser_name == "auto":
        # 先用 PyMuPDF 探测是否有文字层
        try:
            probe = PyMuPDFParser().parse(pdf)
            has_text = any(s.text.strip() for s in probe[:200])
        except Exception:
            has_text = False
        parser = PyMuPDFParser() if has_text else MinerUParser(mineru_exe)
    elif parser_name == "mineru":
        parser = MinerUParser(mineru_exe)
    else:
        parser = PyMuPDFParser()

    # 处理对象(整本或前 N 页)
    tmp_pdf = None
    target = pdf
    if pages:
        workdir = workdir or Path(tempfile.mkdtemp(prefix="mineru_"))
        tmp_pdf = build_subset_pdf(pdf, pages, workdir / (pdf.stem + f"_p1-{pages}.pdf"))
        target = tmp_pdf

    workdir = workdir or (Path(tempfile.mkdtemp(prefix="mineru_")) / "out")
    spans = parser.parse(target, workdir)

    # 定位小节
    located = locate_section(spans, target_section)
    if not located:
        return {
            "meta": {
                "source_pdf": str(pdf),
                "source_pdf_name": pdf.name,
                "parser": parser.name,
                "target_section": normalize_section(target_section),
                "found": False,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "note": "未找到目标小节; 该 PDF 可能不含此节或 OCR 未识别出 '习题 X-X' 标题。",
            },
            "problems": [],
        }

    problems = extract_problems(located["spans"])
    return {
        "meta": {
            "source_pdf": str(pdf),
            "source_pdf_name": pdf.name,
            "parser": parser.name,
            "target_section": normalize_section(target_section),
            "section_title": located["title"],
            "found": True,
            "page_span": [located["start_page"], located["end_page"]],
            "problem_count": len(problems),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "note": "DRAFT — 未写入正式数据库, 待人工/ Agent 阶段复核后入库。",
        },
        "problems": [asdict(p) for p in problems],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="答案 PDF -> MinerU -> 小节 -> JSON(草稿)")
    ap.add_argument("--pdf", required=True, help="答案 PDF 路径")
    ap.add_argument("--section", default="1-1", help="目标小节, 如 1-1 / 1.1 / 习题 1-1")
    ap.add_argument("--parser", default="auto", choices=["auto", "mineru", "pymupdf"])
    ap.add_argument("--mineru-exe", default=None, help="MinerU 可执行文件路径")
    ap.add_argument("--pages", type=int, default=None,
                    help="只解析前 N 页(加速演示, §1.1 在前部)")
    ap.add_argument("--out", default="drafts", help="JSON 草稿输出目录")
    args = ap.parse_args(argv)

    result = run_pipeline(Path(args.pdf), args.section, args.parser,
                          args.mineru_exe, args.pages)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{Path(args.pdf).stem}_sec-{result['meta']['target_section']}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(json.dumps(result["meta"], ensure_ascii=False, indent=2))
    print(f"\n已写出草稿: {out_path}", file=sys.stderr)
    return out_path


if __name__ == "__main__":
    main()
