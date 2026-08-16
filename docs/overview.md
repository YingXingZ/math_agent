# 高数教师工作台 — 工作概览

## 最近完成

### 4. 学生端改造：登录 + 我的作业 + 成绩闭环（本次）

- **需求**：每个学生用学号+姓名登录 → 交作业 → 老师批改后看自己得分 → 记录每次作业。
- **后端**：新增 `GET /students/me/submissions`（按学号返回本人全部提交 + 概览），复用 `GET /submissions/{sid}/grade-detail` 作为成绩详情数据源。
- **前端重写（outputs/高数学生提交页.html）**：
  - 学号+姓名弱登录（localStorage 持久化，顶栏可退出）
  - 首页：成绩概览卡 + 待完成作业 + 我的作业记录（每次得分）
  - 作答页：自动带入学号姓名
  - 成绩详情：逐题得分/标准答案/自动反馈/置信度/修改日志（只读）

### 1. 复核功能体验打通（前次）

- 用户反馈：人工复核页「复核」按钮看不到试卷内容。原因是旧版复核按钮只弹学生提交列表 + 标注一次复核，没有打通到完整试卷复核视图。
- **前端改造**：
  - `openStudentReview(studentId)` 重写：默认取最近一份已批改提交，直接进入 `openReviewDetail(sid)` 完整试卷复核。
  - 新增 `openStudentSubmissions(studentId)`：列出该生全部提交，每条已批改的有「📋 试卷复核」直达完整试卷视图。
  - 抽样卡片按钮重做：「📋 复核试卷」（主）+ 「📚 历史」+ 「📝 去批改」（无已批改时）。
  - 抽样卡片底部展示最近一份试卷摘要 + "📋 打开完整试卷"链接。
  - 标题旁加 ⓘ 按钮 → `showReviewHelp()` 弹完整功能说明。
- **后端增强**：`GET /review/sample` 原本只返回 `pending_submissions`，现新增 `graded_submissions` + `all_submissions`。

### 2. 试卷复核：自动批改后人工改分（前次）

- **数据持久化**：`submissions` 表新增 `grade_detail` 与 `review_log`（JSON 字段），自动批改时持久化每题完整明细。
- **新增后端接口**：
  - `GET  /submissions/{sid}/grade-detail` — 返回试卷完整明细（题目图片、学生作答、标准答案、自动反馈、置信度、当前分数、max_score、修改日志）
  - `PATCH /submissions/{sid}/problems/{pid}` — 教师人工改分，自动重算总分、追加 review_log
  - `GET  /submissions/{sid}/review-log` — 查看分数修改日志
- **前端**：批改中心"已批改作业"每张卡片新增「🔍 复核试卷」按钮，弹出宽屏复核窗，支持逐题改分、备注、实时刷新总分、查看修改日志。

- **复核弹窗修复**：人工复核页每名学生卡片新增「复核」按钮。
- **章节多选**：checkbox chip 多选 + 后端 `section_nos` 跨章节抽题。
- **选题结果二次编辑**：拖拽排序、增删、按题型/难度/知识点筛选。

### 3. 自动批改修复 + 手写批注（前次）

- 自动批改异常改为单题兜底，前端用 Toast 替代 alert 并增加 loading 态。
- 手动批改支持 Canvas 手写批注（笔/直线/圆/橡皮），可保存、回显、清除。

## 关键文件变更

- `outputs/高数教师工作台.html`：新增复核弹窗、章节多选、选题编辑器、手写批注、Toast。
- `api_app.py`：新增 `/submissions/{sid}/grade-detail`、`PATCH /submissions/{sid}/problems/{pid}`、`/submissions/{sid}/review-log`；新增 `/review/student/{id}`、`/review/record/{id}`；`HomeworkReq` 支持 `section_nos`；`/homeworks/smart-select` 支持多章节；`submissions` 表升级 `grade_detail` / `review_log` / `annotations` 字段。
- `difficulty_tier.py`：`select_homework_problems` 支持 `section_nos` 列表跨章节选题。
- `grading_engine.py`：非符号型比对器（区间/极限/导数）。

## 验证结果

| 测试项 | 结果 |
|---|---|
| 自动批改 + grade_detail 持久化 | ✅ 200 |
| grade-detail 完整明细 | ✅ 200 |
| 单题改分覆盖 + 重算总分 | ✅ 200 |
| review-log 记录 | ✅ 200，日志数 +1 |
| 多章节智能选题 | ✅ 200 |
| Python 语法 | ✅ 通过 |
| JS 语法 | ✅ 通过 |
| 服务启动 | ✅ `serve.py` 已在 8011 |

## 当前服务

- 教师工作台: http://localhost:8011
- 学生提交页: http://localhost:8011/student
- API: http://localhost:8011/api/*

## 待办

- 48 道 manual 题（积分/微分方程/应用题 OCR 文字垃圾）需专用比对器或重录答案
- 5 道 OCR 编号损坏题 + 1.3#6 父题仍无答案，需手动配置
- v4 题库（199题）尚未入库