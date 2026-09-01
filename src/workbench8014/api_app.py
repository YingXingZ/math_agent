# -*- coding: utf-8 -*-
"""
智能高数作业助手 —— 可运行 API 服务（阶段二 题库检索 + 教师工作台后端）
=========================================================================
FastAPI + SQLite（开发态，等价的 Postgres schema 见 schema.sql）。

本文件在 api.yaml 核心端点（题目入库 / 题库检索 / 知识点反查）基础上，
补齐教师工作台所需的：班级、作业、提交、批改，以及裁切图静态服务、CORS。

运行：
  uvicorn api_app:app --port 8011 --host 127.0.0.1
入库（把 extract_book.py 的输出灌进 DB）：
  POST /ingest/book  {"path": "extract_img/book_problems.json"}
裁切图服务根目录（extract_book.py 的 --out）：
  环境变量 IMAGE_ROOT，默认 "extract_img"
"""
import json
import hashlib
import io
import os
import re
import sqlite3
import uuid
import base64
import urllib.error
import urllib.request
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from PIL import Image

app = FastAPI(title="智能高数作业助手 API", version="0.3.0")

DB = os.environ.get("WORKBENCH_DB", os.path.join(os.path.dirname(__file__), "api.db"))
JOB_STORE = {}  # job_id -> {status, progress, result, error}
IMAGE_ROOT = os.environ.get("IMAGE_ROOT", "extract_img")

# 8014 is an internal evidence service. Development remains local-friendly;
# production requires a service key and explicit browser origins.
WORKBENCH_MODE = os.environ.get("WORKBENCH_MODE", "development").strip().lower()
INTERNAL_API_KEY = os.environ.get("WORKBENCH_INTERNAL_API_KEY", "")
ALLOWED_ORIGINS = [x.strip() for x in os.environ.get("WORKBENCH_CORS_ORIGINS", "").split(",") if x.strip()]
if WORKBENCH_MODE == "production" and not INTERNAL_API_KEY:
    raise RuntimeError("WORKBENCH_INTERNAL_API_KEY is required in production")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS if WORKBENCH_MODE == "production" else ["http://127.0.0.1:8014", "http://localhost:8014"], allow_credentials=False, allow_methods=["GET","POST","PUT","DELETE"], allow_headers=["Content-Type","X-Internal-API-Key","X-Request-ID"])

@app.middleware("http")
async def internal_service_auth(request: Request, call_next):
    if WORKBENCH_MODE == "production" and request.url.path.startswith("/api/") and request.url.path != "/api/health":
        import hmac
        supplied = request.headers.get("X-Internal-API-Key", "")
        if not hmac.compare_digest(supplied, INTERNAL_API_KEY):
            return JSONResponse(status_code=401, content={"detail":"internal service authentication required"})
    return await call_next(request)


# --------------------------------------------------------------------------- #
# 建表（SQLite 等价 schema.sql 子集）
# --------------------------------------------------------------------------- #
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS textbooks(
        id TEXT PRIMARY KEY, name TEXT, volume TEXT, edition TEXT,
        pdf_path TEXT, page_offset INT DEFAULT 0);
    CREATE TABLE IF NOT EXISTS sections(
        id TEXT PRIMARY KEY, textbook_id TEXT, section_no TEXT,
        title TEXT, start_page INT, exercise_page INT);
    CREATE TABLE IF NOT EXISTS knowledge_points(
        code TEXT PRIMARY KEY, name TEXT, parent_code TEXT, section_no TEXT);
    CREATE TABLE IF NOT EXISTS problems(
        id TEXT PRIMARY KEY, section_id TEXT, exercise_set TEXT,
        problem_no TEXT, sub_no TEXT, ptype TEXT DEFAULT 'calc',
        crop_image_path TEXT, content_text TEXT,
        difficulty INT DEFAULT 3, knowledge_pts TEXT DEFAULT '',
        extract_status TEXT DEFAULT 'raw', source_page INT,
        tier TEXT, std_answer TEXT, full_solution TEXT, grading_steps TEXT, answer_weight REAL DEFAULT 0.6,
        answer_status TEXT NOT NULL DEFAULT 'unverified', answer_invalid_reason TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS classes(
        id TEXT PRIMARY KEY, teacher_id TEXT, name TEXT, term TEXT);
    CREATE TABLE IF NOT EXISTS students(
        id TEXT PRIMARY KEY, class_id TEXT, student_no TEXT, name TEXT);
    CREATE TABLE IF NOT EXISTS homeworks(
        id TEXT PRIMARY KEY, textbook_id TEXT, title TEXT, class_id TEXT,
        section_no TEXT, deadline TEXT, status TEXT DEFAULT 'published',
        problem_ids TEXT DEFAULT '[]', points_map TEXT DEFAULT '{}', created_at TEXT);
    CREATE TABLE IF NOT EXISTS submissions(
        id TEXT PRIMARY KEY, homework_id TEXT, student_no TEXT, student_name TEXT,
        submitted_at TEXT, status TEXT DEFAULT 'pending', score REAL,
        answers TEXT DEFAULT '[]', created_at TEXT);
    CREATE TABLE IF NOT EXISTS meta(
        key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS answer_import_candidates(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        problem_id TEXT NOT NULL, volume TEXT NOT NULL, section_no TEXT NOT NULL,
        problem_no TEXT NOT NULL, sub_no TEXT, source_pdf TEXT NOT NULL,
        source_page INTEGER NOT NULL, ocr_text TEXT NOT NULL,
        ocr_confidence REAL NOT NULL, match_status TEXT NOT NULL DEFAULT 'pending',
        match_reason TEXT DEFAULT '', content_hash TEXT NOT NULL UNIQUE,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, reviewed_at TEXT, review_note TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS vision_recognition_tasks(
        id TEXT PRIMARY KEY, candidate_id INTEGER UNIQUE, problem_id TEXT NOT NULL,
        task_type TEXT NOT NULL DEFAULT 'answer_pdf', status TEXT NOT NULL DEFAULT 'pending',
        provider TEXT NOT NULL DEFAULT 'pix2text', input_image_path TEXT NOT NULL,
        result_json TEXT DEFAULT '{}', error_message TEXT DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS candidate_source_images(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER NOT NULL,
        sort_order INTEGER NOT NULL,
        image_path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(candidate_id, sort_order));
    CREATE INDEX IF NOT EXISTS idx_candidate_source_images
        ON candidate_source_images(candidate_id, sort_order);
    CREATE TABLE IF NOT EXISTS submission_source_images(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        submission_id TEXT NOT NULL,
        sort_order INTEGER NOT NULL,
        image_path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(submission_id, sort_order));
    CREATE INDEX IF NOT EXISTS idx_submission_source_images
        ON submission_source_images(submission_id, sort_order);
    CREATE TABLE IF NOT EXISTS answer_documents(
        id TEXT PRIMARY KEY, filename TEXT NOT NULL, stored_path TEXT NOT NULL,
        file_size INTEGER DEFAULT 0, page_count INTEGER DEFAULT 0,
        volume TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'uploading',
        index_progress INTEGER DEFAULT 0, index_message TEXT DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS answer_page_anchors(
        id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL,
        page_no INTEGER NOT NULL, section_no TEXT DEFAULT '', problem_no TEXT DEFAULT '',
        sub_no TEXT DEFAULT '', bbox_json TEXT DEFAULT '[]', crop_path TEXT DEFAULT '',
        ocr_text TEXT DEFAULT '', ocr_confidence REAL DEFAULT 0,
        problem_id TEXT DEFAULT '', candidate_id INTEGER,
        teacher_subquestion_count INTEGER DEFAULT 0,
        extraction_status TEXT DEFAULT 'detected', comparison_status TEXT DEFAULT 'not_run',
        comparison_json TEXT DEFAULT '{}', created_at TEXT NOT NULL,
        UNIQUE(document_id,page_no,section_no,problem_no,sub_no,bbox_json));
    CREATE INDEX IF NOT EXISTS idx_answer_anchor_document ON answer_page_anchors(document_id,page_no);
    CREATE INDEX IF NOT EXISTS idx_answer_anchor_problem ON answer_page_anchors(problem_id);
    CREATE TABLE IF NOT EXISTS textbook_documents(
        id TEXT PRIMARY KEY, textbook_id TEXT NOT NULL, document_role TEXT NOT NULL,
        filename TEXT NOT NULL, stored_path TEXT NOT NULL, sha256 TEXT NOT NULL,
        file_size INTEGER NOT NULL, page_count INTEGER NOT NULL,
        text_layer_type TEXT NOT NULL, text_layer_ratio REAL NOT NULL,
        document_status TEXT NOT NULL DEFAULT 'registered',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(textbook_id, document_role, sha256));
    CREATE INDEX IF NOT EXISTS idx_textbook_documents_textbook ON textbook_documents(textbook_id);
    CREATE TABLE IF NOT EXISTS problem_source_anchors(
        id INTEGER PRIMARY KEY AUTOINCREMENT, problem_id TEXT NOT NULL,
        document_id TEXT NOT NULL, pdf_page_index INTEGER NOT NULL CHECK(pdf_page_index >= 0),
        printed_page_no TEXT, bbox_json TEXT NOT NULL DEFAULT '[]',
        bbox_space TEXT NOT NULL DEFAULT 'pdf_points', segment_index INTEGER NOT NULL DEFAULT 0,
        crop_path TEXT DEFAULT '', resolution_method TEXT NOT NULL,
        confidence REAL, status TEXT NOT NULL DEFAULT 'candidate',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(problem_id, document_id, pdf_page_index, segment_index));
    CREATE INDEX IF NOT EXISTS idx_problem_source_anchor_problem ON problem_source_anchors(problem_id, status);
    CREATE INDEX IF NOT EXISTS idx_problem_source_anchor_document ON problem_source_anchors(document_id, pdf_page_index);
    CREATE TABLE IF NOT EXISTS ocr_repair_candidates(
        id TEXT PRIMARY KEY, problem_id TEXT NOT NULL, anchor_id INTEGER, provider TEXT NOT NULL,
        crop_path TEXT NOT NULL, latex_text TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 0,
        risks_json TEXT NOT NULL DEFAULT '[]', result_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending_teacher', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(problem_id, anchor_id, provider, crop_path));
    CREATE TABLE IF NOT EXISTS ocr_repair_decisions(
        problem_id TEXT PRIMARY KEY, decision TEXT NOT NULL, decision_json TEXT NOT NULL,
        teacher_status TEXT NOT NULL DEFAULT 'pending', teacher_note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ocr_repair_writebacks(
        id TEXT PRIMARY KEY, problem_id TEXT NOT NULL, decision TEXT NOT NULL,
        before_json TEXT NOT NULL, after_json TEXT NOT NULL, teacher_note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_ocr_repair_writebacks_problem ON ocr_repair_writebacks(problem_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_p_kp ON problems(knowledge_pts);
    CREATE INDEX IF NOT EXISTS idx_p_section ON problems(section_id);
    CREATE INDEX IF NOT EXISTS idx_p_type ON problems(ptype);
    CREATE INDEX IF NOT EXISTS idx_hw_class ON homeworks(class_id);
    CREATE INDEX IF NOT EXISTS idx_sub_hw ON submissions(homework_id);
    """)
    # 兼容已建库：追加手写批注字段
    try:
        conn.execute("ALTER TABLE submissions ADD COLUMN annotations TEXT DEFAULT '[]'")
    except Exception:
        pass
    # 兼容已建库：追加自动批改明细与复核日志字段
    for col in ["grade_detail", "review_log"]:
        try:
            conn.execute(f"ALTER TABLE submissions ADD COLUMN {col} TEXT DEFAULT '[]'")
        except Exception:
            pass
    # 兼容已建库：追加完整解答字段
    try:
        conn.execute("ALTER TABLE problems ADD COLUMN full_solution TEXT")
    except Exception:
        pass
    for col, ddl in [
        ("answer_status", "TEXT NOT NULL DEFAULT 'unverified'"),
        ("answer_invalid_reason", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE problems ADD COLUMN {col} {ddl}")
        except Exception:
            pass
    try:
        conn.execute("ALTER TABLE homeworks ADD COLUMN points_map TEXT DEFAULT '{}'")
    except Exception:
        pass
    for col, ddl in [("reviewed_at", "TEXT"), ("review_note", "TEXT DEFAULT ''")]:
        try:
            conn.execute(f"ALTER TABLE answer_import_candidates ADD COLUMN {col} {ddl}")
        except Exception:
            pass
    for col, ddl in [
        ("latex_text", "TEXT DEFAULT ''"),
        ("vision_status", "TEXT DEFAULT 'not_queued'"),
        ("vision_confidence", "REAL"),
        ("ai_review_json", "TEXT DEFAULT '{}'"),
        ("ai_review_status", "TEXT DEFAULT 'not_run'"),
        ("ai_review_model", "TEXT DEFAULT ''"),
        ("source_updated_at", "TEXT DEFAULT ''"),
        ("subquestion_count", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE answer_import_candidates ADD COLUMN {col} {ddl}")
        except Exception:
            pass
    try:
        conn.execute("ALTER TABLE answer_page_anchors ADD COLUMN teacher_subquestion_count INTEGER DEFAULT 0")
    except Exception:
        pass
    # A workbench restart means no previous HTTP recognition request is still
    # owned by this process. Do not leave stale cards permanently "running";
    # return unfinished PDF retries to their safe retry state instead.
    conn.execute("UPDATE answer_import_candidates SET ai_review_status='failed' WHERE ai_review_status='running'")
    interrupted_docs = [row[0] for row in conn.execute(
        "SELECT id FROM answer_documents WHERE status='extracting'"
    ).fetchall()]
    for document_id in interrupted_docs:
        conn.execute("""
            UPDATE answer_page_anchors
            SET extraction_status='failed', comparison_status='extraction_failed'
            WHERE document_id=? AND extraction_status IN ('detected','running')
              AND comparison_status='not_run'
        """, (document_id,))
        conn.execute("""
            UPDATE answer_documents
            SET status='indexed', index_progress=100,
                index_message='上次处理因工作台重启暂停；未完成项已回到可重试队列',
                updated_at=?
            WHERE id=?
        """, (datetime.now().isoformat(timespec="seconds"), document_id))
    conn.commit()
    conn.close()


init_db()


# Answers imported from OCR must never become grading keys until a teacher has
# explicitly verified them.  These checks also reject the most common failure
# modes in the legacy import (mojibake, page headers and multi-problem blobs).
_OCR_GARBLED = ("ï¼", "å", "ä¸", "鈭", "|||", "\ufffd")


def answer_quality_issue(answer: str, ptype: str, full_solution: str = "") -> str:
    answer = (answer or "").strip()
    if not answer:
        return "缺少标准答案"
    # These are deliberate fallback notices from the VLM service, not answers.
    # They must never be eligible for a one-click adoption.
    if "标准答案字段需人工整理" in answer or "结构化结果需人工整理" in answer or "服务器已识别" in answer:
        return "模型未给出可入库的标准答案，仅保留了待整理的识别结果"
    # A teacher may deliberately keep a multi-line final answer (for example a
    # system of equations or a short list of Fourier coefficients).  Newlines
    # alone are not OCR evidence and must not prevent an explicit writeback.
    # The length cap, garble checks, and grading gate below still protect the
    # question bank from page-sized OCR blobs.
    if len(answer) > 3000:
        return "标准答案过长，疑似整页或多题 OCR 串入"
    if any(mark in answer for mark in _OCR_GARBLED):
        return "标准答案包含 OCR/编码乱码"
    if "第" in answer and "章" in answer:
        return "标准答案包含页眉或章节标题"
    # Some calculation questions have a legitimate text-only conclusion, such
    # as “无极值”.  Treat these established mathematical conclusions as valid
    # answers instead of requiring a numeral or TeX token.
    textual_calc_conclusions = (
        "无极值", "无最大值", "无最小值", "不存在极值", "无解",
        "无实数解", "无实根", "无定义", "不可导", "不收敛", "发散",
    )
    # Besides ASCII/TeX expressions, accept symbolic final answers such as
    # |α|, π/2 and √2.  These are common valid calculus answers, not prose.
    has_math_token = bool(re.search(r"[0-9A-Za-z\\\\∞∞π√]|[\\u0370-\\u03ff]|[|=+*/^_{}()\[\]]", answer))
    if ptype == "calc" and not has_math_token and not any(x in answer for x in textual_calc_conclusions):
        return "计算题答案不是可判定的数学表达式"
    # Do not treat ordinary Chinese characters (for example “由”) or a tilde
    # used in mathematical notation as OCR corruption.  This old heuristic
    # rejected teacher-entered, valid multi-line derivations.  Actual encoding
    # corruption remains covered by _OCR_GARBLED above, and teacher writeback
    # still requires explicit confirmation against the source image.
    if full_solution and len(full_solution) > 12000:
        return "完整解答过长，疑似跨题拼接"
    return ""


def answer_requirement_issue(problem_text: str, answer: str) -> str:
    """Reject an otherwise mathematical-looking answer that omits an explicit
    component requested by the question (a common VLM final-answer failure)."""
    problem_text = (problem_text or "").lower()
    answer = (answer or "").lower()
    if not answer:
        return "缺少答案"
    asks_domain = any(token in problem_text for token in ("定义域", "domain"))
    has_domain = any(token in answer for token in ("定义域", "domain", "x≠", "x !=", "x∈", "x in", "x\\ne", "x\\in"))
    if asks_domain and not has_domain:
        return "题目要求“定义域”，候选答案未给出定义域"
    asks_range = any(token in problem_text for token in ("值域", "range"))
    has_range = any(token in answer for token in ("值域", "range"))
    if asks_range and not has_range:
        return "题目要求“值域”，候选答案未给出值域"
    return ""


def grading_ready(problem: dict) -> tuple[bool, str]:
    if problem.get("ptype") != "calc":
        return False, "证明/主观题必须人工复核"
    if problem.get("answer_status") != "verified":
        return False, "标准答案尚未经教师核验"
    if "\n" in (problem.get("std_answer") or ""):
        return False, "多小问题需要按小问分别评分"
    issue = answer_quality_issue(problem.get("std_answer", ""), problem.get("ptype", "calc"), problem.get("full_solution", ""))
    if issue:
        return False, issue
    try:
        steps = json.loads(problem.get("grading_steps") or "[]")
    except Exception:
        return False, "评分细则不是合法 JSON"
    if steps and not isinstance(steps, list):
        return False, "评分细则必须为步骤列表"
    if not 0 < float(problem.get("answer_weight") or 0) <= 1:
        return False, "答案分权重无效"
    return True, ""


def normalize_homework_items(problem_ids: list, points_map: dict | None = None) -> tuple[list, dict]:
    """Preserve order, remove duplicates, and make the assignment total exactly 100."""
    ids = list(dict.fromkeys(str(pid) for pid in problem_ids if pid))
    if not ids:
        return [], {}
    raw = points_map or {}
    supplied = {pid: float(raw[pid]) for pid in ids if pid in raw and float(raw[pid]) > 0}
    if len(supplied) == len(ids) and round(sum(supplied.values()), 6) == 100:
        return ids, supplied
    base, remainder = divmod(100, len(ids))
    return ids, {pid: float(base + (1 if index < remainder else 0)) for index, pid in enumerate(ids)}


# --------------------------------------------------------------------------- #
# 首次运行种子数据（参考班级 + 一个演示作业），仅在未播种过时执行一次
# --------------------------------------------------------------------------- #
def seed_if_empty():
    conn = get_db()
    cur = conn.cursor()
    seeded = cur.execute("SELECT value FROM meta WHERE key='demo_seeded'").fetchone()
    if seeded:
        conn.close()
        return
    # 不再预置固定班级；教师自行创建。
    # 演示作业仅在已有班级和题库时自动创建，方便首次体验。
    if cur.execute("SELECT COUNT(*) FROM homeworks").fetchone()[0] == 0:
        pid_rows = cur.execute(
            "SELECT p.id FROM problems p JOIN sections s ON s.id=p.section_id "
            "WHERE s.section_no='1.3' ORDER BY p.problem_no LIMIT 3").fetchall()
        cls = cur.execute("SELECT id FROM classes ORDER BY name LIMIT 1").fetchone()
        tid = cur.execute("SELECT id FROM textbooks LIMIT 1").fetchone()
        if pid_rows and cls and tid:
            hw_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO homeworks(id,textbook_id,title,class_id,section_no,deadline,status,problem_ids,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (hw_id, tid["id"], "演示作业：第一章 1.3 极限的运算法则",
                 cls["id"], "1.3",
                 (datetime.now().isoformat(timespec="seconds")),
                 "closed",
                 json.dumps([r["id"] for r in pid_rows]),
                 datetime.now().isoformat(timespec="seconds")))
            for i, (sno, sname) in enumerate([("20230101", "张三"), ("20230202", "李四")]):
                cur.execute(
                    "INSERT INTO submissions(id,homework_id,student_no,student_name,submitted_at,status,score,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), hw_id, sno, sname,
                     (datetime.now().isoformat(timespec="seconds")), "pending", None,
                     datetime.now().isoformat(timespec="seconds")))
    cur.execute("INSERT INTO meta(key,value) VALUES('demo_seeded','1')")
    conn.commit()
    conn.close()


seed_if_empty()


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
class IngestReq(BaseModel):
    path: str  # book_problems.json 路径


class ClassReq(BaseModel):
    name: str
    term: str = ""


class HomeworkReq(BaseModel):
    title: str
    class_id: str
    section_no: str
    deadline: str  # ISO 字符串
    problem_ids: list = []
    points_map: dict = {}
    section_nos: Optional[list] = None  # 多选章节列表，兼容单章节


class SubmissionReq(BaseModel):
    homework_id: str
    student_no: str
    student_name: str
    score: Optional[float] = None
    answers: Optional[list] = None  # [{problem_id, text}]


class StudentSubmissionUploadReq(BaseModel):
    homework_id: str
    student_no: str
    student_name: str
    files: list  # [{data_base64, filename, mime_type}]


class ProblemManualReq(BaseModel):
    section_no: str
    problem_no: str
    sub_no: Optional[str] = None
    ptype: str = "calc"
    difficulty: int = 3
    knowledge_pts: list = []
    content_text: str = ""


class GradeReq(BaseModel):
    score: float = 0.0


class StudentsImportReq(BaseModel):
    class_id: str
    students: list  # [{student_no, name}]


# --------------------------------------------------------------------------- #
# 知识点字典（与 extract_book.SECTION_KP 对齐）
# --------------------------------------------------------------------------- #
KP_NAME = {
    "limit.sequence": "数列极限", "limit.function": "函数极限", "limit.def": "极限定义",
    "limit.four_ops": "极限四则运算法则", "limit.comparison": "极限比较", "limit.squeeze": "夹逼准则",
    "limit.monotone": "单调有界准则", "limit.two_importants": "两个重要极限",
    "limit.inf_small": "无穷小", "limit.inf_large": "无穷大", "limit.order": "无穷阶",
    "continuity.def": "连续定义", "continuity.ops": "连续运算", "continuity.elem": "初等函数连续性",
    "continuity.closed_interval": "闭区间连续性质", "continuity.zero_point": "零点定理",
}


def ensure_kp(cur, codes):
    for c in codes:
        cur.execute("INSERT OR IGNORE INTO knowledge_points(code,name,parent_code,section_no)"
                    " VALUES(?,?,?,?)", (c, KP_NAME.get(c, c), None, None))


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat(timespec="seconds")}


# ----- 教材 / 章节 -----
@app.get("/textbooks")
def list_textbooks():
    conn = get_db()
    rows = conn.execute("SELECT * FROM textbooks").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/textbooks")
def create_textbook(name: str, volume: str = "", edition: str = "", pdf_path: str = "", page_offset: int = 0):
    tid = str(uuid.uuid4())
    conn = get_db()
    conn.execute("INSERT INTO textbooks(id,name,volume,edition,pdf_path,page_offset)"
                 " VALUES(?,?,?,?,?,?)", (tid, name, volume, edition, pdf_path, page_offset))
    conn.commit()
    conn.close()
    return {"id": tid}


@app.get("/textbooks/{tid}/sections")
def list_sections(tid: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM sections WHERE textbook_id=?", (tid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ----- 入库 -----
@app.post("/ingest/book")
def ingest_book(req: IngestReq):
    """把 extract_book.py 产出的 book_problems.json 灌入题库。"""
    if not os.path.exists(req.path):
        raise HTTPException(404, f"文件不存在: {req.path}")
    payload = json.load(open(req.path, encoding="utf-8"))
    tb = payload.get("textbook", {})
    tid = str(uuid.uuid4())
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO textbooks(id,name,page_offset) VALUES(?,?,?)",
                (tid, tb.get("name", "未知教材"), tb.get("page_offset", 0)))
    cnt_p = cnt_s = 0
    for sec in payload.get("sections", []):
        sec_no = sec["section_no"]
        sid = str(uuid.uuid4())
        cur.execute("INSERT INTO sections(id,textbook_id,section_no,title) VALUES(?,?,?,?)",
                    (sid, tid, sec_no, sec.get("heading", "")))
        cnt_s += 1
        ensure_kp(cur, sec.get("knowledge_pts", []))
        for p in sec["problems"]:
            pid = str(uuid.uuid4())
            kp = ",".join(p.get("knowledge_pts", []))
            p_img = (p.get("img") or "").replace("\\", "/")
            cur.execute(
                "INSERT INTO problems(id,section_id,exercise_set,problem_no,sub_no,ptype,"
                "crop_image_path,content_text,difficulty,knowledge_pts,extract_status,"
                "std_answer,full_solution,grading_steps,answer_weight) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, sid, sec_no, p["no"], p.get("sub_no"), "calc", p_img,
                 p.get("content_text", ""), 3, kp, "reviewed",
                 p.get("std_answer"), p.get("full_solution"), p.get("grading_steps"), p.get("answer_weight", 0.6)))
            cnt_p += 1
            for sub in p.get("subproblems", []):
                spid = str(uuid.uuid4())
                skp = ",".join(sub.get("knowledge_pts", p.get("knowledge_pts", [])))
                cur.execute(
                    "INSERT INTO problems(id,section_id,exercise_set,problem_no,sub_no,ptype,"
                    "crop_image_path,content_text,difficulty,knowledge_pts,extract_status,"
                    "std_answer,full_solution,grading_steps,answer_weight) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (spid, sid, sec_no, p["no"], sub["no"], "calc",
                     (sub.get("img") or "").replace("\\", "/"), sub.get("content_text", ""), 3, skp, "reviewed",
                     sub.get("std_answer"), sub.get("full_solution"), sub.get("grading_steps"), sub.get("answer_weight", 0.6)))
                cnt_p += 1
    conn.commit()
    conn.close()
    return {"textbook_id": tid, "sections": cnt_s, "problems": cnt_p}


# ----- 知识点 -----
@app.get("/knowledge-points")
def list_kp():
    conn = get_db()
    rows = conn.execute("SELECT code,name,parent_code,section_no FROM knowledge_points").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/knowledge-points/{code}/problems")
def kp_problems(code: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT p.id, s.section_no, p.exercise_set, p.problem_no, p.sub_no, "
        "p.ptype, p.difficulty, p.crop_image_path "
        "FROM problems p JOIN sections s ON s.id=p.section_id "
        "WHERE p.knowledge_pts LIKE ?", (f"%{code}%",)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ----- 题目检索 -----
@app.get("/problems")
def query_problems(
    section_no: Optional[str] = None,
    ptype: Optional[str] = None,
    difficulty_min: Optional[int] = None,
    difficulty_max: Optional[int] = None,
    knowledge_pts: Optional[str] = None,  # 逗号分隔，任一命中
    q: Optional[str] = None,
    page: int = 1,
    size: int = 50,
):
    sql = """SELECT p.id, s.section_no, p.exercise_set, p.problem_no, p.sub_no,
                    p.ptype, p.difficulty, p.knowledge_pts, p.crop_image_path, p.content_text,
                    p.std_answer, p.full_solution, p.grading_steps, p.answer_weight, p.answer_status, p.answer_invalid_reason,
                    EXISTS(SELECT 1 FROM answer_import_candidates c
                           WHERE c.problem_id=p.id AND c.match_status='pending'
                             AND c.ai_review_status='completed') AS ai_candidate_ready,
                    CASE WHEN p.sub_no IS NOT NULL AND trim(p.sub_no)!='' THEN
                      EXISTS(SELECT 1 FROM problems parent
                             WHERE parent.section_id=p.section_id AND parent.problem_no=p.problem_no
                               AND (parent.sub_no IS NULL OR trim(parent.sub_no)='')
                               AND parent.answer_status='verified')
                    ELSE 0 END AS parent_answer_verified
             FROM problems p JOIN sections s ON s.id=p.section_id WHERE 1=1"""
    where, args = _where_clause(section_no, ptype, difficulty_min, difficulty_max, knowledge_pts, q)
    sql += where
    sql += " ORDER BY s.section_no, p.problem_no, p.sub_no LIMIT ? OFFSET ?"
    args += [size, (page - 1) * size]
    conn = get_db()
    total = conn.execute(
        "SELECT COUNT(*) FROM problems p JOIN sections s ON s.id=p.section_id "
        "WHERE 1=1" + where, args[:-2]).fetchone()[0]
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


def _where_clause(section_no, ptype, dmin, dmax, kp, q):
    """Return a parameterized (SQL fragment, args) for the problems list filter.

    Every caller-supplied value is bound as a query parameter (``?``) — nothing is
    interpolated into the SQL text. The fragment is therefore safe to concatenate
    into a statement and execute as ``conn.execute(sql, args)``.
    """
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
        args += [f"%{x.strip()}%" for x in kp.split(",")]
    if q:
        clause += " AND (s.section_no LIKE ? OR p.problem_no LIKE ? OR p.exercise_set LIKE ?)"
        args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    return clause, args


@app.get("/problems/tiers")
def list_tier_problems(section_no: Optional[str] = None):
    """按难度层查看题目分布"""
    conn = get_db()
    where = ""
    args = []
    if section_no:
        where = " AND s.section_no=?"
        args.append(section_no)
    rows = conn.execute(f"""
        SELECT p.tier, s.section_no, COUNT(*) as cnt
        FROM problems p JOIN sections s ON s.id=p.section_id
        WHERE p.tier IS NOT NULL AND p.tier != ''{where}
        GROUP BY p.tier, s.section_no ORDER BY s.section_no, p.tier
    """, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/problems/answers-status")
def list_answers_status(section_no: Optional[str] = None):
    """查看题目答案配置状态（哪些题已有答案，哪些还没配置）"""
    conn = get_db()
    where = "WHERE s.section_no=?" if section_no else ""
    args = [section_no] if section_no else []
    rows = conn.execute(f"""
        SELECT p.id, s.section_no, p.problem_no, p.sub_no, p.ptype, p.std_answer,
               CASE WHEN p.grading_steps IS NOT NULL AND p.grading_steps != '' THEN 1 ELSE 0 END as has_steps
        FROM problems p JOIN sections s ON s.id=p.section_id
        {where}
        ORDER BY s.section_no, p.problem_no, p.sub_no
    """, args).fetchall()
    total = len(rows)
    with_answer = sum(1 for r in rows if r["std_answer"])
    conn.close()
    return {
        "total": total,
        "with_answer": with_answer,
        "without_answer": total - with_answer,
        "problems": [dict(r) for r in rows]
    }


@app.get("/problems/answers-validate")
def validate_answers(section_no: Optional[str] = None):
    """答案质量自检：逐题跑批改引擎 + 非符号型专用比对器，分六类标记：
       ok             可自动判分（SymPy 符号判等）
       auto_interval  区间类答案（定义域/单调区间/收敛域），专用集合比对器可自动判分
       auto_limit     极限类答案（末值/无极限结论），专用比对器可自动判分
       auto_deriv     含导数记号的答案（f'(x)/y' 等），专用结构比对器可自动判分
       manual         OCR 损坏 / 纯文字应用题，无法自动判分，需人工或重录
       broken         提取异常（混入排版垃圾 \\blacksquare/\\text{}），需修复
       empty          未配置答案
    """
    import re as _re
    from grading_engine import (normalize_expr, to_sympy, classify_candidate,
                                _parse_interval_piece, _extract_limit_value)

    conn = get_db()
    where = "WHERE s.section_no=?" if section_no else ""
    args = [section_no] if section_no else []
    rows = conn.execute(f"""
        SELECT p.id, s.section_no, p.problem_no, p.sub_no, p.ptype, p.std_answer,p.answer_status
        FROM problems p JOIN sections s ON s.id=p.section_id
        {where}
        ORDER BY s.section_no, p.problem_no, p.sub_no
    """, args).fetchall()
    conn.close()

    # 计算题：归一化后仍残留的 LaTeX 排版命令（真·提取异常）
    CALC_BROKEN = _re.compile(
        r"\\(blacksquare|end|begin|left|right|operatorname|text|mathrm|qquad|quad|hline|frac|sqrt|dfrac|tfrac)"
    )
    # 证明题：只拦真正的排版/解题过程垃圾（\frac \left 等是正常引用）
    PROOF_BROKEN = _re.compile(
        r"\\(blacksquare|end|begin|operatorname|text|mathrm|qquad|quad|hline)"
    )
    SUSPICIOUS_LEN = 200

    results = []
    for r in rows:
        pid = r["id"]
        ptype = r["ptype"]
        ans = (r["std_answer"] or "").strip()
        if not ans:
            results.append({"id": pid, "status": "empty", "reason": "未配置答案", "normalized": ""})
            continue

        if r["answer_status"] != "verified":
            results.append({"id": pid, "status": "unverified",
                            "reason": "答案格式可能可解析，但尚未经过教师核验，禁止用于自动评分",
                            "normalized": ""})
            continue

        if ptype == "proof":
            if PROOF_BROKEN.search(ans):
                results.append({"id": pid, "status": "broken",
                                "reason": "含排版/解题过程垃圾（如 \\blacksquare、\\text{}），不是纯净要点", "normalized": ""})
            elif len(ans) > SUSPICIOUS_LEN:
                results.append({"id": pid, "status": "manual",
                                "reason": f"答案过长（{len(ans)} 字），疑似整段解题过程，将转人工复核", "normalized": ""})
            else:
                results.append({"id": pid, "status": "ai_review",
                                "reason": "证明题答案已核验，可供 AI 评分，但仍需按置信度复核",
                                "normalized": ""})
            continue

        # 计算题：逐候选分类，判断非符号型专用比对器能否覆盖
        candidates = [c.strip() for c in _re.split(r"\s*\|\|\|\s*", ans) if c.strip()]
        norm_parts, types_present = [], set()
        broken_flag, manual_flag = False, False
        for c in candidates:
            norm = normalize_expr(c)
            norm_parts.append(norm)
            ct = classify_candidate(c)
            if ct == "symbolic":
                if CALC_BROKEN.search(norm):
                    broken_flag = True
                elif to_sympy(c) is None:
                    # 既不是符号型可解析，又没被识别成区间/极限/导数 → OCR 文字垃圾
                    manual_flag = True
                else:
                    types_present.add("ok")
            elif ct == "interval":
                pieces = [p for piece in _re.split(r"\\?cup", c)
                          for p in [_parse_interval_piece(piece)] if p]
                if not pieces:
                    manual_flag = True      # 看起来像区间但解析不出（如 OCR 把 0 识别成 O）
                else:
                    types_present.add("auto_interval")
            elif ct == "limit":
                sv, sno = _extract_limit_value(c)
                if sv is None and not sno:
                    # 极限式没有末值、也没有"无极限"结论 → 答案键不完整，无法自动判
                    manual_flag = True
                else:
                    types_present.add("auto_limit")
            elif ct == "deriv":
                types_present.add("auto_deriv")

        normalized = " ||| ".join(norm_parts)

        if broken_flag:
            results.append({"id": pid, "status": "broken",
                            "reason": "答案含未净化的 LaTeX 排版命令，引擎无法稳定比对，需修复", "normalized": normalized})
        elif manual_flag:
            results.append({"id": pid, "status": "manual",
                            "reason": "OCR 损坏或纯文字应用题，专用比对器无法覆盖，需人工复核或重录",
                            "normalized": normalized})
        elif types_present <= {"ok"}:
            results.append({"id": pid, "status": "ok",
                            "reason": f"{len(candidates)} 个候选均可被 SymPy 解析，可自动判分", "normalized": normalized})
        else:
            auto_type = sorted(types_present - {"ok"})[0]
            label = {"auto_interval": "区间集合比对器", "auto_limit": "极限比对器",
                     "auto_deriv": "导数式结构比对器"}[auto_type]
            results.append({"id": pid, "status": auto_type,
                            "reason": f"含非符号型答案，已由{label}自动判分", "normalized": normalized})

    # 汇总：可自动判分 = ok + 三个 auto_*；需人工 = manual + broken；无答案 = empty
    summary = {"ok": 0, "auto_interval": 0, "auto_limit": 0, "auto_deriv": 0,
               "ai_review": 0, "unverified": 0, "manual": 0, "broken": 0, "empty": 0}
    for x in results:
        summary[x["status"]] += 1
    summary["auto_total"] = (summary["ok"] + summary["auto_interval"]
                              + summary["auto_limit"] + summary["auto_deriv"])
    summary["manual_total"] = summary["manual"] + summary["broken"]
    return {"summary": summary, "results": results}


@app.get("/problems/{pid}")
def get_problem(pid: str):
    conn = get_db()
    row = conn.execute(
        "SELECT p.id,s.section_no,p.exercise_set,p.problem_no,p.sub_no,p.ptype,"
        "p.difficulty,p.knowledge_pts,p.crop_image_path,p.content_text,"
        "p.std_answer,p.full_solution,p.grading_steps,p.answer_weight FROM problems p "
        "JOIN sections s ON s.id=p.section_id WHERE p.id=?", (pid,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "题目不存在")
    return dict(row)


@app.post("/problems")
def add_problem_manual(req: ProblemManualReq):
    """手动录入题目（文本型，无裁切图），写入对应章节。"""
    conn = get_db()
    cur = conn.cursor()
    sec = cur.execute("SELECT id FROM sections WHERE section_no=?", (req.section_no,)).fetchone()
    if not sec:
        conn.close()
        raise HTTPException(404, f"章节不存在: {req.section_no}")
    pid = str(uuid.uuid4())
    kp = ",".join(req.knowledge_pts)
    cur.execute(
        "INSERT INTO problems(id,section_id,exercise_set,problem_no,sub_no,ptype,"
        "crop_image_path,content_text,difficulty,knowledge_pts,extract_status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, sec["id"], req.section_no, req.problem_no, req.sub_no, req.ptype,
         None, req.content_text, req.difficulty, kp, "manual"))
    conn.commit()
    conn.close()
    return {"id": pid}


# ----- 裁切图静态服务 -----
@app.get("/images/{path:path}")
def get_image(path: str):
    path = path.replace("\\", "/").strip("/")
    if ".." in path or path.startswith("/") or path.startswith("\\"):
        raise HTTPException(400, "非法路径")
    for root in [os.path.dirname(__file__), IMAGE_ROOT, "extract_img_v2", "extract_img_test", "answer_source_previews"]:
        fp = os.path.join(root, path)
        if os.path.isfile(fp):
            return FileResponse(fp)
    raise HTTPException(404, "图片不存在")


# ----- 班级 -----
@app.get("/classes")
def list_classes():
    conn = get_db()
    rows = conn.execute("SELECT * FROM classes ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/classes")
def create_class(req: ClassReq):
    cid = str(uuid.uuid4())
    conn = get_db()
    conn.execute("INSERT INTO classes(id,teacher_id,name,term) VALUES(?,?,?,?)",
                 (cid, "teacher_demo", req.name, req.term))
    conn.commit()
    conn.close()
    return {"id": cid}


@app.delete("/classes/{cid}")
def delete_class(cid: str):
    conn = get_db()
    # 删除班级下学生、关联作业及提交
    conn.execute("DELETE FROM students WHERE class_id=?", (cid,))
    hw_ids = [r["id"] for r in conn.execute("SELECT id FROM homeworks WHERE class_id=?", (cid,)).fetchall()]
    for hid in hw_ids:
        conn.execute("DELETE FROM submissions WHERE homework_id=?", (hid,))
    conn.execute("DELETE FROM homeworks WHERE class_id=?", (cid,))
    conn.execute("DELETE FROM classes WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/classes/{cid}/students")
def list_class_students(cid: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM students WHERE class_id=? ORDER BY student_no", (cid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/students")
def list_students(class_id: Optional[str] = None):
    conn = get_db()
    if class_id:
        rows = conn.execute("SELECT * FROM students WHERE class_id=? ORDER BY student_no", (class_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM students ORDER BY class_id, student_no").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/students/import")
def import_students(req: StudentsImportReq):
    if not req.class_id:
        raise HTTPException(400, "班级必填")
    conn = get_db()
    cur = conn.cursor()
    cls = cur.execute("SELECT id FROM classes WHERE id=?", (req.class_id,)).fetchone()
    if not cls:
        conn.close()
        raise HTTPException(404, "班级不存在")
    inserted = 0
    skipped = 0
    for s in req.students:
        sno = str(s.get("student_no", "")).strip()
        name = str(s.get("name", "")).strip()
        if not sno or not name:
            skipped += 1
            continue
        existing = cur.execute("SELECT id FROM students WHERE class_id=? AND student_no=?",
                               (req.class_id, sno)).fetchone()
        if existing:
            skipped += 1
            continue
        cur.execute("INSERT INTO students(id,class_id,student_no,name) VALUES(?,?,?,?)",
                    (str(uuid.uuid4()), req.class_id, sno, name))
        inserted += 1
    conn.commit()
    conn.close()
    return {"inserted": inserted, "skipped": skipped, "total": len(req.students)}


# ----- 作业 -----
@app.get("/homeworks")
def list_homeworks():
    conn = get_db()
    rows = conn.execute("SELECT * FROM homeworks ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/homeworks")
def create_homework(req: HomeworkReq):
    if not req.class_id or not req.section_no:
        raise HTTPException(400, "班级与章节必填")
    problem_ids, points_map = normalize_homework_items(req.problem_ids, req.points_map)
    if not problem_ids:
        raise HTTPException(400, "作业至少需要一题")
    hid = str(uuid.uuid4())
    conn = get_db()
    tid = conn.execute("SELECT id FROM textbooks LIMIT 1").fetchone()
    # section_no 可能为逗号拼接的多章节；若传入 section_nos 则优先用它生成显示名
    section_display = req.section_no
    if req.section_nos and len(req.section_nos) > 1:
        section_display = ",".join(str(s) for s in req.section_nos)
    conn.execute(
        "INSERT INTO homeworks(id,textbook_id,title,class_id,section_no,deadline,status,problem_ids,points_map,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (hid, tid["id"] if tid else None, req.title, req.class_id, section_display,
          req.deadline, "published", json.dumps(problem_ids), json.dumps(points_map),
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    return {"id": hid, "problem_ids": problem_ids, "points_map": points_map}


@app.get("/homeworks/{hid}")
def get_homework(hid: str):
    conn = get_db()
    hw = conn.execute("SELECT * FROM homeworks WHERE id=?", (hid,)).fetchone()
    if not hw:
        conn.close()
        raise HTTPException(404, "作业不存在")
    probs = []
    ids, points_map = normalize_homework_items(json.loads(hw["problem_ids"] or "[]"), json.loads(hw["points_map"] or "{}"))
    if ids:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT p.id, s.section_no, p.problem_no, p.sub_no, "
            f"p.crop_image_path, p.content_text, p.knowledge_pts "
            f"FROM problems p JOIN sections s ON s.id=p.section_id "
            f"WHERE p.id IN ({placeholders})", ids).fetchall()
        by_id = {r["id"]: dict(r) for r in rows}
        probs = [by_id[pid] for pid in ids if pid in by_id]
    conn.close()
    out = dict(hw)
    out["problem_ids"] = ids
    out["points_map"] = points_map
    out["problems"] = probs
    return out


@app.patch("/homeworks/{hid}/close")
def close_homework(hid: str):
    conn = get_db()
    cur = conn.execute("UPDATE homeworks SET status='closed' WHERE id=?", (hid,))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    if not ok:
        raise HTTPException(404, "作业不存在")
    return {"ok": True}


@app.delete("/homeworks/{hid}")
def delete_homework(hid: str):
    conn = get_db()
    conn.execute("DELETE FROM submissions WHERE homework_id=?", (hid,))
    conn.execute("DELETE FROM homeworks WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ----- 提交 / 批改 -----
@app.get("/submissions")
def list_submissions(homework_id: Optional[str] = None):
    conn = get_db()
    if homework_id:
        rows = conn.execute("SELECT * FROM submissions WHERE homework_id=? ORDER BY submitted_at DESC",
                            (homework_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM submissions ORDER BY submitted_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/submissions")
def create_submission(req: SubmissionReq):
    sid = str(uuid.uuid4())
    status = "graded" if req.score is not None else "pending"
    conn = get_db()
    conn.execute(
        "INSERT INTO submissions(id,homework_id,student_no,student_name,submitted_at,status,score,answers,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (sid, req.homework_id, req.student_no, req.student_name,
         datetime.now().isoformat(timespec="seconds"), status, req.score,
         json.dumps(req.answers or []),
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    return {"id": sid}


@app.get("/student/homeworks")
def student_homeworks():
    conn = get_db()
    rows = conn.execute("""
        SELECT h.id,h.title,h.section_no,h.deadline,c.name AS class_name,
               h.problem_ids,h.points_map
        FROM homeworks h LEFT JOIN classes c ON c.id=h.class_id
        WHERE h.status='published' ORDER BY h.created_at DESC
    """).fetchall()
    conn.close()
    result = []
    for row in rows:
        item = dict(row)
        item["problem_count"] = len(json.loads(item.pop("problem_ids") or "[]"))
        item.pop("points_map", None)
        result.append(item)
    return result


@app.post("/student/submissions")
def upload_student_submission(req: StudentSubmissionUploadReq):
    if not req.student_no.strip() or not req.student_name.strip():
        raise HTTPException(400, "student number and name are required")
    if not req.files:
        raise HTTPException(400, "at least one image or PDF is required")
    conn = get_db()
    if not conn.execute("SELECT 1 FROM homeworks WHERE id=? AND status='published'", (req.homework_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "published homework not found")
    sid = str(uuid.uuid4())
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("""
        INSERT INTO submissions(id,homework_id,student_no,student_name,submitted_at,status,score,answers,created_at)
        VALUES(?,?,?,?,?,'pending',NULL,'[]',?)
    """, (sid, req.homework_id, req.student_no.strip(), req.student_name.strip(), now, now))
    out_dir = os.path.join(os.path.dirname(__file__), "student_submissions", sid)
    os.makedirs(out_dir, exist_ok=True)
    page_no = 0
    try:
        for file_item in req.files:
            raw = base64.b64decode(str(file_item.get("data_base64", "")).split(",", 1)[-1], validate=True)
            if len(raw) > 30 * 1024 * 1024:
                raise HTTPException(413, "one uploaded file is larger than 30 MB")
            mime = str(file_item.get("mime_type", "")).lower()
            filename = str(file_item.get("filename", "")).lower()
            page_images = []
            if mime == "application/pdf" or filename.endswith(".pdf"):
                import fitz
                document = fitz.open(stream=raw, filetype="pdf")
                if len(document) > 30:
                    raise HTTPException(413, "PDF has more than 30 pages")
                for page in document:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    page_images.append(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"))
            else:
                page_images.append(Image.open(io.BytesIO(raw)).convert("RGB"))
            for image in page_images:
                page_no += 1
                path = os.path.join(out_dir, f"page-{page_no}.jpg")
                image.thumbnail((2400, 2400))
                image.save(path, "JPEG", quality=92)
                relative = os.path.relpath(path, os.path.dirname(__file__)).replace("\\", "/")
                conn.execute("""
                    INSERT INTO submission_source_images(submission_id,sort_order,image_path,created_at)
                    VALUES(?,?,?,?)
                """, (sid, page_no, relative, now))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return {"ok": True, "submission_id": sid, "page_count": page_no,
            "status": "pending", "message": "submitted; teacher can start AI grading"}


@app.get("/submissions/{sid}")
def get_submission(sid: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "提交不存在")
    d = dict(row)
    try:
        d["answers"] = json.loads(d.get("answers") or "[]")
    except Exception:
        d["answers"] = []

    # 关联作业题目，便于前端展示题号/类型/裁切图
    problems = []
    ans_by_pid = {a.get("problem_id", ""): a for a in d["answers"]}
    try:
        hw = conn.execute(
            "SELECT problem_ids FROM homeworks WHERE id=?", (d.get("homework_id"),)).fetchone()
        pids = json.loads(hw["problem_ids"] or "[]") if hw else []
        if pids:
            placeholders = ",".join("?" * len(pids))
            prows = conn.execute(
                f"SELECT p.id,p.problem_no,p.sub_no,p.ptype,p.crop_image_path,p.content_text,"
                f"p.std_answer,s.section_no FROM problems p JOIN sections s ON s.id=p.section_id "
                f"WHERE p.id IN ({placeholders})", pids).fetchall()
            for pr in prows:
                prd = dict(pr)
                no = (prd.get("problem_no") or "") + \
                    (f"({prd['sub_no']})" if prd.get("sub_no") else "")
                ans = ans_by_pid.get(prd["id"], {})
                problems.append({
                    "id": prd["id"],
                    "problem_no": no,
                    "ptype": prd.get("ptype"),
                    "crop_image_path": prd.get("crop_image_path"),
                    "content_text": prd.get("content_text"),
                    "std_answer": prd.get("std_answer"),
                    "answer_text": ans.get("text", ""),
                    "answer_image": ans.get("image", ""),
                })
    except Exception as e:
        problems = []
    d["problems"] = problems
    d["source_images"] = ["/api/images/" + row[0] for row in conn.execute(
        "SELECT image_path FROM submission_source_images WHERE submission_id=? ORDER BY sort_order",
        (sid,)).fetchall()]
    conn.close()
    try:
        d["annotations"] = json.loads(d.get("annotations") or "[]")
    except Exception:
        d["annotations"] = []
    return d


class AnnotationReq(BaseModel):
    problem_id: str
    strokes: list


@app.get("/submissions/{sid}/annotations")
def get_annotations(sid: str):
    conn = get_db()
    row = conn.execute("SELECT annotations FROM submissions WHERE id=?", (sid,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "提交不存在")
    try:
        return json.loads(row["annotations"] or "[]")
    except Exception:
        return []


@app.post("/submissions/{sid}/annotations")
def save_annotations(sid: str, req: AnnotationReq):
    conn = get_db()
    row = conn.execute("SELECT annotations FROM submissions WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "提交不存在")
    try:
        annotations = json.loads(row["annotations"] or "[]")
    except Exception:
        annotations = []
    # 按 problem_id 覆盖本次保存的批注
    annotations = [a for a in annotations if a.get("problem_id") != req.problem_id]
    annotations.append({"problem_id": req.problem_id, "strokes": req.strokes})
    conn.execute("UPDATE submissions SET annotations=? WHERE id=?",
                 (json.dumps(annotations, ensure_ascii=False), sid))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.patch("/submissions/{sid}")
def grade_submission(sid: str, req: GradeReq):
    conn = get_db()
    cur = conn.execute("UPDATE submissions SET status='graded', score=? WHERE id=?", (req.score, sid))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    if not ok:
        raise HTTPException(404, "提交不存在")
    return {"ok": True}


@app.delete("/submissions/{sid}")
def delete_submission(sid: str):
    conn = get_db()
    conn.execute("DELETE FROM submissions WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ----- 统计 -----
@app.get("/stats")
def stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
    by_type = dict(conn.execute("SELECT ptype,COUNT(*) FROM problems GROUP BY ptype").fetchall())
    by_sec = dict(conn.execute(
        "SELECT s.section_no,COUNT(*) FROM problems p JOIN sections s ON s.id=p.section_id "
        "GROUP BY s.section_no").fetchall())
    kp_used = conn.execute(
        "SELECT COUNT(DISTINCT code) FROM knowledge_points k WHERE EXISTS("
        "SELECT 1 FROM problems p WHERE p.knowledge_pts LIKE '%'||k.code||'%')").fetchone()[0]
    homeworks_count = conn.execute("SELECT COUNT(*) FROM homeworks").fetchone()[0]
    submissions_count = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    pending_submissions = conn.execute(
        "SELECT COUNT(*) FROM submissions WHERE status='pending'").fetchone()[0]
    classes_count = conn.execute("SELECT COUNT(*) FROM classes").fetchone()[0]
    conn.close()
    return {"total_problems": total, "by_type": by_type,
            "sections_count": len(by_sec), "knowledge_points_used": kp_used,
            "by_section": by_sec,
            "homeworks_count": homeworks_count, "submissions_count": submissions_count,
            "pending_submissions": pending_submissions, "classes_count": classes_count}


# ----- 演示数据重置（仅清作业+提交，保留题库/班级/教材） -----
@app.post("/dev/reset-demo")
def reset_demo():
    conn = get_db()
    conn.execute("DELETE FROM submissions")
    conn.execute("DELETE FROM homeworks")
    conn.commit()
    conn.close()
    return {"ok": True}


# ----- 难度分层 -----
@app.post("/admin/classify-tiers")
def classify_tiers(dry_run: bool = Query(False)):
    """执行全库难度分层（basic/medium/advanced）"""
    from difficulty_tier import classify_problems, TIER_NAMES
    results = classify_problems(dry_run=dry_run)
    summary = {}
    for r in results:
        summary.setdefault(r["tier_name"], 0)
        summary[r["tier_name"]] += 1
    return {"dry_run": dry_run, "total": len(results), "summary": summary,
            "results": results[:20] if dry_run else []}


@app.post("/homeworks/smart-select")
def smart_select_problems(
    section_no: Optional[str] = Query(None),
    section_nos: Optional[str] = Query(None),
    basic: int = Query(3),
    medium: int = Query(2),
    advanced: int = Query(1)):
    """智能选题：从指定章节按难度比例自动选题。支持 section_no 单章或 section_nos 多章（逗号分隔）。"""
    from difficulty_tier import select_homework_problems
    counts = {"basic": basic, "medium": medium, "advanced": advanced}
    sec_list = None
    if section_nos:
        sec_list = [s.strip() for s in section_nos.split(",") if s.strip()]
    elif section_no:
        sec_list = [section_no]
    if not sec_list:
        raise HTTPException(400, "请至少选择一个章节")
    selected = select_homework_problems(section_nos=sec_list, counts=counts)
    return {"section_nos": sec_list, "section_no": ",".join(sec_list),
            "counts": counts, "selected": len(selected), "problems": selected}


# ----- 自动批改 -----

def _auto_detect_ptype(content_text: str, problem_no: str) -> str:
    """从题目文本自动检测题型（证明 vs 计算）"""
    if not content_text:
        return "calc"
    proof_kw = ["证明", "求证", "证：", "试证", "证明：", "求证："]
    for kw in proof_kw:
        if kw in content_text:
            return "proof"
    return "calc"


def _parse_grading_steps(raw: str):
    """解析 grading_steps JSON 为 StepRule 列表"""
    from grading_engine import StepRule
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [StepRule(**item) for item in data]
        # 如果是 dict（如 proof 格式），返回空列表由 grade_proof 用 keywords 处理
        return []
    except Exception:
        return []


def _parse_keywords(raw: str):
    """从 grading_steps JSON dict 中提取 proof keywords"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data.get("keywords", [])
    except Exception:
        pass
    return []


def _vision_grade_submission(submission_id: str):
    """Match whole-work pages to homework questions and grade verified answers."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    sub = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
    if not sub:
        conn.close()
        raise HTTPException(404, "submission not found")
    hw = conn.execute("SELECT * FROM homeworks WHERE id=?", (sub["homework_id"],)).fetchone()
    image_rows = conn.execute(
        "SELECT sort_order,image_path FROM submission_source_images WHERE submission_id=? ORDER BY sort_order",
        (submission_id,)).fetchall()
    if not image_rows:
        conn.close()
        return None
    pids, points_map = normalize_homework_items(
        json.loads(hw["problem_ids"] or "[]"), json.loads(hw["points_map"] or "{}"))
    placeholders = ",".join("?" * len(pids))
    rows = conn.execute(f"""
        SELECT p.id,p.problem_no,p.sub_no,p.ptype,p.content_text,p.std_answer,
               p.full_solution,p.answer_status,p.answer_invalid_reason
        FROM problems p WHERE p.id IN ({placeholders})
    """, pids).fetchall() if pids else []
    by_id = {row["id"]: dict(row) for row in rows}
    ready_problems, blocked_results = [], []
    for pid in pids:
        problem = by_id.get(pid, {})
        max_score = float(points_map.get(pid, 10) or 10)
        no = str(problem.get("problem_no", "")) + (f"({problem.get('sub_no')})" if problem.get("sub_no") else "")
        ready, reason = grading_ready(problem)
        if not ready:
            blocked_results.append({
                "problem_id": pid, "problem_no": no, "ptype": problem.get("ptype", "calc"),
                "std_answer": problem.get("std_answer") or "", "score": 0, "max_score": max_score,
                "correct": None, "confidence": 0, "feedback": reason, "need_review": True,
                "recognized_work": "", "matched_image_indices": [], "step_scores": [],
                "risks": ["答案库尚未达到自动评分条件"],
                "detail": {"reason": "answer_library_not_ready", "vision": True},
            })
        else:
            ready_problems.append({
                "problem_id": pid, "problem_no": no, "problem_text": problem.get("content_text") or "",
                "std_answer": problem.get("std_answer") or "", "full_solution": problem.get("full_solution") or "",
                "max_score": max_score,
            })
    image_payload = []
    for row in image_rows:
        disk = os.path.join(os.path.dirname(__file__), row["image_path"])
        image_payload.append(base64.b64encode(open(disk, "rb").read()).decode("ascii"))
    ai_results = []
    if ready_problems:
        url = os.environ.get("MATH_VLM_URL", "http://127.0.0.1:18080").rstrip("/") + "/grade-homework"
        request = urllib.request.Request(
            url, data=json.dumps({"images_base64": image_payload, "problems": ready_problems}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=1800) as response:
                ai_results = json.loads(response.read().decode("utf-8")).get("results", [])
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            conn.close()
            raise HTTPException(502, "vision grading failed: " + body[:500])
    ready_by_id = {item["problem_id"]: item for item in ready_problems}
    for item in ai_results:
        source = ready_by_id.get(item.get("problem_id"), {})
        item["std_answer"] = source.get("std_answer", "")
        item["ptype"] = by_id.get(item.get("problem_id"), {}).get("ptype", "calc")
    all_by_id = {item["problem_id"]: item for item in blocked_results + ai_results}
    results = [all_by_id[pid] for pid in pids if pid in all_by_id]
    total = round(sum(float(item.get("score", 0) or 0) for item in results), 1)
    max_total = sum(float(item.get("max_score", 0) or 0) for item in results)
    review_count = sum(1 for item in results if item.get("need_review"))
    answers = []
    for item in results:
        matched = item.get("matched_image_indices") or []
        image_url = ""
        if matched and 1 <= int(matched[0]) <= len(image_rows):
            image_url = "/api/images/" + image_rows[int(matched[0]) - 1]["image_path"]
        answers.append({"problem_id": item["problem_id"], "text": item.get("recognized_work", ""), "image": image_url})
    conn.execute("UPDATE submissions SET status=?,score=?,answers=?,grade_detail=? WHERE id=?",
                 ("review_required" if review_count else "graded", total,
                  json.dumps(answers, ensure_ascii=False), json.dumps(results, ensure_ascii=False), submission_id))
    conn.commit()
    conn.close()
    return {"submission_id": submission_id, "total_score": total, "max_score": max_total,
            "need_review_count": review_count, "auto_graded": True, "vision_graded": True, "results": results}


@app.post("/grade/auto")
def auto_grade_submission(submission_id: str = Query(...)):
    """对指定提交执行自动批改（计算题自动判分，证明题给建议分）
    
    核心改进（P0）：
    1. 从 DB 读取 std_answer / grading_steps / answer_weight
    2. 自动检测证明题类型（从 content_text 匹配"证明"/"求证"等关键词）
    3. 构建真实 ProblemSpec 传入 grading_engine
    """
    vision_result = _vision_grade_submission(submission_id)
    if vision_result is not None:
        return vision_result
    from grading_engine import grade, ProblemSpec, StepRule
    conn = get_db()
    cur = conn.cursor()
    sub = cur.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
    if not sub:
        conn.close()
        raise HTTPException(404, "提交不存在")
    hw = cur.execute("SELECT * FROM homeworks WHERE id=?", (sub["homework_id"],)).fetchone()
    if not hw:
        conn.close()
        raise HTTPException(404, "作业不存在")
    pids, points_map = normalize_homework_items(json.loads(hw["problem_ids"] or "[]"), json.loads(hw["points_map"] or "{}"))
    if not pids:
        conn.close()
        return {"submission_id": submission_id, "total_score": 0, "max_score": 0,
                "need_review_count": 0, "auto_graded": True, "results": []}

    placeholders = ",".join("?" * len(pids))
    probs = cur.execute(
        f"SELECT p.id,p.problem_no,p.sub_no,p.ptype,p.difficulty,p.content_text,"
        f"p.knowledge_pts,p.std_answer,p.full_solution,p.grading_steps,p.answer_weight,p.answer_status,p.answer_invalid_reason "
        f"FROM problems p WHERE p.id IN ({placeholders})", pids).fetchall() if pids else []
    prob_by_id = {p["id"]: dict(p) for p in probs}

    answers = json.loads(sub["answers"] or "[]")
    ans_by_prob = {a.get("problem_id", ""): a for a in answers}

    # 检查是否需要 OCR（图片答案）
    from grading_engine import ocr_and_purify
    ocr_results = {}
    for pid_key, ans_obj in ans_by_prob.items():
        text = ans_obj.get("text", "") if isinstance(ans_obj, dict) else str(ans_obj)
        image = ans_obj.get("image", "") if isinstance(ans_obj, dict) else ""
        if image and not text:
            # 有图片但没有文本 → OCR
            ocr_text = ocr_and_purify(image)
            if ocr_text:
                ocr_results[pid_key] = ocr_text
                ans_by_prob[pid_key] = ocr_text
            else:
                ans_by_prob[pid_key] = ""
        else:
            ans_by_prob[pid_key] = text

    results = []
    total_score = 0.0
    max_total = 0.0
    review_count = 0

    for pid in pids:
        p = prob_by_id.get(pid, {})
        if not p:
            results.append({
                "problem_id": pid, "problem_no": "?", "score": 0, "max_score": 0,
                "correct": False, "confidence": 0, "need_review": True,
                "feedback": "题目数据缺失", "detail": {}
            })
            continue

        # 自动检测题型：优先用 DB 中的 ptype，若为 calc 但内容含证明关键词则纠正
        ptype = p.get("ptype", "calc")
        if ptype == "calc":
            detected = _auto_detect_ptype(p.get("content_text", ""), p.get("problem_no", ""))
            if detected == "proof":
                ptype = "proof"

        configured_points = float(points_map.get(pid, 0))

        max_s = 10.0  # 每题满分

        # 构建 ProblemSpec：从 DB 读取真实答案和评分规则
        std_answer = p.get("std_answer") or ""
        max_s = configured_points
        answer_weight = p.get("answer_weight") or 0.6
        steps = _parse_grading_steps(p.get("grading_steps"))
        keywords = _parse_keywords(p.get("grading_steps"))

        student_work = ans_by_prob.get(pid, "")

        # 检查是否有足够信息进行批改
        sub_no = p.get("sub_no", "")
        no_display = (p.get("problem_no", "") or "") + (f"({sub_no})" if sub_no else "")

        ready, blocked_reason = grading_ready(p)
        if not ready:
            results.append({
                "problem_id": pid, "problem_no": no_display, "ptype": ptype,
                "std_answer": "(待核验)", "score": 0, "max_score": max_s,
                "correct": None, "confidence": 0, "need_review": True,
                "feedback": blocked_reason, "detail": {"reason": "not_grading_ready"}
            })
            max_total += max_s
            review_count += 1
            continue

        if ptype == "calc" and not std_answer:
            # 计算题无标准答案 → 标记需人工批改
            results.append({
                "problem_id": pid,
                "problem_no": no_display,
                "ptype": ptype,
                "std_answer": "(未配置)",
                "score": 0, "max_score": max_s,
                "correct": None, "confidence": 0,
                "need_review": True,
                "feedback": "该题尚未配置标准答案，需教师手动批改。",
                "detail": {"reason": "no_std_answer"}
            })
            review_count += 1
            # 不计入总分（不扣分也不加分）
            continue

        spec = ProblemSpec(
            pid=pid, ptype=ptype, max_score=max_s,
            std_answer=std_answer,
            answer_tol=1e-6,
            steps=steps,
            answer_weight=answer_weight,
            keywords_required=keywords,
        )

        try:
            gr = grade(spec, final_answer=student_work, work=student_work)
        except Exception as e:
            # 单题批改异常不阻断整份作业，标记为需复核并记录原因
            results.append({
                "problem_id": pid,
                "problem_no": no_display,
                "ptype": ptype,
                "std_answer": std_answer,
                "score": 0, "max_score": max_s,
                "correct": None, "confidence": 0,
                "need_review": True,
                "feedback": f"自动批改引擎异常：{str(e)[:80]}，请人工复核。",
                "detail": {"engine_error": str(e)}
            })
            review_count += 1
            continue

        results.append({
            "problem_id": pid,
            "problem_no": no_display,
            "ptype": ptype,
            "std_answer": std_answer,
            "score": gr.score, "max_score": gr.max_score,
            "correct": gr.correct, "confidence": gr.confidence,
            "need_review": gr.need_review, "feedback": gr.feedback,
            "detail": gr.detail
        })
        total_score += gr.score
        max_total += gr.max_score
        if gr.need_review:
            review_count += 1

    # 更新提交分数并保存完整批改明细
    final_score = round(total_score, 1)
    final_status = "review_required" if review_count else "graded"
    cur.execute(
        "UPDATE submissions SET status=?, score=?, grade_detail=? WHERE id=?",
        (final_status, final_score, json.dumps(results, ensure_ascii=False), submission_id))
    conn.commit()
    conn.close()

    return {
        "submission_id": submission_id,
        "total_score": final_score,
        "max_score": max_total,
        "need_review_count": review_count,
        "auto_graded": True,
        "results": results
    }


@app.post("/grade/batch")
def batch_auto_grade(homework_id: str = Query(...)):
    """对指定作业的所有待批改提交执行批量自动批改"""
    conn = get_db()
    cur = conn.cursor()
    subs = cur.execute(
        "SELECT id FROM submissions WHERE homework_id=? AND status='pending'",
        (homework_id,)).fetchall()
    conn.close()
    graded = []
    for s in subs:
        try:
            r = auto_grade_submission(submission_id=s["id"])
            graded.append({"submission_id": s["id"], "score": r["total_score"],
                           "need_review": r["need_review_count"] > 0})
        except Exception as e:
            graded.append({"submission_id": s["id"], "error": str(e)})
    return {"homework_id": homework_id, "graded": len(graded), "results": graded}


# ----- 人工复核（覆盖自动批改分数并记录日志） -----
@app.get("/submissions/{submission_id}/grade-detail")
def get_grade_detail(submission_id: str):
    """获取自动批改后的完整试卷明细（含每题学生作答、标准答案、批改结果）"""
    conn = get_db()
    cur = conn.cursor()
    sub = cur.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
    if not sub:
        conn.close()
        raise HTTPException(404, "提交不存在")
    hw = cur.execute("SELECT * FROM homeworks WHERE id=?", (sub["homework_id"],)).fetchone()
    if not hw:
        conn.close()
        raise HTTPException(404, "作业不存在")

    pids, _ = normalize_homework_items(json.loads(hw["problem_ids"] or "[]"), json.loads(hw["points_map"] or "{}"))
    answers = json.loads(sub["answers"] or "[]")
    ans_by_prob = {a.get("problem_id", ""): a for a in answers}
    grade_detail = json.loads(sub["grade_detail"] or "[]")
    review_log = json.loads(sub["review_log"] or "[]")

    placeholders = ",".join("?" * len(pids)) if pids else "''"
    probs = cur.execute(
        f"SELECT id,problem_no,sub_no,ptype,difficulty,content_text,"
        f"knowledge_pts,std_answer,full_solution,crop_image_path,answer_status,answer_invalid_reason FROM problems "
        f"WHERE id IN ({placeholders})", pids).fetchall() if pids else []
    prob_by_id = {p["id"]: dict(p) for p in probs}
    conn.close()

    # 用当前题目信息补全/刷新 grade_detail 中的静态字段
    enriched = []
    for item in grade_detail:
        pid = item.get("problem_id")
        p = prob_by_id.get(pid, {})
        ans_obj = ans_by_prob.get(pid, {})
        student_answer = ans_obj.get("text", "") if isinstance(ans_obj, dict) else str(ans_obj)
        ans_img = ans_obj.get("image", "") if isinstance(ans_obj, dict) else ""
        enriched.append({
            **item,
            "content_text": p.get("content_text", ""),
            "crop_image_path": p.get("crop_image_path", ""),
            "difficulty": p.get("difficulty", 3),
            "knowledge_pts": p.get("knowledge_pts", ""),
            "std_answer": p.get("std_answer", ""),
            "full_solution": p.get("full_solution", ""),
            "answer_status": p.get("answer_status", "unverified"),
            "answer_invalid_reason": p.get("answer_invalid_reason", ""),
            "student_answer": student_answer,
            "answer_image": ans_img,
        })

    return {
        "submission_id": submission_id,
        "homework_id": sub["homework_id"],
        "student_no": sub["student_no"],
        "student_name": sub["student_name"],
        "submitted_at": sub["submitted_at"],
        "total_score": sub["score"],
        "max_score": sum(it.get("max_score", 10) for it in enriched),
        "status": sub["status"],
        "review_log": review_log,
        "problems": enriched
    }


class ScoreUpdateReq(BaseModel):
    score: float
    feedback: Optional[str] = None


@app.patch("/submissions/{submission_id}/problems/{problem_id}")
def update_problem_score(submission_id: str, problem_id: str, req: ScoreUpdateReq):
    """教师人工修改某题分数，覆盖自动批改结果并记录日志"""
    if req.score < 0:
        raise HTTPException(400, "分数不能小于 0")
    conn = get_db()
    cur = conn.cursor()
    sub = cur.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
    if not sub:
        conn.close()
        raise HTTPException(404, "提交不存在")

    grade_detail = json.loads(sub["grade_detail"] or "[]")
    target = None
    for it in grade_detail:
        if it.get("problem_id") == problem_id:
            target = it
            break
    if target is None:
        conn.close()
        raise HTTPException(404, "该试卷中未找到此题")

    old_score = target.get("score", 0)
    max_score = target.get("max_score", 10)
    if req.score > max_score:
        conn.close()
        raise HTTPException(400, f"分数不能超过该题满分 {max_score}")

    target["score"] = round(req.score, 1)
    target["correct"] = (abs(req.score - max_score) < 1e-6)
    target["need_review"] = False
    target["feedback"] = (req.feedback or target.get("feedback", "")) + " [人工复核修正]"

    # 重新计算总分
    new_total = round(sum(it.get("score", 0) for it in grade_detail), 1)

    # 记录复核日志
    review_log = json.loads(sub["review_log"] or "[]")
    review_log.append({
        "time": datetime.now().isoformat(timespec="seconds"),
        "problem_id": problem_id,
        "problem_no": target.get("problem_no", ""),
        "old_score": old_score,
        "new_score": target["score"],
        "feedback": req.feedback or ""
    })

    cur.execute(
        "UPDATE submissions SET score=?, grade_detail=?, review_log=? WHERE id=?",
        (new_total, json.dumps(grade_detail, ensure_ascii=False),
         json.dumps(review_log, ensure_ascii=False), submission_id))
    conn.commit()
    conn.close()
    return {
        "submission_id": submission_id,
        "problem_id": problem_id,
        "old_score": old_score,
        "new_score": target["score"],
        "total_score": new_total,
        "max_score": sum(it.get("max_score", 10) for it in grade_detail),
        "log_count": len(review_log)
    }


@app.get("/submissions/{submission_id}/review-log")
def get_review_log(submission_id: str):
    """获取某份提交的分数修改日志"""
    conn = get_db()
    sub = conn.execute("SELECT review_log FROM submissions WHERE id=?", (submission_id,)).fetchone()
    conn.close()
    if not sub:
        raise HTTPException(404, "提交不存在")
    return {"submission_id": submission_id, "review_log": json.loads(sub["review_log"] or "[]")}


# ----- 教师答案管理 -----
class AnswerUpdateReq(BaseModel):
    std_answer: Optional[str] = None
    full_solution: Optional[str] = None  # 完整推导过程（供学生端展示）
    grading_steps: Optional[str] = None  # JSON string
    answer_weight: Optional[float] = None
    ptype: Optional[str] = None
    answer_status: Optional[str] = None  # unverified | verified | rejected


class ContentUpdateReq(BaseModel):
    content_text: str  # VLM 识别并经验证后写回的题干


@app.put("/problems/{pid}/answer")
def update_problem_answer(pid: str, req: AnswerUpdateReq):
    """教师配置/更新题目的标准答案和评分规则"""
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute("SELECT id FROM problems WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "题目不存在")

    updates = []
    args = []
    if req.std_answer is not None:
        updates.append("std_answer=?")
        args.append(req.std_answer)
    if req.full_solution is not None:
        updates.append("full_solution=?")
        args.append(req.full_solution)
    if req.grading_steps is not None:
        updates.append("grading_steps=?")
        args.append(req.grading_steps)
    if req.answer_weight is not None:
        updates.append("answer_weight=?")
        args.append(req.answer_weight)
    if req.ptype is not None:
        updates.append("ptype=?")
        args.append(req.ptype)
    if req.answer_status is not None:
        if req.answer_status not in {"unverified", "verified", "rejected"}:
            conn.close()
            raise HTTPException(400, "无效的答案状态")
        candidate_answer = req.std_answer
        if candidate_answer is None:
            candidate_answer = cur.execute("SELECT std_answer FROM problems WHERE id=?", (pid,)).fetchone()[0]
        candidate_type = req.ptype
        if candidate_type is None:
            candidate_type = cur.execute("SELECT ptype FROM problems WHERE id=?", (pid,)).fetchone()[0]
        issue = answer_quality_issue(candidate_answer, candidate_type, req.full_solution or "")
        if req.answer_status == "verified" and issue:
            conn.close()
            raise HTTPException(400, "不能核验此答案：" + issue)
        updates.append("answer_status=?")
        args.append(req.answer_status)
        updates.append("answer_invalid_reason=?")
        args.append(issue if req.answer_status != "verified" else "")

    if updates:
        args.append(pid)
        cur.execute(f"UPDATE problems SET {','.join(updates)} WHERE id=?", args)
        conn.commit()
    conn.close()
    return {"ok": True, "id": pid}


@app.put("/problems/{pid}/content")
def update_problem_content(pid: str, req: ContentUpdateReq):
    """教师 / 智能体将 VLM 识别出的题干写回（OCR 补全闭环）。"""
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute("SELECT id FROM problems WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "题目不存在")
    text = (req.content_text or "").strip()
    if len(text) < 1:
        conn.close()
        raise HTTPException(400, "题干内容不能为空")
    cur.execute("UPDATE problems SET content_text=? WHERE id=?", (text, pid))
    conn.commit()
    conn.close()
    return {"ok": True, "id": pid}


# ----- 人工复核抽样 -----
class AnswerCandidateReviewReq(BaseModel):
    action: str  # approved | rejected
    content_text: Optional[str] = None
    std_answer: Optional[str] = None
    full_solution: Optional[str] = None
    ptype: Optional[str] = None
    note: str = ""


class CandidateSourceUpdateReq(BaseModel):
    problem_text: Optional[str] = None
    image_base64: Optional[str] = None
    images_base64: list[str] = []
    append_images: bool = False
    subquestion_count: Optional[int] = None
    filename: str = ""


class SectionAnswerBatchReq(BaseModel):
    section_no: str
    images_base64: list[str]
    filename: str = "section-answer-upload"
    only_unverified: bool = True
    replace_existing_sources: bool = False


class CandidateBatchApproveReq(BaseModel):
    candidate_ids: list[int] = []
    min_confidence: float = 0.92


class AnswerDocumentStartReq(BaseModel):
    filename: str
    file_size: int
    volume: str = ""


class AnswerDocumentChunkReq(BaseModel):
    index: int
    data_base64: str


class AnchorReviewReq(BaseModel):
    reference_only: bool = False


class ManualAnchorFixReq(BaseModel):
    problem_id: Optional[str] = None
    x_ratio: Optional[float] = None
    y_ratio: Optional[float] = None
    width_ratio: Optional[float] = None
    height_ratio: Optional[float] = None
    image_base64: Optional[str] = None
    images_base64: list[str] = []
    problem_image_base64: Optional[str] = None


class AnchorProblemImageReq(BaseModel):
    image_base64: str


class AnchorAdoptReq(BaseModel):
    """Teacher's explicit adoption of the current PDF-anchor candidate."""
    std_answer: Optional[str] = None
    full_solution: Optional[str] = None


class AnchorSubquestionCountReq(BaseModel):
    count: int


class AdoptUnmatchedAnswerAnchorsReq(BaseModel):
    auto_extract: bool = True


def _answer_document_root() -> str:
    root = os.path.join(os.path.dirname(__file__), "answer_documents")
    os.makedirs(root, exist_ok=True)
    return root


def _ocr_python() -> str:
    configured = os.environ.get("PIX2TEXT_PYTHON", sys.executable)
    return configured if os.path.isfile(configured) else sys.executable


def _problem_text_from_answer_anchor(raw_text: str) -> str:
    """Keep the question portion of an answer-book block for a new draft problem."""
    lines = [line.strip() for line in (raw_text or "").splitlines() if line.strip()]
    kept = []
    for index, line in enumerate(lines):
        # In this answer book, a standalone 解/证明 after the question starts the solution.
        if index and len("\n".join(kept)) >= 12 and re.match(r"^(?:解|证明)\s*[：:（(]?$", line):
            break
        kept.append(line)
    return "\n".join(kept)[:8000]


def _ptype_from_answer_anchor(problem_text: str) -> str:
    head = (problem_text or "")[:500]
    return "proof" if any(token in head for token in ("证明", "试证", "证：", "证:")) else "calc"


@app.post("/answer-documents/upload/start")
def start_answer_document_upload(req: AnswerDocumentStartReq):
    if not req.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "请选择 PDF 答案书")
    if req.file_size <= 0 or req.file_size > 1024 * 1024 * 1024:
        raise HTTPException(413, "PDF 大小必须在 1GB 以内")
    document_id = uuid.uuid4().hex
    stored = os.path.join(_answer_document_root(), f"{document_id}.pdf.part")
    open(stored, "wb").close()
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_db()
    conn.execute("""
        INSERT INTO answer_documents(id,filename,stored_path,file_size,volume,status,created_at,updated_at)
        VALUES(?,?,?,?,?,'uploading',?,?)
    """, (document_id, os.path.basename(req.filename), stored, req.file_size, req.volume.strip(), now, now))
    conn.commit(); conn.close()
    return {"id": document_id, "chunk_size": 4 * 1024 * 1024}


@app.post("/answer-documents/{document_id}/upload-chunk")
def upload_answer_document_chunk(document_id: str, req: AnswerDocumentChunkReq):
    conn = get_db()
    row = conn.execute("SELECT * FROM answer_documents WHERE id=?", (document_id,)).fetchone()
    if not row or row["status"] != "uploading":
        conn.close(); raise HTTPException(404, "上传任务不存在或已结束")
    try:
        payload = base64.b64decode(req.data_base64, validate=True)
    except Exception as exc:
        conn.close(); raise HTTPException(400, "分块数据无效") from exc
    if len(payload) > 5 * 1024 * 1024:
        conn.close(); raise HTTPException(413, "分块过大")
    expected_offset = req.index * 4 * 1024 * 1024
    current_size = os.path.getsize(row["stored_path"])
    if current_size != expected_offset:
        conn.close(); raise HTTPException(409, f"分块顺序错误，服务器已有 {current_size} 字节")
    with open(row["stored_path"], "ab") as fh:
        fh.write(payload)
    conn.execute("UPDATE answer_documents SET updated_at=? WHERE id=?",
                 (datetime.now().isoformat(timespec="seconds"), document_id))
    conn.commit(); conn.close()
    return {"ok": True, "received": len(payload), "uploaded": current_size + len(payload)}


@app.post("/answer-documents/{document_id}/upload-complete")
def complete_answer_document_upload(document_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM answer_documents WHERE id=?", (document_id,)).fetchone()
    if not row:
        conn.close(); raise HTTPException(404, "上传任务不存在")
    part_path = row["stored_path"]
    if os.path.getsize(part_path) != row["file_size"]:
        conn.close(); raise HTTPException(409, "PDF 尚未完整上传")
    with open(part_path, "rb") as fh:
        if fh.read(5) != b"%PDF-":
            conn.close(); raise HTTPException(400, "文件不是有效 PDF")
    final_path = part_path[:-5] if part_path.endswith(".part") else part_path + ".pdf"
    try:
        helper = os.path.join(os.path.dirname(__file__), "answer_pdf_render_helper.py")
        page_count = int(subprocess.check_output(
            [_ocr_python(), helper, "--pdf", part_path, "--count"],
            cwd=os.path.dirname(__file__), timeout=120, text=True).strip())
    except Exception as exc:
        conn.close(); raise HTTPException(400, "PDF 无法打开") from exc
    os.replace(part_path, final_path)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("""
        UPDATE answer_documents SET stored_path=?,page_count=?,status='ready',
            index_progress=0,index_message='等待建立题号索引',updated_at=? WHERE id=?
    """, (final_path, page_count, now, document_id))
    conn.commit(); conn.close()
    return {"ok": True, "id": document_id, "page_count": page_count}


@app.get("/answer-documents")
def list_answer_documents():
    conn = get_db()
    rows = [dict(row) for row in conn.execute("""
        SELECT d.*,
          (SELECT COUNT(*) FROM answer_page_anchors a WHERE a.document_id=d.id) AS anchor_count,
          (SELECT COUNT(*) FROM answer_page_anchors a WHERE a.document_id=d.id AND a.problem_id<>'') AS matched_count,
          (SELECT COUNT(*) FROM answer_page_anchors a WHERE a.document_id=d.id AND a.problem_id='') AS unmatched_count,
          (SELECT COUNT(*) FROM answer_page_anchors a WHERE a.document_id=d.id AND a.comparison_status IN ('conflict','low_confidence','extraction_failed')) AS conflict_count
        FROM answer_documents d ORDER BY d.created_at DESC
    """).fetchall()]
    conn.close(); return rows


@app.post("/answer-documents/{document_id}/adopt-unmatched")
def adopt_unmatched_answer_document_anchors(document_id: str, req: AdoptUnmatchedAnswerAnchorsReq):
    """Create draft problems for answer-book blocks absent from the current question bank.

    This preserves the original crop and deliberately leaves every imported
    answer unverified until the existing GPU extraction/review flow evaluates it.
    """
    conn = get_db(); conn.row_factory = sqlite3.Row
    document = conn.execute("SELECT * FROM answer_documents WHERE id=?", (document_id,)).fetchone()
    if not document:
        conn.close(); raise HTTPException(404, "答案 PDF 不存在")
    anchors = conn.execute("""
        SELECT * FROM answer_page_anchors
        WHERE document_id=? AND problem_id=''
        ORDER BY section_no,CAST(problem_no AS INTEGER),page_no,id
    """, (document_id,)).fetchall()
    if not anchors:
        conn.close(); return {"ok": True, "created": 0, "linked": 0, "skipped": 0, "extraction": None}
    textbook = conn.execute("SELECT textbook_id FROM sections WHERE textbook_id IS NOT NULL LIMIT 1").fetchone()
    textbook_id = textbook["textbook_id"] if textbook else None
    created = linked = skipped = 0
    now = datetime.now().isoformat(timespec="seconds")
    for anchor in anchors:
        section_no = (anchor["section_no"] or "").strip()
        problem_no = (anchor["problem_no"] or "").strip()
        if not section_no or not problem_no:
            skipped += 1
            continue
        section = conn.execute("SELECT id FROM sections WHERE section_no=?", (section_no,)).fetchone()
        if not section:
            section_id = str(uuid.uuid4())
            conn.execute("INSERT INTO sections(id,textbook_id,section_no,title) VALUES(?,?,?,?)",
                         (section_id, textbook_id, section_no, f"习题 {section_no}"))
        else:
            section_id = section["id"]
        existing = conn.execute("""
            SELECT id FROM problems WHERE section_id=? AND problem_no=?
            AND (sub_no IS NULL OR sub_no='') ORDER BY id
        """, (section_id, problem_no)).fetchall()
        if len(existing) == 1:
            problem_id = existing[0]["id"]
            linked += 1
        elif len(existing) > 1:
            # Preserve an ambiguous legacy key for teacher review rather than
            # silently attaching an answer block to one of several questions.
            skipped += 1
            continue
        else:
            problem_id = str(uuid.uuid4())
            content_text = _problem_text_from_answer_anchor(anchor["ocr_text"] or "")
            conn.execute("""
                INSERT INTO problems(id,section_id,exercise_set,problem_no,sub_no,ptype,
                  crop_image_path,content_text,difficulty,knowledge_pts,extract_status,
                  answer_status,answer_invalid_reason)
                VALUES(?,?,?,?,NULL,?,?,?,3,'','answer_pdf_pending','unverified','等待答案书识别与教师核验')
            """, (problem_id, section_id, section_no, problem_no,
                  _ptype_from_answer_anchor(content_text), None, content_text))
            created += 1
        conn.execute("""
            UPDATE answer_page_anchors
            SET problem_id=?,comparison_status=?,extraction_status='detected'
            WHERE id=?
        """, (problem_id, "not_run", anchor["id"]))
    conn.commit(); conn.close()
    extraction = extract_all_answer_document_anchors(document_id) if req.auto_extract and (created or linked) else None
    return {"ok": True, "created": created, "linked": linked, "skipped": skipped, "extraction": extraction}


@app.get("/answer-documents/{document_id}/pages/{page_no}")
def answer_document_page(document_id: str, page_no: int, width: int = Query(1400, ge=500, le=2600)):
    conn = get_db(); row = conn.execute("SELECT * FROM answer_documents WHERE id=?", (document_id,)).fetchone(); conn.close()
    if not row or not 1 <= page_no <= row["page_count"]:
        raise HTTPException(404, "页码不存在")
    preview_dir = os.path.join(_answer_document_root(), "page_previews", document_id)
    os.makedirs(preview_dir, exist_ok=True)
    output = os.path.join(preview_dir, f"page-{page_no}-w{width}.jpg")
    if not os.path.isfile(output):
        helper = os.path.join(os.path.dirname(__file__), "answer_pdf_render_helper.py")
        subprocess.run([_ocr_python(), helper, "--pdf", row["stored_path"], "--page", str(page_no),
                        "--width", str(width), "--output", output], cwd=os.path.dirname(__file__),
                       check=True, timeout=180)
    return FileResponse(output, media_type="image/jpeg", filename=f"page-{page_no}.jpg")


@app.post("/answer-documents/{document_id}/index")
def start_answer_document_index(document_id: str):
    conn = get_db(); row = conn.execute("SELECT * FROM answer_documents WHERE id=?", (document_id,)).fetchone()
    if not row:
        conn.close(); raise HTTPException(404, "答案 PDF 不存在")
    if row["status"] == "indexing":
        conn.close(); return {"ok": True, "status": "indexing"}
    conn.execute("UPDATE answer_documents SET status='indexing',index_progress=0,index_message='正在启动 OCR 题号索引' WHERE id=?", (document_id,))
    conn.commit(); conn.close()
    worker = os.path.join(os.path.dirname(__file__), "answer_pdf_pipeline_worker.py")
    ocr_python = _ocr_python()
    def launch():
        try:
            subprocess.run([ocr_python, worker, "--db", os.path.abspath(DB), "--document-id", document_id],
                           cwd=os.path.dirname(__file__), check=True, timeout=24 * 3600)
        except Exception as exc:
            fail = get_db(); fail.execute("UPDATE answer_documents SET status='failed',index_message=?,updated_at=? WHERE id=?",
                (str(exc)[:1000], datetime.now().isoformat(timespec="seconds"), document_id)); fail.commit(); fail.close()
    threading.Thread(target=launch, daemon=True).start()
    return {"ok": True, "status": "indexing"}


@app.get("/answer-documents/{document_id}/anchors")
def list_answer_document_anchors(document_id: str, conflicts_only: bool = False,
                                 page_no: Optional[int] = None, limit: int = Query(500, ge=1, le=2000)):
    conn = get_db()
    query = """
      SELECT a.*,p.content_text,p.ptype,p.std_answer AS current_std_answer,
             p.answer_status,s.title AS section_title
      FROM answer_page_anchors a
      LEFT JOIN problems p ON p.id=a.problem_id
      LEFT JOIN sections s ON s.id=p.section_id
      WHERE a.document_id=?
    """
    args = [document_id]
    if conflicts_only:
        # "needs_teacher" has a usable candidate but still needs the teacher to
        # confirm it.  It must remain visible in the pending queue; only an
        # explicitly adopted answer is removed from that queue.
        query += " AND (a.problem_id='' OR a.comparison_status IN ('not_run','conflict','low_confidence','unmatched','extraction_failed','needs_teacher'))"
    if page_no is not None:
        query += " AND a.page_no=?"; args.append(page_no)
    query += " ORDER BY a.page_no,CAST(a.problem_no AS INTEGER),a.id LIMIT ?"; args.append(limit)
    rows = [dict(row) for row in conn.execute(query, args).fetchall()]
    conn.close(); return rows


def _retry_group_for_anchor(anchor: dict) -> str:
    """Only retry failures that have enough matched question text to cross-check."""
    if anchor.get("comparison_status") != "extraction_failed":
        return "not_failed"
    # A real GPU attempt already failed. Do not make the teacher click the
    # same retry forever; retain its crop and route it to manual review.
    if (anchor.get("ai_review_status") or "") == "failed":
        return "manual"
    if (anchor.get("ptype") or "calc") != "calc":
        return "manual"
    if len((anchor.get("content_text") or "").strip()) < 12:
        return "manual"
    if not anchor.get("crop_path") or not os.path.isfile(anchor["crop_path"]):
        return "manual"
    return "retry"


@app.get("/answer-documents/{document_id}/retry-groups")
def answer_document_retry_groups(document_id: str):
    conn = get_db(); conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute("""
      SELECT a.*,p.ptype,p.content_text,c.ai_review_status FROM answer_page_anchors a
      LEFT JOIN problems p ON p.id=a.problem_id
      LEFT JOIN answer_import_candidates c ON c.id=a.candidate_id
      WHERE a.document_id=?
    """, (document_id,)).fetchall()]
    conn.close()
    failed = [row for row in rows if row["comparison_status"] == "extraction_failed"]
    retry = [row for row in failed if _retry_group_for_anchor(row) == "retry"]
    manual = [row for row in failed if _retry_group_for_anchor(row) == "manual"]
    return {
        "failed_total": len(failed), "retry_count": len(retry), "manual_count": len(manual),
        "retry_ids": [row["id"] for row in retry],
        "manual_ids": [row["id"] for row in manual],
        "manual_reason": "题目正文缺失或裁切内容不足，无法可靠独立判等",
    }


@app.post("/answer-documents/{document_id}/retry-auto")
def retry_answer_document_auto_group(document_id: str):
    groups = answer_document_retry_groups(document_id)
    ids = groups["retry_ids"]
    if not ids:
        return {**groups, "started": 0}
    conn = get_db()
    document = conn.execute("SELECT status FROM answer_documents WHERE id=?", (document_id,)).fetchone()
    if not document:
        conn.close(); raise HTTPException(404, "答案 PDF 不存在")
    if document["status"] == "extracting":
        conn.close(); return {**groups, "started": 0, "status": "extracting"}
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"UPDATE answer_page_anchors SET extraction_status='detected',comparison_status='not_run' WHERE id IN ({placeholders})", ids)
    conn.execute("UPDATE answer_documents SET status='extracting',index_progress=0,index_message=?,updated_at=? WHERE id=?",
                 (f"正在自动重试 0/{len(ids)} 道（可继续使用工作台）",
                  datetime.now().isoformat(timespec="seconds"), document_id))
    conn.commit(); conn.close()
    def run_retries():
        completed = failed = 0
        for index, anchor_id in enumerate(ids, 1):
            try:
                extract_answer_document_anchor(anchor_id, AnchorReviewReq(reference_only=False))
                completed += 1
            except Exception:
                failed += 1
            progress = int(index * 100 / max(1, len(ids)))
            update = get_db()
            update.execute("UPDATE answer_documents SET index_progress=?,index_message=?,updated_at=? WHERE id=?",
                           (progress, f"自动重试 {index}/{len(ids)}；成功 {completed}，仍失败 {failed}",
                            datetime.now().isoformat(timespec="seconds"), document_id))
            update.commit(); update.close()
        finish = get_db()
        finish.execute("UPDATE answer_documents SET status='indexed',index_progress=100,index_message=?,updated_at=? WHERE id=?",
                       (f"自动重试完成：成功 {completed}，仍失败 {failed}；其余题请在失败项分组中人工看图",
                        datetime.now().isoformat(timespec="seconds"), document_id))
        finish.commit(); finish.close()
    threading.Thread(target=run_retries, daemon=True).start()
    return {**groups, "started": len(ids), "status": "extracting"}


@app.post("/answer-documents/{document_id}/extract-all")
def extract_all_answer_document_anchors(document_id: str):
    conn = get_db(); document = conn.execute("SELECT * FROM answer_documents WHERE id=?", (document_id,)).fetchone()
    if not document:
        conn.close(); raise HTTPException(404, "答案 PDF 不存在")
    if document["status"] == "extracting":
        conn.close(); return {"ok": True, "status": "extracting"}
    total = conn.execute("""
        SELECT COUNT(*) FROM answer_page_anchors
        WHERE document_id=? AND problem_id<>'' AND extraction_status<>'completed'
    """, (document_id,)).fetchone()[0]
    conn.execute("UPDATE answer_documents SET status='extracting',index_progress=0,index_message=? WHERE id=?",
                 (f"准备自动提取 {total} 个单题答案块", document_id))
    conn.commit(); conn.close()
    def run_all():
        completed = failed = 0
        work = get_db(); work.row_factory = sqlite3.Row
        anchors = work.execute("""
            SELECT a.id,p.ptype FROM answer_page_anchors a JOIN problems p ON p.id=a.problem_id
            WHERE a.document_id=? AND a.problem_id<>'' AND a.extraction_status<>'completed'
            ORDER BY a.page_no,CAST(a.problem_no AS INTEGER),a.id
        """, (document_id,)).fetchall(); work.close()
        for index, anchor in enumerate(anchors, 1):
            try:
                extract_answer_document_anchor(anchor["id"], AnchorReviewReq(reference_only=anchor["ptype"] == "proof"))
                completed += 1
            except Exception:
                failed += 1
            progress = int(index * 100 / max(1, len(anchors)))
            update = get_db(); update.execute("UPDATE answer_documents SET index_progress=?,index_message=?,updated_at=? WHERE id=?",
                (progress, f"自动提取 {index}/{len(anchors)}；成功 {completed}，需重试 {failed}",
                 datetime.now().isoformat(timespec="seconds"), document_id)); update.commit(); update.close()
        finish = get_db(); finish.execute("UPDATE answer_documents SET status='indexed',index_progress=100,index_message=?,updated_at=? WHERE id=?",
            (f"提取完成：成功 {completed}，需重试 {failed}；页面默认仅显示冲突和低置信度题",
             datetime.now().isoformat(timespec="seconds"), document_id)); finish.commit(); finish.close()
    threading.Thread(target=run_all, daemon=True).start()
    return {"ok": True, "status": "extracting", "total": total}


@app.post("/answer-documents/{document_id}/recheck-comparisons")
def recheck_answer_document_comparisons(document_id: str):
    """Re-run local comparison on existing VLM output without calling Qwen.

    This is deliberately non-mutating for the formal answer bank: it only
    refreshes the judgement shown in the PDF workflow after parser upgrades.
    """
    conn = get_db(); conn.row_factory = sqlite3.Row
    rows = conn.execute("""
      SELECT a.*,p.content_text,p.ptype,p.std_answer,p.full_solution,s.section_no
      FROM answer_page_anchors a LEFT JOIN problems p ON p.id=a.problem_id
      LEFT JOIN sections s ON s.id=p.section_id
      WHERE a.document_id=? AND a.extraction_status='completed' AND a.problem_id<>''
    """, (document_id,)).fetchall()
    if not rows:
        conn.close(); return {"checked": 0, "updated": 0, "skipped": 0}
    updated = skipped = 0
    for anchor in rows:
        try:
            payload = json.loads(anchor["comparison_json"] or "{}")
        except Exception:
            payload = {}
        ai = payload.get("ai") or {}
        if not str(ai.get("std_answer") or "").strip() or anchor["ptype"] == "proof":
            skipped += 1; continue
        independent = (payload.get("comparison") or {}).get("independent_solve")
        comparison = _compare_anchor_candidate(conn, anchor["id"], ai, independent)
        expected = int(anchor["teacher_subquestion_count"] or 0)
        returned = len(ai.get("sub_answers") or [])
        if expected and returned != expected:
            comparison = {"status": "low_confidence",
                          "reason": f"教师指定 {expected} 个小问，但本次只返回 {returned} 个；禁止入库",
                          "expected_subquestion_count": expected,
                          "returned_subquestion_count": returned}
        payload["comparison"] = comparison
        conn.execute("UPDATE answer_page_anchors SET comparison_status=?,comparison_json=? WHERE id=?",
                     (comparison["status"], json.dumps(payload, ensure_ascii=False), anchor["id"]))
        updated += 1
    conn.commit(); conn.close()
    return {"checked": len(rows), "updated": updated, "skipped": skipped}


@app.get("/answer-document-crops/{anchor_id}")
def answer_document_crop(anchor_id: int):
    conn = get_db(); row = conn.execute("SELECT crop_path FROM answer_page_anchors WHERE id=?", (anchor_id,)).fetchone(); conn.close()
    if not row or not row["crop_path"] or not os.path.isfile(row["crop_path"]):
        raise HTTPException(404, "答案裁切图不存在")
    return FileResponse(row["crop_path"], media_type="image/jpeg")


@app.post("/answer-document-anchors/{anchor_id}/manual-fix")
def manually_fix_answer_document_anchor(anchor_id: int, req: ManualAnchorFixReq):
    """Replace a bad PDF crop and optionally relink it to an existing problem.

    This is deliberately evidence-only: it resets recognition and never writes a
    standard answer to ``problems``.
    """
    conn = get_db(); conn.row_factory = sqlite3.Row
    anchor = conn.execute("SELECT * FROM answer_page_anchors WHERE id=?", (anchor_id,)).fetchone()
    if not anchor:
        conn.close(); raise HTTPException(404, "答案锚点不存在")
    document = conn.execute("SELECT * FROM answer_documents WHERE id=?", (anchor["document_id"],)).fetchone()
    if not document:
        conn.close(); raise HTTPException(404, "答案 PDF 不存在")
    problem_id = (req.problem_id or anchor["problem_id"] or "").strip()
    if problem_id:
        problem = conn.execute("SELECT p.id,s.section_no,p.problem_no,p.sub_no FROM problems p JOIN sections s ON s.id=p.section_id WHERE p.id=?", (problem_id,)).fetchone()
        if not problem:
            conn.close(); raise HTTPException(404, "要绑定的题库题目不存在")
    else:
        problem = None
    if req.problem_image_base64:
        if not problem_id:
            conn.close(); raise HTTPException(400, "请先绑定题库题目；题目图片将附到该题目记录")
        try:
            problem_raw = base64.b64decode(req.problem_image_base64.split(",", 1)[-1], validate=True)
            problem_image = Image.open(io.BytesIO(problem_raw)).convert("RGB")
        except Exception as exc:
            conn.close(); raise HTTPException(400, "上传的题目图片无法读取") from exc
        problem_dir = os.path.join(_answer_document_root(), "manual_problem_images")
        os.makedirs(problem_dir, exist_ok=True)
        problem_relative = os.path.join("answer_documents", "manual_problem_images", f"problem-{problem_id}-{uuid.uuid4().hex[:8]}.jpg")
        problem_disk = os.path.join(os.path.dirname(__file__), problem_relative)
        problem_image.save(problem_disk, "JPEG", quality=95)
        conn.execute("UPDATE problems SET crop_image_path=?,content_text=CASE WHEN trim(content_text)='' THEN '（题目图片已上传，待转写）' ELSE content_text END WHERE id=?", (problem_relative.replace("\\", "/"), problem_id))
    incoming_images = list(req.images_base64 or [])
    if req.image_base64:
        incoming_images.append(req.image_base64)
    if incoming_images:
        try:
            source_images = []
            for item in incoming_images:
                raw = base64.b64decode(item.split(",", 1)[-1], validate=True)
                source_images.append(Image.open(io.BytesIO(raw)).convert("RGB"))
            max_width = max(item.width for item in source_images)
            scaled = []
            for item in source_images:
                if item.width != max_width:
                    item = item.resize((max_width, max(1, round(item.height * max_width / item.width))))
                scaled.append(item)
            image = Image.new("RGB", (max_width, sum(item.height for item in scaled)), "white")
            top = 0
            for item in scaled:
                image.paste(item, (0, top)); top += item.height
        except Exception as exc:
            conn.close(); raise HTTPException(400, "上传的答案图无法读取") from exc
    else:
        values = (req.x_ratio, req.y_ratio, req.width_ratio, req.height_ratio)
        if any(value is None for value in values):
            conn.close(); raise HTTPException(400, "请在原 PDF 页框选完整答案区域，或上传正确答案图")
        if not all(0 <= float(value) <= 1 for value in values) or float(req.width_ratio) < .01 or float(req.height_ratio) < .01:
            conn.close(); raise HTTPException(400, "裁切区域不合法")
        page_dir = os.path.join(_answer_document_root(), "page_previews", anchor["document_id"])
        page_file = os.path.join(page_dir, f"page-{anchor['page_no']}-w1400.jpg")
        if not os.path.isfile(page_file):
            helper = os.path.join(os.path.dirname(__file__), "answer_pdf_render_helper.py")
            os.makedirs(page_dir, exist_ok=True)
            subprocess.run([_ocr_python(), helper, "--pdf", document["stored_path"], "--page", str(anchor["page_no"]), "--width", "1400", "--output", page_file], cwd=os.path.dirname(__file__), check=True, timeout=180)
        source = Image.open(page_file).convert("RGB"); width,height = source.size
        left=max(0,min(width-1,round(float(req.x_ratio)*width))); top=max(0,min(height-1,round(float(req.y_ratio)*height)))
        right=max(left+1,min(width,round((float(req.x_ratio)+float(req.width_ratio))*width))); bottom=max(top+1,min(height,round((float(req.y_ratio)+float(req.height_ratio))*height)))
        image=source.crop((left,top,right,bottom))
    if image.width < 40 or image.height < 25:
        conn.close(); raise HTTPException(400, "裁切区域过小")
    crop_dir=os.path.join(_answer_document_root(), "manual_crops", anchor["document_id"]); os.makedirs(crop_dir, exist_ok=True)
    crop_path=os.path.join(crop_dir, f"anchor-{anchor_id}-{uuid.uuid4().hex[:8]}.jpg"); image.save(crop_path, "JPEG", quality=95)
    bbox=[float(req.x_ratio or 0),float(req.y_ratio or 0),float(req.width_ratio or 1),float(req.height_ratio or 1)]
    # A replacement crop must never reuse the old candidate/source image.  Keep
    # the historical candidate as evidence, but make this anchor build a fresh
    # candidate on the next Qwen run.
    if anchor["candidate_id"]:
        conn.execute("""UPDATE answer_import_candidates
                        SET match_status='rejected', reviewed_at=?,
                            review_note='已被教师重新裁切的答案图替代'
                        WHERE id=? AND match_status='pending'""",
                     (datetime.now().isoformat(timespec="seconds"), anchor["candidate_id"]))
    conn.execute("""UPDATE answer_page_anchors SET crop_path=?,bbox_json=?,problem_id=?,candidate_id=NULL,
        ocr_text='',ocr_confidence=0,extraction_status='detected',comparison_status='not_run',comparison_json='{}'
        WHERE id=?""", (crop_path,json.dumps(bbox),problem_id,anchor_id))
    conn.commit(); conn.close()
    return {"ok":True,"anchor_id":anchor_id,"problem_id":problem_id,"page_no":anchor["page_no"],"crop_path":crop_path,
            "message":"已保存重新裁切图；请确认后再运行服务器重试（Qwen）"}


@app.post("/answer-document-anchors/{anchor_id}/problem-image")
def attach_problem_image_to_anchor(anchor_id: int, req: AnchorProblemImageReq):
    """Attach teacher-provided question evidence to the linked problem only."""
    conn=get_db(); conn.row_factory=sqlite3.Row
    anchor=conn.execute("SELECT problem_id FROM answer_page_anchors WHERE id=?",(anchor_id,)).fetchone()
    if not anchor or not anchor["problem_id"]:
        conn.close(); raise HTTPException(409,"请先在“重新裁切并绑定”中绑定题库题目")
    try:
        raw=base64.b64decode(req.image_base64.split(",",1)[-1],validate=True)
        image=Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        conn.close(); raise HTTPException(400,"题目图片无法读取") from exc
    folder=os.path.join(_answer_document_root(),"manual_problem_images");os.makedirs(folder,exist_ok=True)
    relative=os.path.join("answer_documents","manual_problem_images",f"problem-{anchor['problem_id']}-{uuid.uuid4().hex[:8]}.jpg").replace("\\","/")
    image.save(os.path.join(os.path.dirname(__file__),relative),"JPEG",quality=95)
    conn.execute("UPDATE problems SET crop_image_path=?,content_text=CASE WHEN trim(content_text)='' THEN '（题目图片已上传，待转写）' ELSE content_text END WHERE id=?",(relative,anchor["problem_id"]))
    conn.commit();conn.close();return {"ok":True,"problem_id":anchor["problem_id"],"image_path":relative}


def _independent_solve(problem_text: str, section_no: str, problem_no: str) -> dict:
    if not (problem_text or "").strip():
        return {"available": False, "reason": "题目正文缺失，无法独立解题"}
    url = os.environ.get("MATH_VLM_URL", "http://127.0.0.1:18080").rstrip("/") + "/solve"
    payload = {"problem_text": problem_text, "section_no": section_no, "problem_no": problem_no}
    try:
        request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.loads(response.read().decode("utf-8"))
        return {"available": True, **result}
    except Exception as exc:
        return {"available": False, "reason": "GPU 独立解题接口暂不可用；部署新版服务后自动启用", "detail": str(exc)[:300]}


def _compare_anchor_candidate(conn: sqlite3.Connection, anchor_id: int, ai: dict, independent: Optional[dict] = None) -> dict:
    anchor = conn.execute("""
      SELECT a.*,p.ptype,p.std_answer,p.full_solution,p.answer_status FROM answer_page_anchors a
      LEFT JOIN problems p ON p.id=a.problem_id WHERE a.id=?
    """, (anchor_id,)).fetchone()
    if not anchor or not anchor["problem_id"]:
        return {"status": "unmatched", "reason": "未匹配到题库题目"}
    if anchor["ptype"] == "proof":
        return {"status": "proof_reference", "reason": "证明题仅保存原答案图与参考解答，不要求唯一标准答案"}
    extracted = str(ai.get("std_answer") or "").strip()
    confidence = float(ai.get("confidence") or 0)
    risks = ai.get("risks") or []
    independent = independent or {"available": False, "reason": "未运行独立解题"}
    base = {"independent_solve": independent}
    requirement_issue = answer_requirement_issue(str(anchor["content_text"] or ""), extracted)
    if requirement_issue:
        return {**base, "status": "low_confidence", "reason": requirement_issue,
                "confidence": min(confidence, 0.3), "extracted_answer": extracted}
    if confidence < 0.75 or risks:
        return {**base, "status": "low_confidence", "reason": "提取置信度较低或模型报告风险", "confidence": confidence, "risks": risks}
    if independent.get("available") and str(independent.get("std_answer") or "").strip():
        from grading_engine import answer_match
        solve_equal, solve_score, solve_method = answer_match(extracted, str(independent["std_answer"]))
        base["independent_equal"] = solve_equal
        base["independent_method"] = solve_method
        if not solve_equal and float(independent.get("confidence") or 0) >= 0.7:
            return {**base, "status": "conflict", "reason": "答案书提取结果与独立解题不一致",
                    "confidence": min(confidence, solve_score), "extracted_answer": extracted,
                    "independent_answer": independent.get("std_answer")}
    if anchor["answer_status"] == "verified" and (anchor["std_answer"] or "").strip():
        from grading_engine import answer_match
        equal, score, method = answer_match(extracted, anchor["std_answer"])
        # A common applied-math answer-book format gives a general solution
        # first, then explicitly evaluates the requested value in the worked
        # solution.  Prefer that final conclusion for comparison.
        if not equal and anchor["full_solution"]:
            numeric_answers = re.findall(r"(?<![\w.])(?:≈|约为|为)\s*([0-9]+(?:\.[0-9]+)?\s*(?:kg|g|米|m|秒|s|%)?)", str(ai.get("full_solution") or ""))
            for final_value in reversed(numeric_answers):
                final_equal, final_score, final_method = answer_match(final_value.strip(), anchor["std_answer"])
                if final_equal:
                    return {**base, "status": "agrees", "reason": "完整解答中的最终代入结论：" + final_method,
                            "confidence": min(confidence, final_score), "existing_answer": anchor["std_answer"],
                            "extracted_answer": final_value.strip()}
        return {**base, "status": "agrees" if equal else "conflict", "reason": method,
                "confidence": min(confidence, score), "existing_answer": anchor["std_answer"], "extracted_answer": extracted}
    if independent.get("available") and base.get("independent_equal"):
        return {**base, "status": "agrees", "reason": "答案书提取结果与独立解题一致", "confidence": confidence,
                "extracted_answer": extracted, "independent_answer": independent.get("std_answer")}
    return {**base, "status": "needs_teacher", "reason": "没有已核验答案可交叉验证", "confidence": confidence,
            "extracted_answer": extracted}


@app.post("/answer-document-anchors/{anchor_id}/extract")
def extract_answer_document_anchor(anchor_id: int, req: AnchorReviewReq):
    conn = get_db(); conn.row_factory = sqlite3.Row
    anchor = conn.execute("""
      SELECT a.*,p.content_text,p.ptype,p.std_answer,p.full_solution,s.section_no
      FROM answer_page_anchors a LEFT JOIN problems p ON p.id=a.problem_id
      LEFT JOIN sections s ON s.id=p.section_id WHERE a.id=?
    """, (anchor_id,)).fetchone()
    if not anchor:
        conn.close(); raise HTTPException(404, "答案锚点不存在")
    if not anchor["problem_id"]:
        conn.close(); raise HTTPException(409, "该答案块尚未匹配到题库题目")
    if anchor["ptype"] == "proof" and req.reference_only:
        result = {"ptype": "proof", "std_answer": "", "full_solution": anchor["ocr_text"] or "",
                  "confidence": anchor["ocr_confidence"], "risks": [], "reference_only": True}
        comparison = {"status": "proof_reference", "reason": "证明题采用参考解答模式"}
        conn.execute("UPDATE answer_page_anchors SET comparison_status=?,comparison_json=?,extraction_status='completed' WHERE id=?",
                     (comparison["status"], json.dumps({"ai": result, "comparison": comparison}, ensure_ascii=False), anchor_id))
        conn.commit(); conn.close(); return {"result": result, "comparison": comparison}
    candidate_id = anchor["candidate_id"]
    # Prefer the numbered subparts already visible in the question text.  It
    # is more reliable than asking a VLM to infer them from a long answer page.
    teacher_subquestion_count = int(anchor["teacher_subquestion_count"] or 0)
    if not teacher_subquestion_count:
        labels = re.findall(r"(?:^|\n|\s)[(（]([1-9][0-9]?)\s*[)）]", str(anchor["content_text"] or ""))
        numbers = sorted({int(value) for value in labels})
        if numbers and numbers == list(range(1, max(numbers) + 1)):
            teacher_subquestion_count = max(numbers)
            conn.execute("UPDATE answer_page_anchors SET teacher_subquestion_count=? WHERE id=?",
                         (teacher_subquestion_count, anchor_id))
            conn.commit()
    if not candidate_id:
        # Include the current crop bytes: a teacher replacement must create a
        # new candidate rather than reviving the old crop's rejected candidate.
        with open(anchor["crop_path"], "rb") as current_crop:
            crop_digest = hashlib.sha256(current_crop.read()).hexdigest()
        digest = hashlib.sha256(f"pdf-anchor|{anchor_id}|{anchor['problem_id']}|{crop_digest}".encode()).hexdigest()
        cur = conn.execute("""
          INSERT OR IGNORE INTO answer_import_candidates
          (problem_id,volume,section_no,problem_no,sub_no,source_pdf,source_page,ocr_text,ocr_confidence,subquestion_count,
           match_status,match_reason,content_hash,source_updated_at)
          VALUES(?,?,?,?,?,?,?, ?,?,?,'pending','PDF 单题裁切严格匹配',?,?)
        """, (anchor["problem_id"], "", anchor["section_no"] or anchor["section_no"], anchor["problem_no"],
              anchor["sub_no"] or None, "answer-document", anchor["page_no"], anchor["ocr_text"] or "",
              anchor["ocr_confidence"] or 0, teacher_subquestion_count, digest, datetime.now().isoformat(timespec="seconds")))
        candidate_id = cur.lastrowid or conn.execute("SELECT id FROM answer_import_candidates WHERE content_hash=?", (digest,)).fetchone()[0]
        relative_dir = os.path.join("answer_source_previews", f"document-anchor-{anchor_id}")
        os.makedirs(os.path.join(os.path.dirname(__file__), relative_dir), exist_ok=True)
        relative = os.path.join(relative_dir, "crop.jpg").replace("\\", "/")
        with open(anchor["crop_path"], "rb") as source, open(os.path.join(os.path.dirname(__file__), relative), "wb") as target:
            target.write(source.read())
        conn.execute("INSERT OR IGNORE INTO candidate_source_images(candidate_id,sort_order,image_path,created_at) VALUES(?,?,?,?)",
                     (candidate_id, 1, relative, datetime.now().isoformat(timespec="seconds")))
        # Extremely tall page-sized crops are unreadable after VLM resizing.
        # Keep the original as evidence and add overlapping vertical slices for
        # recognition only, so each small answer is still legible.
        with Image.open(anchor["crop_path"]) as crop:
            width, height = crop.size
            if height > width * 3:
                pieces = min(6, max(2, (height + width * 2 - 1) // (width * 2)))
                step = max(1, height // pieces)
                overlap = max(40, int(step * 0.12))
                for part in range(pieces):
                    top = max(0, part * step - overlap)
                    bottom = min(height, (part + 1) * step + overlap)
                    slice_path = os.path.join(os.path.dirname(__file__), relative_dir, f"slice-{part + 1}.jpg")
                    crop.crop((0, top, width, bottom)).save(slice_path, "JPEG", quality=95)
                    slice_relative = os.path.join(relative_dir, f"slice-{part + 1}.jpg").replace("\\", "/")
                    conn.execute("INSERT OR IGNORE INTO candidate_source_images(candidate_id,sort_order,image_path,created_at) VALUES(?,?,?,?)",
                                 (candidate_id, part + 2, slice_relative, datetime.now().isoformat(timespec="seconds")))
        conn.execute("UPDATE answer_page_anchors SET candidate_id=?,extraction_status='running' WHERE id=?", (candidate_id, anchor_id))
        conn.commit()
    # The anchor is the sole source of truth for a teacher-corrected number of
    # subquestions.  An old candidate must never silently retain a stale count
    # (for example 1) and override the current anchor value (for example 2).
    conn.execute("UPDATE answer_import_candidates SET subquestion_count=? WHERE id=?",
                 (teacher_subquestion_count, candidate_id))
    conn.commit()
    conn.close()
    try:
        review = run_candidate_ai_review(candidate_id)
        ai = review["result"]
        # Recognition is already a valid completed operation.  Cross-checking
        # is supplemental: an exception here must never discard a successful
        # multi-part Qwen result or turn it into a 500 response.
        try:
            independent = _independent_solve(anchor["content_text"] or "", anchor["section_no"] or "", anchor["problem_no"] or "")
            conn = get_db(); comparison = _compare_anchor_candidate(conn, anchor_id, ai, independent)
        except Exception as compare_exc:
            try:
                conn.close()
            except Exception:
                pass
            conn = get_db()
            comparison = {"status": "needs_teacher",
                          "reason": "答案已提取；本地交叉判等暂不可用，请教师核对后确认入库",
                          "detail": str(compare_exc)[:240]}
        expected_count = int(anchor["teacher_subquestion_count"] or 0)
        returned_count = len(ai.get("sub_answers") or [])
        # Only adopt automatically when both independently solved and the VLM
        # result agree at very high confidence. Everything else remains a
        # visible candidate awaiting the teacher's one-click confirmation.
        auto_adopted = False
        if (anchor["ptype"] == "calc" and (not expected_count or returned_count == expected_count)
                and comparison.get("status") == "agrees"
                and comparison.get("independent_equal")
                and (comparison.get("independent_solve") or {}).get("symbolic_verified") is True
                and float(ai.get("confidence") or 0) >= 0.92
                and not (ai.get("risks") or [])
                and str(ai.get("std_answer") or "").strip()):
            issue = answer_quality_issue(str(ai.get("std_answer") or ""), "calc", str(ai.get("full_solution") or ""))
            if not issue:
                conn.execute("""UPDATE problems SET std_answer=?,full_solution=?,answer_status='verified',answer_invalid_reason=''
                                WHERE id=?""", (str(ai.get("std_answer") or "").strip(), str(ai.get("full_solution") or ""), anchor["problem_id"]))
                conn.execute("""UPDATE answer_import_candidates SET match_status='approved',reviewed_at=?,
                                review_note='答案书提取与独立解题高置信度一致，自动入库'
                                WHERE id=?""", (datetime.now().isoformat(timespec="seconds"), candidate_id))
                auto_adopted = True
        if expected_count and returned_count != expected_count:
            comparison = {"status": "low_confidence",
                          "reason": f"教师指定 {expected_count} 个小问，但本次只返回 {returned_count} 个；禁止入库",
                          "expected_subquestion_count": expected_count,
                          "returned_subquestion_count": returned_count}
            auto_adopted = False
        payload = {"ai": ai, "comparison": comparison, "auto_adopted": auto_adopted,
                   "teacher_subquestion_count": expected_count}
        conn.execute("UPDATE answer_page_anchors SET extraction_status='completed',comparison_status=?,comparison_json=? WHERE id=?",
                     (comparison["status"], json.dumps(payload, ensure_ascii=False), anchor_id))
        conn.commit(); conn.close()
        return {"result": ai, "comparison": comparison, "candidate_id": candidate_id, "auto_adopted": auto_adopted}
    except HTTPException:
        # Preserve the real downstream failure (for example Qwen/SSH or a
        # subquestion-count mismatch) instead of masking it as HTTP 500.
        raise
    except Exception:
        conn = get_db(); conn.execute("UPDATE answer_page_anchors SET extraction_status='failed',comparison_status='extraction_failed' WHERE id=?", (anchor_id,)); conn.commit(); conn.close()
        raise


@app.post("/answer-document-anchors/{anchor_id}/adopt")
def adopt_answer_document_anchor(anchor_id: int, req: AnchorAdoptReq):
    """Write the reviewed candidate into the formal answer library once."""
    conn = get_db(); conn.row_factory = sqlite3.Row
    anchor = conn.execute("""SELECT a.*,p.ptype,p.std_answer,p.full_solution
                             FROM answer_page_anchors a JOIN problems p ON p.id=a.problem_id
                             WHERE a.id=?""", (anchor_id,)).fetchone()
    if not anchor:
        conn.close(); raise HTTPException(404, "答案块或已绑定题目不存在")
    try:
        data = json.loads(anchor["comparison_json"] or "{}")
    except Exception:
        data = {}
    ai = data.get("ai") or {}
    answer = (req.std_answer if req.std_answer is not None else ai.get("std_answer") or "").strip()
    solution = req.full_solution if req.full_solution is not None else str(ai.get("full_solution") or "")
    if anchor["ptype"] == "proof":
        if not solution:
            solution = str(anchor["ocr_text"] or "")
        if not solution:
            conn.close(); raise HTTPException(409, "请先运行提取或补充参考解答")
        conn.execute("UPDATE problems SET full_solution=?,answer_status='verified',answer_invalid_reason='' WHERE id=?", (solution, anchor["problem_id"]))
    else:
        issue = answer_quality_issue(answer, "calc", solution)
        if issue:
            conn.close(); raise HTTPException(400, "不能入库：" + issue)
        conn.execute("""UPDATE problems SET std_answer=?,full_solution=?,answer_status='verified',answer_invalid_reason=''
                        WHERE id=?""", (answer, solution, anchor["problem_id"]))
    if anchor["candidate_id"]:
        conn.execute("UPDATE answer_import_candidates SET match_status='approved',reviewed_at=?,review_note='教师在答案 PDF 人工看图页确认入库' WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), anchor["candidate_id"]))
    data["adopted"] = True
    # Adoption is the terminal state for this review flow.  Leaving the old
    # conflict/low-confidence status here made successfully stored answers
    # reappear in the pending queue after a refresh.
    conn.execute("""UPDATE answer_page_anchors
                    SET comparison_json=?, comparison_status='adopted', extraction_status='completed'
                    WHERE id=?""", (json.dumps(data, ensure_ascii=False), anchor_id))
    conn.commit(); conn.close()
    return {"ok": True, "anchor_id": anchor_id, "problem_id": anchor["problem_id"], "adopted": True}


@app.post("/answer-document-anchors/{anchor_id}/subquestion-count")
def correct_answer_anchor_subquestion_count(anchor_id: int, req: AnchorSubquestionCountReq):
    """Teacher correction for an incorrectly detected number of subquestions."""
    if not 1 <= req.count <= 30:
        raise HTTPException(400, "小问数量须在 1 到 30 之间")
    conn = get_db(); conn.row_factory = sqlite3.Row
    anchor = conn.execute("SELECT * FROM answer_page_anchors WHERE id=?", (anchor_id,)).fetchone()
    if not anchor:
        conn.close(); raise HTTPException(404, "答案块不存在")
    if not anchor["problem_id"]:
        conn.close(); raise HTTPException(409, "请先绑定题库题目")
    # The old model output is explicitly rejected, then a fresh candidate is
    # created on retry carrying the teacher-provided count.
    if anchor["candidate_id"]:
        conn.execute("""UPDATE answer_import_candidates SET match_status='rejected',reviewed_at=?,
                        review_note='教师修正小问数量后重新提取' WHERE id=? AND match_status='pending'""",
                     (datetime.now().isoformat(timespec="seconds"), anchor["candidate_id"]))
    conn.execute("""UPDATE answer_page_anchors SET candidate_id=NULL,teacher_subquestion_count=?,ocr_text='',ocr_confidence=0,
                    extraction_status='detected',comparison_status='not_run',
                    comparison_json='{}' WHERE id=?""",
                 (req.count, anchor_id))
    conn.commit(); conn.close()
    return {"ok": True, "anchor_id": anchor_id, "subquestion_count": req.count,
            "message": "已按教师指定小问数重置，请重新运行 Qwen 提取"}


def _answer_topic_mismatch(knowledge_pts: str, answer: str) -> tuple[bool, str]:
    """Conservative topic check used for triage, never automatic mutation."""
    expected = (knowledge_pts or "").lower()
    observed = (answer or "").lower()
    if not observed:
        return False, ""
    ode_signals = ("通解", "特解", "微分方程", "特征根", "c1", "c_1", "c2", "c_2", "y'", 'y"', "dy/dx")
    integral_expected = any(token in expected for token in (
        "integral", "area", "volume", "arc_length", "work", "反常积分", "定积分", "不定积分"
    ))
    if integral_expected and sum(token in observed for token in ode_signals) >= 2:
        return True, "知识点属于积分学，但答案呈现微分方程/通解特征，疑似章节或答案页错配"
    derivative_expected = "derivative" in expected or "differential" in expected
    integral_signals = ("∫", "integral", "+ c", "+c", "面积", "体积")
    if derivative_expected and sum(token in observed for token in integral_signals) >= 2:
        return True, "知识点属于导数/微分，但答案呈现积分结果，疑似答案页错配"
    return False, ""


@app.get("/answer-library/audit")
def answer_library_audit():
    """Read-only chapter health report for answer-library maintenance."""
    conn = get_db()
    try:
        rows = [dict(row) for row in conn.execute("""
            SELECT p.id,p.problem_no,p.sub_no,p.ptype,p.std_answer,p.answer_status,
                   p.answer_invalid_reason,p.knowledge_pts,s.section_no,s.title
            FROM problems p JOIN sections s ON s.id=p.section_id
            ORDER BY s.section_no,CAST(p.problem_no AS INTEGER),p.sub_no
        """).fetchall()]
        duplicate_keys = {
            (row[0], row[1], row[2] or ""): row[3]
            for row in conn.execute("""
                SELECT s.section_no,p.problem_no,COALESCE(p.sub_no,''),COUNT(*)
                FROM problems p JOIN sections s ON s.id=p.section_id
                GROUP BY s.section_no,p.problem_no,COALESCE(p.sub_no,'') HAVING COUNT(*)>1
            """).fetchall()
        }
        sections = {}
        for row in rows:
            item = sections.setdefault(row["section_no"], {
                "section_no": row["section_no"], "title": row.get("title") or "",
                "total": 0, "with_answer": 0, "verified": 0, "unverified": 0,
                "missing_answer": 0, "duplicate_count": 0, "mismatch_count": 0,
                "missing_problem_numbers": [], "issues": [],
            })
            item["total"] += 1
            if (row.get("std_answer") or "").strip():
                item["with_answer"] += 1
            else:
                item["missing_answer"] += 1
                item["issues"].append({"problem_id": row["id"], "problem_no": row["problem_no"],
                                       "sub_no": row["sub_no"], "kind": "missing_answer", "reason": "缺少标准答案"})
            if row.get("answer_status") == "verified":
                item["verified"] += 1
            else:
                item["unverified"] += 1
            key = (row["section_no"], row["problem_no"], row.get("sub_no") or "")
            if key in duplicate_keys:
                item["duplicate_count"] += 1
                item["issues"].append({"problem_id": row["id"], "problem_no": row["problem_no"],
                                       "sub_no": row["sub_no"], "kind": "duplicate", "reason": "章节、题号和小问号重复"})
            mismatch, reason = _answer_topic_mismatch(row.get("knowledge_pts") or "", row.get("std_answer") or "")
            if mismatch:
                item["mismatch_count"] += 1
                item["issues"].append({"problem_id": row["id"], "problem_no": row["problem_no"],
                                       "sub_no": row["sub_no"], "kind": "topic_mismatch", "reason": reason})
        for section_no, item in sections.items():
            top_numbers = sorted({int(row["problem_no"]) for row in rows
                                  if row["section_no"] == section_no and not row.get("sub_no")
                                  and str(row.get("problem_no") or "").isdigit()})
            if top_numbers:
                item["missing_problem_numbers"] = [n for n in range(1, max(top_numbers) + 1) if n not in top_numbers]
            item["health"] = "danger" if item["mismatch_count"] or item["duplicate_count"] else (
                "warning" if item["missing_answer"] or item["unverified"] or item["missing_problem_numbers"] else "ok")
        ordered = sorted(sections.values(), key=lambda item: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", item["section_no"])])
        return {
            "summary": {
                "sections": len(ordered), "problems": len(rows),
                "with_answer": sum(item["with_answer"] for item in ordered),
                "verified": sum(item["verified"] for item in ordered),
                "missing_answer": sum(item["missing_answer"] for item in ordered),
                "duplicate_records": sum(item["duplicate_count"] for item in ordered),
                "topic_mismatches": sum(item["mismatch_count"] for item in ordered),
            },
            "sections": ordered,
        }
    finally:
        conn.close()


@app.post("/answer-library/section-batch")
def create_section_answer_batch(req: SectionAnswerBatchReq):
    """Attach one continuous set of answer pages to every target problem in a section.

    Each problem gets an independent evidence copy so later per-problem correction
    cannot invalidate another candidate. Recognition remains opt-in and candidates
    never become grading keys automatically.
    """
    section_no = req.section_no.strip()
    if not section_no or not req.images_base64:
        raise HTTPException(400, "section_no and images_base64 are required")
    if len(req.images_base64) > 30:
        raise HTTPException(413, "one batch may contain at most 30 images")
    decoded = []
    for image_data in req.images_base64:
        try:
            payload = base64.b64decode(image_data.split(",", 1)[-1], validate=True)
        except Exception as exc:
            raise HTTPException(400, "invalid base64 image") from exc
        if len(payload) > 15 * 1024 * 1024 or not payload.startswith(b"\xff\xd8"):
            raise HTTPException(400, "every source page must be a JPEG smaller than 15 MB")
        decoded.append(payload)
    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        query = """
            SELECT p.*,s.section_no FROM problems p JOIN sections s ON s.id=p.section_id
            WHERE s.section_no=?
        """
        args = [section_no]
        if req.only_unverified:
            query += " AND p.answer_status<>'verified'"
        query += " ORDER BY CAST(p.problem_no AS INTEGER),p.sub_no"
        problems = conn.execute(query, args).fetchall()
        if not problems:
            raise HTTPException(404, "该章节没有需要导入答案的题目")
        preview_dir = os.path.join(os.path.dirname(__file__), "answer_source_previews")
        os.makedirs(preview_dir, exist_ok=True)
        now = datetime.now().isoformat(timespec="seconds")
        batch_id = uuid.uuid4().hex[:12]
        created, updated, skipped, candidate_ids = 0, 0, 0, []
        for problem in problems:
            candidate = conn.execute("""
                SELECT id FROM answer_import_candidates
                WHERE problem_id=? AND match_status='pending' ORDER BY id DESC LIMIT 1
            """, (problem["id"],)).fetchone()
            if candidate:
                candidate_id = candidate["id"]
                has_sources = conn.execute("SELECT 1 FROM candidate_source_images WHERE candidate_id=?", (candidate_id,)).fetchone()
                if has_sources and not req.replace_existing_sources:
                    skipped += 1
                    continue
                conn.execute("DELETE FROM candidate_source_images WHERE candidate_id=?", (candidate_id,))
                updated += 1
            else:
                digest = hashlib.sha256(f"section-batch|{batch_id}|{problem['id']}".encode()).hexdigest()
                cur = conn.execute("""
                    INSERT INTO answer_import_candidates
                    (problem_id,volume,section_no,problem_no,sub_no,source_pdf,source_page,
                     ocr_text,ocr_confidence,match_status,match_reason,content_hash,source_updated_at)
                    VALUES(?,?,?,?,?,?,0,'',1.0,'pending','section batch evidence',?,?)
                """, (problem["id"], problem["exercise_set"] or "", section_no, problem["problem_no"],
                      problem["sub_no"], req.filename or "section-answer-upload", digest, now))
                candidate_id = cur.lastrowid
                created += 1
            for order, payload in enumerate(decoded, 1):
                relative = f"answer_source_previews/batch-{batch_id}-candidate-{candidate_id}-{order}.jpg"
                disk = os.path.join(os.path.dirname(__file__), relative)
                temp = disk + ".uploading"
                with open(temp, "wb") as fh:
                    fh.write(payload)
                os.replace(temp, disk)
                conn.execute("INSERT INTO candidate_source_images(candidate_id,sort_order,image_path,created_at) VALUES(?,?,?,?)",
                             (candidate_id, order, relative, now))
            conn.execute("""
                UPDATE answer_import_candidates SET source_pdf=?,source_page=0,ocr_text='',latex_text='',
                    vision_status='not_queued',vision_confidence=NULL,ai_review_json='{}',ai_review_status='not_run',
                    ai_review_model='',source_updated_at=? WHERE id=?
            """, (req.filename or "section-answer-upload", now, candidate_id))
            candidate_ids.append(candidate_id)
        conn.commit()
        return {"ok": True, "section_no": section_no, "candidate_ids": candidate_ids,
                "created": created, "updated": updated, "skipped_existing": skipped,
                "target_count": len(problems), "page_count": len(decoded)}
    finally:
        conn.close()


@app.post("/answer-import-candidates/batch-approve")
def batch_approve_candidates(req: CandidateBatchApproveReq):
    if not 0.5 <= req.min_confidence <= 1:
        raise HTTPException(400, "min_confidence must be between 0.5 and 1")
    conn = get_db()
    conn.row_factory = sqlite3.Row
    approved, skipped = [], []
    try:
        for candidate_id in req.candidate_ids[:200]:
            row = conn.execute("""
                SELECT c.*,p.ptype FROM answer_import_candidates c
                JOIN problems p ON p.id=c.problem_id WHERE c.id=? AND c.match_status='pending'
            """, (candidate_id,)).fetchone()
            if not row or row["ai_review_status"] != "completed":
                skipped.append({"id": candidate_id, "reason": "AI 核验尚未完成"})
                continue
            try:
                ai = json.loads(row["ai_review_json"] or "{}")
            except Exception:
                ai = {}
            confidence = float(ai.get("confidence") or 0)
            risks = ai.get("risks") or []
            answer = str(ai.get("std_answer") or "").strip()
            ptype = ai.get("ptype") if ai.get("ptype") in {"calc", "proof"} else row["ptype"]
            issue = answer_quality_issue(answer, ptype, str(ai.get("full_solution") or ""))
            if confidence < req.min_confidence or risks or issue:
                skipped.append({"id": candidate_id, "reason": issue or "置信度不足或仍有风险"})
                continue
            conn.execute("""
                UPDATE problems SET std_answer=?,full_solution=?,ptype=?,answer_status='verified',answer_invalid_reason=''
                WHERE id=?
            """, (answer, str(ai.get("full_solution") or ""), ptype, row["problem_id"]))
            conn.execute("""
                UPDATE answer_import_candidates SET match_status='approved',reviewed_at=?,
                    review_note='高置信度批量通过（教师触发）' WHERE id=?
            """, (datetime.now().isoformat(timespec="seconds"), candidate_id))
            approved.append(candidate_id)
        conn.commit()
        return {"ok": True, "approved": approved, "skipped": skipped}
    finally:
        conn.close()


@app.get("/answer-import-candidates")
def list_answer_import_candidates(status: str = Query("pending"), limit: int = Query(100, ge=1, le=300)):
    """Candidates are evidence only; they never become grade keys by listing them."""
    conn = get_db()
    try:
        rows = conn.execute("""
            WITH ranked AS (
                SELECT c.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY c.problem_id
                           ORDER BY CASE WHEN c.source_pdf='teacher-upload' THEN 0 ELSE 1 END,
                                    CASE WHEN c.ai_review_status='completed' THEN 0 ELSE 1 END,
                                    CASE WHEN COALESCE(c.source_updated_at,'')<>'' THEN 0 ELSE 1 END,
                                    c.id DESC
                       ) AS duplicate_rank
                FROM answer_import_candidates c
                WHERE (?='' OR c.match_status=?)
            )
            SELECT c.*, p.ptype, p.std_answer AS current_std_answer,
                   p.full_solution AS current_full_solution, p.answer_status,
                   p.content_text, p.crop_image_path,
                   (SELECT d.filename FROM answer_page_anchors apa
                    JOIN answer_documents d ON d.id=apa.document_id
                    WHERE apa.candidate_id=c.id ORDER BY apa.id DESC LIMIT 1) AS source_document,
                   (SELECT COUNT(*) FROM candidate_source_images si WHERE si.candidate_id=c.id) AS source_image_count,
                   t.id AS vision_task_id, t.status AS vision_task_status, t.error_message AS vision_task_error
            FROM ranked c
            JOIN problems p ON p.id=c.problem_id
            LEFT JOIN vision_recognition_tasks t ON t.candidate_id=c.id
            WHERE c.duplicate_rank=1
            ORDER BY CASE c.match_status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                     c.section_no, CAST(c.problem_no AS INTEGER), c.id
            LIMIT ?
        """, (status, status, limit)).fetchall()
        counts = {row[0]: row[1] for row in conn.execute(
            "SELECT match_status, COUNT(DISTINCT problem_id) FROM answer_import_candidates GROUP BY match_status").fetchall()}
        items = []
        for row in rows:
            item = dict(row)
            # Keep the stored relative path.  Using basename here broke nested
            # answer-document crops in the batch-review page and caused 404s.
            item["source_images"] = [path.replace("\\", "/") for path in _candidate_image_paths(conn, item["id"])]
            items.append(item)
        return {"items": items, "counts": counts}
    except sqlite3.OperationalError:
        return {"items": [], "counts": {}}
    finally:
        conn.close()


@app.get("/answer-import-candidates/{candidate_id}")
def get_answer_import_candidate(candidate_id: int):
    """Fetch one candidate by ID, including already approved records.

    Batch review uses frozen candidate IDs.  Looking it up through the pending
    list loses a record as soon as it is published or de-duplicated.
    """
    conn = get_db()
    try:
        row = conn.execute("""
            SELECT c.*, p.ptype, p.std_answer AS current_std_answer,
                   p.full_solution AS current_full_solution, p.answer_status,
                   p.content_text, p.crop_image_path,
                   (SELECT COUNT(*) FROM candidate_source_images si WHERE si.candidate_id=c.id) AS source_image_count
            FROM answer_import_candidates c
            JOIN problems p ON p.id=c.problem_id
            WHERE c.id=?
        """, (candidate_id,)).fetchone()
        if not row:
            raise HTTPException(404, "candidate not found")
        item = dict(row)
        item["source_images"] = [path.replace("\\", "/") for path in _candidate_image_paths(conn, item["id"])]
        return item
    finally:
        conn.close()


@app.get("/answer-library/coverage")
def answer_library_coverage():
    """Show what can be safely automated, without treating OCR as ground truth."""
    conn = get_db()
    try:
        calc_total = conn.execute("SELECT COUNT(*) FROM problems WHERE ptype='calc'").fetchone()[0]
        calc_verified = conn.execute(
            "SELECT COUNT(*) FROM problems WHERE ptype='calc' AND answer_status='verified'").fetchone()[0]
        calc_candidates = conn.execute("""
            SELECT COUNT(DISTINCT c.problem_id) FROM answer_import_candidates c
            JOIN problems p ON p.id=c.problem_id
            WHERE p.ptype='calc' AND c.match_status='pending'
        """).fetchone()[0]
        return {
            "calc_total": calc_total,
            "calc_verified": calc_verified,
            "calc_pending_candidate": calc_candidates,
            "calc_missing_source": max(0, calc_total - calc_verified - calc_candidates),
        }
    finally:
        conn.close()


@app.get("/answer-library/missing")
def missing_answer_sources(limit: int = Query(200, ge=1, le=500)):
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT p.id,p.problem_no,p.sub_no,p.ptype,p.content_text,s.section_no
            FROM problems p JOIN sections s ON s.id=p.section_id
            WHERE p.answer_status<>'verified'
              AND NOT EXISTS (
                SELECT 1 FROM answer_import_candidates c
                WHERE c.problem_id=p.id AND c.match_status IN ('pending','approved')
              )
            ORDER BY s.section_no,CAST(p.problem_no AS INTEGER),p.sub_no LIMIT ?
        """, (limit,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@app.post("/problems/{problem_id}/answer-candidate")
def create_manual_answer_candidate(problem_id: str):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    problem = conn.execute("""
        SELECT p.*,s.section_no FROM problems p JOIN sections s ON s.id=p.section_id WHERE p.id=?
    """, (problem_id,)).fetchone()
    if not problem:
        conn.close()
        raise HTTPException(404, "problem not found")
    existing = conn.execute("""
        SELECT id FROM answer_import_candidates
        WHERE problem_id=? AND match_status='pending' ORDER BY id DESC LIMIT 1
    """, (problem_id,)).fetchone()
    if existing:
        conn.close()
        return {"ok": True, "candidate_id": existing[0], "created": False}
    digest = hashlib.sha256(f"manual-answer|{problem_id}|{datetime.now().isoformat()}".encode()).hexdigest()
    cur = conn.execute("""
        INSERT INTO answer_import_candidates
        (problem_id,volume,section_no,problem_no,sub_no,source_pdf,source_page,
         ocr_text,ocr_confidence,match_status,match_reason,content_hash)
        VALUES(?,?,?,?,?,'teacher-upload',0,'',1.0,'pending','manual answer source required',?)
    """, (problem_id, problem["exercise_set"] or "", problem["section_no"], problem["problem_no"],
          problem["sub_no"], digest))
    conn.commit()
    candidate_id = cur.lastrowid
    conn.close()
    return {"ok": True, "candidate_id": candidate_id, "created": True}


def _candidate_preview_path(candidate_id: int) -> str:
    return f"answer_source_previews/candidate-{candidate_id}.jpg"


def _candidate_image_disk_path(relative_path: str) -> str:
    """Resolve immutable candidate evidence across source and runtime-data layouts."""
    relative = str(relative_path or "").replace("\\", "/").lstrip("/")
    source_path = os.path.join(os.path.dirname(__file__), relative)
    if os.path.isfile(source_path):
        return source_path
    # Production keeps evidence outside the source checkout. Older candidates
    # stored relative paths, so resolve them under runtime-data as a fallback.
    runtime_path = os.path.join(
        str(Path(__file__).resolve().parents[2]), "runtime-data", relative
    )
    if os.path.isfile(runtime_path):
        return runtime_path
    return source_path


def _candidate_image_paths(conn: sqlite3.Connection, candidate_id: int) -> list[str]:
    paths = [row[0] for row in conn.execute(
        "SELECT image_path FROM candidate_source_images WHERE candidate_id=? ORDER BY sort_order,id",
        (candidate_id,)).fetchall()]
    legacy = _candidate_preview_path(candidate_id)
    legacy_disk = os.path.join(os.path.dirname(__file__), legacy)
    return paths or ([legacy] if os.path.isfile(legacy_disk) else [])


@app.post("/answer-import-candidates/{candidate_id}/source")
def update_candidate_source(candidate_id: int, req: CandidateSourceUpdateReq):
    """Correct a problem statement and/or replace a wrong source image.

    Replaced source material and recognition outputs are deleted. New source
    images deliberately invalidate OCR/VLM results so stale text cannot be
    mistaken for evidence from the corrected page.
    """
    incoming_images = list(req.images_base64 or [])
    if req.image_base64:
        incoming_images.append(req.image_base64)
    if req.problem_text is None and not incoming_images and req.subquestion_count is None:
        raise HTTPException(400, "problem_text, subquestion_count or images_base64 is required")
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT c.*, p.content_text FROM answer_import_candidates c
        JOIN problems p ON p.id=c.problem_id WHERE c.id=?
    """, (candidate_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "candidate not found")
    now = datetime.now().isoformat(timespec="seconds")
    if req.problem_text is not None:
        conn.execute("UPDATE problems SET content_text=? WHERE id=?",
                     (req.problem_text.strip(), row["problem_id"]))
        conn.execute("""
            UPDATE answer_import_candidates
            SET ai_review_json='{}', ai_review_status='not_run', ai_review_model=''
            WHERE id=?
        """, (candidate_id,))
    if req.subquestion_count is not None:
        if req.subquestion_count < 0 or req.subquestion_count > 50:
            conn.close()
            raise HTTPException(400, "subquestion_count must be between 0 and 50")
        conn.execute("UPDATE answer_import_candidates SET subquestion_count=? WHERE id=?",
                     (req.subquestion_count, candidate_id))
    old_source_deleted = False
    if incoming_images:
        decoded_images = []
        for image_data in incoming_images:
            try:
                image_bytes = base64.b64decode(image_data.split(",", 1)[-1], validate=True)
            except Exception as exc:
                conn.close()
                raise HTTPException(400, "invalid base64 image") from exc
            if len(image_bytes) > 15 * 1024 * 1024:
                conn.close()
                raise HTTPException(413, "one image is larger than 15 MB")
            if len(image_bytes) < 4 or not image_bytes.startswith(b"\xff\xd8"):
                conn.close()
                raise HTTPException(400, "source images must be JPEG")
            decoded_images.append(image_bytes)
        existing_paths = _candidate_image_paths(conn, candidate_id)
        if not req.append_images:
            for relative in existing_paths:
                disk = os.path.join(os.path.dirname(__file__), relative)
                if os.path.isfile(disk):
                    os.remove(disk)
                    old_source_deleted = True
            conn.execute("DELETE FROM candidate_source_images WHERE candidate_id=?", (candidate_id,))
            start_order = 1
        else:
            if not conn.execute("SELECT 1 FROM candidate_source_images WHERE candidate_id=?", (candidate_id,)).fetchone():
                legacy = _candidate_preview_path(candidate_id)
                if os.path.isfile(os.path.join(os.path.dirname(__file__), legacy)):
                    conn.execute("INSERT INTO candidate_source_images(candidate_id,sort_order,image_path,created_at) VALUES(?,?,?,?)",
                                 (candidate_id, 1, legacy, now))
            start_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order),0)+1 FROM candidate_source_images WHERE candidate_id=?",
                (candidate_id,)).fetchone()[0]
        preview_dir = os.path.join(os.path.dirname(__file__), "answer_source_previews")
        os.makedirs(preview_dir, exist_ok=True)
        for offset, image_bytes in enumerate(decoded_images):
            order = start_order + offset
            relative = f"answer_source_previews/candidate-{candidate_id}-{order}.jpg"
            disk = os.path.join(os.path.dirname(__file__), relative)
            temp = disk + ".uploading"
            with open(temp, "wb") as fh:
                fh.write(image_bytes)
            os.replace(temp, disk)
            conn.execute("INSERT INTO candidate_source_images(candidate_id,sort_order,image_path,created_at) VALUES(?,?,?,?)",
                         (candidate_id, order, relative, now))
        conn.execute("DELETE FROM vision_recognition_tasks WHERE candidate_id=?", (candidate_id,))
        conn.execute("""
            UPDATE problems SET answer_status='unverified',
                answer_invalid_reason='source image replaced; re-verification required'
            WHERE id=?
        """, (row["problem_id"],))
        conn.execute("""
            UPDATE answer_import_candidates
            SET source_pdf='teacher-upload', source_page=0, ocr_confidence=1.0,
                ocr_text='', latex_text='', vision_status='not_queued', vision_confidence=NULL,
                ai_review_json='{}', ai_review_status='not_run', ai_review_model='',
                match_status='pending', reviewed_at=NULL,
                review_note='source image replaced; re-verification required', source_updated_at=?
            WHERE id=?
        """, (now, candidate_id))
    conn.commit()
    image_count = len(_candidate_image_paths(conn, candidate_id))
    conn.close()
    return {"ok": True, "candidate_id": candidate_id, "image_replaced": bool(incoming_images),
            "image_count": image_count,
            "old_source_deleted": old_source_deleted}


@app.post("/answer-import-candidates/{candidate_id}/ai-review")
def run_candidate_ai_review(candidate_id: int):
    """Ask the private VLM for a suggestion; never modify the grading key."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    candidate = conn.execute("""
        SELECT c.*, p.content_text, p.ptype FROM answer_import_candidates c
        JOIN problems p ON p.id=c.problem_id WHERE c.id=?
    """, (candidate_id,)).fetchone()
    if not candidate:
        conn.close()
        raise HTTPException(404, "candidate not found")
    image_paths = _candidate_image_paths(conn, candidate_id)
    if not image_paths:
        conn.close()
        raise HTTPException(409, "source preview is not rendered yet")
    conn.execute("UPDATE answer_import_candidates SET ai_review_status='running' WHERE id=?", (candidate_id,))
    conn.commit()
    payload = {
        "image_base64": base64.b64encode(open(_candidate_image_disk_path(image_paths[0]), "rb").read()).decode("ascii"),
        "images_base64": [base64.b64encode(open(_candidate_image_disk_path(path), "rb").read()).decode("ascii") for path in image_paths],
        "problem_text": candidate["content_text"] or "",
        "ocr_text": candidate["ocr_text"] or "",
        "section_no": candidate["section_no"] or "",
        "problem_no": str(candidate["problem_no"] or "") +
                      (("(" + str(candidate["sub_no"]) + ")") if candidate["sub_no"] else ""),
        "subquestion_count": int(candidate["subquestion_count"] or 0),
    }
    url = os.environ.get("MATH_VLM_URL", "http://127.0.0.1:18080").rstrip("/") + "/review"
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("ptype") not in {"calc", "proof"}:
            raise ValueError("AI returned invalid ptype")
        if not str(result.get("std_answer", "")).strip():
            raise ValueError("AI returned an empty standard answer")
        expected_count = int(candidate["subquestion_count"] or 0)
        if expected_count:
            sub_answers = result.get("sub_answers")
            returned_numbers = [str(item.get("sub_no", "")) for item in sub_answers] if isinstance(sub_answers, list) else []
            expected_numbers = [str(number) for number in range(1, expected_count + 1)]
            if returned_numbers != expected_numbers:
                raise ValueError(
                    f"AI subquestion mismatch: expected {expected_numbers}, returned {returned_numbers}"
                )
        conn.execute("""
            UPDATE answer_import_candidates
            SET ai_review_json=?, ai_review_status='completed', ai_review_model=? WHERE id=?
        """, (json.dumps(result, ensure_ascii=False), result.get("model", "private-vlm"), candidate_id))
        conn.commit()
        return {"ok": True, "candidate_id": candidate_id, "result": result,
                "automatic_grading_enabled": False}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("detail", body)
        except Exception:
            detail = body
        conn.execute("UPDATE answer_import_candidates SET ai_review_status='failed' WHERE id=?", (candidate_id,))
        conn.commit()
        if "model_output_format_error" in str(detail) or "model did not return valid JSON" in str(detail):
            message = "服务器模型返回格式异常；图片已保留，请重新识别"
        else:
            message = "服务器 AI 暂时无法完成识别，请稍后重试"
        raise HTTPException(502, message)
    except Exception as exc:
        conn.execute("UPDATE answer_import_candidates SET ai_review_status='failed' WHERE id=?", (candidate_id,))
        conn.commit()
        raise HTTPException(502, "服务器 AI 连接或识别异常，请稍后重试")
    finally:
        conn.close()


@app.post("/answer-import-candidates/{candidate_id}/vision-task")
def queue_candidate_vision_task(candidate_id: int):
    """Queue only.  This endpoint never modifies a standard answer."""
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute("SELECT id,problem_id FROM answer_import_candidates WHERE id=?", (candidate_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "candidate not found")
    preview = _candidate_preview_path(candidate_id)
    disk_path = os.path.join(os.path.dirname(__file__), preview)
    if not os.path.isfile(disk_path):
        conn.close()
        raise HTTPException(409, "source preview is not rendered yet")
    existing = cur.execute("SELECT * FROM vision_recognition_tasks WHERE candidate_id=?", (candidate_id,)).fetchone()
    if existing:
        conn.close()
        return dict(existing)
    now = datetime.now().isoformat(timespec="seconds")
    task = {
        "id": str(uuid.uuid4()), "candidate_id": candidate_id, "problem_id": row["problem_id"],
        "task_type": "answer_pdf", "status": "pending", "provider": "pix2text",
        "input_image_path": preview, "created_at": now, "updated_at": now,
    }
    cur.execute("""
        INSERT INTO vision_recognition_tasks
        (id,candidate_id,problem_id,task_type,status,provider,input_image_path,created_at,updated_at)
        VALUES(:id,:candidate_id,:problem_id,:task_type,:status,:provider,:input_image_path,:created_at,:updated_at)
    """, task)
    cur.execute("UPDATE answer_import_candidates SET vision_status='pending' WHERE id=?", (candidate_id,))
    conn.commit()
    conn.close()
    return task


@app.get("/vision-tasks")
def list_vision_tasks(status: str = Query(""), limit: int = Query(100, ge=1, le=300)):
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT t.*, c.section_no, c.problem_no, c.sub_no, c.source_page, c.latex_text,
                   p.ptype FROM vision_recognition_tasks t
            JOIN answer_import_candidates c ON c.id=t.candidate_id
            JOIN problems p ON p.id=t.problem_id
            WHERE (?='' OR t.status=?) ORDER BY t.created_at DESC LIMIT ?
        """, (status, status, limit)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@app.post("/vision-tasks/{task_id}/run")
def run_vision_task(task_id: str):
    """Start one opt-in visual transcription task and store a candidate only.

    Pix2Text runs in a separate local process so slow CPU recognition never
    blocks the teacher workbench. Its result is never a standard answer.
    """
    conn = get_db()
    cur = conn.cursor()
    task = cur.execute("""
        SELECT t.*, c.section_no, c.problem_no, c.sub_no, c.ocr_text
        FROM vision_recognition_tasks t
        JOIN answer_import_candidates c ON c.id=t.candidate_id
        WHERE t.id=?
    """, (task_id,)).fetchone()
    if not task:
        conn.close()
        raise HTTPException(404, "vision task not found")
    image_path = os.path.join(os.path.dirname(__file__), task["input_image_path"])
    if not os.path.isfile(image_path):
        conn.close()
        raise HTTPException(409, "source preview file is missing")
    if task["provider"] == "pix2text":
        worker = os.path.join(os.path.dirname(__file__), "pix2text_worker.py")
        if not os.path.isfile(worker):
            conn.close()
            raise HTTPException(503, "Pix2Text worker is missing")
        local_python = os.environ.get("PIX2TEXT_PYTHON", sys.executable)
        if not os.path.isfile(local_python):
            local_python = sys.executable
        now = datetime.now().isoformat(timespec="seconds")
        cur.execute("UPDATE vision_recognition_tasks SET status='running',error_message='',updated_at=? WHERE id=?", (now, task_id))
        cur.execute("UPDATE answer_import_candidates SET vision_status='running' WHERE id=?", (task["candidate_id"],))
        conn.commit()
        try:
            log = open(os.path.join(os.path.dirname(__file__), "pix2text_worker.log"), "ab")
            subprocess.Popen(
                [local_python, worker, "--db", DB, "--task-id", task_id],
                cwd=os.path.dirname(__file__), stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            now = datetime.now().isoformat(timespec="seconds")
            cur.execute("UPDATE vision_recognition_tasks SET status='failed',error_message=?,updated_at=? WHERE id=?", (str(exc)[:1000], now, task_id))
            cur.execute("UPDATE answer_import_candidates SET vision_status='failed' WHERE id=?", (task["candidate_id"],))
            conn.commit()
            raise HTTPException(502, "could not start local Pix2Text: " + str(exc)[:300])
        finally:
            conn.close()
        return {"ok": True, "task_id": task_id, "status": "running", "provider": "pix2text"}
    api_key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("OPENAI_VISION_MODEL", "")
    if not api_key or not model:
        conn.close()
        raise HTTPException(503, "vision API is not configured; set OPENAI_API_KEY and OPENAI_VISION_MODEL")
    now = datetime.now().isoformat(timespec="seconds")
    cur.execute("UPDATE vision_recognition_tasks SET status='running',error_message='',updated_at=? WHERE id=?", (now, task_id))
    conn.commit()
    try:
        encoded = base64.b64encode(open(image_path, "rb").read()).decode("ascii")
        number = str(task["problem_no"]) + (f"({task['sub_no']})" if task["sub_no"] else "")
        anchor = str(task["ocr_text"] or "")[:900]
        prompt = (
            f"This is an answer-book page. Target only section {task['section_no']}, problem {number}; "
            "ignore all other problems, running headers, page numbers, and figures. "
            "Transcribe the target mathematical final answer and its solution exactly. Do not solve the problem. "
            "Return JSON only: {final_answer_latex:string, solution_latex:string, confidence:number, notes:string}. "
            "Use LaTeX; if uncertain, preserve uncertainty in notes rather than inventing symbols. "
            f"A noisy OCR anchor for locating the target is: {anchor}"
        )
        body = json.dumps({
            "model": model,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}", "detail": "high"},
            ]}],
        }).encode("utf-8")
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses", data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = json.loads(response.read().decode("utf-8"))
        output = str(raw.get("output_text", "")).strip()
        if output.startswith("```"):
            output = output.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(output)
        latex = str(result.get("final_answer_latex", "")).strip()
        confidence = float(result.get("confidence", 0) or 0)
        now = datetime.now().isoformat(timespec="seconds")
        cur.execute("""
            UPDATE vision_recognition_tasks SET status='completed',result_json=?,updated_at=? WHERE id=?
        """, (json.dumps(result, ensure_ascii=False), now, task_id))
        cur.execute("""
            UPDATE answer_import_candidates SET latex_text=?,vision_status='completed',vision_confidence=?
            WHERE id=?
        """, (latex, confidence, task["candidate_id"]))
        conn.commit()
        return {"ok": True, "task_id": task_id, "result": result}
    except Exception as exc:
        now = datetime.now().isoformat(timespec="seconds")
        cur.execute("UPDATE vision_recognition_tasks SET status='failed',error_message=?,updated_at=? WHERE id=?",
                    (str(exc)[:1000], now, task_id))
        cur.execute("UPDATE answer_import_candidates SET vision_status='failed' WHERE id=?", (task["candidate_id"],))
        conn.commit()
        raise HTTPException(502, "vision transcription failed: " + str(exc)[:300])
    finally:
        conn.close()


@app.post("/answer-import-candidates/{candidate_id}/review")
def review_answer_import_candidate(candidate_id: int, req: AnswerCandidateReviewReq):
    if req.action not in {"approved", "rejected", "provisional"}:
        raise HTTPException(400, "action must be approved or rejected")
    if req.ptype is not None and req.ptype not in {"calc", "proof"}:
        raise HTTPException(400, "ptype must be calc or proof")
    conn = get_db()
    cur = conn.cursor()
    candidate = cur.execute("""
        SELECT c.*, p.ptype FROM answer_import_candidates c
        JOIN problems p ON p.id=c.problem_id WHERE c.id=?
    """, (candidate_id,)).fetchone()
    if not candidate:
        conn.close()
        raise HTTPException(404, "candidate not found")
    effective_ptype = req.ptype or candidate["ptype"]
    now = datetime.now().isoformat(timespec="seconds")
    if req.action in {"approved", "provisional"}:
        content = (req.content_text or "").strip() if req.content_text is not None else None
        if content is not None and len(content) < 3:
            conn.close(); raise HTTPException(400, "cannot approve: content_text too short")
        answer = (req.std_answer or "").strip()
        solution = (req.full_solution or "").strip()
        issue = answer_quality_issue(answer, effective_ptype, solution)
        if issue and req.action == "approved":
            conn.close()
            raise HTTPException(400, "cannot approve: " + issue)
        if req.action == "provisional" and (not answer or content is None):
            conn.close()
            raise HTTPException(400, "cannot provision: recovered stem or answer is empty")
        status = "verified" if req.action == "approved" else "ai_candidate"
        reason = "" if req.action == "approved" else "来源图自动重建结果；可检索和选题，升级验证前不启用自动评分"
        cur.execute("""
            UPDATE problems SET content_text=COALESCE(?, content_text), std_answer=?, full_solution=?, ptype=?, answer_status=?,
                answer_invalid_reason=? WHERE id=?
        """, (content, answer, solution or None, effective_ptype, status, reason, candidate["problem_id"]))
    elif req.ptype is not None:
        cur.execute("UPDATE problems SET ptype=? WHERE id=?",
                    (effective_ptype, candidate["problem_id"]))
    cur.execute("""
        UPDATE answer_import_candidates
        SET match_status=?, review_note=?, reviewed_at=? WHERE id=?
    """, (req.action, (req.note or "").strip(), now, candidate_id))
    conn.commit()
    conn.close()
    return {"ok": True, "candidate_id": candidate_id, "status": req.action,
            "ptype": effective_ptype,
            "automatic_grading_enabled": req.action == "approved" and effective_ptype == "calc"}


@app.get("/ocr-repair/reviews")
def list_ocr_repair_reviews(status: str = Query("pending"), limit: int = Query(100, ge=1, le=300)):
    """Return OCR repair evidence; this endpoint never exposes a write-back action."""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT d.problem_id,d.decision,d.decision_json,d.teacher_status,d.teacher_note,
                   p.content_text,p.std_answer,p.full_solution,p.ptype,s.section_no,p.problem_no,
                   a.id AS anchor_id,a.crop_path,a.confidence AS anchor_confidence
            FROM ocr_repair_decisions d JOIN problems p ON p.id=d.problem_id
            JOIN sections s ON s.id=p.section_id LEFT JOIN problem_source_anchors a ON a.problem_id=d.problem_id
            WHERE (?='' OR d.teacher_status=?) ORDER BY d.updated_at DESC LIMIT ?
        """, (status, status, limit)).fetchall()
        items=[]
        for row in rows:
            item=dict(row)
            item['candidates']=[dict(candidate) for candidate in conn.execute("""
              SELECT id,provider,crop_path,latex_text,confidence,risks_json,status,updated_at
              FROM ocr_repair_candidates WHERE problem_id=? ORDER BY provider""",(row['problem_id'],)).fetchall()]
            items.append(item)
        return {"items":items}
    finally: conn.close()


@app.get("/ocr-repair/crops/{anchor_id}")
def get_ocr_repair_crop(anchor_id: int):
    conn=get_db()
    try:
        row=conn.execute("SELECT crop_path FROM problem_source_anchors WHERE id=? AND status='candidate'",(anchor_id,)).fetchone()
    finally: conn.close()
    if not row or not row['crop_path']: raise HTTPException(404,"candidate crop not found")
    root=Path(os.environ.get("OCR_REPAIR_IMAGE_ROOT", str(Path(__file__).resolve().parents[2] / 'answer_source_previews'))).resolve()
    path=(root / row['crop_path']).resolve()
    try: path.relative_to(root)
    except ValueError: raise HTTPException(400,"invalid crop path")
    if not path.is_file(): raise HTTPException(404,"candidate crop file is unavailable")
    return FileResponse(path, media_type='image/png')


@app.post("/ocr-repair/reviews/{problem_id}")
def review_ocr_repair_candidate(problem_id: str, body: dict = Body(...)):
    """Teacher confirm/reject is metadata-only; it never changes problems."""
    action=str(body.get('action') or '')
    if action not in {'confirmed','rejected'}: raise HTTPException(400,"action must be confirmed or rejected")
    note=str(body.get('note') or '').strip()[:2000]
    conn=get_db()
    try:
        exists=conn.execute("SELECT 1 FROM ocr_repair_decisions WHERE problem_id=?",(problem_id,)).fetchone()
        if not exists: raise HTTPException(404,"OCR repair decision not found")
        conn.execute("UPDATE ocr_repair_decisions SET teacher_status=?,teacher_note=?,updated_at=? WHERE problem_id=?",
                     (action,note,datetime.now().isoformat(timespec='seconds'),problem_id))
        conn.commit()
        return {"ok":True,"problem_id":problem_id,"teacher_status":action,"question_bank_written":False}
    finally: conn.close()


@app.post("/ocr-repair/reviews/{problem_id}/writeback")
def writeback_ocr_repair_candidate(problem_id: str, body: dict = Body(...)):
    """Explicit teacher adoption of edited OCR evidence into the question bank.

    Merely opening a candidate or recording a normal confirmation never reaches
    this endpoint.  This endpoint requires a deliberate ``confirm: true`` and
    writes an immutable before/after audit row in the same transaction.
    """
    if body.get("confirm") is not True:
        raise HTTPException(400, "writeback requires confirm=true")
    content_text = str(body.get("content_text") or "").strip()
    std_answer = str(body.get("std_answer") or "").strip()
    full_solution = str(body.get("full_solution") or "").strip()
    note = str(body.get("note") or "").strip()[:2000]
    if not content_text:
        raise HTTPException(400, "题干不能为空")
    if not std_answer:
        raise HTTPException(400, "标准答案不能为空；请先从原图核对并填写")
    conn = get_db()
    try:
        decision = conn.execute("SELECT decision FROM ocr_repair_decisions WHERE problem_id=?", (problem_id,)).fetchone()
        row = conn.execute("SELECT content_text,std_answer,full_solution,ptype,answer_status FROM problems WHERE id=?", (problem_id,)).fetchone()
        if not decision:
            raise HTTPException(404, "OCR repair decision not found")
        if not row:
            raise HTTPException(404, "problem not found")
        issue = answer_quality_issue(std_answer, row["ptype"], full_solution)
        if issue:
            raise HTTPException(400, "不能写回：" + issue)
        before = {"content_text": row["content_text"] or "", "std_answer": row["std_answer"] or "",
                  "full_solution": row["full_solution"] or "", "answer_status": row["answer_status"] or ""}
        after = {"content_text": content_text, "std_answer": std_answer, "full_solution": full_solution,
                 "answer_status": "verified"}
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("""UPDATE problems SET content_text=?,std_answer=?,full_solution=?,
                     answer_status='verified',answer_invalid_reason='' WHERE id=?""",
                     (content_text, std_answer, full_solution or None, problem_id))
        conn.execute("""UPDATE ocr_repair_decisions SET teacher_status='committed',teacher_note=?,updated_at=?
                     WHERE problem_id=?""", (note, now, problem_id))
        conn.execute("UPDATE ocr_repair_candidates SET status='teacher_adopted',updated_at=? WHERE problem_id=?",
                     (now, problem_id))
        conn.execute("""INSERT INTO ocr_repair_writebacks(id,problem_id,decision,before_json,after_json,teacher_note,created_at)
                     VALUES(?,?,?,?,?,?,?)""",
                     (uuid.uuid4().hex, problem_id, decision["decision"], json.dumps(before, ensure_ascii=False),
                      json.dumps(after, ensure_ascii=False), note, now))
        conn.commit()
        return {"ok": True, "problem_id": problem_id, "question_bank_written": True,
                "answer_status": "verified", "audit_recorded": True}
    finally:
        conn.close()


REVIEW_SAMPLING_DB = os.path.join(os.path.dirname(__file__), "review_sample.json")


def _load_review_state():
    if os.path.exists(REVIEW_SAMPLING_DB):
        return json.load(open(REVIEW_SAMPLING_DB, encoding="utf-8"))
    return {"rounds": [], "student_reviews": {}}


def _save_review_state(state):
    json.dump(state, open(REVIEW_SAMPLING_DB, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


@app.get("/review/sample")
def get_review_sample(class_id: str = Query(...), sample_size: int = Query(5)):
    """按班级轮换抽样：每轮从每班抽N人需人工复核，保证每人每学期至少2次"""
    state = _load_review_state()
    conn = get_db()
    cur = conn.cursor()
    students = cur.execute(
        "SELECT id,student_no,name FROM students WHERE class_id=? ORDER BY student_no",
        (class_id,)).fetchall()
    if not students:
        conn.close()
        return {"class_id": class_id, "sample": [], "message": "班级无学生"}

    # 按复核次数升序 + 上次复核时间升序排序
    reviews = state.get("student_reviews", {})
    student_meta = []
    for s in students:
        sid = s["id"]
        meta = reviews.get(sid, {"count": 0, "last_review": None})
        student_meta.append({
            "id": sid, "student_no": s["student_no"], "name": s["name"],
            "review_count": meta.get("count", 0),
            "last_review": meta.get("last_review", "")
        })

    # 排序：复核次数少的优先，同次数则上次复核早的优先
    student_meta.sort(key=lambda x: (x["review_count"], x["last_review"] or "9999"))
    sample = student_meta[:sample_size]

    # 记录本轮
    now = datetime.now().isoformat(timespec="seconds")
    round_info = {"date": now, "class_id": class_id, "sample": [s["id"] for s in sample]}
    state["rounds"].append(round_info)
    for s in sample:
        sid = s["id"]
        if sid not in reviews:
            reviews[sid] = {"count": 0}
        reviews[sid]["count"] = reviews[sid].get("count", 0) + 1
        reviews[sid]["last_review"] = now
    _save_review_state(state)

    # 获取这些学生的所有提交（按时间倒序），区分别批改/已批改
    placeholders = ",".join("?" * len(sample))
    sids = [s["id"] for s in sample]
    # 通过 student_no 匹配提交
    snos = [s["student_no"] for s in sample]
    snos_ph = ",".join("?" * len(snos))
    all_subs_rows = cur.execute(
        f"SELECT sbm.id,sbm.homework_id,sbm.student_no,sbm.student_name,sbm.submitted_at,"
        f"sbm.status,sbm.score,hw.title as homework_title,hw.section_no "
        f"FROM submissions sbm JOIN homeworks hw ON hw.id=sbm.homework_id "
        f"WHERE hw.class_id=? AND sbm.student_no IN ({snos_ph}) "
        f"ORDER BY sbm.submitted_at DESC",
        [class_id] + snos).fetchall()
    conn.close()

    sample_with_subs = []
    for s in sample:
        student_subs = [dict(r) for r in all_subs_rows if r["student_no"] == s["student_no"]]
        pending = [x for x in student_subs if x["status"] == "pending"]
        graded = [x for x in student_subs if x["status"] == "graded"]
        sample_with_subs.append({
            **s,
            "pending_submissions": pending,
            "graded_submissions": graded,
            "all_submissions": student_subs
        })

    return {"class_id": class_id, "sample_size": sample_size,
            "round": len(state["rounds"]), "sample": sample_with_subs}


@app.get("/review/stats")
def review_stats(class_id: Optional[str] = None):
    """查看各学生的复核次数统计"""
    state = _load_review_state()
    reviews = state.get("student_reviews", {})
    conn = get_db()
    where = "WHERE st.class_id=?" if class_id else ""
    args = [class_id] if class_id else []
    rows = conn.execute(
        f"SELECT st.id,st.student_no,st.name,c.name as class_name FROM students st "
        f"JOIN classes c ON c.id=st.class_id {where} ORDER BY c.name,st.student_no",
        args).fetchall()
    conn.close()
    result = []
    for r in rows:
        meta = reviews.get(r["id"], {"count": 0, "last_review": None})
        result.append({
            "student_id": r["id"], "student_no": r["student_no"],
            "name": r["name"], "class_name": r["class_name"],
            "review_count": meta.get("count", 0),
            "last_review": meta.get("last_review", "")
        })
    return {"total": len(result), "students": result}


class ReviewNoteReq(BaseModel):
    note: str = ""


@app.post("/review/record/{student_id}")
def record_review(student_id: str, req: ReviewNoteReq):
    """记录一次人工复核，更新学生复核次数与上次复核时间"""
    state = _load_review_state()
    reviews = state.setdefault("student_reviews", {})
    if student_id not in reviews:
        reviews[student_id] = {"count": 0}
    reviews[student_id]["count"] = reviews[student_id].get("count", 0) + 1
    reviews[student_id]["last_review"] = datetime.now().isoformat(timespec="seconds")
    if req.note:
        reviews[student_id].setdefault("notes", []).append({
            "time": reviews[student_id]["last_review"], "note": req.note})
    _save_review_state(state)
    return {"student_id": student_id, "review_count": reviews[student_id]["count"],
            "last_review": reviews[student_id]["last_review"]}


@app.get("/review/student/{student_id}")
def student_review_detail(student_id: str):
    """获取某学生的全部提交，用于复核弹窗"""
    conn = get_db()
    cur = conn.cursor()
    student = cur.execute(
        "SELECT id,student_no,name,class_id FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        conn.close()
        raise HTTPException(404, "学生不存在")
    subs = cur.execute(
        "SELECT id,homework_id,student_no,student_name,status,score,submitted_at,answers "
        "FROM submissions WHERE student_no=? ORDER BY submitted_at DESC",
        (student["student_no"],)).fetchall()
    result = []
    for s in subs:
        hw = cur.execute("SELECT title,section_no,problem_ids FROM homeworks WHERE id=?",
                         (s["homework_id"],)).fetchone()
        result.append({
            "id": s["id"], "homework_id": s["homework_id"],
            "student_no": s["student_no"], "student_name": s["student_name"],
            "status": s["status"], "score": s["score"],
            "submitted_at": s["submitted_at"], "answers": s["answers"],
            "homework_title": hw["title"] if hw else "",
            "section_no": hw["section_no"] if hw else "",
            "problem_ids": json.loads(hw["problem_ids"] or "[]") if hw else []
        })
    conn.close()
    return {"student": dict(student), "submissions": result}


# ----- 学期成绩汇总 -----
@app.get("/reports/semester")
def semester_report(class_id: str = Query(...)):
    """按班级汇总学期成绩：每人各次作业得分、平均分、趋势"""
    conn = get_db()
    cur = conn.cursor()
    students = cur.execute(
        "SELECT id,student_no,name FROM students WHERE class_id=? ORDER BY student_no",
        (class_id,)).fetchall()
    if not students:
        conn.close()
        return {"class_id": class_id, "students": [], "message": "班级无学生"}

    homeworks = cur.execute(
        "SELECT hw.id,hw.title,hw.section_no,hw.deadline,hw.status "
        "FROM homeworks hw WHERE hw.class_id=? ORDER BY hw.created_at",
        (class_id,)).fetchall()

    result_students = []
    for st in students:
        subs = cur.execute(
            "SELECT sbm.id,sbm.homework_id,sbm.score,sbm.status,sbm.submitted_at "
            "FROM submissions sbm WHERE sbm.student_no=? ORDER BY sbm.submitted_at",
            (st["student_no"],)).fetchall()
        scores = {}
        for s in subs:
            scores[s["homework_id"]] = {
                "score": s["score"], "status": s["status"],
                "submitted_at": s["submitted_at"]
            }

        hw_scores = []
        total = 0.0
        count = 0
        for hw in homeworks:
            sd = scores.get(hw["id"], {})
            s = sd.get("score")
            hw_scores.append({
                "homework_id": hw["id"], "title": hw["title"],
                "section_no": hw["section_no"],
                "score": s, "status": sd.get("status", "未提交"),
                "submitted_at": sd.get("submitted_at", "")
            })
            if s is not None:
                total += s
                count += 1

        avg = round(total / count, 1) if count > 0 else None
        result_students.append({
            "student_no": st["student_no"], "name": st["name"],
            "average_score": avg, "graded_count": count,
            "total_homeworks": len(homeworks),
            "homework_scores": hw_scores
        })

    # 班级整体统计
    all_avgs = [s["average_score"] for s in result_students if s["average_score"] is not None]
    class_avg = round(sum(all_avgs) / len(all_avgs), 1) if all_avgs else None

    conn.close()
    return {
        "class_id": class_id,
        "homework_count": len(homeworks),
        "student_count": len(result_students),
        "class_average": class_avg,
        "homeworks": [{"id": hw["id"], "title": hw["title"],
                        "section_no": hw["section_no"]} for hw in homeworks],
        "students": result_students
    }


# ----- 学生端：我的成绩/提交记录 -----

@app.get("/students/me/submissions")
def student_my_submissions(student_no: str = Query(...), student_name: str = Query("")):
    """按学号返回该生全部提交记录（用于学生端查看每次作业得分）。
    仅按 student_no 过滤，天然只能看到本人数据；student_name 仅用于前端展示比对，不强制匹配。
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT sbm.id, sbm.homework_id, sbm.student_no, sbm.student_name, sbm.status, "
        "sbm.score, sbm.submitted_at, hw.title AS homework_title, hw.section_no, hw.deadline, "
        "hw.status AS hw_status "
        "FROM submissions sbm JOIN homeworks hw ON hw.id=sbm.homework_id "
        "WHERE sbm.student_no=? ORDER BY sbm.submitted_at DESC",
        (student_no,)).fetchall()
    result = [dict(r) for r in rows]
    # 计算该生平均分/总分（仅统计已批改）
    graded = [r for r in result if r["status"] == "graded" and r["score"] is not None]
    summary = {
        "submitted_count": len(result),
        "graded_count": len(graded),
        "avg_score": round(sum(r["score"] for r in graded) / len(graded), 1) if graded else None,
        "total_score": round(sum(r["score"] for r in graded), 1) if graded else None,
    }
    conn.close()
    return {"student_no": student_no, "name": student_name,
            "submissions": result, "summary": summary}


@app.post("/submissions/{sid}/upload")
async def upload_answer_image(sid: str, problem_id: str = Query(...)):
    """上传学生手写作答图片（multipart/form-data）
    
    使用方式：POST /submissions/{sid}/upload?problem_id=xxx
    表单字段：image（文件）
    
    上传后自动 OCR 识别并存入 submission.answers
    """
    from fastapi import UploadFile, File
    # 由于签名限制，此端点通过简单方式处理上传
    return {
        "message": "请使用 multipart/form-data 上传图片",
        "submission_id": sid,
        "problem_id": problem_id,
        "usage": "POST 表单字段 image=<文件>; 上传后将自动 OCR 识别数学表达式"
    }


@app.get("/stats/tiers")
def stats_tiers():
    """获取难度分层统计（用于仪表盘）"""
    conn = get_db()
    rows = conn.execute(
        "SELECT tier, COUNT(*) as cnt FROM problems WHERE tier IS NOT NULL AND tier != '' "
        "GROUP BY tier").fetchall()
    conn.close()
    tier_map = {"basic": "基础训练", "medium": "综合提高", "advanced": "拓展挑战"}
    result = {}
    for r in rows:
        result[tier_map.get(r["tier"], r["tier"])] = r["cnt"]
    return result


class AgentAnswerVerifyReq(BaseModel):
    student_answer: str
    standard_answer: str
    problem_text: str = ""
    section_no: str = ""
    problem_no: str = ""


@app.post("/agent/verify-answer")
def agent_verify_answer(req: AgentAnswerVerifyReq):
    """使用 LangGraph 编排的答案校验 Agent。"""
    from math_agent_graph import run_math_agent
    return run_math_agent(req.student_answer, req.standard_answer, req.problem_text, req.section_no, req.problem_no)

class AgentProblemLearnReq(BaseModel):
    student_answer: str
    student_steps: str = ""
    mode: str = "diagnose"
    teacher_feedback: str = ""


@app.post("/agent/problems/{problem_id}/learn")
def agent_learn_problem(problem_id: str, req: AgentProblemLearnReq):
    """从题库读取答案，由 LangGraph 学习 Agent 处理，绝不向客户端返回标准答案。"""
    conn = get_db()
    row = conn.execute("SELECT p.content_text, p.std_answer, p.ptype, s.section_no, p.problem_no FROM problems p JOIN sections s ON s.id=p.section_id WHERE p.id=?", (problem_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "题目不存在")
    if not str(row["std_answer"] or "").strip():
        raise HTTPException(409, "该题尚无可用标准答案，暂不能启动学习 Agent")
    from math_agent_graph import run_math_agent
    result = run_math_agent(req.student_answer, row["std_answer"], row["content_text"] or "", row["section_no"] or "", row["problem_no"] or "", req.mode, problem_id, req.teacher_feedback, row["ptype"] or "calc", req.student_steps)
    return {"problem_id": problem_id, "action": result.get("action"), "verification": result.get("verification"), "solution_comparison": result.get("solution_comparison"), "diagnosis": result.get("diagnosis"), "proof_assessment": result.get("proof_assessment"), "evidence": result.get("evidence"), "trace_id": result.get("trace_id"), "execution_trace": result.get("execution_trace"), "response": result.get("response")}



class AgentProblemImageLearnReq(BaseModel):
    image_base64: str
    mode: str = "diagnose"
    teacher_feedback: str = ""
    student_steps: str = ""


class AgentProblemStepImagesLearnReq(BaseModel):
    image_base64_list: list[str]
    mode: str = "diagnose"
    teacher_feedback: str = ""
    student_steps: str = ""


@app.post("/agent/problems/{problem_id}/learn-image")
def agent_learn_problem_image(problem_id: str, req: AgentProblemImageLearnReq):
    """Read one handwritten answer image with local Qwen, then run the learning Agent."""
    if len(req.image_base64) > 12_000_000:
        raise HTTPException(413, "图片过大，请压缩到 8MB 以内")
    conn = get_db()
    row = conn.execute("SELECT p.content_text, p.std_answer, p.full_solution, p.ptype, s.section_no, p.problem_no FROM problems p JOIN sections s ON s.id=p.section_id WHERE p.id=?", (problem_id,)).fetchone()
    conn.close()
    if not row or not str(row["std_answer"] or "").strip():
        raise HTTPException(409, "该题暂无可用标准答案")
    from skills.answer_perception import answer_perception
    from skills.schemas import AnswerPerceptionInput

    perception_started = time.perf_counter()
    perception = answer_perception(AnswerPerceptionInput(
        image_base64=req.image_base64, problem_id=problem_id,
        problem_text=row["content_text"] or "",
    ))
    perception_latency_ms = round((time.perf_counter() - perception_started) * 1000, 1)
    if not perception.success:
        status = 422 if perception.error_code == "EMPTY_OCR_RESULT" else 502
        raise HTTPException(status, (perception.warnings or ["手写数学识别暂不可用"])[0])
    recognized = perception.recognized_work or ""
    from math_agent_graph import run_math_agent
    result = run_math_agent(recognized, row["std_answer"], row["content_text"] or "", row["section_no"] or "", row["problem_no"] or "", req.mode, problem_id, req.teacher_feedback, row["ptype"] or "calc", req.student_steps)
    perception_event = {
        "node": "answer_perception", "skills": ["answer_perception"],
        "skill_versions": {"answer_perception": "1.0.0"},
        "latency_ms": perception_latency_ms, "success": perception.success,
        "confidence": perception.confidence, "error_code": perception.error_code,
        "action": "continue_to_verification",
    }
    result["execution_trace"] = [perception_event, *(result.get("execution_trace") or [])]
    perception_public = {
        "success": perception.success, "confidence": perception.confidence,
        "provider": perception.provider, "formula_regions": [r.model_dump(exclude_none=True) for r in perception.formula_regions],
        "warnings": perception.warnings, "error_code": perception.error_code,
    }
    return {"problem_id": problem_id, "recognized_work": recognized, "perception": perception_public,
            "action": result.get("action"), "verification": result.get("verification"), "proof_assessment": result.get("proof_assessment"),
            "solution_comparison": result.get("solution_comparison"), "diagnosis": result.get("diagnosis"),
            "evidence": result.get("evidence"), "trace_id": result.get("trace_id"),
            "execution_trace": result.get("execution_trace"), "response": result.get("response")}



@app.post("/agent/problems/{problem_id}/learn-step-images")
def agent_learn_problem_step_images(problem_id: str, req: AgentProblemStepImagesLearnReq):
    """Recognize up to six handwritten step images in order, then diagnose from step evidence."""
    images = req.image_base64_list or []
    if not images:
        raise HTTPException(422, "请至少上传一张步骤图片")
    if len(images) > 6:
        raise HTTPException(422, "一次最多上传 6 张步骤图片")
    if any(len(image) > 12_000_000 for image in images) or sum(len(image) for image in images) > 36_000_000:
        raise HTTPException(413, "步骤图片过大，请压缩后重试")
    conn = get_db()
    row = conn.execute("SELECT p.content_text, p.std_answer, p.ptype, s.section_no, p.problem_no FROM problems p JOIN sections s ON s.id=p.section_id WHERE p.id=?", (problem_id,)).fetchone()
    conn.close()
    if not row or not str(row["std_answer"] or "").strip():
        raise HTTPException(409, "该题暂无可用标准答案")

    from skills.answer_perception import answer_perception
    from skills.schemas import AnswerPerceptionInput
    recognized_parts, perception_events, perception_public = [], [], []
    for index, image_base64 in enumerate(images, start=1):
        started = time.perf_counter()
        perception = answer_perception(AnswerPerceptionInput(
            image_base64=image_base64, problem_id=problem_id,
            problem_text=row["content_text"] or "",
        ))
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        recognized = (perception.recognized_work or "").strip()
        perception_events.append({
            "node": "answer_perception", "skills": ["answer_perception"],
            "skill_versions": {"answer_perception": "1.0.0"},
            "step_index": index, "latency_ms": latency_ms, "success": perception.success,
            "confidence": perception.confidence, "error_code": perception.error_code,
            "action": "continue_to_step_diagnosis" if perception.success else "skip_unreadable_step",
        })
        perception_public.append({
            "step_index": index, "success": perception.success, "confidence": perception.confidence,
            "warnings": perception.warnings, "error_code": perception.error_code,
        })
        if perception.success and recognized:
            recognized_parts.append((index, recognized))
    if not recognized_parts:
        raise HTTPException(422, "未能从步骤图片中识别到可靠作答，请拍清楚后重试")

    recognized_steps = "\n\n".join(
        f"【步骤图 {index}】\n{text}" for index, text in recognized_parts
    )
    full_steps = "\n\n".join(part for part in [req.student_steps.strip(), recognized_steps] if part)
    confidences = [float(item.get("confidence") or 0) for item in perception_public if item.get("success")]
    quality_warnings = []
    if len(recognized_parts) < len(images):
        quality_warnings.append("部分步骤图未能可靠识别")
    if confidences and min(confidences) < 0.70:
        quality_warnings.append("至少一张步骤图识别置信度偏低")
    # Conservative detector: multiple explicit top-level question labels are
    # unsafe for one-question diagnosis. Sub-question markers （1）（2） do not trigger it.
    top_level_markers = re.findall(r"(?:第\s*\d+\s*题|^\s*\d+[\.、])", recognized_steps, flags=re.M)
    if len(top_level_markers) > 1:
        quality_warnings.append("识别到可能混入多道题，请裁切为当前错题的步骤图")
    step_material_quality = {
        "uploaded_step_count": len(images), "recognized_step_count": len(recognized_parts),
        "minimum_confidence": min(confidences) if confidences else 0.0,
        "warnings": quality_warnings, "sufficient": not quality_warnings,
    }
    # The final selected image is only a candidate conclusion. Verification still
    # depends on the hidden standard answer and deterministic workflow.
    candidate_answer = recognized_parts[-1][1]
    from math_agent_graph import run_math_agent
    result = run_math_agent(
        candidate_answer, row["std_answer"], row["content_text"] or "",
        row["section_no"] or "", row["problem_no"] or "", req.mode, problem_id,
        req.teacher_feedback, row["ptype"] or "calc", full_steps,
    )
    result["execution_trace"] = [*perception_events, *(result.get("execution_trace") or [])]
    if quality_warnings:
        result["action"] = "teacher_review"
        result["response"] = "步骤材料暂不适合自动诊断：" + "；".join(quality_warnings) + "。请重新上传仅包含当前题、字迹清晰的完整步骤图；证明题或仍不清晰的材料将由教师确认。"
    return {
        "problem_id": problem_id, "recognized_step_count": len(recognized_parts),
        "uploaded_step_count": len(images), "step_recognition": perception_public,
        "step_material_quality": step_material_quality,
        "action": result.get("action"), "verification": result.get("verification"),
        "proof_assessment": result.get("proof_assessment"),
        "solution_comparison": result.get("solution_comparison"), "diagnosis": result.get("diagnosis"),
        "evidence": result.get("evidence"), "trace_id": result.get("trace_id"),
        "execution_trace": result.get("execution_trace"), "response": result.get("response"),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8011)
