# 本地启动与环境配置

## 开发 API（无需 GPU）

从仓库根目录执行：

```bash
cd src/agent8000
python scripts/bootstrap_local.py --run
```

脚本会创建 `src/agent8000/.venv`、安装 `requirements.txt`、仅在不存在时从 `.env.example` 复制 `.env`，最后启动 `http://127.0.0.1:8001`。第二次启动仍可执行同一命令；若依赖已准备好，使用 `--no-install` 可跳过 pip。

```bash
python scripts/bootstrap_local.py --no-install --run
```

本地开发默认使用 inline 队列，不会调用 GPU。浏览器接口与 OpenAPI 文档可用，但完整自动批改必须启动下列私有依赖。

## 完整批改栈

| 组件 | 职责 | 生产约束 |
| --- | --- | --- |
| 8001 API / Worker / Scheduler | 用户请求、异步批改、定时任务 | 使用 Compose 或 systemd 常驻运行 |
| Redis | RQ 队列 | 不对公网暴露 |
| 18080 VLM | 手写答案识别和独立求解 | `MATH_VLM_MODE=production` 与 `VLM_INTERNAL_API_KEY` 必填 |
| 8014 Evidence | 权威题目/答案与技能 | `WORKBENCH_MODE=production` 与内部密钥必填 |

生产环境请复制 `deploy/.env.production.example` 为不提交的 `.env.production`，设置随机的 `VLM_INTERNAL_API_KEY` 与 8014 密钥。8000、18080、8014 均应只接受来自反向代理或私网的访问。

## 启动前检查

```bash
python -m pytest tests/test_service_security_contract.py -q
python -m pytest tests/test_grading_completion.py tests/test_grading_quality_eval.py -q
```

`/healthz` 应返回 API 与 RQ 状态；VLM `/health` 只适合私网探针使用，模型在第一次推理时按需加载。
