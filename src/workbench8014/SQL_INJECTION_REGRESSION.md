# SQL 注入修复 · 全链路回归测试文档（Phase ⑦）

> 范围：`api_app.vision.py` / `api_app.original.py` / `api_app.candidate-ui.py` 中的
> `_where_clause()` 及其调用点 `GET /problems`（经 `serve_vision.py` 挂载为
> `GET /api/problems`）。
> 修复目标：消除 `GET /problems` 过滤参数（section_no / ptype / difficulty_min/max
> / knowledge_pts / q）的 SQL 注入面，确保所有用户输入经参数化查询处理。

---

## 1. 漏洞摘要（修复前）

| 项 | 内容 |
|----|------|
| 位置 | 三个文件各自定义的 `def _where_clause(...)`，以及 `query_problems` 中对 `total` 的 COUNT 查询 |
| 根因 | `_where_clause` 用 f-string 把用户输入**直接拼进 SQL 文本**并返回字符串；调用点把它拼到<br>`"SELECT COUNT(*) ... WHERE 1=1" + where` 中，**未绑定任何参数** |
| 危险语句（旧） | `c += f" AND s.section_no='{section_no}'"`、<br>`c += f" AND p.knowledge_pts LIKE '%{x.strip()}%'"`、<br>`c += f" AND (s.section_no LIKE '%{q}%' OR ... )"` |
| 影响 | 攻击者可在 `q` / `section_no` / `knowledge_pts` / `ptype` 中注入 `' OR '1'='1' -- ` 之类的载体，<br>闭合外层括号并注入永真条件，使 COUNT 与列表查询**绕过全部过滤**（返回全表），或触发语法错误 |
| 注意 | 同一端点的主查询（`sql` 变量）**原本就是参数化**的（`?` + `args`），漏洞只存在于 COUNT 分支 |

---

## 2. 修复方案

1. `_where_clause` 改为返回 **`(clause, args)` 元组**：所有值用 `?` 占位，值进入 `args` 列表，
   绝不出现在 SQL 文本中。
2. `query_problems` 改为**单一数据源**：先 `where, args = _where_clause(...)`，
   主查询 `sql += where` 并追加 `LIMIT/OFFSET`；`total` 查询复用同一 `where`，
   参数取 `args[:-2]`（去掉末尾的 size / offset）后执行 `conn.execute(sql, args)`。
3. 三份重复实现（`vision` / `original` / `candidate-ui`）一并修复，杜绝遗留风险。

```python
def _where_clause(section_no, ptype, dmin, dmax, kp, q):
    clause = ""
    args: list = []
    if section_no:
        clause += " AND s.section_no=?"; args.append(section_no)
    if ptype:
        clause += " AND p.ptype=?"; args.append(ptype)
    if dmin is not None:
        clause += " AND p.difficulty>=?"; args.append(dmin)
    if dmax is not None:
        clause += " AND p.difficulty<=?"; args.append(dmax)
    if kp:
        parts = ["p.knowledge_pts LIKE ?" for _ in kp.split(",")]
        clause += " AND (" + " OR ".join(parts) + ")"
        args += [f"%{x.strip()}%" for x in kp.split(",")]
    if q:
        clause += " AND (s.section_no LIKE ? OR p.problem_no LIKE ? OR p.exercise_set LIKE ?)"
        args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    return clause, args
```

---

## 3. 修复前 / 修复后测试用例

### 3.1 单元回归（`regression_where_clause.py`，直接加载被修复源文件中的真实函数）

测试数据库（内存 SQLite，最小 schema + 3 行）：

| id | section_no | exercise_set | ptype | difficulty |
|----|-----------|--------------|-------|-----------|
| 1  | 1.1       | A            | calc  | 1         |
| 2  | 1.1       | A            | proof | 3         |
| 3  | 2.3       | B            | calc  | 2         |

| 用例 | 输入 | 修复前行为 | 修复后行为 | 结论 |
|------|------|-----------|-----------|------|
| 注入绕过（q） | `q="x') OR ('1'='1') -- "` | COUNT 返回 **3**（全表，过滤被绕过） | COUNT 返回 **0**（输入被当作字面量 `%x') OR ('1'='1') -- %`，无匹配） | ✅ 注入失效 |
| 正常按节过滤 | `section_no="1.1"` | 2 | 2 | ✅ 语义不变 |
| 正常关键字搜索 | `q="A"` | 2 | 2 | ✅ 语义不变 |
| 正常难度区间 | `ptype="calc"`, `difficulty 1..2` | 2 | 2 | ✅ 语义不变 |
| 返回值契约 | — | 返回字符串（拼接） | 返回 `(clause, args)` 且 clause 含 `?` | ✅ 参数化 |

实测输出（节选）：
```
old_injection_total = 3          # 修复前：注入返回全部行
new_injection_total = 0          # 修复后：注入被当字面值，0 行
new_benign_section_total = 2     # 正常过滤仍正确
new_benign_search_total = 2
new_benign_difficulty_total = 2
returns_parameterized = True
REGRESSION_TEST_PASS
```

### 3.2 全链路回归（重启 8014 后，对真实 `GET /api/problems` 发起请求）

| 用例 | 请求 | 预期 | 实测 |
|------|------|------|------|
| 正常列表 | `GET /api/problems?size=2` | 200 + 合法 JSON | ✅ 200（本库暂空，`total=0`） |
| 注入 q | `GET /api/problems?q=x') OR ('1'='1') -- ` | 200 + `total` 为字面搜索结果（非全表），无 500 | ✅ `total=0`，合法 JSON |
| 注入 section_no | `GET /api/problems?section_no=x') OR ('1'='1') -- ` | 同上 | ✅ `total=0` |
| 注入 knowledge_pts | `GET /api/problems?knowledge_pts=x') OR ('1'='1') -- ` | 同上 | ✅ `total=0` |
| 注入 ptype | `GET /api/problems?ptype=x') OR ('1'='1') -- ` | 同上 | ✅ `total=0` |

> 说明：当前 `api.db` 尚未入库题目，故 `total` 均为 0；重点验证的是**恶意载荷不再引发
> 语法错误、不再绕过过滤、被当作普通搜索字符串处理**——单元回归已用带数据的库证明
> "修复前返回全表 / 修复后返回 0"。

---

## 4. 回归范围

| 类别 | 覆盖项 |
|------|--------|
| 直接修复 | `_where_clause()` ×3 文件；`query_problems` 的 COUNT 分支 ×3 文件 |
| 关联代码 | 主查询 `sql`（原本已参数化，确认未被改动破坏）；`args[:-2]` 取参逻辑 |
| 调用链 | `GET /problems` → `query_problems` → `_where_clause` → `conn.execute` |
| 外部影响 | 8000 Agent 经 `knowledge_bridge` 调 8014 `/api/problems` 的同步逻辑不受影响（入参来自 8014 内部，非用户直接注入；且参数化后更安全） |
| 未改动 | 其他端点（/problems/tiers、/problems/answers-status 等）本就用参数化 `?`，不在本次范围 |

---

## 5. 验证流程

### 5.1 单元回归（推荐，CI 可重复）
```bash
# 直接用系统/隔离 Python 运行即可，无需启动服务
python regression_where_clause.py
# 期望输出结尾出现 REGRESSION_TEST_PASS
```
脚本路径：`D:/workbuddy/2026-08-06-15-31-48/regression_where_clause.py`
（它用 `ast` 从 `api_app.vision.py` **抽取真实 `_where_clause`** 执行，确保测的是线上代码。）

### 5.2 全链路冒烟（需 8014 在线）
```bash
# 启动打过补丁的 8014（挂载在 /api，复刻 serve_vision.py 行为）
cd "D:/My File/大四/高数教材答案"
python "D:/workbuddy/2026-08-06-15-31-48/run_workbench_8014.py"

# 冒烟
curl -s -G "http://127.0.0.1:8014/api/problems" --data-urlencode "size=2"
curl -s -G "http://127.0.0.1:8014/api/problems" --data-urlencode "q=x') OR ('1'='1') -- "
curl -s -G "http://127.0.0.1:8014/api/problems" --data-urlencode "section_no=x') OR ('1'='1') -- "
```
期望：全部返回合法 JSON，注入用例 `total` 为字面搜索结果且不报错。

---

## 6. 预期结果与通过标准

1. ✅ `regression_where_clause.py` 输出 `REGRESSION_TEST_PASS`；
2. ✅ 修复前对照实现：恶意载荷使 COUNT 返回全表（证明原漏洞确实存在）；
3. ✅ 修复后：同一恶意载荷被参数化绑定，COUNT 返回字面搜索结果（0 或正常数），**不绕过、不报错**；
4. ✅ 正常过滤（按节 / 关键字 / 难度）结果与原实现一致；
5. ✅ 三份文件（`vision`/`original`/`candidate-ui`）均已完成相同修复；
6. ✅ 三文件 `py_compile` 通过；
7. ✅ 重启 8014 后，`GET /api/problems` 对四个注入分支均返回合法 JSON、无 500。

---

## 7. 已知限制 / 后续

- 本修复只覆盖 `_where_clause` 链路。后续应对其余手写 SQL 做一次全局审计（搜索
  `f"..."` / `%` 拼接进 `execute` 的语句），确保无遗漏注入面。
- `run_workbench_8014.py` 仅复刻了 `serve_vision.py` 的 `/api` 挂载与 CORS；
  若需教师工作台静态页，仍用原 `serve_vision.py`（其 `import api_app` 在扁平文件布局下
  需先把 `api_app.vision.py` 暴露为 `api_app` 模块后再启动）。
