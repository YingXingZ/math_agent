# -*- coding: utf-8 -*-
"""Bring the 8014 teacher-workbench back up with the *patched* api_app.vision.

Replicates serve_vision.py's behaviour (mounts the workbench app under /api) by
loading api_app.vision.py directly via importlib, so we don't depend on the
`import api_app` resolution that the flat file layout no longer provides.
"""
import importlib.util
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

WORKBENCH = r"D:/My File/大四/高数教材答案/api_app.vision.py"
# 默认指向真实生产库 api.workbench.db（463 题）；可用环境变量 WORKBENCH_DB 覆盖。
REAL_DB = r"D:/My File/大四/高数教材答案/api.workbench.db"
os.environ.setdefault("WORKBENCH_DB", REAL_DB)
# 教材裁切图（problems.crop_image_path 指向 book/...）真实存放于本工作区
# extract_img；8014 默认的 IMAGE_ROOT='extract_img' 是相对 cwd 的路径，在生产目录
# 下不存在，会导致 /images/ 404、VLM 识别原题图失败。这里显式指向真实位置。
REAL_IMG_ROOT = r"D:/workbuddy/2026-08-06-15-31-48/extract_img"
os.environ.setdefault("IMAGE_ROOT", REAL_IMG_ROOT)

spec = importlib.util.spec_from_file_location("api_app_vision", WORKBENCH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
vision_app = mod.app

app = FastAPI(title="高数作业助手 · 统一服务 (api mount)")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.mount("/api", vision_app)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8014, log_level="info")
