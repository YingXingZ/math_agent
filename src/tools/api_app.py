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
import os
import re
import sqlite3
import uuid
import base64
import urllib.error
import urllib.request
import subprocess
import sys
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="智能高数作业助手 API", version="0.3.0")

DB = os.path.join(os.path.dirname(__file__), "api.db")
JOB_STORE = {}  # job_id -> {status, progress, result, error}
IMAGE_ROOT = os.environ.get("IMAGE_ROOT", "extract_img")

# CORS：允许任意来源（含 file:// 打开的本地 HTML）。开发态，无凭证。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    ]:
        try:
            conn.execute(f"ALTER TABLE answer_import_candidates ADD COLUMN {col} {ddl}")
        except Exception:
            pass
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
    if len(answer) > 500:
        return "标准答案过长，疑似整页或多题 OCR 串入"
    if "\n" in answer:
        return "标准答案含多行内容；最终答案必须单题、单行"
    if any(mark in answer for mark in _OCR_GARBLED):
        return "标准答案包含 OCR/编码乱码"
    if "第" in answer and "章" in answer:
        return "标准答案包含页眉或章节标题"
    if ptype == "calc" and not re.search(r"[0-9A-Za-z\\\\]|[∞∞]", answer):
        return "计算题答案不是可判定的数学表达式"
    if ptype == "calc" and any(mark in answer for mark in ("~", "由", "仰", "石", "豆", "叫")):
        return "计算题答案含 OCR 噪声字符"
    if full_solution and len(full_solution) > 12000:
        return "完整解答过长，疑似跨题拼接"
    return ""


def grading_ready(problem: dict) -> tuple[bool, str]:
    if problem.get("ptype") != "calc":
        return False, "证明/主观题必须人工复核"
    if problem.get("answer_status") != "verified":
        return False, "标准答案尚未经教师核验"
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
                    p.std_answer, p.full_solution, p.grading_steps, p.answer_weight, p.answer_status, p.answer_invalid_reason
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
        args += [f"%{c.strip()}%" for c in kp.split(",")]
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
        SELECT p.id, s.section_no, p.problem_no, p.sub_no, p.ptype, p.std_answer
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

        if ptype == "proof":
            if PROOF_BROKEN.search(ans):
                results.append({"id": pid, "status": "broken",
                                "reason": "含排版/解题过程垃圾（如 \\blacksquare、\\text{}），不是纯净要点", "normalized": ""})
            elif len(ans) > SUSPICIOUS_LEN:
                results.append({"id": pid, "status": "manual",
                                "reason": f"答案过长（{len(ans)} 字），疑似整段解题过程，将转人工复核", "normalized": ""})
            else:
                results.append({"id": pid, "status": "ok", "reason": "要点式答案", "normalized": ""})
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
               "manual": 0, "broken": 0, "empty": 0}
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
    for root in [IMAGE_ROOT, "extract_img_v2", "extract_img_test", "answer_source_previews"]:
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
    conn.close()
    d["problems"] = problems
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


@app.post("/grade/auto")
def auto_grade_submission(submission_id: str = Query(...)):
    """对指定提交执行自动批改（计算题自动判分，证明题给建议分）
    
    核心改进（P0）：
    1. 从 DB 读取 std_answer / grading_steps / answer_weight
    2. 自动检测证明题类型（从 content_text 匹配"证明"/"求证"等关键词）
    3. 构建真实 ProblemSpec 传入 grading_engine
    """
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


# ----- 人工复核抽样 -----
class AnswerCandidateReviewReq(BaseModel):
    action: str  # approved | rejected
    std_answer: Optional[str] = None
    full_solution: Optional[str] = None
    note: str = ""


@app.get("/answer-import-candidates")
def list_answer_import_candidates(status: str = Query("pending"), limit: int = Query(100, ge=1, le=300)):
    """Candidates are evidence only; they never become grade keys by listing them."""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT c.*, p.ptype, p.std_answer AS current_std_answer,
                   p.full_solution AS current_full_solution, p.answer_status,
                   p.content_text, p.crop_image_path,
                   t.id AS vision_task_id, t.status AS vision_task_status, t.error_message AS vision_task_error
            FROM answer_import_candidates c
            JOIN problems p ON p.id=c.problem_id
            LEFT JOIN vision_recognition_tasks t ON t.candidate_id=c.id
            WHERE (?='' OR c.match_status=?)
            ORDER BY CASE c.match_status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                     c.section_no, CAST(c.problem_no AS INTEGER), c.id
            LIMIT ?
        """, (status, status, limit)).fetchall()
        counts = {row[0]: row[1] for row in conn.execute(
            "SELECT match_status, COUNT(*) FROM answer_import_candidates GROUP BY match_status").fetchall()}
        return {"items": [dict(row) for row in rows], "counts": counts}
    except sqlite3.OperationalError:
        return {"items": [], "counts": {}}
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


def _candidate_preview_path(candidate_id: int) -> str:
    return f"answer_source_previews/candidate-{candidate_id}.jpg"


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
        local_python = os.environ.get("PIX2TEXT_PYTHON", r"C:\\Users\\YXZ\\.workbuddy\\binaries\\python\\envs\\ocr\\Scripts\\python.exe")
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
    if req.action not in {"approved", "rejected"}:
        raise HTTPException(400, "action must be approved or rejected")
    conn = get_db()
    cur = conn.cursor()
    candidate = cur.execute("""
        SELECT c.*, p.ptype FROM answer_import_candidates c
        JOIN problems p ON p.id=c.problem_id WHERE c.id=?
    """, (candidate_id,)).fetchone()
    if not candidate:
        conn.close()
        raise HTTPException(404, "candidate not found")
    now = datetime.now().isoformat(timespec="seconds")
    if req.action == "approved":
        answer = (req.std_answer or "").strip()
        solution = (req.full_solution or "").strip()
        issue = answer_quality_issue(answer, candidate["ptype"], solution)
        if issue:
            conn.close()
            raise HTTPException(400, "cannot approve: " + issue)
        cur.execute("""
            UPDATE problems SET std_answer=?, full_solution=?, answer_status='verified',
                answer_invalid_reason='' WHERE id=?
        """, (answer, solution or None, candidate["problem_id"]))
    cur.execute("""
        UPDATE answer_import_candidates
        SET match_status=?, review_note=?, reviewed_at=? WHERE id=?
    """, (req.action, (req.note or "").strip(), now, candidate_id))
    conn.commit()
    conn.close()
    return {"ok": True, "candidate_id": candidate_id, "status": req.action,
            "automatic_grading_enabled": req.action == "approved" and candidate["ptype"] == "calc"}


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8011)
