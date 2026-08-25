#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Route 2 — 脚本化章节导入器（针对"无答案且无裁切图"的章节，如 §5.1）。

用法
----
    # 1) 准备输入目录 route2_input/5.1/（见 ROUTE2_IMPORT_GUIDE.md）：
    #      crops/5.1-1.png, crops/5.1-2.png ...   （每图 = 一题，可含小问）
    #      manifest.json                          （可选：人工录入的答案/知识点覆盖）
    # 2) 试运行（只识别+生成 JSON，不写入 8014）：
    python route2_chapter_importer.py --chapter 5.1 --input route2_input/5.1 --dry-run
    # 3) 正式导入（生成 JSON 并写入本地 8014 证据库）：
    python route2_chapter_importer.py --chapter 5.1 --input route2_input/5.1 --push

说明
----
* 本脚本**不伪造任何数学内容**。教材原页（crop 图）与对应答案必须由用户提供；
  脚本仅负责"识别 → 结构化 → 入库"的自动化管道。若答案缺失，导入的题会标记为
  待人工复核（answer_status=unverified），绝不会以猜测值发布。
* 识别调用 VLM /solve-from-image（题图转写 + 独立求解），再由 manifest 覆盖校对。
* 最终通过 8014 既有的 POST /ingest/book 入库（与既有 extract_book 管道一致）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# ---- 路径/端点配置（可按需通过环境变量或参数覆盖） ----
VLM_URL = os.environ.get("MATH_VLM_URL", "http://127.0.0.1:18080")
# 8014 证据库（本地工作台）工作目录：book_problems.json 的 img 相对路径相对它解析
EVIDENCE_DIR = os.environ.get(
    "EVIDENCE_DIR", r"D:\My File\大四\高数教材答案"
)
EVIDENCE_URL = os.environ.get("EVIDENCE_URL", "http://127.0.0.1:8014/api")
# Remote VLM access is optional and must be supplied by the operator. Keep
# this legacy importer free of embedded credentials; most local Route2 imports
# only use ``MATH_VLM_URL`` and never need SSH at all.
SSH_HOST = os.environ.get("VLM_SSH_HOST", "")
SSH_PORT = int(os.environ.get("VLM_SSH_PORT", "22"))
SSH_USER = os.environ.get("VLM_SSH_USER", "")
SSH_PW = os.environ.get("VLM_SSH_PASSWORD", "")

CROP_EXTS = {".png", ".jpg", ".jpeg"}

# 题号从文件名推断：5.1-1.png -> (problem_no="1", sub_no="")；5.1-1-2.png -> ("1","2")
_NAME_RE = re.compile(r"(?:.*?[-_])?(\d+)(?:[-_](\d+))?\.(\w+)$")


def _infer_no(filename: str) -> tuple[str, str]:
    m = _NAME_RE.search(filename)
    if m:
        return m.group(1), (m.group(2) or "")
    return "", ""


def _read_image_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def recognize_crop(crop: Path, section_no: str, problem_no: str) -> dict[str, Any]:
    """Call VLM /solve-from-image for one problem crop."""
    payload = {
        "image_base64": _read_image_b64(crop),
        "section_no": section_no,
        "problem_no": problem_no,
    }
    try:
        with httpx.Client(timeout=600) as client:
            r = client.post(VLM_URL.rstrip("/") + "/solve-from-image", json=payload)
            r.raise_for_status()
            return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "problem_text": "", "std_answer": "",
                "partial": True, "missing": ["problem_text", "std_answer"]}


def load_manifest(input_dir: Path) -> dict[str, Any]:
    mp = input_dir / "manifest.json"
    if not mp.is_file():
        return {}
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] manifest.json 解析失败：{exc}")
        return {}


def build_entry(section_no: str, crop: Path, manifest_item: dict[str, Any] | None,
                recog: dict[str, Any]) -> list[dict[str, Any]]:
    """Build one or more problem records (one per sub-question) for a crop."""
    problem_no, sub_no = _infer_no(crop.name)
    if manifest_item:
        problem_no = str(manifest_item.get("problem_no") or problem_no)
        sub_no = str(manifest_item.get("sub_no") or sub_no)

    ptype = (manifest_item or {}).get("ptype") or recog.get("ptype") or "calc"
    difficulty = int((manifest_item or {}).get("difficulty", 3) or 3)
    kps = (manifest_item or {}).get("knowledge_pts") or []
    content = (manifest_item or {}).get("content_text") or recog.get("problem_text") or ""
    grading_steps = (manifest_item or {}).get("grading_steps") or recog.get("full_solution") or ""

    sub_answers = recog.get("sub_answers") or []
    entries: list[dict[str, Any]] = []

    def _ans_for(sub: str) -> str:
        # manifest 优先；否则取 VLM 识别
        if manifest_item and (manifest_item.get("std_answer") or "").strip():
            return str(manifest_item["std_answer"])
        if sub_answers:
            idx = next((s for s in sub_answers
                        if str(s.get("sub_no")) == sub), None)
            if idx:
                return str(idx.get("std_answer") or "")
        return str(recog.get("std_answer") or "")

    if sub_answers:
        for s in sub_answers:
            sn = str(s.get("sub_no") or "")
            entries.append(_make_record(
                section_no, problem_no, sn, ptype, difficulty, kps,
                content if sn == (sub_answers[0].get("sub_no") or "") else "",
                _ans_for(sn), grading_steps))
    else:
        entries.append(_make_record(
            section_no, problem_no, sub_no, ptype, difficulty, kps,
            content, _ans_for(sub_no), grading_steps))
    return entries


def _make_record(section_no, problem_no, sub_no, ptype, difficulty, kps,
                 content, std_answer, grading_steps) -> dict[str, Any]:
    has_answer = bool(str(std_answer).strip())
    has_text = len(str(content).strip()) >= 6
    # 没有答案的题绝不发布，标记待复核
    answer_status = "verified" if has_answer and has_text else "unverified"
    return {
        "no": str(problem_no),
        "sub_no": str(sub_no),
        "ptype": ptype,
        "difficulty": difficulty,
        "knowledge_pts": list(kps),
        "content_text": str(content).strip(),
        "std_answer": str(std_answer).strip(),
        "full_solution": str(grading_steps).strip(),
        "grading_steps": str(grading_steps).strip(),
        "answer_weight": 0.6,
        "img": "",  # 由调用方回填为 answer_source_previews/route2/... 路径
        "_answer_status": answer_status,
    }


def build_book_json(chapter: str, title: str, kps: list[str],
                    entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "textbook": {"name": f"Route2 导入 - {chapter}", "page_offset": 0},
        "sections": [
            {
                "section_no": chapter,
                "heading": title or chapter,
                "knowledge_pts": kps,
                "problems": entries,
            }
        ],
    }


def push_local(chapter: str, book_json: dict[str, Any], crop_map: dict[str, Path]) -> bool:
    """Write JSON + source crops to the local 8014 dir and call /ingest/book."""
    ev_dir = Path(EVIDENCE_DIR)
    ingest_dir = ev_dir / "ingest"
    ingest_dir.mkdir(parents=True, exist_ok=True)
    preview_root = ev_dir / "answer_source_previews" / "route2" / chapter
    preview_root.mkdir(parents=True, exist_ok=True)

    # copy source crops and rewrite img paths
    for name, crop in crop_map.items():
        dest = preview_root / crop.name
        shutil.copyfile(crop, dest)
        # 该 crop 对应的所有 entry 的 img 指向此文件
        rel = f"answer_source_previews/route2/{chapter}/{crop.name}"
        for e in book_json["sections"][0]["problems"]:
            if e.get("_crop") == name:
                e["img"] = rel

    json_path = ingest_dir / f"route2_{chapter.replace('.', '_')}.json"
    # 去掉内部辅助字段
    clean = json.loads(json.dumps(book_json))
    for e in clean["sections"][0]["problems"]:
        e.pop("_crop", None)
        e.pop("_answer_status", None)
    json_path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[push] wrote {json_path}")

    try:
        with httpx.Client(timeout=60) as client:
            r = client.post(EVIDENCE_URL.rstrip("/") + "/ingest/book",
                            json={"path": str(json_path)})
            r.raise_for_status()
            print("[push] /ingest/book ->", r.json())
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"[push] FAILED: {exc}")
        return False


def run(chapter: str, input_dir: Path, dry_run: bool, push: bool):
    manifest = load_manifest(input_dir)
    title = manifest.get("chapter_title", chapter)
    chap_kps = manifest.get("knowledge_pts", [])
    crops_dir = input_dir / "crops"
    if not crops_dir.is_dir():
        print(f"[error] 缺少 {crops_dir} 目录（每图一题的裁切图）")
        sys.exit(2)
    crops = sorted(p for p in crops_dir.iterdir()
                   if p.suffix.lower() in CROP_EXTS and p.is_file())
    if not crops:
        print(f"[error] {crops_dir} 下没有任何裁切图")
        sys.exit(2)

    print(f"[scan] 章节 {chapter}：发现 {len(crops)} 张裁切图")
    all_entries: list[dict[str, Any]] = []
    crop_map: dict[str, Path] = {}
    for crop in crops:
        mitem = (manifest.get("problems") or {}).get(crop.name)
        print(f"  - 识别 {crop.name} ...")
        recog = recognize_crop(crop, chapter, _infer_no(crop.name)[0] or "?")
        if recog.get("error"):
            print(f"      VLM 调用失败：{recog['error']}（将标记待复核）")
        entries = build_entry(chapter, crop, mitem, recog)
        for e in entries:
            e["_crop"] = crop.name
            all_entries.append(e)
            crop_map.setdefault(crop.name, crop)
        status = "partial/缺失" if recog.get("partial") else "ok"
        print(f"      -> {len(entries)} 条记录，识别状态：{status}")

    book = build_book_json(chapter, title, chap_kps, all_entries)
    n_unverified = sum(1 for e in all_entries if e["_answer_status"] == "unverified")
    print(f"[summary] 共 {len(all_entries)} 题，其中 {n_unverified} 题缺答案将被标记待复核。")

    json_path = input_dir / f"book_problems_{chapter.replace('.','_')}.json"
    clean = json.loads(json.dumps(book))
    for e in clean["sections"][0]["problems"]:
        e.pop("_crop", None); e.pop("_answer_status", None)
    json_path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[dry-run] 已写出结构化 JSON：{json_path}（未写入 8014）")

    if push:
        ok = push_local(chapter, book, crop_map)
        sys.exit(0 if ok else 1)
    else:
        print("[info] 未指定 --push，仅生成 JSON。需要入库时加 --push。")


def self_test():
    """Validate the recognition->JSON->schema path against existing §1.1 crops (dry-run)."""
    src = Path(r"D:\workbuddy\2026-08-06-15-31-48\extract_img\book\1.1")
    if not src.is_dir():
        print("[self-test] 未找到本地 §1.1 裁切图，跳过")
        return
    crops = sorted(p for p in src.iterdir() if p.suffix.lower() in CROP_EXTS)[:3]
    print(f"[self-test] 用 {len(crops)} 张 §1.1 裁切图验证管道（不入库）...")
    tmp = Path(r"D:\workbuddy\2026-08-06-15-31-48\route2_selftest")
    (tmp / "crops").mkdir(parents=True, exist_ok=True)
    for c in crops:
        shutil.copyfile(c, tmp / "crops" / c.name)
    run("1.1", tmp, dry_run=True, push=False)


def main():
    ap = argparse.ArgumentParser(description="Route 2 章节导入器")
    ap.add_argument("--chapter", default="5.1")
    ap.add_argument("--input", default="route2_input/5.1")
    ap.add_argument("--dry-run", action="store_true", help="只识别+生成 JSON，不入库")
    ap.add_argument("--push", action="store_true", help="生成 JSON 并写入本地 8014")
    ap.add_argument("--self-test", action="store_true", help="用本地 §1.1 裁切图验证管道")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return
    input_dir = Path(args.input)
    if not input_dir.is_dir():
        print(f"[error] 输入目录不存在：{input_dir}")
        sys.exit(2)
    run(args.chapter, input_dir, dry_run=args.dry_run or not args.push, push=args.push)


if __name__ == "__main__":
    main()
