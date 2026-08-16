# -*- coding: utf-8 -*-
"""Feasibility test: render 李继成 教材上册 page(s) for 习题 3.5 (printed pg 173) and
call VLM /solve-from-image for problem 5. Observe what VLM returns for a multi-problem page.
"""
import fitz, os, base64, json, urllib.request, re

PDF = r"D:\My File\大四\高数教材答案\李继成高数-教材-上册-2版(1).pdf"
VLM = "http://127.0.0.1:18080"
OUT = r"D:\workbuddy\2026-08-06-15-31-48\_feas_s35"
os.makedirs(OUT, exist_ok=True)

def render(pdf_index, w=1000):
    doc = fitz.open(PDF)
    page = doc[pdf_index]
    zoom = w / page.rect.width
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    p = os.path.join(OUT, f"pg{pdf_index}.png")
    pix.save(p)
    doc.close()
    return p

def call_vlm(image_path, sec, no):
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    body = json.dumps({"image_base64": b64, "section_no": sec, "problem_no": str(no)}).encode()
    req = urllib.request.Request(VLM + "/solve-from-image", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())

# printed 173 -> try pdf indices 171,172,173,174
for idx in [171, 172, 173, 174]:
    p = render(idx)
    print(f"\n########## PDF index {idx} ##########")
    try:
        out = call_vlm(p, "3.5", "5")
    except Exception as e:
        print("VLM ERROR:", repr(e)); continue
    pt = out.get("problem_text") or ""
    print("confidence:", out.get("confidence"), "ptype:", out.get("ptype"))
    print("problem_text len:", len(pt))
    print("---- problem_text ----")
    print(pt[:1200])
    print("---- std_answer (head) ----")
    print((out.get("std_answer") or "")[:300])
