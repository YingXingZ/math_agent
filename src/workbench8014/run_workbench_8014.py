# -*- coding: utf-8 -*-
"""Start the 8014 workbench and mount its flat-file API under ``/api``.

The implementation stays importlib-based because ``api_app.py`` is a
flat file, not an importable package.  Paths are repository-relative by
default and can be overridden in production with environment variables.
"""
import importlib.util
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import uvicorn


WORKBENCH_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = WORKBENCH_DIR.parents[1]
WORKBENCH = Path(os.environ.get("WORKBENCH_API_MODULE", WORKBENCH_DIR / "api_app.py"))
os.environ.setdefault("WORKBENCH_DB", str(REPOSITORY_ROOT / "api.workbench.db"))
os.environ.setdefault("IMAGE_ROOT", str(REPOSITORY_ROOT / "extract_img"))

if not WORKBENCH.is_file():
    raise RuntimeError(f"Workbench API module does not exist: {WORKBENCH}")

spec = importlib.util.spec_from_file_location("api_app_vision", WORKBENCH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Could not load workbench API module: {WORKBENCH}")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

app = FastAPI(title="高数作业助手 — 统一服务 (api mount)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/api", mod.app)


@app.get("/")
def teacher_workbench():
    """Serve the preserved teacher workbench rather than a bare API 404."""
    page = REPOSITORY_ROOT / "teacher_vision.html"
    if not page.is_file():
        raise RuntimeError(f"Teacher workbench page does not exist: {page}")
    return FileResponse(page)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8014, log_level="info")
