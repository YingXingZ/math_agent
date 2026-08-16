# -*- coding: utf-8 -*-
"""
回归测试：验证 _where_clause() 的 SQL 注入修复（四份文件全覆盖）。

覆盖文件：
  1. D:/My File/大四/高数教材答案/api_app.vision.py
  2. D:/My File/大四/高数教材答案/api_app.original.py
  3. D:/My File/大四/高数教材答案/api_app.candidate-ui.py
  4. D:/workbuddy/2026-08-06-15-31-48/api_app.py（工作区副本）

做法：
  1. 直接从每份源文件抽取真实的 `def _where_clause`，执行得到 new_where_clause；
  2. 用一份与"修复前"完全一致的参考实现 old_where_clause 作对照；
  3. 在内存 SQLite 中建最小 problems/sections 表并灌入数据；
  4. 用"修复前"的拼接方式（字符串直接拼入、无参数）执行 —— 证明注入可绕过过滤；
  5. 用"修复后"的参数化方式（? 占位 + args）执行 —— 证明恶意输入被当作字面值，
     且不破坏正常过滤语义。
"""
import ast
import sqlite3

PATCHED_FILES = [
    r"D:/My File/大四/高数教材答案/api_app.vision.py",
    r"D:/My File/大四/高数教材答案/api_app.original.py",
    r"D:/My File/大四/高数教材答案/api_app.candidate-ui.py",
    r"D:/workbuddy/2026-08-06-15-31-48/api_app.py",
]


def load_real_where_clause(path: str):
    """从源文件抽取真实的 _where_clause 函数定义并执行，返回可调用对象。"""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_where_clause":
            fn_src = ast.get_source_segment(src, node)
            ns: dict = {}
            exec(fn_src, ns)
            return ns["_where_clause"]
    raise RuntimeError("未找到 _where_clause 定义")


# 修复前（有注入漏洞）的参考实现：用户值通过 f-string 直接拼入 SQL 文本
def old_where_clause(section_no, ptype, dmin, dmax, kp, q):
    c = ""
    if section_no:
        c += f" AND s.section_no='{section_no}'"
    if ptype:
        c += f" AND p.ptype='{ptype}'"
    if dmin is not None:
        c += f" AND p.difficulty>={dmin}"
    if dmax is not None:
        c += f" AND p.difficulty<={dmax}"
    if kp:
        parts = [f"p.knowledge_pts LIKE '%{x.strip()}%'" for x in kp.split(",")]
        c += " AND (" + " OR ".join(parts) + ")"
    if q:
        c += f" AND (s.section_no LIKE '%{q}%' OR p.problem_no LIKE '%{q}%' OR p.exercise_set LIKE '%{q}%')"
    return c


new_where_clause = load_real_where_clause(PATCHED_FILES[0])

# ---- 最小数据库 ----
db = sqlite3.connect(":memory:")
db.executescript(
    """
    CREATE TABLE sections(id INTEGER PRIMARY KEY, section_no TEXT);
    CREATE TABLE problems(id INTEGER PRIMARY KEY, section_id INTEGER,
        exercise_set TEXT, problem_no TEXT, sub_no TEXT, ptype TEXT,
        difficulty INTEGER, knowledge_pts TEXT);
    INSERT INTO sections(id, section_no) VALUES (1,'1.1'),(2,'2.3');
    INSERT INTO problems(section_id,exercise_set,problem_no,ptype,difficulty,knowledge_pts)
        VALUES (1,'A','1','calc',1,'极限'),
               (1,'A','2','proof',3,'连续'),
               (2,'B','1','calc',2,'导数');
    """
)


def count_raw(where_clause_str):
    """修复前的执行方式：字符串拼接、无参数绑定。"""
    return db.execute(
        "SELECT COUNT(*) FROM problems p JOIN sections s ON s.id=p.section_id "
        "WHERE 1=1" + where_clause_str
    ).fetchone()[0]


def count_param(clause, args):
    """修复后的执行方式：占位符 + 参数绑定。"""
    return db.execute(
        "SELECT COUNT(*) FROM problems p JOIN sections s ON s.id=p.section_id "
        "WHERE 1=1" + clause, args
    ).fetchone()[0]


results = {}

# ===== 1) 修复前：恶意载荷注入绕过过滤 =====
# 该载荷在旧的"字符串拼接"实现中会闭合外层括号并注入永真条件，
# 使 COUNT 查询返回全部 3 行（过滤被完全绕过）。
malicious = "x') OR ('1'='1') -- "
old_clause = old_where_clause(None, None, None, None, None, malicious)
results["old_clause_text"] = old_clause
results["old_injection_total"] = count_raw(old_clause)        # 预期 = 全部 3 行（注入成功）

# ===== 2) 修复后：同一恶意载荷被当作字面值，返回 0，且不报错 =====
clause, args = new_where_clause(None, None, None, None, None, malicious)
results["new_clause"] = clause
results["new_args"] = args
results["new_injection_total"] = count_param(clause, args)   # 预期 = 0（安全：字面值无匹配）

# ===== 3) 修复后：正常过滤仍正确 =====
c2, a2 = new_where_clause("1.1", None, None, None, None, None)
results["new_benign_section_total"] = count_param(c2, a2)     # 预期 = 2（section 1.1 两题）
c3, a3 = new_where_clause(None, None, None, None, None, "A")
results["new_benign_search_total"] = count_param(c3, a3)      # 预期 = 2（exercise_set 含 A）
c4, a4 = new_where_clause(None, "calc", 1, 2, None, None)
results["new_benign_difficulty_total"] = count_param(c4, a4)  # 预期 = 2（calc 且难度 1~2）

# ===== 4) 返回值契约：必须是 (clause, args) 元组且 clause 含占位符 =====
returns_tuple = isinstance(clause, str) and isinstance(args, list) and "?" in clause
results["returns_parameterized"] = returns_tuple

# ---- 断言 ----
assert results["old_injection_total"] == 3, "修复前应被注入绕过（返回全部行）"
assert results["new_injection_total"] == 0, "修复后恶意输入须被当作字面值，返回 0"
assert results["new_benign_section_total"] == 2, "正常按节过滤应得 2 行"
assert results["new_benign_search_total"] == 2, "正常按 exercise_set 过滤应得 2 行"
assert results["new_benign_difficulty_total"] == 2, "正常按难度过滤应得 2 行"
assert returns_tuple, "_where_clause 必须返回参数化的 (clause, args)"

print("=== 回归测试结果 ===")
for k, v in results.items():
    print(f"{k} = {v!r}")
print("REGRESSION_TEST_PASS")

# ===== 5) 全文件覆盖：每份补丁文件的 _where_clause 必须返回参数化元组 =====
print("\n=== 全文件覆盖测试 ===")
all_ok = True
for fpath in PATCHED_FILES:
    try:
        fn = load_real_where_clause(fpath)
        c, a = fn(None, None, None, None, None, malicious)
        inj = count_param(c, a)
        ok = isinstance(c, str) and isinstance(a, list) and "?" in c and inj == 0
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"  {fpath.split('/')[-1]:30s}  injection_total={inj}  {status}")
    except Exception as exc:
        all_ok = False
        print(f"  {fpath.split('/')[-1]:30s}  ERROR: {exc}")

assert all_ok, "部分文件未通过参数化注入测试"
print("ALL_FILES_PASS")
