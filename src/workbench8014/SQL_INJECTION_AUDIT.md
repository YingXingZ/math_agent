# 全局 SQL 注入审计报告

> 审计日期：2026-08-15
> 审计范围：工作台 8014（`api_app.*.py`）+ Agent 8000（`高数作业助手/app/`）+ 工作区副本

## 一、审计方法

对以下三个危险模式进行全量搜索：

| 模式 | 说明 |
|---|---|
| `execute(f"...")` | f-string 插值进 SQL 文本 |
| `.format(...)` 拼接 SQL | 字符串格式化注入 |
| `"..." + variable` 拼接 SQL | 字符串拼接注入（特指 WHERE/SELECT/INSERT/UPDATE/DELETE 上下文） |
| `execute("...%s..." %)` | `%` 格式化注入 |

搜索工具：ripgrep（Grep tool），覆盖所有 `.py` 文件。

## 二、审计结果总览

### 已发现并修复的漏洞

| # | 文件 | 行号 | 漏洞 | 修复状态 |
|---|---|---|---|---|
| 1 | `api_app.vision.py` | L586-610 | `_where_clause()` f-string 插值用户输入；COUNT 查询字符串拼接 | ✅ 已修复（上一轮） |
| 2 | `api_app.original.py` | L448-472 | 同上 | ✅ 已修复（上一轮） |
| 3 | `api_app.candidate-ui.py` | L461-485 | 同上 | ✅ 已修复（上一轮） |
| 4 | **`workspace/api_app.py`** | L486-497 | 同上（工作区副本，独立于 8014 运行实例） | ✅ **本轮修复** |

### 确认安全的模式（无需修改）

以下 f-string SQL 模式经审查确认安全——f-string 仅插入静态 SQL 片段或 `?` 占位符，用户输入全部通过参数绑定传递：

| 文件 | 行号 | 模式 | 安全原因 |
|---|---|---|---|
| `api_app.vision.py` | L163,176,185,199 | `f"ALTER TABLE ... ADD COLUMN {col} {ddl}"` | `col`/`ddl` 为代码内硬编码迁移常量 |
| `api_app.vision.py` | L622,638,674 | `f"""SELECT ... {where}"""` | `where` 为空字符串或 `" AND s.section_no=?"` 静态片段 |
| `api_app.vision.py` | L1351 | `f"SELECT ... WHERE id IN ({placeholders})"` | `placeholders` = `",".join("?" * len(pids))` |
| `api_app.vision.py` | L1829 | `f"UPDATE problems SET {','.join(updates)} WHERE id=?"` | `updates` 仅含 `"column=?"` 硬编码字符串 |
| `api_app.vision.py` | L2207 | `f"UPDATE ... WHERE id IN ({placeholders})"` | 同上，`placeholders` 全为 `?` |
| `api_app.original.py` | L103,116,474,490,526,1503 | 同上模式 | 同上原因 |
| `api_app.candidate-ui.py` | L111,124,133,487,503,539,1516 | 同上模式 | 同上原因 |
| Agent `main.py` | L324-328,609-611 | `sql += " AND chapter=?"` + `sql + " ORDER BY ..."` | 静态 SQL 片段拼接，用户值走 `?` |
| Agent `mineru_review.py` | L388-391 | `f"SELECT ... {where}"` | `where` 由 `"session_id=?"` 等静态片段拼接 |
| Agent `assemble_assignment.py` | L77-84 | `f"SELECT ... WHERE chapter IN ({placeholders})"` | `placeholders` 全为 `?` |
| Agent `grading_pipeline.py` | L96-219 | 全部 `?` 参数化 | 无 f-string |
| Agent `orchestrator.py` | L76-78 | `"SELECT ... WHERE id=?"` | 参数化 |

### 未发现 SQL 执行的文件

- `answer_upload_server.py` — 无 `execute()` 调用
- `answer_pdf_mineru_pipeline.py` — 无 `execute()` 调用
- `knowledge_bridge.py` — HTTP 桥接，无 DB 操作

## 三、本轮修复详情（workspace/api_app.py）

### 修复前（漏洞代码）

```python
def _where_clause(section_no, ptype, dmin, dmax, kp, q):
    c = ""
    if section_no: c += f" AND s.section_no='{section_no}'"      # ← 用户输入直接拼入
    if ptype: c += f" AND p.ptype='{ptype}'"                      # ← 同上
    if dmin is not None: c += f" AND p.difficulty>={dmin}"        # ← 同上
    if dmax is not None: c += f" AND p.difficulty<={dmax}"        # ← 同上
    if kp:
        parts = [f"p.knowledge_pts LIKE '%{x.strip()}%'" for x in kp.split(",")]  # ← 同上
        c += " AND (" + " OR ".join(parts) + ")"
    if q:
        c += f" AND (s.section_no LIKE '%{q}%' ...)"             # ← 同上
    return c  # ← 返回裸字符串，无参数

# 调用点：
where = _where_clause(...)  # 裸字符串
total = conn.execute("... WHERE 1=1" + where).fetchone()[0]  # ← 无参数绑定
```

### 修复后（参数化查询）

```python
def _where_clause(section_no, ptype, dmin, dmax, kp, q):
    """Return a parameterized (SQL fragment, args) for the problems list filter."""
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
        args += [f"%{c.strip()}%" for c in kp.split(",")]
    if q:
        clause += " AND (s.section_no LIKE ? OR p.problem_no LIKE ? OR p.exercise_set LIKE ?)"
        args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    return clause, args  # ← 返回 (clause, args) 元组

# 调用点：
where, args = _where_clause(...)
sql += where
sql += " ORDER BY ... LIMIT ? OFFSET ?"
args += [size, (page - 1) * size]
# COUNT 复用同一 where + args[:-2]（去掉末尾 size/offset）
total = conn.execute("... WHERE 1=1" + where, args[:-2]).fetchone()[0]
rows = conn.execute(sql, args).fetchall()
```

### 验证

- `py_compile` 通过
- 四份文件（`api_app.vision.py` / `api_app.original.py` / `api_app.candidate-ui.py` / `workspace/api_app.py`）修复模式完全一致
- 回归测试脚本 `regression_where_clause.py` 已覆盖（从源文件直接加载真实函数验证）

## 四、结论

| 项目 | 注入面数量 | 状态 |
|---|---|---|
| 工作台 8014（3 份 `api_app.*.py`） | 3 处（同一 `_where_clause`） | ✅ 全部修复 |
| 工作区副本（`api_app.py`） | 1 处（同一漏洞） | ✅ 本轮修复 |
| Agent 8000（`app/` 全模块） | 0 处 | ✅ 全部参数化 |
| 辅助脚本 | 0 处 | ✅ 无 SQL 执行 |

**全局审计结论：项目中所有用户输入可达的 SQL 执行路径现已全部参数化，无残留注入面。**
