#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Route 2 —— 从"习题解答册"PDF 抽取 §5.1 并入库 8014。

与 route2_chapter_importer.py 的区别：输入是整本 OCR PDF（题目+解答逐题紧挨），
而非"每图一题"的裁切图。本脚本：
  1. 用 PyMuPDF 定位 §5.1（"习题5.1" → "习题5.2" 之间）；
  2. 按题号（"1."..."18."）切分，并跨页合成（不少题解答跨页，如第4题横跨两页）；
  3. 每题裁成一张图，送 VLM /solve-from-image 读图识别（比脏 OCR 文本层更准）；
  4. 组装 book_problems.json 并复用 importer 的 push_local 推到 8014 /ingest/book。

用法：
  python route2_pdf_extract.py --dry-run [--limit N]      # 仅生成 JSON+crops，不入库
  python route2_pdf_extract.py --push   [--limit N]      # 生成并写入本地 8014
"""
import argparse
import base64
import io
import json
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

# 复用既有导入器的 VLM 调用与入库逻辑（同目录）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from route2_chapter_importer import (  # noqa: E402
    recognize_crop, push_local, build_book_json, _make_record,
    VLM_URL, EVIDENCE_DIR, EVIDENCE_URL,
)

PDF_PATH = (r"E:/xwechat_files/wxid_l7836vvhxpdh11_4125/msg/file/2026-08/"
            r"李继成高数-答案-下册-OCR.pdf")
SECTION_NO = "5.1"
SECTION_KPS_MAP = {
    "5.1": ["向量代数与空间解析几何", "空间直角坐标系"],
    "5.2": ["向量代数", "数量积", "向量积", "混合积"],
    "5.3": ["平面及其方程"],
    "5.4": ["空间直线及其方程"],
    "5.5": ["曲面与空间曲线", "旋转曲面"],
    "5.6": ["二次曲面", "柱面", "锥面"],
}


def section_title(section_no: str) -> str:
    return f"第五章 向量代数与空间解析几何 · 习题{section_no}"


DPI = 200
# 行首题号 "12."；OCR 常把 "1." 误成小写 l / 大写 I / 竖线 |，统一视作 1；
# 也允许题号后无小数点（脏 OCR 把 "9." 打成 "9 "），故 . 设为可选
PROB_RE = re.compile(r"^\s*([0-9lI|])([0-9]?)\s*[\.\u3002]?\s")
LEFT_FRAC = 0.34                                    # 题号须在页面左侧 34% 区域内


def _parse_prob_no(g1: str, g2: str) -> int:
    g1 = g1.replace("l", "1").replace("I", "1").replace("|", "1")
    return int(g1 + (g2 or ""))


def find_section_span(doc, start_marker="习题5.1", end_marker="习题5.2"):
    start_idx = end_idx = None
    for i in range(doc.page_count):
        t = doc[i].get_text()
        if start_idx is None and start_marker in t:
            start_idx = i
        if start_idx is not None and end_marker in t:
            end_idx = i
            break
    if start_idx is None:
        raise SystemExit(f"[error] 未找到 {start_marker}")
    if end_idx is None:
        end_idx = doc.page_count - 1
    print(f"[span] {start_marker} @{start_idx+1}页  →  {end_marker} @{end_idx+1}页")
    return start_idx, end_idx


def find_heading_y(doc, page_idx, marker):
    """Return the y of the section-heading line on a page, or None."""
    page = doc[page_idx]
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            txt = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
            if marker in txt:
                return line["bbox"][1]
    return None


def page_problem_starts(doc, page_idx, end_marker_y=None):
    """返回该页所有题号起始 (y0, number)，可选 end_marker_y 截断（排除 5.2）。"""
    page = doc[page_idx]
    W = page.rect.width
    starts = []
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            txt = "".join(s.get("text", "") for s in spans).strip()
            x0 = min(s.get("bbox", [0, 0, 0, 0])[0] for s in spans)
            m = PROB_RE.match(txt)
            if m and (x0 / W) < LEFT_FRAC:
                y0 = line["bbox"][1]
                if end_marker_y is not None and y0 >= end_marker_y:
                    continue
                starts.append((round(y0, 1), _parse_prob_no(m.group(1), m.group(2))))
    starts.sort()
    # 去重同号（保留最靠上的）
    seen, uniq = set(), []
    for y, n in starts:
        if n in seen:
            continue
        seen.add(n), uniq.append((y, n))
    return uniq


def render_slice(page, clip):
    pm = page.get_pixmap(dpi=DPI, clip=clip)
    return Image.open(io.BytesIO(pm.tobytes("png"))).convert("RGB")


def composite_crop(doc, start, nxt):
    """跨页合成从 (page_a,y_a) 到 (page_b,y_b) 的纵向切片。"""
    page_a, y_a = start
    page_b, y_b = nxt if nxt else (start[0], doc[start[0]].rect.height)
    slices = []
    if page_a == page_b:
        page = doc[page_a]
        slices.append(render_slice(page, fitz.Rect(0, y_a, page.rect.width, y_b)))
    else:
        pa = doc[page_a]
        slices.append(render_slice(pa, fitz.Rect(0, y_a, pa.rect.width, pa.rect.height)))
        for p in range(page_a + 1, page_b):
            slices.append(render_slice(doc[p], fitz.Rect(0, 0, doc[p].rect.width, doc[p].rect.height)))
        pb = doc[page_b]
        slices.append(render_slice(pb, fitz.Rect(0, 0, pb.rect.width, y_b)))
    W = max(s.width for s in slices)
    H = sum(s.height for s in slices)
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    y = 0
    for s in slices:
        canvas.paste(s, (0, y))
        y += s.height
    return canvas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=PDF_PATH)
    ap.add_argument("--section", default=SECTION_NO)
    ap.add_argument("--title", default=None)
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 题（调试）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()
    if not (args.dry_run or args.push):
        ap.error("需指定 --dry-run 或 --push")

    out_dir = Path(__file__).resolve().parent / "route2_input" / f"{args.section}_pdf"
    crops_dir = out_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(args.pdf)
    # 推导本小节的起止标记（5.2→5.3，…，5.6→第六章）
    try:
        major, minor = args.section.split(".")
        next_minor = str(int(minor) + 1)
        next_marker = f"习题{major}.{next_minor}"
    except Exception:
        next_marker = "第六章"
    if not any(next_marker in doc[i].get_text() for i in range(doc.page_count)):
        next_marker = "第六章"
    start_marker = f"习题{args.section}"
    start_idx, end_idx = find_section_span(doc, start_marker, next_marker)
    heading_y = find_heading_y(doc, start_idx, start_marker)
    section_kps = SECTION_KPS_MAP.get(args.section, ["向量代数与空间解析几何"])

    # 收集全部题号起始点（跨页）；记录每个题号的 (page, y0)
    all_starts = []  # (page_idx, y0, number)
    # 在 end_idx 页找到 next_marker 的 y，用于排除下一小节题号
    end_marker_y = None
    if end_idx is not None:
        for block in doc[end_idx].get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                txt = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
                if next_marker in txt:
                    end_marker_y = line["bbox"][1]
    for p in range(start_idx, end_idx + 1):
        hy = heading_y if p == start_idx else None
        for y, n in page_problem_starts(doc, p, end_marker_y if p == end_idx else None):
            if hy is not None and y < hy:
                continue  # 起始页顶部的上一小节残留题号（如 5.2 页顶的 5.1 第18题）
            all_starts.append((p, y, n))
    # 按 (page, y) 排序，得到有序题号序列
    all_starts.sort(key=lambda t: (t[0], t[1]))
    # 去重（同一题号可能在前一页结尾与后一页开头重复检测到，保留先出现的）
    seen, ordered = set(), []
    for p, y, n in all_starts:
        if n in seen:
            continue
        seen.add(n), ordered.append((p, y, n))

    # 习题通常从 1 连续编号；丢弃超出"最大完整前缀 1..K"的离群号
    # （脏 OCR 会把某行误识别成 17. 之类，这类号不在 1..K 内，必为噪声）
    nums = set(n for _, _, n in ordered)
    K = 0
    while (K + 1) in nums:
        K += 1
    if nums and K < max(nums):
        dropped = sorted(x for x in nums if x > K)
        print(f"[filter] 丢弃离群题号(超出 1..{K}): {dropped}")
        ordered = [(p, y, n) for (p, y, n) in ordered if n <= K]

    print(f"[detect] §{args.section} 共检测到 {len(ordered)} 题：",
          [n for _, _, n in ordered])

    entries = []
    crop_map = {}
    for i, (p, y, n) in enumerate(ordered):
        if args.limit and i >= args.limit:
            break
        # 裁切终点：优先取下一题起始；最后一题截止到"习题5.2"行（避免把 5.2 卷进来）
        if i + 1 < len(ordered):
            nxt_pair = (ordered[i + 1][0], ordered[i + 1][1])
        elif p == end_idx and end_marker_y is not None:
            nxt_pair = (end_idx, end_marker_y)
        else:
            nxt_pair = None
        crop_img = composite_crop(doc, (p, y), nxt_pair)
        cpath = crops_dir / f"{args.section}-{n}.png"
        crop_img.save(cpath, "PNG")
        crop_map[cpath.name] = cpath
        print(f"  · 题 {n}: crop {cpath.name} ({crop_img.width}x{crop_img.height})")
        recog = recognize_crop(cpath, args.section, str(n))
        if recog.get("error"):
            print(f"    [VLM error] {recog['error']}")
        content = recog.get("problem_text") or ""
        std = recog.get("std_answer") or ""
        sol = recog.get("full_solution") or ""
        kps = recog.get("knowledge_pts") or section_kps
        ptype = recog.get("ptype") or "calc"
        sub_answers = recog.get("sub_answers") or []
        rec = _make_record(args.section, str(n), "", ptype, 3, kps,
                           content, std, sol)
        rec["_crop"] = cpath.name
        if sub_answers and not std.strip():
            # 把 sub_answers 展开为多条记录（保持与 importer 一致）
            for s in sub_answers:
                sn = str(s.get("sub_no") or "")
                r2 = _make_record(args.section, str(n), sn, ptype, 3, kps,
                                  content if sn == (sub_answers[0].get("sub_no") or "") else "",
                                  str(s.get("std_answer") or ""), sol)
                r2["_crop"] = cpath.name
                entries.append(r2)
        else:
            entries.append(rec)
        print(f"    content[{len(content)}字] answer[{len(std)}字] "
              f"kps={kps} ans_ok={bool(std.strip())}")

    book = build_book_json(args.section, args.title or section_title(args.section), section_kps, entries)
    # 写入本地 JSON 供预览
    out_json = out_dir / f"book_problems_{args.section.replace('.','_')}.json"
    clean = json.loads(json.dumps(book))
    for e in clean["sections"][0]["problems"]:
        e.pop("_crop", None)
        e.pop("_answer_status", None)
    out_json.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[json] wrote {out_json}  ({len(entries)} 题)")

    if args.push:
        ok = push_local(args.section, book, crop_map)
        print("[result]", "PUSH OK" if ok else "PUSH FAILED")
    else:
        print("[dry-run] 未写入 8014；请用 --push 入库")


if __name__ == "__main__":
    main()
