import sqlite3, re
SRC_DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
s = sqlite3.connect(SRC_DB); s.row_factory = sqlite3.Row

# 已知数学/变量 token 白名单（小写）
WHITE = set("""sin cos tan cot sec cosec csc_ alt lim log ln lg exp dx dy dz dt du dv dw ds
grad div curl det max min inf sup sum int alpha beta gamma delta theta lambda mu nu
pi sigma omega phi psi rho tau xi eta zeta forall exists frac sqrt partial infty
equiv approx le ge ne pm times mathbb mathrm mathbf mathcal vec hat bar tilde
sqrt sinx cosx tanx expx logx lnx arctan arcsin arccos arccot arctanh deg mod
res sub sup adj ker rank col row span null dim prod sympy oo dfrac tfrac cn
eta iota kappa tanh sinh cosh arsinh arcosh artanh""".split())
VARPAIRS = set("xy ab uv ij kl mn pq rs wx yz ax bx cx ay by cy az bz ac bc ad bd".split())

FW_LATIN = re.compile(r"[\uFF21-\uFF3A\uFF41-\uFF5A]")
FW_PUNCT = re.compile(r"[\uFF5E\uFF07\uFF3C\uFF5C]")
PUA = re.compile(r"[\uE000-\uF8FF]")
def garbage_score(t):
    n = len(t)
    if n == 0:
        return 0.0
    return (len(FW_LATIN.findall(t)) + len(FW_PUNCT.findall(t)) + len(PUA.findall(t))) / n

LATRUN = re.compile(r"[A-Za-z]{2,}")
def salad_score(t):
    # 仅对“非结构化 LaTeX”内容做 salad 检测：含反斜杠或花括号视为有结构的 LaTeX，跳过
    if "\\" in t or "{" in t:
        return (0, 0)
    runs = LATRUN.findall(t)
    if not runs:
        return (0, 0)
    weird = 0
    for w in runs:
        wl = w.lower()
        if wl in WHITE:
            continue
        if wl in VARPAIRS:
            continue
        if len(wl) == 2 and wl[0] == wl[1]:
            continue
        weird += 1
    return (weird, len(runs))

print("=== scan 8014 full problems: fullwidth corruption OR ascii-salad (non-LaTeX) corruption ===")
hits = []
for r in s.execute("""SELECT p.id,p.problem_no,p.sub_no,p.content_text,p.std_answer,p.answer_status,s.section_no
                      FROM problems p JOIN sections s ON p.section_id=s.id
                      WHERE p.content_text IS NOT NULL AND p.content_text<>''
                        AND p.std_answer IS NOT NULL AND p.std_answer<>''"""):
    c = r["content_text"]; g = garbage_score(c); wk, tot = salad_score(c)
    flag = ""
    if g > 0.006:
        flag += "[FW]"
    if wk >= 4:
        flag += "[SALAD]"
    if flag:
        hits.append((r["section_no"], r["problem_no"], r["sub_no"], flag, g, wk, tot, r["id"], c[:55]))
print(f"corrupt hits: {len(hits)}")
for sec, no, sub, flag, g, wk, tot, pid, c0 in sorted(hits, key=lambda x: (-x[4], -x[5])):
    print(f"  S{sec} #{no}{sub or ''} {flag} fw={g:.4f} weird={wk}/{tot}  {c0!r}")
