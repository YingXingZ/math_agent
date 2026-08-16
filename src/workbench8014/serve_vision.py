"""
高数作业助手 · 统一服务

把「教师工作台 / 学生提交页 / API」合并成一个同源服务：
  - /           教师工作台
  - /student    学生提交页
  - /api/*      原 FastAPI 接口（路径与后续 Next.js 工程的 /api 一致，便于迁移）
  - /manifest.webmanifest /icon.png   PWA 资源（添加到主屏幕用）

用法：
  python serve.py --host 0.0.0.0 --port 8011
手机与电脑在同一 WiFi 下，浏览器打开 http://<本机内网IP>:8011 即可。
"""
import os
import argparse
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn


def load_vision_env():
    """Load local-only vision settings without putting secrets in source code."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vision_api.env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in {"OPENAI_API_KEY", "OPENAI_VISION_MODEL", "PIX2TEXT_PYTHON"} and value.strip():
                os.environ.setdefault(key.strip(), value.strip())


load_vision_env()
import api_app  # 原 API 应用（含全部 /problems、/homeworks、/images 等路由）

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "outputs")

app = FastAPI(title="高数作业助手 · 统一服务")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 原 API 挂到 /api 下；后续迁移到 Next.js 时，前端继续走 /api，后端可独立部署
app.mount("/api", api_app.app)


@app.get("/")
def teacher_page():
    return FileResponse(os.path.join(OUT, "高数教师工作台.html"))


@app.get("/teacher")
def teacher_redirect():
    return RedirectResponse("/")


@app.get("/student")
@app.get("/student.html")
def student_page():
    return FileResponse(os.path.join(OUT, "高数学生提交页.html"))


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(
        os.path.join(OUT, "manifest.webmanifest"),
        media_type="application/manifest+json",
    )


@app.get("/icon.png")
def icon_png():
    return FileResponse(os.path.join(OUT, "icon.png"), media_type="image/png")


@app.get("/icon.svg")
def icon_svg():
    return FileResponse(os.path.join(OUT, "icon.svg"), media_type="image/svg+xml")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8011)
    a = p.parse_args()
    print(f"高数作业助手已启动：http://{a.host}:{a.port}  (本机访问用 http://localhost:{a.port})")
    uvicorn.run(app, host=a.host, port=a.port)
