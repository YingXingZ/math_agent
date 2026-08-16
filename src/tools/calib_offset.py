# -*- coding: utf-8 -*-
"""Calibrate PDF-index -> printed-page offset by reading footers via VLM.
Render several indices of 李继成 教材上册, ask VLM for the footer page number.
If offset is constant, target_index = printed_page + offset.
"""
import fitz, os, base64, json, urllib.request

PDF = r"D:\My File\大四\高数教材答案\李继成高数-教材-上册-2版(1).pdf"
VLM = "http://127.0.0.1:18080"
OUT = r"D:\workbuddy\2026-08-06-15-31-48\_calib"
os.makedirs(OUT, exist_ok=True)

def render(pdf_index, w=700):
    doc = fitz.open(PDF)
    page = doc[pdf_index]
    zoom = w / page.rect.width
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    p = os.path.join(OUT, f"pg{pdf_index}.png")
    pix.save(p); doc.close()
    return p

def read_footer(image_path):
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    # use /solve-from-image with a steer prompt won't give footer; instead craft a minimal
    # We'll abuse /solve-from-image but only care about a custom instruction is not supported.
    # So just ask and parse: we send image and rely on model to mention page. Better: use a tiny prompt via /solve.
    body = json.dumps({"image_base64": b64, "section_no": "?", "problem_no": "?"}).encode()
    req = urllib.request.Request(VLM + "/solve-from-image", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())

for idx in [20, 40, 60, 80, 100, 120, 140]:
    p = render(idx)
    try:
        out = read_footer(p)
    except Exception as e:
        print(idx, "ERR", repr(e)); continue
    pt = out.get("problem_text") or ""
    # footer page number often appears; print full problem_text tail to spot a page number
    print(f"idx={idx} conf={out.get('confidence')} ptlen={len(pt)}")
    print("   tail:", pt[-120:].replace("\n", " | "))
