# 高数 Agent 工作台 — 重构交接仓库

本仓库是「高数作业助手」三层系统的**自包含交接包**，供 Codex 重构/重写使用。

## 目录
```
CODEX_HANDOVER/
├── HANDOVER.md          # 主交接文档：状态 / 已完成 / 地雷 / 移交 SOP / 验收
├── ENVIRONMENT.md       # 解释器 / 依赖 / 端口 / 环境变量 / 外部服务 / 数据库
├── BACKLOG.md           # 已完成清单 + 待办分类 + 重构范围 + 验收基线
├── src/
│   ├── agent8000/       # 8000 智能体（FastAPI 教师端+学生端，核心）
│   ├── workbench8014/   # 8014 工作台（证据库 api_app.vision 等）
│   ├── vlm18080/        # 18080 VLM 服务（server_vlm_service + deploy/pull）
│   └── tools/           # 工具/恢复脚本（96 个 .py，含探针；维护清单见 HANDOVER.md §4）
├── db_schema/           # 两个 SQLite 的结构 + 表行数（无需 24MB 二进制）
└── docs/                # 设计/诊断/验证文档
```

## 快速开始
1. 读 `HANDOVER.md`（先看 §0 结论、§1 状态、§4 地雷）。
2. 读 `ENVIRONMENT.md` 配环境（托管 venv / 依赖 / 端口 / 外部服务）。
3. 如需本地跑：按 `ENVIRONMENT.md` 提供 `api.workbench.db` 与源 PDF（未随包），启动三服务。
4. 重构参考：`src/`（现有实现）、`db_schema/`（结构）、`docs/`（设计意图）、`src/tools/`（既有恢复脚本）。

## 未随包的数据（见 ENVIRONMENT.md §7/§8）
- `api.workbench.db`（~24MB 真实库）、源教材/答案 PDF（各 ~25MB）、`extract_img/` 裁切图。
- 结构已转储在 `db_schema/`，足够理解 schema；运行所需二进制由你侧单独提供。
