import sqlite3, os

DB = r"D:/My File/大四/高数教材答案/api.workbench.db"
con = sqlite3.connect(DB)
cur = con.cursor()

targets = [
    ("1.1","9",""),("1.2","6",""),("1.5","8",""),
    ("2.1","14",""),("2.2","4",""),("2.2","7",""),("2.8","3",""),
    ("3.5","5",""),("3.5","7",""),("3.5","8",""),("3.6","6",""),("3.6","7",""),("3.8","4",""),
    ("6.2","9",""),("6.3","1",""),("6.3","4",""),("6.5","4",""),("6.5","5",""),("6.6","2",""),("6.8","5",""),
    ("7.2","2",""),("7.3","10",""),("7.3","12",""),("7.4","1",""),("7.4","5",""),
    ("8.2","5",""),("8.2","9",""),("8.4","4",""),("8.6","5",""),("8.6","6",""),("8.6","7",""),("8.6","8",""),
    ("9.1","1",""),("9.1","6",""),("9.2","1",""),("9.3","2",""),("9.3","3",""),("9.5","3",""),("9.7","2",""),
]

def garb(t):
    if not t: return 0.0
    bad = sum(1 for c in t if (0xFF21<=ord(c)<=0xFF3A) or (0xFF41<=ord(c)<=0xFF5A)
              or c in "～＇＼｜" or (0xE000<=ord(c)<=0xF8FF))
    return bad/len(t)

for sec,pn,sub in targets:
    cur.execute("SELECT id,exercise_set,problem_no,sub_no,answer_status,content_text,std_answer,full_solution,source_page,crop_image_path FROM problems WHERE exercise_set=? AND problem_no=? AND (sub_no=? OR (?='' AND (sub_no IS NULL OR sub_no='')))",
                (sec,pn,sub,sub))
    rows = cur.fetchall()
    if not rows:
        print(f"MISS {sec}#{pn}{('/'+sub) if sub else ''}")
        continue
    for r in rows:
        rid,es,pno,sb,ast,ct,sa,fs,sp,cp = r
        ct = ct or ""; sa = sa or ""; fs = fs or ""
        cg = garb(ct); sg = garb(sa); fg = garb(fs)
        cflag = "GARB" if cg>0.006 else ("STUB" if len(ct.strip())<6 else "ok")
        print(f"id={rid} {es}#{pno}{('/'+sb) if sb else ''} status={ast} srcpage={sp} crop={'Y' if cp else '-'}")
        print(f"    content[{cflag} g={cg:.4f} len={len(ct)}] answer[g={sg:.4f} len={len(sa)}] sol[g={fg:.4f} len={len(fs)}]")
        if cflag!="ok":
            print("    CT_HEAD:", repr(ct[:100]))
con.close()
