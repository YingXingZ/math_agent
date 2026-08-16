# -*- coding: utf-8 -*-
"""VLM 重识别流水线：对 8014 中仍含损坏（content 全角垃圾，已被门禁 block）的题，
用原教材题图（extract_img/book/<sec>/p<n>_<m>_p<no>.png）调 18080 /solve-from-image
重新转写题干(problem_text)并求解(std_answer+full_solution)，写回 8014。

安全：
  - 仅处理 content 全角垃圾(garbage_score>thr)且不在已放出白名单的题；
  - VLM 输出须 problem_text 与 std_answer 均非空、无替换符、garbage_score<=thr、confidence>=阈值
    才写回并置 answer_status='vlm_recovered'；否则置 'vlm_review'（仍 block，待人工）。
  - 无题图(如 §1.5 缺图)的题跳过，不处理。

用法：
  --test S2.1#14      单题试跑并打印 VLM 原始输出
  (默认) dry-run       打印每题题图是否存在 / VLM confidence / 是否写回
  --apply             写回通过门禁的题
"""
import sys, re, json, sqlite3, os, base64, urllib.request

SRC_DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
IMAGE_ROOT = r"D:\workbuddy\2026-08-06-15-31-48\extract_img"
VLM = "http://127.0.0.1:18080"
GARBAGE_THRESH = 0.006
CONF_THRESH = 0.5
WHITELIST = {("6.2", "4", ""), ("6.7", "8", "")}

FW = re.compile(r"[\uFF21-\uFF3A\uFF41-\uFF5A]"); FP = re.compile(r"[\uFF5E\uFF07\uFF3C\uFF5C]"); PUA = re.compile(r"[\uE000-\uF8FF]")
def gs(t):
    n = len(t); return 0.0 if n == 0 else (len(FW.findall(t)) + len(FP.findall(t)) + len(PUA.findall(t))) / n
def dirty(t):
    return ("\ufffd" in t) or gs(t) > GARBAGE_THRESH

def link_image(section, problem, sub=""):
    n, m = section.split(".")
    cand = os.path.join(IMAGE_ROOT, "book", section, f"p{n}_{m}_p{problem}")
    if sub:
        s = re.search(r"\(?(\d{1,2})\)?", sub)
        if s:
            c2 = cand + f"_sub{s.group(1).zfill(2)}.png"
            if os.path.exists(c2):
                return c2
    for ext in (".png", ".jpg", ".jpeg"):
        if os.path.exists(cand + ext):
            return cand + ext
    return None

def call_vlm(image_path, section, problem):
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    body = json.dumps({"image_base64": b64, "section_no": section, "problem_no": str(problem)}).encode()
    req = urllib.request.Request(VLM + "/solve-from-image", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())

def main():
    test = None
    for a in sys.argv[1:]:
        if a.startswith("--test"):
            test = a.split("=", 1)[1] if "=" in a else sys.argv[sys.argv.index(a) + 1]
    apply = "--apply" in sys.argv

    s = sqlite3.connect(SRC_DB); s.row_factory = sqlite3.Row
    rows = s.execute("""SELECT p.id, p.problem_no, p.sub_no, p.content_text, p.std_answer, s.section_no
                        FROM problems p JOIN sections s ON s.id=p.section_id""").fetchall()
    corrupt = []
    for r in rows:
        if gs(r["content_text"] or "") > 0.006:
            key = (str(r["section_no"]), str(r["problem_no"]), r["sub_no"] or "")
            if key in WHITELIST:
                continue
            corrupt.append((key, r))

    if test:
        test = test.lstrip("S")
        sec, no = test.split("#")
        no = no.split("(")[0]
        img = link_image(sec, no)
        print(f"[TEST] S{sec}#{no} image={img}")
        if not img:
            print("  无题图，无法 VLM"); return
        try:
            out = call_vlm(img, sec, no)
            print(json.dumps(out, ensure_ascii=False, indent=2)[:2500])
        except Exception as e:
            print("  VLM ERROR:", repr(e))
        return

    print(f"=== VLM 重识别 (corrupt={len(corrupt)}, apply={apply}, conf>={CONF_THRESH}) ===")
    written = skipped = noreview = 0
    for key, r in corrupt:
        sec, no, sub = key
        img = link_image(sec, no, sub)
        if not img:
            print(f"  [SKIP-NOIMG] S{sec}#{no}{sub} 无题图")
            skipped += 1
            continue
        try:
            out = call_vlm(img, sec, no)
        except Exception as e:
            print(f"  [VLM-ERR] S{sec}#{no}{sub} {e!r}")
            skipped += 1
            continue
        pt = (out.get("problem_text") or "").strip()
        ans = (out.get("std_answer") or "").strip()
        sol = (out.get("full_solution") or "").strip()
        conf = float(out.get("confidence") or 0)
        ok = pt and ans and not dirty(pt) and not dirty(ans) and conf >= CONF_THRESH
        print(f"  S{sec}#{no}{sub} conf={conf:.2f} pt_len={len(pt)} ans_len={len(ans)} -> {'WRITE' if ok else 'REVIEW'}")
        if not apply:
            continue
        if ok:
            s.execute("UPDATE problems SET content_text=?, std_answer=?, full_solution=?, answer_status='vlm_recovered' WHERE id=?",
                      (pt, ans, sol, r["id"]))
            written += 1
        else:
            s.execute("UPDATE problems SET answer_status='vlm_review' WHERE id=?", (r["id"],))
            noreview += 1
    if apply:
        s.commit()
        print(f"\n[APPLIED] written(vlm_recovered)={written}  marked_review={noreview}  skipped={skipped}")
        print("[next] 运行 bulk_sync_8014.py 放出通过门禁的题。")
    else:
        print(f"\n[DRY-RUN] skipped(no img/err)={skipped}. 加 --apply 写回通过门禁的题。")

if __name__ == "__main__":
    main()
