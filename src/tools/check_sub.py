#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, json, base64, sqlite3, urllib.request, re
DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
ROOT = r"D:\My File\大四\高数教材答案"
VLM = "http://222.211.217.7:18080/review"
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
c = conn.execute("SELECT image_path FROM candidate_source_images WHERE candidate_id=509 ORDER BY sort_order").fetchall()
conn.close()
imgs = []
for r in c:
    fp = os.path.join(ROOT, r[0].replace("\\","/"))
    if os.path.isfile(fp): imgs.append(base64.b64encode(open(fp,"rb").read()).decode("ascii"))
payload = {"image_base64": imgs[0], "images_base64": imgs, "problem_text":"", "ocr_text":"",
           "section_no":"4.7","problem_no":"4","subquestion_count":0}
req = urllib.request.Request(VLM, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                             headers={"Content-Type":"application/json"}, method="POST")
res = json.loads(urllib.request.urlopen(req, timeout=600).read().decode("utf-8"))
open("last_review.json","w",encoding="utf-8").write(json.dumps(res, ensure_ascii=False, indent=2))
sa = res.get("sub_answers") or []
print("sub_answers 数量:", len(sa))
print("detected_subquestion_count:", res.get("detected_subquestion_count"))
print("=== std_answer 前 400 字符 ===")
print(repr(res.get("std_answer","")[:400]))
# local regex test
pat = re.compile(r"(?m)(?:^|\n)\s*[\(（\[]?\s*(\d{1,2})\s*[\)）\.\]]\s+")
m = pat.findall(res.get("std_answer",""))
print("本地正则匹配到的编号:", m)
