#!/usr/bin/env python3
"""Recover the 27 blocked (OCR-garbled) problems via VLM re-transcription of the
local 李继成 textbook PDFs.

Strategy
--------
* Locate each 习题 X.Y page via TOC printed-page number + a per-volume offset
  (upper=+22, lower=+21, both empirically confirmed on anchors).
* Render the page, call VLM /solve-from-image(section, problem_no) which returns
  a CLEAN transcribed stem (problem_text) AND a model-solved answer (std_answer /
  full_solution).  Generated text => no OCR mojibake => satisfies "never push garbage".
* Sweep a small idx window around the candidate and keep the highest-confidence hit.
* Write results to recover_27_results.json + a CSV for review.  No DB write here.

Run:  python recover_27_vlm.py            # all 27
       python recover_27_vlm.py --pilot   # 3 anchors only
"""
import argparse, base64, csv, json, os, re, sys, time, urllib.request
from pathlib import Path

import fitz  # PyMuPDF (managed venv)

WS = Path(r"D:\workbuddy\2026-08-06-15-31-48")
IMA = WS / "ima_text"
VLM = "http://127.0.0.1:18080/solve-from-image"

PDF_UP = r"D:\My File\大四\高数教材答案\李继成高数-教材-上册-2版(1).pdf"
PDF_LO = r"D:\My File\大四\高数教材答案\李继成高数-教材-下册-2版(1).pdf"

OFFSET = {"upper": 22, "lower": 21}
SWEEP_LO, SWEEP_HI = -6, 12   # idx window around candidate
RENDER_W = 1000

# 27 blocked targets.  volume inferred from section number (1-4 upper, 5-9 lower).
TARGETS = [
    ("2.8", 3), ("3.5", 5), ("3.5", 7), ("3.5", 8),
    ("3.6", 6), ("3.6", 7), ("3.8", 4),
    ("6.8", 5), ("7.2", 2), ("7.3", 10), ("7.3", 12),
    ("7.4", 1), ("7.4", 5), ("8.2", 5), ("8.2", 9),
    ("8.4", 4), ("8.6", 5), ("8.6", 6), ("8.6", 7), ("8.6", 8),
    ("9.1", 1), ("9.1", 6), ("9.2", 1), ("9.3", 2), ("9.3", 3),
    ("9.5", 3), ("9.7", 2),
]

PILOT = [("2.8", 3), ("3.5", 5), ("6.8", 5)]


def volume_of(sec: str) -> str:
    ch = int(sec.split(".")[0])
    return "upper" if ch <= 4 else "lower"


def parse_toc_printed(sec: str, vol: str):
    """First occurrence of '习题 X.Y <pagenum>' in the IMA textbook OCR text."""
    path = IMA / ("textbook_upper.txt" if vol == "upper" else "textbook_lower.txt")
    txt = path.read_text(encoding="utf-8").splitlines()
    pat = re.compile(r"^习题\s*(" + re.escape(sec) + r")\s+(\d+)\s*$")
    for line in txt:
        m = pat.match(line.strip())
        if m:
            return int(m.group(2))
    return None


def render_page(pdf, idx, out_png, w=RENDER_W):
    if os.path.exists(out_png):
        return out_png
    doc = fitz.open(pdf)
    if idx >= doc.page_count:
        doc.close(); return None
    pg = doc[idx]
    zoom = w / pg.rect.width
    pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).save(out_png)
    doc.close()
    return out_png


def vlm_solve(png, sec, no, retries=2):
    with open(png, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    body = json.dumps({"image_base64": b64, "section_no": sec,
                       "problem_no": str(no)}).encode()
    for _ in range(retries):
        try:
            req = urllib.request.Request(VLM, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=200) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = repr(e)
            time.sleep(2)
    return {"error": last}


def recover_one(sec, no):
    vol = volume_of(sec)
    pdf = PDF_UP if vol == "upper" else PDF_LO
    printed = parse_toc_printed(sec, vol)
    if printed is None:
        return {"sec": sec, "no": no, "error": f"no TOC printed page for {sec}"}
    cand = printed + OFFSET[vol]
    best = None
    out_dir = WS / f"_vlm_{sec.replace('.', '_')}"
    out_dir.mkdir(exist_ok=True)
    for off in range(SWEEP_LO, SWEEP_HI + 1):
        idx = cand + off
        png = str(out_dir / f"p{idx}.png")
        if not render_page(pdf, idx, png):
            continue
        res = vlm_solve(png, sec, no)
        if "error" in res:
            continue
        conf = float(res.get("confidence", 0) or 0)
        stem = (res.get("problem_text") or "").strip()
        # accept if high conf OR the page actually shows this problem number
        hit = conf >= 0.9 or (stem and re.search(rf"(?:^|\n)\s*{re.escape(str(no))}\s*[\.\)]", stem) is not None)
        if hit and (best is None or conf > best["conf"]):
            best = {"idx": idx, "conf": conf, "png": png,
                    "stem": stem, "ptype": res.get("ptype"),
                    "std_answer": res.get("std_answer"),
                    "full_solution": res.get("full_solution"),
                    "risks": res.get("risks"),
                    "partial": res.get("partial")}
        if conf >= 0.95:
            break
    if best is None:
        return {"sec": sec, "no": no, "printed": printed, "cand": cand,
                "error": "no confident page found in sweep"}
    best.update({"sec": sec, "no": no, "printed": printed, "cand": cand,
                 "vol": vol})
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", action="store_true")
    args = ap.parse_args()
    targets = PILOT if args.pilot else TARGETS
    results = []
    for sec, no in targets:
        print(f"[*] recovering {sec}#{no} ...", flush=True)
        r = recover_one(sec, no)
        if "error" in r:
            print(f"    ! {r['error']}", flush=True)
        else:
            print(f"    idx={r['idx']} conf={r['conf']:.2f} ptype={r['ptype']} "
                  f"stem[:70]={r['stem'][:70]!r}", flush=True)
        results.append(r)
        time.sleep(0.3)
    out = WS / "recover_27_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    # CSV summary
    csvp = WS / "recover_27_summary.csv"
    with open(csvp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["sec", "no", "vol", "printed", "cand_idx", "best_idx",
                    "conf", "ptype", "partial", "stem_head", "risks"])
        for r in results:
            if "error" in r:
                w.writerow([r.get("sec"), r.get("no"), "", r.get("printed", ""),
                            r.get("cand", ""), "", "", "", "", "ERROR: " + r["error"], ""])
                continue
            w.writerow([r["sec"], r["no"], r.get("vol"), r.get("printed"),
                        r.get("cand"), r["idx"], f"{r['conf']:.2f}", r.get("ptype"),
                        r.get("partial"), (r.get("stem") or "")[:60],
                        "; ".join(r.get("risks") or [])])
    print(f"\n[done] wrote {out} and {csvp}")


if __name__ == "__main__":
    main()
