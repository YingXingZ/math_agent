#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Drive the LIVE server /review with the real candidate images, exactly like
api_app.vision.py:run_candidate_ai_review does. Reports which problems the model
fails to give a storable answer for. Read-only on the DB."""
import sqlite3, os, json, base64, urllib.request, urllib.error, time, sys

DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
ROOT = r"D:\My File\大四\高数教材答案"
VLM = os.environ.get("MATH_VLM_URL", "http://222.211.217.7:18080").rstrip("/") + "/review"
REPORT = r"D:\workbuddy\2026-08-06-15-31-48\recognition_report.json"

FALLBACK_MARKS = ("标准答案字段需人工整理", "结构化结果需人工整理", "服务器已识别",
                  "未识别", "请补充对应答案图片或人工填写", "可入库")

conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row

# Select multi-part first, then a spread of single-part across sections.
rows = conn.execute("""
SELECT c.id, c.section_no, c.problem_no, c.sub_no, c.subquestion_count, c.ocr_text,
       p.content_text, p.ptype,
       (SELECT COUNT(*) FROM candidate_source_images s WHERE s.candidate_id=c.id) AS nsrc
FROM answer_import_candidates c
JOIN problems p ON p.id=c.problem_id
""").fetchall()

def image_paths(cid):
    paths = [r[0] for r in conn.execute(
        "SELECT image_path FROM candidate_source_images WHERE candidate_id=? ORDER BY sort_order,id", (cid,))]
    return paths

multi = [r for r in rows if (r["subquestion_count"] or 0) >= 2 and r["nsrc"] > 0]
single = [r for r in rows if (r["subquestion_count"] or 0) == 0 and r["nsrc"] > 0]
# spread single across sections, cap
seen_sec = set(); single_sample = []
for r in single:
    if r["section_no"] not in seen_sec:
        seen_sec.add(r["section_no"]); single_sample.append(r)
single_sample = single_sample[:14]
targets = multi + single_sample
print(f"multi-part={len(multi)} single-sample={len(single_sample)} total={len(targets)}", flush=True)

def call_review(c):
    paths = image_paths(c["id"])
    imgs = []
    for p in paths:
        disk = os.path.join(ROOT, p)
        if not os.path.isfile(disk): 
            return None, f"missing image {p}"
        imgs.append(base64.b64encode(open(disk,"rb").read()).decode("ascii"))
    if not imgs:
        return None, "no images"
    payload = {
        "image_base64": imgs[0],
        "images_base64": imgs,
        "problem_text": c["content_text"] or "",
        "ocr_text": c["ocr_text"] or "",
        "section_no": c["section_no"] or "",
        "problem_no": str(c["problem_no"] or "") + (("(" + str(c["sub_no"]) + ")") if c["sub_no"] else ""),
        "subquestion_count": int(c["subquestion_count"] or 0),
    }
    req = urllib.request.Request(VLM, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

results = []
for c in targets:
    t0 = time.time()
    res, err = call_review(c)
    dt = round(time.time()-t0, 1)
    label = f"{c['section_no']}-{c['problem_no']}" + (f"({c['sub_no']})" if c['sub_no'] else "")
    if err:
        results.append({"label":label,"id":c["id"],"expected_sub":c["subquestion_count"],
                        "error":err,"verdict":"CALL_FAIL","seconds":dt})
        print(f"[CALL_FAIL] {label}: {err[:120]} ({dt}s)", flush=True)
        continue
    sa = res.get("sub_answers") or []
    ans = str(res.get("std_answer","") or "")
    conf = res.get("confidence")
    is_fallback = any(m in ans for m in FALLBACK_MARKS)
    returned_nums = [str(x.get("sub_no","")) for x in sa] if isinstance(sa,list) else []
    expected = int(c["subquestion_count"] or 0)
    mismatch = bool(expected) and returned_nums != [str(n) for n in range(1, expected+1)]
    if is_fallback:
        verdict = "FALLBACK"
    elif mismatch:
        verdict = "SUB_MISMATCH"
    elif not ans.strip():
        verdict = "EMPTY"
    else:
        verdict = "OK"
    snippet = ans.replace("\n"," / ")[:140]
    results.append({"label":label,"id":c["id"],"expected_sub":expected,
                    "returned_sub":len(sa),"returned_nums":returned_nums,
                    "confidence":conf,"verdict":verdict,"risks":res.get("risks",[]),
                    "std_answer":ans[:400],"seconds":dt})
    print(f"[{verdict}] {label} exp={expected} got={len(sa)}{returned_nums} conf={conf} ({dt}s) :: {snippet}", flush=True)

conn.close()
json.dump(results, open(REPORT,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
# summary
from collections import Counter
print("\nSUMMARY:", dict(Counter(r["verdict"] for r in results)))
print("report ->", REPORT)
