# 高数作业助手 —— 本机自测指南

目标：在本机把「8014 工作台 + 8000 智能体 + 18080 远程 VLM」三层跑通，
走完「同步题库 → 组卷 → 学生提交 → 批改 → 教师复核 → 看报表」全链路。

## 0. 前置条件
- 三个服务：8014（工作台）、8000（智能体）、18080（VLM，远程已部署）。
- 远程 VLM 须网络可达：先 `curl -m 5 http://222.211.217.7:18080/health` 应返回 `{"status":"ok"}`。
- 若 VLM 不通，批改/同步会失败，但组卷、提交归档、报表仍可测。

## 1. 启动服务

### 方式 A（推荐，一键）
双击 `高数作业助手/deploy/start_windows.bat` —— 自动拉起 8014 与 8000。

### 方式 B（手动，便于看日志）
```bat
REM 8014 工作台（必须用这个启动器，且固化环境变量）
set WORKBENCH_DB=D:\My File\大四\高数教材答案\api.workbench.db
set IMAGE_ROOT=D:\workbuddy\2026-08-06-15-31-48\extract_img
C:\Users\YXZ\.workbuddy\binaries\python\versions\3.13.12\python.exe D:\workbuddy\2026-08-06-15-31-48\run_workbench_8014.py

REM 8000 智能体（另开一个终端）
cd /d D:\My File\大四\高数教材答案\高数作业助手
C:\Users\YXZ\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 2. 健康检查
```bash
curl http://127.0.0.1:8000/api/agent/capabilities   # 智能体，应含 assignment/reports/ai_stem 等
curl http://127.0.0.1:8014/api/health               # 工作台，应返回 ok
```

## 3. 教师端界面（浏览器）
- 打开 `http://127.0.0.1:8000/` → 教师门户（组卷、AI 题干候选复核、教学报表都在这里）。
- 打开 `http://127.0.0.1:8000/answer-import-review` → 答案导入复核（MinerU）。

## 4. 端到端自测脚本（推荐顺序）

### 4.1 同步一个章节进题库（从 8014 拉题）
```bash
curl -s -X POST http://127.0.0.1:8000/api/agent/sync-section \
  -H "Content-Type: application/json" -d '{"section_no":"1.1"}'
```
返回应含同步进来的题目列表。可在教师门户「AI 题干候选复核」里看到待审项。

### 4.2 生成一份作业（组卷）
```bash
curl -s -X POST http://127.0.0.1:8000/api/agent/assignments \
  -H "Content-Type: application/json" \
  -d '{"title":"自测作业1.1","chapter":"1.1","class_name":"自测班","total_score":100,"count":5,"semester":"2026秋"}'
```
记下返回的 `assignment_id`。

### 4.3 导出作业 PDF（给学生打印）
```bash
curl -s -o hw.pdf http://127.0.0.1:8000/api/assignments/<ID>/pdf && echo "作业PDF大小:" && wc -c hw.pdf
```

### 4.4 学生提交（两种方式）
- **网页**：浏览器打开 `http://127.0.0.1:8000/submit?assignment_id=<ID>`，填姓名/学号、传 PDF 或图片，提交。
- **命令行**：
```bash
curl -s -X POST http://127.0.0.1:8000/api/assignments/<ID>/submissions \
  -F "student_no=2026001" -F "student_name=张三" -F "file=@学生作业.pdf"
```
提交后系统自动启动 VLM 批改（后台）。可立即看到返回含 `grading_job_id`。

### 4.5 查看批改进度 / 结果
```bash
curl -s http://127.0.0.1:8000/api/submissions/<SUB_ID>/grading   # 单份批改详情
curl -s http://127.0.0.1:8000/api/reviews                        # 教师复核队列（含待人工确认项）
```

### 4.6 教师复核并发布成绩
```bash
curl -s -X POST http://127.0.0.1:8000/api/submissions/<SUB_ID>/review \
  -H "Content-Type: application/json" \
  -d '{"confirmed_score":85,"teacher_feedback":"步骤完整，符号规范","publish":true}'
```

### 4.7 看教学报表
```bash
curl -s http://127.0.0.1:8000/api/reports/weak-points      # 薄弱知识点
curl -s http://127.0.0.1:8000/api/reports/review-quota     # 复核配额
curl -s "http://127.0.0.1:8000/api/reports/semester-summary?class_name=自测班&semester=2026秋"  # 学期汇总
curl -s http://127.0.0.1:8000/api/reports/summary          # 总览
```

## 5. 学生端加固已生效（自测时可验证）
- 学号含 `/` 或 `..` → `422 学号格式不合法`（防路径穿越）。
- 传 `.txt` → `415 仅支持 PDF、图片或 Word 文档`。
- 同一作业+学号重复提交（批改进行中）→ `409 已有正在批改的提交`。
- 文件 > 30MB → `413 文件过大`。
- 空文件 → `422 空文件`。

## 6. 常见问题排查
| 现象 | 原因 / 处理 |
|------|-------------|
| 8014 `/images/` 全部 404 | `IMAGE_ROOT` 未设置或路径错 → 用 `run_workbench_8014.py` 启动并固化 |
| 8000 同步报 422/空库 | `WORKBENCH_DB` 未指向真实 `api.workbench.db` |
| 批改卡住 / qwen_error | 远程 VLM 不可达或 busy → 查 `curl ...:18080/health` |
| 提交端点无响应 | 检查 8000 进程是否在跑；Windows 杀进程用 `os.kill(pid, SIGTERM)` |
| 大 PDF（>12 页）被拒识 | 已修复：现渲染全部页交 VLM 跨页定位，仅 >80 页转人工 |

## 7. 自动化回归（改代码后用）
```bash
C:\Users\YXZ\.workbuddy\binaries\python\envs\default\Scripts\python.exe D:\workbuddy\2026-08-06-15-31-48\validate_agent_reports.py
C:\Users\YXZ\.workbuddy\binaries\python\envs\default\Scripts\python.exe D:\workbuddy\2026-08-06-15-31-48\regress_student_submit.py
```
两个脚本均基于临时库副本运行，不影响生产数据，全绿即代表接口契约未被破坏。
