#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pull the 4.7 第4题 candidate from the local workbench DB, call the live
server /review, and validate exactly like backend run_candidate_ai_review()."""
import os, json, base64, sqlite3, urllib.request, urllib.error

DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
ROOT = r"D:\My File\大四\高数教材答案"
VLM = "http://222.211.217.7:18080/review"
TIMEOUT = 600

def candidate_image_paths(conn, cid):
    rows = conn.execute(
        "SELECT image_path FROM candidate_source_images WHERE candidate_id=? ORDER BY sort_order",
        (cid,)).fetchall()
    return [r[0] for r in rows]

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
# find 4.7 problem 4 candidate(s)
rows = conn.execute("""
    SELECT c.id, c.section_no, c.problem_no, c.sub_no, c.subquestion_count,
           p.content_text, p.ptype
    FROM answer_import_candidates c
    JOIN problems p ON p.id=c.problem_id
    WHERE c.section_no LIKE '4.7%'
    ORDER BY c.problem_no, c.sub_no
""").fetchall()
print("=== 4.7 候选 ===")
for r in rows:
    print(f"  id={r['id']} sec={r['section_no']} no={r['problem_no']}({r['sub_no']}) "
          f"subq={r['subquestion_count']} ptype={r['ptype']}")

# pick problem 4 (sub_no empty -> the parent multi-part)
target = None
for r in rows:
    if str(r['problem_no']) == '4' and (r['sub_no'] in (None, '', 0)):
        target = r
if not target:
    # fallback: any problem_no 4
    for r in rows:
        if str(r['problem_no']) == '4':
            target = r
            break
if not target:
    print("未找到 4.7 第4题候选，改用第一个多小问候选")
    for r in rows:
        if int(r['subquestion_count'] or 0) >= 2:
            target = r
            break
if not target:
    target = rows[0] if rows else None
if not target:
    print("没有任何 4.7 候选"); raise SystemExit(1)

cid = target['id']
print(f"\n=== 测试目标 candidate id={cid} (sec={target['section_no']} no={target['problem_no']} subq={target['subquestion_count']}) ===")
paths = candidate_image_paths(conn, cid)
print("source images:", len(paths), paths[:3])
conn.close()

imgs_b64 = []
for p in paths:
    fp = os.path.join(ROOT, p.replace("\\", "/"))
    if os.path.isfile(fp):
        imgs_b64.append(base64.b64encode(open(fp, "rb").read()).decode("ascii"))
    else:
        print("  缺失图片:", fp)
if not imgs_b64:
    print("无可用图片，退出"); raise SystemExit(1)

payload = {
    "image_base64": imgs_b64[0],
    "images_base64": imgs_b64,
    "problem_text": target['content_text'] or "",
    "ocr_text": "",
    "section_no": target['section_no'] or "",
    "problem_no": str(target['problem_no'] or ""),
    "subquestion_count": int(target['subquestion_count'] or 0),
}
print("\n>>> POST", VLM, "subquestion_count=", payload["subquestion_count"])
try:
    req = urllib.request.Request(VLM, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        result = json.loads(resp.read().decode("utf-8"))
except Exception as exc:
    print("!!! 调用异常:", repr(exc))
    raise SystemExit(1)

# ---- replicate backend validation ----
print("\n=== 后端校验 ===")
ptype = result.get("ptype")
print("ptype:", ptype)
if ptype not in {"calc", "proof"}:
    print("  [FAIL] invalid ptype -> 后端会抛 ValueError 'AI returned invalid ptype'")
std = str(result.get("std_answer", "")).strip()
print("std_answer:", repr(std[:120]))
if not std:
    print("  [FAIL] empty std_answer -> 后端抛 'AI returned an empty standard answer'")
expected = int(target['subquestion_count'] or 0)
if expected:
    sub = result.get("sub_answers")
    returned = [str(i.get("sub_no", "")) for i in sub] if isinstance(sub, list) else []
    exp_list = [str(n) for n in range(1, expected + 1)]
    print(f"sub_answers 返回序号: {returned}  期望: {exp_list}")
    if returned != exp_list:
        print("  [FAIL] 子题数量/序号不符 -> 后端抛 ValueError 'AI subquestion mismatch'")
    # show each sub std_answer
    for i in sub if isinstance(sub, list) else []:
        print(f"    sub {i.get('sub_no')}: {repr(str(i.get('std_answer',''))[:80])} conf={i.get('confidence')}")
print("\n=== 完整结果(截断) ===")
print(json.dumps(result, ensure_ascii=False)[:1500])
