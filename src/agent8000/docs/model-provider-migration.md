# 模型与服务器迁移预留

当前平台默认使用 `LLM_PROVIDER=local_qwen`，即继续调用现有 A100 上的
`QWEN_GRADING_URL`。业务代码只通过 `app/llm_provider.py` 调用模型；题库、
组卷、SymPy 判等、成绩、任务队列均不依赖 GPU。

## 迁到普通 CPU 服务器时

1. 迁移数据库和上传文件目录，并把 `DATABASE_PATH`、`UPLOAD_DIR` 改为新服务器路径。
2. 部署 API、Redis/RQ Worker；OCR/MinerU 可以继续做后台任务或迁到独立 Worker。
3. 在新服务器私有 `.env` 中设置：

```env
LLM_PROVIDER=qwen_api
QWEN_API_KEY=你的密钥
QWEN_API_MODEL=qwen-plus
QWEN_API_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

4. 重启 API 和 Worker。每条批改结果会记录 `model_runtime`，便于区分本地 A100 与 API 结果。

## 两种 API 接入方式

- **直连兼容 API**：未设置 `QWEN_API_GRADING_URL` 时，系统调用
  `/chat/completions`，要求模型只返回既有的 `results` JSON。
- **推荐生产方式**：设置 `QWEN_API_GRADING_URL` 为一个轻量网关。网关接收当前
  `images_base64 + problems` 格式，并调用任意云模型后返回同样的 `results`。
  这样本地 A100 与云 API 的输出协议完全一致，切换最稳。

## 当前不需要做的事

不要现在填写 API Key，也不要关闭 A100。本次改造默认仍走本地服务；只有显式把
`LLM_PROVIDER` 改成 `qwen_api` 才会使用云 API。来源图重建/OCR 仍可先保留在
现有 GPU 服务，迁移时再作为独立 Worker 处理。
