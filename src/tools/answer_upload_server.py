"""answer_upload_server.py
==========================
极简上传服务: 上传答案 PDF -> 调用 answer_pdf_mineru_pipeline 解析 -> 返回结构化 JSON 草稿。
仅做原型验证, 不写入任何正式数据库。

启动:
    pip install fastapi uvicorn
    python answer_upload_server.py
接口:
    POST /upload-answer-pdf       表单字段 file=PDF, section="1-1", pages=60
    GET  /drafts                  列出已生成的草稿
    GET  /health
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

import answer_pdf_mineru_pipeline as pipe

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "uploads"
DRAFTS = BASE / "drafts"
UPLOADS.mkdir(exist_ok=True)
DRAFTS.mkdir(exist_ok=True)

app = FastAPI(title="答案 PDF 解析原型 (MinerU)")


@app.get("/health")
def health():
    return {"ok": True, "parser_hint": "mineru"}


@app.post("/upload-answer-pdf")
def upload_answer_pdf(
    file: UploadFile = File(...),
    section: str = Form("1-1"),
    pages: int = Form(60),
    parser: str = Form("auto"),
):
    # 1) 保存上传文件(不进正式库, 仅临时落盘)
    pdf_path = UPLOADS / file.filename
    with pdf_path.open("wb") as f:
        f.write(file.file.read())

    # 2) 解析 -> 定位 -> 抽题 -> 草稿 JSON(由 pipeline 内部写 drafts/)
    workdir = Path(tempfile.mkdtemp(prefix="mineru_up_"))
    result = pipe.run_pipeline(
        pdf_path, target_section=section, parser_name=parser,
        pages=pages, workdir=workdir,
    )
    out_path = DRAFTS / f"{pdf_path.stem}_sec-{result['meta']['target_section']}.json"
    out_path.write_text(
        __import__("json").dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return JSONResponse({
        "draft_path": str(out_path),
        "meta": result["meta"],
        "problems": result["problems"],
    })


@app.get("/drafts")
def list_drafts():
    return {"drafts": [p.name for p in sorted(DRAFTS.glob("*.json"))]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8015)
