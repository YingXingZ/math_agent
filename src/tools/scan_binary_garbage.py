import sqlite3, re
AGENT_DB = r"D:\My File\大四\高数教材答案\高数作业助手\data\homework.db"
a = sqlite3.connect(AGENT_DB); a.row_factory = sqlite3.Row

WHITE = set("""sin cos tan cot sec cosec lim log ln lg exp dx dy dz dt du dv dw ds
grad div curl det max min inf sup sum int alpha beta gamma delta theta lambda mu nu
pi sigma omega phi psi rho tau xi eta zeta forall exists frac sqrt partial infty
equiv approx le ge ne pm times mathbb mathrm mathbf mathcal vec hat bar tilde
sinh cosh tanh arsinh arcosh artanh arctan arcsin arccos arccot deg mod
res sub sup adj ker rank col row span null dim prod oo dfrac tfrac cn iota kappa
cosh sinh tanh prj vo yo xo yo neo im re arg erf bessel""".split())
VARPAIRS = set("xy ab uv ij kl mn pq rs wx yz ax bx cx ay by cy az bz ac bc ad bd".split())
LATRUN = re.compile(r"[A-Za-z]{2,}")
CJK = re.compile(r"[一-鿿]")

def stats(t):
    n = len(t)
    if n == 0:
        return (0, 0, 0.0)
    cjk = len(CJK.findall(t))
    if "\\" in t or "{" in t:
        return (0, 0, cjk / n)  # LaTeX -> skip salad
    runs = LATRUN.findall(t)
    weird = 0
    for w in runs:
        wl = w.lower()
        if wl in WHITE or wl in VARPAIRS:
            continue
        if len(wl) == 2 and wl[0] == wl[1]:
            continue
        weird += 1
    return (weird, len(runs), cjk / n)

print("=== published Agent rows: binary-garbage signature (weird>=8 & cjk_ratio<0.25 & no-LaTeX) ===")
hits = []
for r in a.execute("SELECT id,chapter,source_problem_no,content FROM questions WHERE review_status='published'"):
    c = r["content"] or ""
    wk, tot, ratio = stats(c)
    if wk >= 8 and ratio < 0.25:
        hits.append((wk, ratio, r["id"], r["chapter"], r["source_problem_no"], c))
print(f"candidates: {len(hits)}")
for wk, ratio, qid, ch, no, c in sorted(hits, key=lambda x: -x[0]):
    print(f"\n--- id={qid} §{ch}#{no} weird={wk} cjk={ratio:.2f} ---")
    print(c[:300])
