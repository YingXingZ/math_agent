# -*- coding: utf-8 -*-
"""
全本题目提取（阶段二 · 题库扩展）
================================
针对扫描版教材（无文本层），一次性 OCR 发现全书所有「习题X.Y」章节，
逐页提取大题与双栏小题，原图保真裁切，输出可供 API 入库的结构化数据。

复用 extract_prototype.py 的核心算法（题号定位 / 双栏小题切分 / 缺号补齐）。

输出：
  <out>/book/<section>/p{N}.png, p{N}_sub{MM}.png, meta.json   （每节裁切图）
  <out>/book_problems.json                                    （API 入库载荷）

知识点标签：教材无现成标签，按「节→知识点」种子字典预打（VLM 自动打标可后续覆盖）。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

import fitz
import numpy as np
from PIL import Image

import extract_prototype as ep

sys.stdout.reconfigure(encoding="utf-8")

# --------------------------------------------------------------------------- #
# 节 -> 知识点 种子字典（阶段二检索演示用；VLM 接入后可覆盖/补全）
# --------------------------------------------------------------------------- #
SECTION_KP = {
    # 第一章 极限与连续
    "1.1": ["limit.sequence", "limit.def"],
    "1.2": ["limit.function", "limit.def"],
    "1.3": ["limit.four_ops", "limit.comparison", "limit.squeeze"],
    "1.4": ["limit.monotone", "limit.two_importants", "limit.squeeze"],
    "1.5": ["limit.inf_small", "limit.inf_large", "limit.order"],
    "1.6": ["continuity.def", "continuity.ops", "continuity.elem"],
    "1.7": ["continuity.closed_interval", "continuity.zero_point"],

    # 第二章 导数与微分 / 微分中值定理与导数应用
    "2.1": ["derivative.concept", "derivative.geometric"],
    "2.2": ["derivative.rules", "derivative.chain_rule"],
    "2.3": ["derivative.higher_order"],
    "2.4": ["derivative.implicit", "derivative.parametric"],
    "2.5": ["differential"],
    "2.6": ["mean_value_theorem", "rolle_theorem", "lagrange_theorem", "cauchy_theorem"],
    "2.7": ["lhopital_rule", "limit.indeterminate"],
    "2.8": ["taylor_formula", "taylor.expansion"],
    "2.9": ["function.monotonicity", "function.concavity", "function.asymptote"],
    "2.10": ["function.extrema", "function.optimization"],

    # 第三章 不定积分
    "3.1": ["indefinite_integral.concept", "indefinite_integral.properties"],
    "3.2": ["integration.substitution"],
    "3.3": ["integration.substitution"],
    "3.4": ["integration.by_parts"],
    "3.5": ["integration.rational"],
    "3.6": ["integration.trigonometric"],
    "3.7": ["integration.irrational"],
    "3.8": ["integration.table", "integration.techniques"],
    "3.9": ["integration.comprehensive"],
    "3.10": ["integration.comprehensive"],

    # 第四章 定积分及其应用
    "4.1": ["definite_integral.concept", "definite_integral.properties"],
    "4.2": ["fundamental_theorem", "definite_integral.calculation"],
    "4.3": ["definite_integral.substitution", "definite_integral.by_parts"],
    "4.4": ["improper_integral"],
    "4.5": ["definite_integral.geometry", "area", "volume"],
    "4.6": ["definite_integral.physics", "arc_length", "work"],
    "4.7": ["definite_integral.comprehensive"],
}


def section_kp(section_no: str):
    if section_no in SECTION_KP:
        return SECTION_KP[section_no]
    ch = section_no.split(".")[0]
    return [f"ch{ch}.overview"]


# --------------------------------------------------------------------------- #
# 发现阶段：OCR 定位全书所有「习题X.Y」
# --------------------------------------------------------------------------- #
def discover(pdf, dpi, detect, page_limit=None):
    doc = fitz.open(pdf)
    n = doc.page_count if page_limit is None else min(page_limit, doc.page_count)
    print(f"[发现] 扫描 {n} 页 @ {dpi}dpi 定位习题章节 ...", flush=True)
    secs = []          # {page, y, section, heading}
    page_items = {}    # page_index -> items (仅存 OCR 结果，不存图片，省内存)
    sec_seen = set()
    for i in range(n):
        img = ep.render_page(doc, i, dpi)
        items = detect(np.array(img))
        items.sort(key=lambda it: it["y0"])
        page_items[i] = items  # 只存 OCR items，不存 img
        del img  # 显式释放图片内存
        # 先收集本页所有"习题X.Y"匹配
        page_matches = []
        for it in items:
            m = re.search(r"习题\s*(\d+)\.(\d+)", it["text"].replace(" ", ""))
            if m:
                sec = f"{m.group(1)}.{m.group(2)}"
                page_matches.append((sec, it["y0"], it["text"].strip()))
        # 一页出现 3+ 个不同章节标题 -> 目录/索引页，不参与首次定位
        if len({m[0] for m in page_matches}) >= 3:
            continue
        for sec, y, text in page_matches:
            if sec in sec_seen:
                continue
            sec_seen.add(sec)
            secs.append({"page": i, "y": y,
                         "section": sec, "heading": text})
        if (i + 1) % 40 == 0:
            print(f"      已扫描 {i + 1}/{n} 页，发现 {len(secs)} 个习题节", flush=True)
    secs.sort(key=lambda s: (s["page"], s["y"]))
    print(f"[发现] 共定位 {len(secs)} 个习题节: "
          + ", ".join(s["section"] for s in secs), flush=True)
    return doc, page_items, secs


# --------------------------------------------------------------------------- #
# 提取阶段：逐页把题切出来，挂到所属节下
# --------------------------------------------------------------------------- #
def _extract_band(img, items, start_y, end_y, section, asec, out_root,
                  prefix, detect, dual_threshold):
    """从图片的垂直带 [start_y, end_y] 中提取某节的题目。"""
    if end_y <= start_y:
        return
    anchors = ep.find_problem_anchors(items, start_y, end_y)
    if not anchors:
        # DEBUG: 没找到锚点时，列出范围内所有可能的题号文本
        candidates = []
        for it in items:
            if start_y <= it["y0"] <= end_y:
                t = it["text"].strip()
                if re.match(r"^\s*(\d{1,2})\s*[\.．、：]", t):
                    candidates.append(f"#{re.match(r'^\s*(\d{1,2})', t).group(1)}@y={it['y0']:.0f},x0={it['x0']:.0f} [{t[:40]}]")
        if candidates:
            print(f"      [DEBUG] {section} [{start_y:.0f}-{end_y:.0f}] 未找到锚点，但范围内有疑似题号: {'; '.join(candidates[:8])}", flush=True)
        else:
            print(f"      [DEBUG] {section} [{start_y:.0f}-{end_y:.0f}] 无任何匹配", flush=True)
        return
    try:
        problems = ep.crop_problems(img, anchors, end_y, asec["dir"], prefix,
                                    pad_x=30, left_x=200, right_margin=100)
    except Exception as e:
        print(f"      [跳过] {section} 切大题失败: {e}")
        return
    heights = [p.height_px for p in problems] or [0]
    median_h = sorted(heights)[len(heights) // 2]
    thresh = median_h * dual_threshold
    for p in problems:
        sub = []
        try:
            if p.height_px >= thresh:
                sub = ep.split_subproblems(Image.open(p.img), asec["dir"], p.no,
                                            prefix, detect, col_order="odd_left")
        except Exception as e:
            print(f"      [跳过] {section} p{p.no} 小题切分失败: {e}")
        pe = {
            "no": p.no, "sub_no": None,
            "img": os.path.relpath(p.img, out_root),
            "height_px": p.height_px,
            "knowledge_pts": section_kp(section),
            "subproblems": [{"no": s.no,
                             "img": os.path.relpath(s.img, out_root),
                             "w": s.w, "h": s.h} for s in sub],
        }
        asec["problems"].append(pe)
    if anchors:
        print(f"      [提取] {section} [{start_y:.0f}-{end_y:.0f}] -> {len(anchors)} 锚点 -> {len(problems)} 大题", flush=True)


def extract_all(doc, page_items, secs, out_root, dpi, detect, dual_threshold=2.5):
    book_dir = os.path.join(out_root, "book")
    os.makedirs(book_dir, exist_ok=True)
    result = []  # 每节一个 entry

    valid_sections = {s["section"] for s in secs}
    # 预建每节目录 & 记录节元信息
    sec_by_no = {}
    for s in secs:
        d = os.path.join(book_dir, s["section"])
        os.makedirs(d, exist_ok=True)
        sec_by_no[s["section"]] = {"heading": s["heading"], "dir": d,
                                   "problems": []}

    def _is_body_heading(text, y, H, x0=0):
        """教材正文章节标题，如 1.2函数极限 / §1.2连续函数 / 第一章极限。
        与习题标题'习题 1.2'、例题'例1'、题号'1.'区分。

        关键：页面顶部的节号页眉（如 '1.5' 或 '1.5 无穷小'）不是正文标题，
        不应触发 active=None 导致整页习题被丢弃。

        正文中的交叉引用（如 "...参见 2.3 节..."）形如 "2.3" 但 x0 偏右，
        也不应被当作正文标题。
        """
        t = text.strip()
        # 页眉/页脚区域（y < 120px @ 300dpi）一律不当正文标题
        # 页眉通常是 "1.5" 或 "1.5 无穷小与无穷大"，每页都有；
        # 也可能含页码+章名如 "32 第一章"，必须在最前过滤
        if y < 120:
            return False
        if re.search(r"习题\s*\d+\.\d+", t):
            return False
        # 章标题：必须是 "第X章" 形式（含"章"字），不能只匹配"数字+空白"，
        # 否则 "1 -- cos " 这类公式碎片会被误判为章标题而截断习题带
        if re.match(r"^第\s*[\d一二三四五六七八九十]+\s*章", t):
            return True
        # 正文区域的交叉引用：形如 "2.3" 但 x0 偏右（不在左边缘）
        # 真正的小节标题都在页面左边缘（x0 < 350）
        if x0 > 350:
            return False
        # 形如 1.2、1.2.3 后跟非数字字符（真正的节标题，位于正文区域）
        if re.match(r"^\s*\d+\.\d+(\.\d+)?\s*\D", t):
            return True
        # 单独的节号（如 "1.2"）在正文区域也可能是标题
        if re.match(r"^\s*\d+\.\d+(\.\d+)?\s*$", t):
            return True
        return False

    n = len(page_items)
    active = None  # 当前活动章节（跨页继承）
    skipped_toc = 0
    for i in range(n):
        items = page_items[i]
        img = ep.render_page(doc, i, dpi)  # 按需重渲染，不缓存
        H = img.size[1]

        # 在该页 OCR 结果里重新找一次章节标题（习题 + 正文节标题）
        headings = []
        seen_ex = set()
        for it in items:
            text = it["text"].replace(" ", "")
            m = re.search(r"习题\s*(\d+)\.(\d+)", text)
            if m:
                sec = f"{m.group(1)}.{m.group(2)}"
                if sec in valid_sections and sec not in seen_ex:
                    headings.append({"y": it["y0"], "section": sec,
                                     "type": "exercise"})
                    seen_ex.add(sec)
            elif _is_body_heading(it["text"], it["y0"], H, it["x0"]):
                headings.append({"y": it["y0"], "section": None,
                                 "type": "body"})
        headings.sort(key=lambda h: h["y"])

        if headings:
            ex_count = sum(1 for h in headings if h["type"] == "exercise")
            # 一页出现 3+ 个不同习题标题 -> 目录/索引页，跳过不裁
            if ex_count >= 3:
                skipped_toc += 1
                continue

            # DEBUG: 打印每页的标题信息用于诊断
            hdr = ", ".join(f"{h['type'][:3]}:{h.get('section','body')}@y={h['y']:.0f}" for h in headings)
            print(f"      [页{i}] active={active} headings=[{hdr}]", flush=True)

            first = headings[0]
            # 如果本页以正文节标题开头（非页眉），说明上一节习题已结束；
            # 此时[0, first_y] 是正文，不应继承给上一节习题。
            if first["type"] == "body":
                # 页眉已被 _is_body_heading 过滤（y<120 不判为 body）
                # 真正的正文标题才清 active
                active = None

            # 第一个标题以上的区域属于上一页习题的延续
            # 无论第一个标题是 exercise 还是 body，只要 active 还在，
            # 且标题位置不在页面极顶部（>100px），就提取延续内容
            if active and first["y"] > 100:
                asec = sec_by_no[active]
                _extract_band(img, items, 0, first["y"], active, asec,
                              out_root, f"p{active.replace('.', '_')}", detect,
                              dual_threshold)

            # 标题之间的分块
            boundaries = [h["y"] for h in headings] + [H]
            for idx, h in enumerate(headings):
                if h["type"] == "body":
                    active = None
                    continue
                active = h["section"]
                start_y = h["y"]
                end_y = boundaries[idx + 1] if idx + 1 < len(boundaries) else H
                asec = sec_by_no[active]
                _extract_band(img, items, start_y, end_y, active, asec,
                              out_root, f"p{active.replace('.', '_')}", detect,
                              dual_threshold)
        else:
            # 无标题页 -> 沿用当前活动章节
            if active:
                asec = sec_by_no[active]
                _extract_band(img, items, 0, H, active, asec, out_root,
                              f"p{active.replace('.', '_')}", detect,
                              dual_threshold)
        del img  # 释放当前页图片内存

    # 组装输出
    for s in secs:
        asec = sec_by_no[s["section"]]
        if not asec["problems"]:
            continue
        result.append({
            "section_no": s["section"],
            "heading": asec["heading"],
            "knowledge_pts": section_kp(s["section"]),
            "problems": asec["problems"],
        })
        json.dump({"section": s["section"], "problems": asec["problems"]},
                  open(os.path.join(asec["dir"], "meta.json"), "w",
                       encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    total_p = sum(len(r["problems"]) for r in result)
    total_sub = sum(len(p["subproblems"]) for r in result for p in r["problems"])
    print(f"[提取] 完成：{len(result)} 节，{total_p} 道大题，{total_sub} 个小题 "
          f"（跳过目录页 {skipped_toc} 页）", flush=True)
    return result


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(pdf, out_root, dpi=300, ocr_engine="rapidocr", page_limit=None,
        no_vl=True):
    os.makedirs(out_root, exist_ok=True)
    detect = ep.build_ocr(ocr_engine)
    doc, page_items, secs = discover(pdf, dpi, detect, page_limit=page_limit)
    problems = extract_all(doc, page_items, secs, out_root, dpi, detect)
    payload = {
        "textbook": {"name": os.path.basename(pdf), "page_offset": 21,
                     "dpi": dpi, "generated_at": datetime.now().isoformat(timespec="seconds")},
        "sections": problems,
    }
    out_json = os.path.join(out_root, "book_problems.json")
    json.dump(payload, open(out_json, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"[完成] 入库载荷 -> {out_json}", flush=True)
    doc.close()
    return out_json


def main():
    ap = argparse.ArgumentParser(description="全本习题提取（阶段二）")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", default="extract_img")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--ocr-engine", default="rapidocr", choices=["rapidocr", "paddle"])
    ap.add_argument("--limit-pages", type=int, default=None,
                    help="仅扫描前 N 页（验证用，不填则全本）")
    args = ap.parse_args()
    run(args.pdf, args.out, dpi=args.dpi, ocr_engine=args.ocr_engine,
        page_limit=args.limit_pages)


if __name__ == "__main__":
    main()
