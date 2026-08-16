#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务器端答案 PDF 解析流水线。

流程：答案 PDF -> MinerU(OCR+版面+公式) -> 定位目标小节 -> 逐题抽取 -> JSON 草稿。
本脚本不连接数据库；所有结果保持 persisted=false，必须经教师审核后才能写回。

调试入口：
  --pages N        只取 PDF 前 N 页运行 MinerU，避免整本反复 OCR
  --workdir DIR    保留 MinerU middle.json/Markdown/图片等中间产物
  --middle-json P  直接重放已有 *_middle.json，不重新运行 MinerU
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

MINERU_EXE = "/opt/mineru-env/bin/mineru"
# 在 NFKC + 去空白后的文本上匹配；兼容 . / - / 全角／Unicode 连字符。
SECTION_RE = re.compile(r"习题(\d+)[.\-‐‑‒–—−﹣－·。_](\d+)", re.IGNORECASE)
# 题号后可直接跟正文（"1.求..."），但不能把小数 "12.5" 误判为第 12 题。
PROBLEM_RE = re.compile(r"^\s*([0-9]{1,3})\s*([.、)）])\s*(?![0-9])")
SUB_RE = re.compile(r"[（(]\s*([0-9]+)\s*[）)]")


def normalize_text(text: str, *, compact: bool = False) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace("−", "-").replace("‐", "-").replace("‑", "-")
    value = value.replace("‒", "-").replace("–", "-").replace("—", "-")
    value = value.replace("﹣", "-").replace("－", "-")
    if compact:
        value = re.sub(r"\s+", "", value)
    return value


def normalize_section(value: str) -> str:
    value = normalize_text(value, compact=True)
    value = re.sub(r"^习题", "", value, flags=re.IGNORECASE)
    value = value.replace(".", "-").replace("·", "-").replace("。", "-").replace("_", "-")
    return value


def build_subset_pdf(pdf: Path, pages: int, destination: Path) -> Path:
    """用 pypdf 构造前 N 页小样；pypdf 随 MinerU 环境安装，不依赖 PyMuPDF。"""
    if pages <= 0:
        raise ValueError("--pages 必须大于 0")
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise RuntimeError("--pages 需要 pypdf；请在 MinerU venv 中运行本脚本") from exc
    reader = PdfReader(str(pdf))
    writer = PdfWriter()
    selected = min(int(pages), len(reader.pages))
    for page in reader.pages[:selected]:
        writer.add_page(page)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as fh:
        writer.write(fh)
    return destination


def run_mineru(pdf: Path, workdir: Path) -> tuple[dict, Path]:
    """调用 MinerU CLI，返回 middle.json 内容及路径。"""
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [MINERU_EXE, "-p", str(pdf.resolve()), "-o", str(workdir.resolve()),
           "--method", "ocr", "--backend", "pipeline"]
    print(f"[mineru] {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-4000:]
        raise RuntimeError(f"MinerU 解析失败 (exit={proc.returncode}): {tail}")
    middle_files = sorted(workdir.rglob("*_middle.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not middle_files:
        raise RuntimeError(f"MinerU 未产出 middle.json (workdir={workdir})")
    middle = middle_files[0]
    return json.loads(middle.read_text(encoding="utf-8")), middle


def middle_to_spans(data: dict) -> list[dict]:
    spans: list[dict] = []
    for page in data.get("pdf_info", []):
        page_no = int(page.get("page_idx", 0)) + 1
        for block in page.get("para_blocks", []):
            block_type = block.get("type")
            block_bbox = block.get("bbox") or [0, 0, 0, 0]
            if block_type not in {"image", "interline_equation", "table"}:
                # MinerU 会把小节标题标成 title，而非 text；必须保留所有有行文本的 block。
                for line in block.get("lines", []):
                    contents = []
                    kinds = []
                    for span in line.get("spans", []):
                        content = str(span.get("content", "") or "")
                        span_type = span.get("type") or "text"
                        if content:
                            contents.append(content)
                            kinds.append(span_type)
                    text = "".join(contents).strip()
                    if text:
                        spans.append({
                            "page": page_no,
                            "text": text,
                            "bbox": line.get("bbox") or block_bbox,
                            "kind": "title" if block_type == "title" else (
                                "formula" if kinds and all(k != "text" for k in kinds) else "text"
                            ),
                        })
            elif block_type in {"image", "interline_equation"}:
                text = "".join(
                    str(span.get("content", "") or "")
                    for line in block.get("lines", [])
                    for span in line.get("spans", [])
                ).strip() or "<formula>"
                spans.append({"page": page_no, "text": text, "bbox": block_bbox, "kind": "formula"})
    return spans


def _section_marker_at(spans: list[dict], index: int, max_window: int = 4):
    """从 index 起拼接同页 span；首 span 必须含“习”，避免从正文跨到下个标题。"""
    first = normalize_text(spans[index]["text"], compact=True)
    if "习" not in first:
        return None
    page = spans[index]["page"]
    combined = ""
    for end in range(index, min(len(spans), index + max_window)):
        if spans[end]["page"] != page:
            break
        combined += normalize_text(spans[end]["text"], compact=True)
        match = SECTION_RE.search(combined)
        if match:
            return {
                "section": f"{match.group(1)}-{match.group(2)}",
                "title": match.group(0),
                "start_index": index,
                "end_index": end,
            }
    return None


def find_section_markers(spans: list[dict]) -> list[dict]:
    markers: list[dict] = []
    occupied_until = -1
    for index in range(len(spans)):
        if index <= occupied_until:
            continue
        marker = _section_marker_at(spans, index)
        if marker:
            markers.append(marker)
            occupied_until = marker["end_index"]
    return markers


def locate_section(spans: list[dict], target: str):
    target_key = normalize_section(target)
    markers = find_section_markers(spans)
    selected = next((m for m in markers if m["section"] == target_key), None)
    if not selected:
        return None
    start = selected["start_index"]
    end = len(spans)
    for marker in markers:
        if marker["start_index"] > start and marker["section"] != target_key:
            end = marker["start_index"]
            break
    segment = spans[start:end]
    return {
        "title": selected["title"],
        "spans": segment,
        "start_page": segment[0]["page"],
        "end_page": segment[-1]["page"],
        "detected_markers": markers,
    }


def _union_bbox(boxes: list[list]) -> list[float]:
    valid = [box for box in boxes if isinstance(box, list) and len(box) == 4]
    if not valid:
        return []
    return [min(float(b[0]) for b in valid), min(float(b[1]) for b in valid),
            max(float(b[2]) for b in valid), max(float(b[3]) for b in valid)]


def extract_problems(segment_spans: list[dict]) -> list[dict]:
    problems: list[dict] = []
    current_no: str | None = None
    current_page = 0
    current_boxes: list[list] = []
    current_pages: set[int] = set()
    raw_lines: list[str] = []

    def flush() -> None:
        nonlocal current_no, current_page, current_boxes, current_pages, raw_lines
        if current_no is None:
            return
        text = " ".join(line.strip() for line in raw_lines if line.strip())
        if text:
            normalized = normalize_text(text)
            parts = re.split(r"([（(]\s*\d+\s*[）)])", normalized)
            answer_text = parts[0].strip()
            sub_items = []
            pos = 1
            while pos + 1 < len(parts):
                marker = SUB_RE.search(parts[pos])
                if marker:
                    sub_items.append({"sub_no": marker.group(1), "answer_text": parts[pos + 1].strip()})
                pos += 2
            problems.append({
                "problem_no": current_no,
                "answer_text": answer_text,
                "sub_items": sub_items,
                "source_page": current_page,
                "source_pages": sorted(current_pages),
                "bbox": _union_bbox(current_boxes),
                "source_image": None,
            })
        current_no, current_page = None, 0
        current_boxes, current_pages, raw_lines = [], set(), []

    for span in segment_spans:
        text = normalize_text(span["text"]).strip()
        if not text:
            continue
        match = PROBLEM_RE.match(text)
        if match:
            flush()
            current_no = match.group(1)
            current_page = int(span["page"])
            current_pages = {current_page}
            current_boxes = [list(span.get("bbox") or [])]
            raw_lines = [text[match.end():].strip()]
        elif current_no is not None:
            page_no = int(span["page"])
            current_pages.add(page_no)
            # source_image 采用题目起始页裁图；跨页信息另由 source_pages 保留。
            if page_no == current_page:
                current_boxes.append(list(span.get("bbox") or []))
            raw_lines.append(text)
    flush()
    return problems


def validate_middle_pdf(pdf: Path, data: dict, pages_limit: int | None = None) -> None:
    """阻止用 A 书的 middle.json 裁 B 书；允许 --pages 产生的前 N 页小样。"""
    from pypdf import PdfReader
    pdf_pages = len(PdfReader(str(pdf)).pages)
    parsed_pages = len(data.get("pdf_info", []))
    expected = min(pdf_pages, pages_limit) if pages_limit else pdf_pages
    if parsed_pages != expected:
        raise ValueError(
            f"PDF 与 middle.json 页数不一致：PDF={pdf_pages}，解析结果={parsed_pages}，"
            "不能生成可追溯 source_image"
        )


def render_problem_images(pdf: Path, data: dict, problems: list[dict], image_dir: Path,
                          render_scale: float = 2.0) -> None:
    """按 MinerU bbox 裁出每题起始页截图并回填 source_image。"""
    import pypdfium2 as pdfium

    image_dir.mkdir(parents=True, exist_ok=True)
    page_sizes = {
        int(page.get("page_idx", 0)) + 1: page.get("page_size") or []
        for page in data.get("pdf_info", [])
    }
    by_page: dict[int, list[tuple[int, dict]]] = {}
    for index, problem in enumerate(problems):
        by_page.setdefault(int(problem["source_page"]), []).append((index, problem))

    document = pdfium.PdfDocument(str(pdf))
    try:
        for page_no, entries in by_page.items():
            page = document[page_no - 1]
            bitmap = page.render(scale=render_scale)
            image = bitmap.to_pil()
            middle_size = page_sizes.get(page_no) or [image.width / render_scale, image.height / render_scale]
            sx = image.width / max(float(middle_size[0]), 1.0)
            sy = image.height / max(float(middle_size[1]), 1.0)
            for index, problem in entries:
                box = problem.get("bbox") or [0, 0, middle_size[0], middle_size[1]]
                padding = 10.0
                left = max(0, int((float(box[0]) - padding) * sx))
                top = max(0, int((float(box[1]) - padding) * sy))
                right = min(image.width, int((float(box[2]) + padding) * sx))
                bottom = min(image.height, int((float(box[3]) + padding) * sy))
                if right <= left or bottom <= top:
                    left, top, right, bottom = 0, 0, image.width, image.height
                filename = f"{normalize_section(problem.get('section_no') or 'section')}_q{problem['problem_no']}_p{page_no}_{index+1}.jpg"
                target = image_dir / filename
                image.crop((left, top, right, bottom)).convert("RGB").save(target, "JPEG", quality=92)
                problem["source_image"] = str(target)
    finally:
        document.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--section", default="1-1")
    parser.add_argument("--out", default="/opt/mineru_out")
    parser.add_argument("--pages", type=int, default=None,
                        help="只解析前 N 页，用于小范围调试")
    parser.add_argument("--workdir", default=None,
                        help="MinerU 工作目录；指定后保留 middle.json 等产物")
    parser.add_argument("--middle-json", default=None,
                        help="直接读取已有 *_middle.json，跳过 MinerU")
    args = parser.parse_args()

    pdf = Path(args.pdf)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{pdf.stem}_sec-{normalize_section(args.section)}.json"
    base = {
        "status": "error", "stage": "mineru_parse", "error": None,
        "meta": {
            "source_pdf": str(pdf), "source_pdf_name": pdf.name,
            "parser": "mineru", "backend": "pipeline+ocr",
            "target_section": normalize_section(args.section),
            "found": False, "page_span": None, "problem_count": 0,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "persisted": False, "output_file": str(output_path),
            "pages_limit": args.pages, "middle_json": args.middle_json,
        },
        "section": None, "problems": [],
    }

    temp_root: Path | None = None
    try:
        if not pdf.exists():
            raise FileNotFoundError(pdf)
        if args.middle_json:
            middle_path = Path(args.middle_json)
            data = json.loads(middle_path.read_text(encoding="utf-8"))
        else:
            if args.workdir:
                workdir = Path(args.workdir)
            else:
                temp_root = Path(tempfile.mkdtemp(prefix="mineru_parse_"))
                workdir = temp_root / "mineru-output"
            parse_pdf = pdf
            if args.pages:
                parse_pdf = build_subset_pdf(pdf, args.pages, workdir.parent / f"{pdf.stem}_p1-{args.pages}.pdf")
            data, middle_path = run_mineru(parse_pdf, workdir)
        base["meta"]["middle_json"] = str(middle_path)
        base["stage"] = "locate_section"
        spans = middle_to_spans(data)
        markers = find_section_markers(spans)
        base["meta"]["detected_sections"] = [m["section"] for m in markers]
        located = locate_section(spans, args.section)
        if not located:
            base["status"] = "ok"
            base["error"] = (
                f"未在解析结果中找到目标小节 '{args.section}'；"
                f"已识别章节：{base['meta']['detected_sections'][:30]}"
            )
        else:
            base["stage"] = "extract"
            problems = extract_problems(located["spans"])
            for problem in problems:
                problem["section_no"] = base["meta"]["target_section"]
            # source_image 是事实源的一部分：只允许 middle 与 PDF 页数一致时生成。
            validate_middle_pdf(pdf, data, args.pages)
            image_dir = out_dir / "source_images" / f"{pdf.stem}_sec-{base['meta']['target_section']}"
            render_problem_images(pdf, data, problems, image_dir)
            base["status"] = "ok"
            base["stage"] = "done"
            base["meta"].update({
                "found": True,
                "section_title": located["title"],
                "page_span": [located["start_page"], located["end_page"]],
                "problem_count": len(problems),
            })
            base["section"] = {
                "title": located["title"],
                "content_text": " ".join(span["text"] for span in located["spans"]),
            }
            base["problems"] = problems
        output_path.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(base, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        base["status"] = "error"
        base["error"] = f"[{base['stage']}] {type(exc).__name__}: {exc}"
        output_path.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(base, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    main()
