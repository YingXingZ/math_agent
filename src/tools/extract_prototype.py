# -*- coding: utf-8 -*-
"""
题目提取原型脚本（阶段一 MVP + 阶段二可复用骨架）
================================================
针对扫描版《高等数学》教材，按「原图保真」策略提取习题：

  - 用 RapidOCR 定位版面与题号（OCR 仅用于版面 / 编号检测，不解析公式，
    公式以高分辨率原图裁切嵌入，保证 100% 保真）。
  - 自动按大题切分；双栏小题自动识别并切分（含缺号补齐）。
  - 可选接入 Qwen-VL 做知识点标签（需 DASHSCOPE_API_KEY 环境变量）。

为什么用「原图保真」而非 OCR 公式转 LaTeX？
  实测 RapidOCR / PaddleOCR 对分式、根号、上下标丢失严重，转 LaTeX 后
  公式失真。原图裁切直接嵌入作业卷，公式零损耗，且图文混排、页边距可控。

用法：
  python extract_prototype.py \
      --pdf "D:/My File/大四/高数教材答案/李继成高数-教材-上册-2版(1).pdf" \
      --page 45 --dpi 300 \
      --section "习题1.3" --next-section "1.4" \
      --out extract_img/p13

  # 也可直接给教材页码（PDF 索引 = 教材页 + offset，本册 offset=21）
  python extract_prototype.py --pdf ... --textbook-page 24 --page-offset 21 ...

依赖：PyMuPDF, Pillow, numpy, rapidocr_onnxruntime
生产替代 OCR 引擎：PaddleOCR（接口一致，见 build_ocr 可替换）
"""
import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import List, Optional

import fitz
import numpy as np
from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")

# --------------------------------------------------------------------------- #
# OCR 引擎（可插拔：RapidOCR 默认，PaddleOCR 生产可选）
# --------------------------------------------------------------------------- #
def build_ocr(engine: str = "rapidocr"):
    """返回一个 OCR 对象，暴露 detect(np_img) -> List[OCRItem]。"""
    if engine == "rapidocr":
        from rapidocr_onnxruntime import RapidOCR
        ocr = RapidOCR()
        def detect(arr):
            res, _ = ocr(arr)
            if not res:
                return []
            out = []
            for box, text, score in res:
                ys = [p[1] for p in box]
                xs = [p[0] for p in box]
                out.append({"text": text, "y0": min(ys), "y1": max(ys),
                            "x0": min(xs), "x1": max(xs), "score": score})
            return out
        return detect
    elif engine == "paddle":
        # 生产环境若已安装 paddleocr，可切换；接口保持一致
        from paddleocr import PaddleOCR
        ocr = PaddleOCR(use_angle_cls=True, lang="ch")
        def detect(arr):
            res = ocr(arr, cls=True)
            out = []
            for line in res:
                box, (text, score) = line
                ys = [p[1] for p in box]
                xs = [p[0] for p in box]
                out.append({"text": text, "y0": min(ys), "y1": max(ys),
                            "x0": min(xs), "x1": max(xs), "score": score})
            return out
        return detect
    raise ValueError(f"未知 OCR 引擎: {engine}")


# --------------------------------------------------------------------------- #
# 知识点打标（可选：Qwen-VL / 通义视觉理解）
# --------------------------------------------------------------------------- #
class KnowledgeTagger:
    """基类：返回该大题的知识点标签列表。默认空实现。"""

    def available(self) -> bool:
        return False

    def tag(self, crop_img: Image.Image, problem_text_hint: str) -> List[str]:
        return []


class NullTagger(KnowledgeTagger):
    pass  # available() 默认返回 False


class QwenVLTagger(KnowledgeTagger):
    """接入 DashScope 通义千问 VL。需环境变量 DASHSCOPE_API_KEY。"""

    def __init__(self, model: str = "qwen-vl-max"):
        self.model = model
        self.api_key = os.environ.get("DASHSCOPE_API_KEY")
        self._client = None
        if self.api_key:
            try:
                import dashscope
                self._client = dashscope
            except ImportError:
                print("[warn] 未安装 dashscope，跳过 Qwen-VL 打标（pip install dashscope）")

    def available(self) -> bool:
        return self._client is not None and bool(self.api_key)

    def tag(self, crop_img: Image.Image, problem_text_hint: str) -> List[str]:
        if not self.available():
            return []
        import base64
        from io import BytesIO
        buf = BytesIO()
        crop_img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        prompt = (
            "这是一道高等数学习题的图片。请只输出该题目涉及的知识点标签，"
            "用中文逗号分隔，不要解释。例如：极限，洛必达法则，等价无穷小。"
        )
        try:
            resp = self._client.MultiModalConversation.call(
                model=self.model,
                api_key=self.api_key,
                messages=[{
                    "role": "user",
                    "content": [
                        {"image": f"data:image/png;base64,{b64}"},
                        {"text": prompt},
                    ],
                }],
            )
            text = resp.output.choices[0].message.content[0]["text"]
            return [t.strip() for t in text.replace("，", ",").split(",") if t.strip()]
        except Exception as e:  # 打标失败不影响提取主流程
            print(f"[warn] Qwen-VL 打标失败: {e}")
            return []


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class SubProblem:
    no: str
    img: str
    w: int
    h: int


@dataclass
class Problem:
    no: str
    img: str
    y0: float
    y1: float
    height_px: int
    text_hint: str = ""
    knowledge_points: List[str] = field(default_factory=list)
    subproblems: List[SubProblem] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 核心步骤
# --------------------------------------------------------------------------- #
def render_page(doc, page_index: int, dpi: int):
    pix = doc[page_index].get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return img


def locate_section(items, section: str, next_section: str):
    """返回习题区 (start_y, end_y)。section 形如 '习题1.3'，next 形如 '1.4'。"""
    start_y = end_y = None
    for it in items:
        t = it["text"].replace(" ", "")
        if start_y is None and re.search(re.escape(section), t):
            start_y = it["y1"] + 10
        if start_y is not None and end_y is None and re.match(r"^" + re.escape(next_section), t) \
                and it["y0"] > start_y:
            end_y = it["y0"] - 15
    if start_y is None:
        raise RuntimeError(f"未找到章节锚点: {section}")
    if end_y is None:  # 全页即习题区（兜底）
        end_y = max(it["y1"] for it in items)
    return start_y, end_y


def find_problem_anchors(items, start_y, end_y, left_x_max=600):
    """在习题区内找大题题号锚点（行首 N. / N．/ N、）。"""
    anchors = []
    for it in items:
        if not (start_y <= it["y0"] <= end_y):
            continue
        if it["x0"] > left_x_max:
            continue
        m = re.match(r"^\s*(\d{1,2})\s*[\.．、：]", it["text"])
        if m:
            n = int(m.group(1))
            if 1 <= n <= 40:
                anchors.append({"no": n, "y": it["y0"], "text": it["text"][:60]})
    seen, uniq = set(), []
    for a in sorted(anchors, key=lambda x: x["y"]):
        if a["no"] in seen:
            continue
        seen.add(a["no"])
        uniq.append(a)
    uniq.sort(key=lambda x: x["y"])  # 必须按纵向位置排序，供 crop_problems 用相邻题作切分边界
    return uniq


def crop_problems(img, anchors, end_y, out_dir, prefix, pad_x=30, left_x=200, right_margin=100):
    """按大题切分，返回 Problem 列表。"""
    W, H = img.size
    # 按纵向位置排序，确保用相邻题作切分下边界时不会上下颠倒
    anchors = sorted(anchors, key=lambda a: a["y"])
    problems: List[Problem] = []
    for i, a in enumerate(anchors):
        y0 = max(0, a["y"] - 12)
        y1 = anchors[i + 1]["y"] - 12 if i + 1 < len(anchors) else end_y
        if y1 <= y0:  # 退化题块，跳过避免裁切异常
            print(f"      [跳过] 退化题块 p{a['no']} (y0={y0:.0f},y1={y1:.0f})")
            continue
        try:
            crop = img.crop((int(left_x - pad_x), int(y0), int(W - right_margin), int(y1)))
            fn = os.path.join(out_dir, f"{prefix}_p{a['no']}.png")
            crop.save(fn)
            problems.append(Problem(
                no=str(a["no"]), img=fn, y0=y0, y1=y1,
                height_px=int(y1 - y0), text_hint=a["text"],
            ))
        except Exception as e:
            print(f"      [跳过] p{a['no']} 裁切失败: {e}")
    return problems


def split_subproblems(crop_img: Image.Image, out_dir, parent_no, prefix,
                      detect, col_order="odd_left", col_gap_thresh=300,
                      row_tol=45):
    """
    对单个大题裁切图做双栏小题切分。
    返回 SubProblem 列表（空列表表示非双栏单题）。
    col_order: 'odd_left' -> 奇数在左栏、偶数在右栏（本教材规律）；'even_left' 反之。
    """
    W, H = crop_img.size
    arr = np.array(crop_img)
    items = detect(arr)
    # 找 (N) / （N） 标记
    marks = []
    for it in items:
        t = it["text"].strip()
        m = re.match(r"^[\(（]\s*(\d{1,2})\s*[\)）]$", t)
        if m:
            marks.append({"no": int(m.group(1)), "y0": it["y0"], "y1": it["y1"],
                          "x0": it["x0"], "x1": it["x1"]})
    if len(marks) < 2:
        return []
    marks.sort(key=lambda m: (m["y0"], m["x0"]))

    # 栏分界线：x 坐标最大间隙
    xs_all = sorted(m["x0"] for m in marks)
    gaps = [(xs_all[i + 1] - xs_all[i], i) for i in range(len(xs_all) - 1)]
    max_gap, gi = max(gaps) if gaps else (0, 0)
    left_start = xs_all[0]
    right_start = xs_all[gi + 1] if max_gap > col_gap_thresh else None
    col_split = (right_start - 25) if right_start else W

    # 按 y 聚类成行
    rows = []
    for m in marks:
        placed = False
        for r in rows:
            if abs(r["y"] - m["y0"]) < row_tol:
                r["items"].append(m)
                placed = True
                break
        if not placed:
            rows.append({"y": m["y0"], "items": [m]})
    rows.sort(key=lambda r: r["y"])
    for r in rows:
        r["items"].sort(key=lambda m: m["x0"])

    # 缺号补齐：依据同行伙伴号对称定位（本教材双栏顺序规律）
    found = sorted(m["no"] for m in marks)
    expected = list(range(1, max(found) + 1))
    missing = [n for n in expected if n not in found]
    for n in missing:
        odd_in_left = (col_order == "odd_left")
        target_col_x = left_start if (n % 2 == 1) == odd_in_left else (right_start or W)
        partner = n + 1 if (n + 1) in found else n - 1
        prow = next((r for r in rows if any(m["no"] == partner for m in r["items"])), None)
        if prow:
            marks.append({"no": n, "y0": prow["y"], "y1": prow["y"] + 40,
                          "x0": target_col_x, "x1": target_col_x + 40})
            prow["items"].append({"no": n, "y0": prow["y"], "x0": target_col_x})
            prow["items"].sort(key=lambda m: m["x0"])

    content_x0, content_x1 = int(left_start) - 20, W - 120
    subs: List[SubProblem] = []
    for i, r in enumerate(rows):
        y0 = r["y"] - 15
        y1 = (rows[i + 1]["y"] - 15) if i + 1 < len(rows) else H - 10
        if y1 <= y0:
            continue
        for m in r["items"]:
            if m["x0"] < col_split:
                x0, x1 = content_x0, int(col_split)
            else:
                x0, x1 = int(right_start) - 20 if right_start else content_x0, content_x1
            try:
                crop = crop_img.crop((x0, int(y0), x1, int(y1)))
                # 去除周边纯白边，让裁切更紧凑
                g = np.array(crop.convert("L"))
                mask = g < 220
                if mask.any():
                    rs = np.where(mask.any(axis=1))[0]
                    cs = np.where(mask.any(axis=0))[0]
                    crop = crop.crop((max(0, cs[0] - 8), max(0, rs[0] - 8),
                                      min(crop.width, cs[-1] + 8), min(crop.height, rs[-1] + 8)))
                fn = os.path.join(out_dir, f"{prefix}_p{parent_no}_sub{m['no']:02d}.png")
                crop.save(fn)
                subs.append(SubProblem(no=f"({m['no']})", img=fn, w=crop.width, h=crop.height))
            except Exception as e:
                print(f"      [跳过] 小题 ({m['no']}) 裁切失败: {e}")
    subs.sort(key=lambda s: int(re.sub(r"[\(\)（）]", "", s.no)))
    return subs


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="扫描版高数教材习题提取原型（原图保真）")
    ap.add_argument("--pdf", required=True, help="教材 PDF 路径")
    ap.add_argument("--page", type=int, default=None, help="PDF 页索引（0 起）")
    ap.add_argument("--textbook-page", type=int, default=None, help="教材页码（与 --page-offset 配合）")
    ap.add_argument("--page-offset", type=int, default=0, help="PDF索引=教材页+offset")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--section", default="习题1.3", help="本章节锚点，如 习题1.3")
    ap.add_argument("--next-section", default="1.4", help="下一节锚点，用于界定结束，如 1.4")
    ap.add_argument("--out", default="extract_img/p13", help="输出目录")
    ap.add_argument("--ocr-engine", default="rapidocr", choices=["rapidocr", "paddle"])
    ap.add_argument("--no-vl", action="store_true", help="关闭 Qwen-VL 知识点打标")
    ap.add_argument("--sub-col-order", default="odd_left", choices=["odd_left", "even_left"])
    ap.add_argument("--dual-threshold", type=float, default=2.5,
                    help="大题高度 ≥ 中位高度*该倍数 时尝试双栏小题切分")
    args = ap.parse_args()

    if args.page is None:
        if args.textbook_page is None:
            ap.error("需提供 --page 或 --textbook-page")
        args.page = args.textbook_page + args.page_offset

    os.makedirs(args.out, exist_ok=True)
    detect = build_ocr(args.ocr_engine)
    tagger = NullTagger() if args.no_vl else QwenVLTagger()

    doc = fitz.open(args.pdf)
    print(f"[1/5] 渲染第 {args.page} 页 @ {args.dpi}dpi ...")
    img = render_page(doc, args.page, args.dpi)
    W, H = img.size
    print(f"      页面尺寸: {W}x{H}")

    print("[2/5] OCR 版面检测 ...")
    items = detect(np.array(img))
    items.sort(key=lambda it: it["y0"])
    print(f"      识别文本块: {len(items)}")

    print(f"[3/5] 定位习题区 [{args.section}] ...")
    start_y, end_y = locate_section(items, args.section, args.next_section)
    print(f"      习题区 y: {start_y:.0f} -> {end_y:.0f}")

    anchors = find_problem_anchors(items, start_y, end_y)
    print(f"      大题锚点: {len(anchors)} 道 -> {[a['no'] for a in anchors]}")

    print("[4/5] 切分大题 ...")
    problems = crop_problems(img, anchors, end_y, args.out,
                             prefix=os.path.basename(args.out).replace("/", "_"))
    if tagger.available():
        print("      [Qwen-VL] 知识点打标中 ...")
    for p in problems:
        if tagger.available():
            p.knowledge_points = tagger.tag(Image.open(p.img), p.text_hint)
        print(f"      -> 第{p.no}题  {p.img}  {p.height_px}px"
              + (f"  知识点={p.knowledge_points}" if p.knowledge_points else ""))

    # 双栏小题切分：高度异常的大题才尝试
    heights = [p.height_px for p in problems] or [0]
    median_h = sorted(heights)[len(heights) // 2]
    thresh = median_h * args.dual_threshold
    print(f"[5/5] 双栏小题切分（阈值高度 {thresh:.0f}px）...")
    sub_total = 0
    for p in problems:
        if p.height_px >= thresh:
            subs = split_subproblems(Image.open(p.img), args.out, p.no,
                                     os.path.basename(args.out).replace("/", "_"),
                                     detect, col_order=args.sub_col_order)
            if subs:
                p.subproblems = subs
                sub_total += len(subs)
                print(f"      第{p.no}题 拆出 {len(subs)} 个小题")
    print(f"      共拆出 {sub_total} 个双栏小题")

    # 输出结构化元数据
    meta = []
    for p in problems:
        d = asdict(p)
        d["img"] = os.path.relpath(d["img"], args.out)
        for s in d["subproblems"]:
            s["img"] = os.path.relpath(s["img"], args.out)
        meta.append(d)
    out_meta = os.path.join(args.out, "meta.json")
    json.dump({"section": args.section, "page": args.page, "dpi": args.dpi,
               "problems": meta},
              open(out_meta, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n完成 -> 元数据: {out_meta}")
    print(f"大题 {len(problems)} 道，小题 {sub_total} 个")
    doc.close()


if __name__ == "__main__":
    main()
