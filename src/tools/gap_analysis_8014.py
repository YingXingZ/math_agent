"""缺口分析：对 8014 每道「题干或答案不全」的题，判断根因并导出复核清单。

根因分类：
  ABSENT       答案书 OCR 压根没有该题号（需人工录入 / 重 OCR 源页）
  CORRUPT      答案书有但 OCR 是乱码(未过 is_clean)，被 --fix 跳过（下册 §9.x 多见）
  MATCHER_MISS 答案书有干净文本，但 8014 仍不全 → 匹配/回填 bug，属「免费可补」优先修

按 (section_no, problem_no, sub_no) 对齐答案书索引（复用 recover_from_answer_ocr）。
导出 gap_report.csv / gap_report.json 供教师逐条复核。
"""
import sys, re, json, csv, sqlite3
from pathlib import Path
from tool_config import REPOSITORY_ROOT, workbench_db
sys.path.insert(0, r"D:\workbuddy\2026-08-06-15-31-48")
import recover_from_answer_ocr as R

SRC_DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
OUT_CSV = r"D:\workbuddy\2026-08-06-15-31-48\gap_report.csv"
OUT_JSON = r"D:\workbuddy\2026-08-06-15-31-48\gap_report.json"

SRC_DB = str(workbench_db())
OUT_CSV = str(REPOSITORY_ROOT / "tmp" / "gap_report.csv")
OUT_JSON = str(REPOSITORY_ROOT / "tmp" / "gap_report.json")
idx = R.build_book_index()
s = sqlite3.connect(SRC_DB); s.row_factory = sqlite3.Row
rows = s.execute("""SELECT p.id, p.problem_no, p.sub_no, p.content_text, p.std_answer,
                           p.answer_status, s.section_no
                    FROM problems p JOIN sections s ON p.section_id=s.id
                    ORDER BY s.section_no, p.problem_no, p.sub_no""").fetchall()

def sub_key(sub_no):
    if not sub_no:
        return None
    m = re.search(r"\((\d{1,2})\)", sub_no)
    return m.group(1) if m else None

# 损坏判定（比 bulk_sync 的 is_clean 略严，但仅针对「不完整行」使用，不误伤完整长答案）：
# 全角拉丁/punct/私用区 密度超阈 → 严重乱码；否则若中文 mojibake 字符密集(>=3 且密度>=1%) → 也判损坏。
# 长答案里零星 1-2 个中文杂字(可用)不判损坏。
MOJI = set("士咛町叫咱归功巾叮芒亘辶亍迭迖")
def field_corrupt(t):
    if not t or len(t.strip()) < 2:
        return False
    if not R.is_clean(t):
        return True  # 全角拉丁类严重乱码
    c = sum(1 for ch in t if ch in MOJI)
    return c >= 3 and (c / len(t)) >= 0.01

# 预计算：哪些 (section, problem_no) 存在"带内容的父题行"(sub 空且 content 非空)。
# 有父题的子题：题干由父题承载，其缺题干是设计刻意，不算缺口。
parent_with_content = set()
for r in rows:
    if (not r["sub_no"]) and (r["content_text"] and r["content_text"].strip()):
        parent_with_content.add((r["section_no"], str(r["problem_no"])))

cat = {"ABSENT": [], "CORRUPT": [], "NEED_TEXTBOOK": [], "FREEWIN": []}
skipped_parent = 0
for r in rows:
    sec = r["section_no"]; no = str(r["problem_no"]); sub = r["sub_no"]
    sk = sub_key(sub)
    has_c = bool(r["content_text"] and r["content_text"].strip())
    has_a = bool(r["std_answer"] and r["std_answer"].strip())
    if has_c and has_a:
        continue  # 完整，跳过
    # (1) 有父题的子题：题干由父题承载，不计缺口
    if sub and (sec, no) in parent_with_content:
        skipped_parent += 1
        continue
    miss = []
    if not has_c:
        miss.append("stem")
    if not has_a:
        miss.append("answer")
    entry = idx.get(sec, {}).get(no)
    rec = {"section": sec, "problem_no": no, "sub_no": sub or "", "pid": r["id"],
           "answer_status": r["answer_status"], "missing": miss,
           "crop_image_path": ""}
    if entry is None:
        rec["category"] = "ABSENT"
        cat["ABSENT"].append(rec)
        continue
    # 取相关文本
    rel_stem = entry["stem_subs"].get(sk) if (sk and sk in entry["stem_subs"]) else entry["stem"]
    rel_ans = entry["subs"].get(sk) if (sk and sk in entry["subs"]) else entry["solution"]
    # 已裁决为 corrupt_ocr 的源（如 §9.9#2，下册答案书 OCR 损坏）→ 直接 CORRUPT，不误信答案书"看似干净"的文本
    if r["answer_status"] == "corrupt_ocr":
        rec["category"] = "CORRUPT"
        rec["ocr_stem_sample"] = (rel_stem or "")[:80]
        rec["ocr_ans_sample"] = (rel_ans or "")[:80]
        cat["CORRUPT"].append(rec)
        continue
    # (2) 已存在字段本就乱码 → CORRUPT（无论缺什么，数据已污染）
    existing_corrupt = False
    if has_c and field_corrupt(r["content_text"]):
        existing_corrupt = True
    if has_a and field_corrupt(r["std_answer"]):
        existing_corrupt = True
    if existing_corrupt:
        rec["category"] = "CORRUPT"
        rec["ocr_stem_sample"] = (rel_stem or "")[:80]
        rec["ocr_ans_sample"] = (rel_ans or "")[:80]
        cat["CORRUPT"].append(rec)
        continue
    # (3) 缺失字段可用性
    needs = []
    for field in miss:
        txt = rel_stem if field == "stem" else rel_ans
        if not txt or not txt.strip():
            needs.append("NEED_TEXTBOOK")   # 答案书该栏为空 → 得从教材来
        elif field == "stem" and len(txt.strip()) < 4:
            needs.append("NEED_TEXTBOOK")   # 父题干只是 "N." → 无真实题干可补
        elif field_corrupt(txt):
            needs.append("CORRUPT")          # 答案书有但 OCR 乱码（含密集中文乱码）
        else:
            needs.append("FREEWIN")           # 答案书有干净文本 → 可自动补
    if "CORRUPT" in needs:
        rec["category"] = "CORRUPT"
    elif "FREEWIN" in needs:
        rec["category"] = "FREEWIN"
    else:
        rec["category"] = "NEED_TEXTBOOK"
    rec["ocr_stem_sample"] = (rel_stem or "")[:80]
    rec["ocr_ans_sample"] = (rel_ans or "")[:80]
    cat[rec["category"]].append(rec)

# 输出
with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(cat, f, ensure_ascii=False, indent=2)
with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["category", "section", "problem_no", "sub_no", "missing", "answer_status", "pid", "ocr_stem_sample", "ocr_ans_sample"])
    for c in ("FREEWIN", "CORRUPT", "NEED_TEXTBOOK", "ABSENT"):
        for r in cat[c]:
            w.writerow([c, r["section"], r["problem_no"], r["sub_no"], ",".join(r["missing"]),
                        r["answer_status"] or "", r["pid"],
                        r.get("ocr_stem_sample", ""), r.get("ocr_ans_sample", "")])

print(f"不完整题总数: {sum(len(v) for v in cat.values())}  (另有 {skipped_parent} 道有父题的子题缺题干，属设计刻意，已跳过)")
print(f"  FREEWIN(答案书有干净文本、无父题、字段未污染→可自动补): {len(cat['FREEWIN'])}")
print(f"  CORRUPT(答案书OCR损坏 或 8014已存在字段本身乱码,需重OCR/VLM/手录): {len(cat['CORRUPT'])}")
print(f"  NEED_TEXTBOOK(答案书该栏为空 或 父题干仅题号→得从教材crop/VLM补): {len(cat['NEED_TEXTBOOK'])}")
print(f"  ABSENT(答案书无此题号,需教材/手录/重OCR): {len(cat['ABSENT'])}")
print("\n--- FREEWIN 明细(优先自动补) ---")
for r in cat["FREEWIN"]:
    print(f"  §{r['section']} #{r['problem_no']}{r['sub_no']} 缺={','.join(r['missing'])}  ocr={(r.get('ocr_stem_sample','') or r.get('ocr_ans_sample',''))[:50]!r}")
print("\n--- CORRUPT 按章节 ---")
from collections import Counter
cc = Counter(r["section"] for r in cat["CORRUPT"])
for sec, n in sorted(cc.items()):
    print(f"  §{sec}: {n}")
print("\n--- NEED_TEXTBOOK 按章节 ---")
nc = Counter(r["section"] for r in cat["NEED_TEXTBOOK"])
for sec, n in sorted(nc.items()):
    print(f"  §{sec}: {n}")
print("\n--- ABSENT 按章节 ---")
ac = Counter(r["section"] for r in cat["ABSENT"])
for sec, n in sorted(ac.items()):
    print(f"  §{sec}: {n}")
print(f"\n报告已导出: {OUT_CSV}\n           {OUT_JSON}")
