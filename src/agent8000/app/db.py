import sqlite3
from contextlib import contextmanager
from .config import settings


# Question types arrive in mixed languages.  The 8014 evidence API returns the
# English enum ("calc"/"proof") while the VLM-recognised stem candidates (Route 1
# approval path) sometimes carry Chinese labels ("计算题"/"证明题"/"应用题").
# Normalise to a single canonical token so the grader's symbolic-equivalence gate
# and the report aggregations behave consistently.
_QTYPE_PROOF = {"proof", "证明题", "求证", "证明"}
_QTYPE_CALC = {"calc", "计算题", "应用题", "填空题", "选择题", "填空", "选择"}


def normalize_question_type(raw) -> str:
    """Return 'proof' or 'calc' for any source label."""
    if raw is None:
        return "calc"
    s = str(raw).strip()
    if not s:
        return "calc"
    if s in _QTYPE_PROOF:
        return "proof"
    if s in _QTYPE_CALC:
        return "calc"
    # Unknown label: infer from keywords, otherwise treat as computational.
    lower = s.lower()
    if "证明" in s or "prove" in lower:
        return "proof"
    return "calc"


# ---------------------------------------------------------------------------
# Knowledge-point tagging (concept-level) for the weak-point report (设计文档 2.1).
# Pure local keyword rules — no VLM call, no remote dependency.  Returns the most
# specific concept matched; falls back to the textbook subsection topic when no
# keyword fires, so the report always has a human-readable grouping key.
# ---------------------------------------------------------------------------
_SECTION_TOPICS = {
    "1.1": "函数概念与性质", "1.2": "数列极限", "1.3": "函数极限",
    "1.4": "无穷小与极限运算", "1.5": "极限存在准则", "1.6": "极限与连续",
    "5.1": "向量及其线性运算", "5.2": "数量积、向量积、混合积",
    "5.3": "平面及其方程", "5.4": "空间直线及其方程",
    "5.5": "曲面及其方程", "5.6": "二次曲面",
    "第一章 函数与极限": "函数与极限", "第二章 导数与微分": "导数与微分",
}

_KP_RULES = [
    ("混合积", ["混合积"]),
    ("向量积", ["向量积", "叉积", "×"]),
    ("数量积", ["数量积", "点积", "·"]),
    ("投影", ["投影", "Prj", "prj", "Proj"]),
    ("平面方程", ["平面方程", "法向量", "法线"]),
    ("点到平面距离", ["点到平面", "到平面距离", "平面的距离"]),
    ("空间直线", ["直线方程", "对称式", "参数式", "空间直线", "异面直线", "线面角", "线面夹角"]),
    ("曲面方程", ["曲面", "旋转面", "旋转曲面", "柱面", "柱面方程"]),
    ("二次曲面", ["二次曲面", "椭球", "双曲面", "抛物面", "马鞍面", "双曲抛物面"]),
    ("方向角与方向余弦", ["方向角", "方向余弦"]),
    ("向量共线共面", ["共线", "共面"]),
    ("函数奇偶性", ["奇偶性", "奇函数", "偶函数"]),
    ("函数单调性", ["单调性", "单调增", "单调减", "单调递增", "单调递减", "递增", "递减"]),
    ("函数周期性", ["周期性", "周期函数", "以", "为周期"]),
    ("函数有界性", ["有界", "无界"]),
    ("复合与反函数", ["反函数", "复合函数"]),
]


def tag_knowledge_points(content, answer=None, chapter=None) -> str:
    """Return a concept-level knowledge-point label for one question.

    `content` is the question stem; `answer` (optional) is also scanned so that
    answer-side keywords (e.g. a vector-product result) still tag correctly.
    Falls back to the textbook subsection topic, then to the raw chapter string.
    """
    text = f"{content or ''}\n{answer or ''}"
    for label, kws in _KP_RULES:
        if any(kw in text for kw in kws):
            return label
    if chapter and chapter in _SECTION_TOPICS:
        return _SECTION_TOPICS[chapter]
    return chapter or "未分类"


SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, chapter TEXT NOT NULL,
  difficulty TEXT NOT NULL CHECK(difficulty IN ('基础','提高','综合')),
  question_type TEXT NOT NULL DEFAULT '计算题', answer TEXT, rubric TEXT,
  source_page INTEGER, review_status TEXT NOT NULL DEFAULT 'published', created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, chapter TEXT NOT NULL,
  class_name TEXT NOT NULL, due_at TEXT NOT NULL, total_score INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'published', created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS classes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, semester TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(name, semester)
);
CREATE TABLE IF NOT EXISTS students (
  id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL, student_no TEXT NOT NULL,
  name TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(class_id, student_no)
);
CREATE TABLE IF NOT EXISTS assignment_questions (
  assignment_id INTEGER NOT NULL, question_id INTEGER NOT NULL, sort_order INTEGER NOT NULL,
  score INTEGER NOT NULL, PRIMARY KEY(assignment_id, question_id)
);
CREATE TABLE IF NOT EXISTS assignment_question_parts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  assignment_id INTEGER NOT NULL, question_id INTEGER NOT NULL,
  subpart_no TEXT NOT NULL, part_order INTEGER NOT NULL,
  content TEXT NOT NULL, answer TEXT NOT NULL, rubric TEXT NOT NULL DEFAULT '',
  score REAL NOT NULL,
  UNIQUE(assignment_id, question_id, subpart_no)
);
CREATE INDEX IF NOT EXISTS idx_assignment_question_parts_assignment
  ON assignment_question_parts(assignment_id, question_id, part_order);
CREATE TABLE IF NOT EXISTS assignment_question_overrides (
  assignment_id INTEGER NOT NULL, question_id INTEGER NOT NULL, content TEXT NOT NULL,
  score REAL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(assignment_id, question_id)
);
CREATE TABLE IF NOT EXISTS assignment_notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  assignment_id INTEGER NOT NULL UNIQUE, class_id INTEGER,
  title_snapshot TEXT NOT NULL, due_at_snapshot TEXT NOT NULL,
  submit_path TEXT NOT NULL, created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS assignment_notification_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  notification_id INTEGER NOT NULL, actor_user_id INTEGER,
  action TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_notification_events_notification
  ON assignment_notification_events(notification_id, created_at DESC);
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id INTEGER NOT NULL, student_no TEXT NOT NULL,
  student_name TEXT, file_path TEXT NOT NULL, submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
  status TEXT NOT NULL DEFAULT 'submitted', score REAL, feedback TEXT, needs_review INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS grading_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'queued', result_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS grading_experiences (
  id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL,
  assignment_id INTEGER NOT NULL, confirmed_score REAL,
  teacher_feedback TEXT, evidence_json TEXT NOT NULL,
  decision_json TEXT NOT NULL DEFAULT '{}',
  confirmed_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_questions_chapter_difficulty ON questions(chapter, difficulty);
CREATE INDEX IF NOT EXISTS idx_assignments_class_due ON assignments(class_name, due_at);
CREATE INDEX IF NOT EXISTS idx_students_class_no ON students(class_id, student_no);
CREATE INDEX IF NOT EXISTS idx_submissions_assignment_student ON submissions(assignment_id, student_no);
CREATE INDEX IF NOT EXISTS idx_grading_experiences_assignment ON grading_experiences(assignment_id);
CREATE TABLE IF NOT EXISTS submission_question_regions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  submission_id INTEGER NOT NULL, question_id INTEGER NOT NULL,
  subpart_no TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL,
  page_no INTEGER NOT NULL, x REAL NOT NULL DEFAULT 0, y REAL NOT NULL DEFAULT 0,
  width REAL NOT NULL DEFAULT 1, height REAL NOT NULL DEFAULT 1,
  confirmed_by INTEGER, confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(submission_id, question_id, subpart_no, sort_order)
);
CREATE INDEX IF NOT EXISTS idx_submission_regions_submission ON submission_question_regions(submission_id, sort_order);
CREATE TABLE IF NOT EXISTS mineru_review_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, document_name TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS mineru_review_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
  source_problem_id TEXT, section_no TEXT NOT NULL, question_no TEXT NOT NULL,
  subquestion_no TEXT, candidate_text TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0,
  quality_json TEXT NOT NULL DEFAULT '{}', evidence_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending', review_note TEXT DEFAULT '', reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mineru_review_session ON mineru_review_items(session_id, status);
CREATE TABLE IF NOT EXISTS mineru_review_audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL,
  session_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  old_status TEXT,
  new_status TEXT,
  old_answer TEXT,
  new_answer TEXT,
  old_solution TEXT,
  new_solution TEXT,
  note TEXT,
  actor TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mineru_audit_session_item ON mineru_review_audit_log(session_id, item_id, created_at);
CREATE TABLE IF NOT EXISTS mineru_pending_sync (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  section_no TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'mineru_review',
  reason TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  synced_at TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_mineru_pending_sync_status ON mineru_pending_sync(status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mineru_pending_sync_section ON mineru_pending_sync(section_no, source);
CREATE TABLE IF NOT EXISTS ai_stem_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_problem_id TEXT NOT NULL,
  section_no TEXT NOT NULL,
  problem_no TEXT,
  sub_no TEXT,
  candidate_text TEXT NOT NULL,
  ptype TEXT,
  std_answer TEXT,
  full_solution TEXT,
  difficulty TEXT,
  confidence REAL,
  agreement_json TEXT NOT NULL DEFAULT '{}',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  review_note TEXT DEFAULT '',
  reviewed_at TEXT,
  approved_content TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ai_stem_candidates_status ON ai_stem_candidates(status, section_no);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_stem_candidates_source ON ai_stem_candidates(source_problem_id);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','teacher','student')),
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  password_changed_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS user_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  expires_at TEXT NOT NULL, last_seen_at TEXT, revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id, expires_at);
CREATE TABLE IF NOT EXISTS class_invites (
  id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL,
  code_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL,
  max_uses INTEGER NOT NULL DEFAULT 1, used_count INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER NOT NULL, revoked_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_class_invites_class ON class_invites(class_id, expires_at);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, actor_user_id INTEGER,
  tenant_teacher_id INTEGER, action TEXT NOT NULL, resource_type TEXT NOT NULL,
  resource_id TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', ip TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_created ON audit_logs(tenant_teacher_id, created_at DESC);
"""


@contextmanager
def connection():
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(SCHEMA)
        experience_cols = {row[1] for row in conn.execute("PRAGMA table_info(grading_experiences)")}
        if "decision_json" not in experience_cols:
            conn.execute("ALTER TABLE grading_experiences ADD COLUMN decision_json TEXT NOT NULL DEFAULT '{}'")

        # The local question table is a working cache, while 8014 remains the
        # authoritative evidence library.  These columns make every imported
        # question traceable back to its source record and image/PDF evidence.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(questions)")}
        if "source_problem_id" not in columns:
            conn.execute("ALTER TABLE questions ADD COLUMN source_problem_id TEXT")
        if "source_evidence_json" not in columns:
            conn.execute("ALTER TABLE questions ADD COLUMN source_evidence_json TEXT")
        if "source_problem_no" not in columns:
            conn.execute("ALTER TABLE questions ADD COLUMN source_problem_no TEXT")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_source_problem_id
                        ON questions(source_problem_id)
                        WHERE source_problem_id IS NOT NULL""")
        # assignment_questions carries the *original* textbook problem number so
        # the generated paper keeps the source numbering (设计二：保持原本题号).
        aq_columns = {row[1] for row in conn.execute("PRAGMA table_info(assignment_questions)")}
        if "original_no" not in aq_columns:
            conn.execute("ALTER TABLE assignment_questions ADD COLUMN original_no TEXT")

        # --- Route 2 / 设计文档增强 字段 ---
        # semester: 学期维度（学期末分数汇总）；handwriting_score: 书写整洁度；
        # knowledge_points: 知识点标签（薄弱知识点建议）。
        notification_cols = {row[1] for row in conn.execute("PRAGMA table_info(assignment_notifications)")}
        if "activation_path" not in notification_cols:
            conn.execute("ALTER TABLE assignment_notifications ADD COLUMN activation_path TEXT NOT NULL DEFAULT ''")
        assign_cols = {row[1] for row in conn.execute("PRAGMA table_info(assignments)")}
        if "status" not in assign_cols:
            conn.execute("ALTER TABLE assignments ADD COLUMN status TEXT NOT NULL DEFAULT 'published'")
        if "semester" not in assign_cols:
            conn.execute("ALTER TABLE assignments ADD COLUMN semester TEXT NOT NULL DEFAULT ''")
        # A class id is deliberately nullable for legacy/demo assignments.  New
        # assignments must have it; reports use it as the boundary between real
        # class data and historical sample records.
        if "class_id" not in assign_cols:
            conn.execute("ALTER TABLE assignments ADD COLUMN class_id INTEGER")
        if "is_demo" not in assign_cols:
            conn.execute("ALTER TABLE assignments ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0")
        # New assignments use a transparent 100-point policy: 20 points for a
        # valid submission and 80 points for answer quality.  Legacy records
        # retain their original score policy and totals.
        if "score_policy" not in assign_cols:
            conn.execute("ALTER TABLE assignments ADD COLUMN score_policy TEXT NOT NULL DEFAULT 'legacy'")
        if "completion_points" not in assign_cols:
            conn.execute("ALTER TABLE assignments ADD COLUMN completion_points REAL NOT NULL DEFAULT 0")
        class_cols = {row[1] for row in conn.execute("PRAGMA table_info(classes)")}
        if "teacher_user_id" not in class_cols:
            conn.execute("ALTER TABLE classes ADD COLUMN teacher_user_id INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_classes_teacher ON classes(teacher_user_id)")
        student_cols = {row[1] for row in conn.execute("PRAGMA table_info(students)")}
        if "user_id" not in student_cols:
            conn.execute("ALTER TABLE students ADD COLUMN user_id INTEGER")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_students_user_class ON students(class_id,user_id) WHERE user_id IS NOT NULL")
        question_owner_cols = {row[1] for row in conn.execute("PRAGMA table_info(questions)")}
        if "owner_teacher_id" not in question_owner_cols:
            conn.execute("ALTER TABLE questions ADD COLUMN owner_teacher_id INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_questions_owner ON questions(owner_teacher_id)")
        sub_cols = {row[1] for row in conn.execute("PRAGMA table_info(submissions)")}
        if "handwriting_score" not in sub_cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN handwriting_score REAL")
        if "released_at" not in sub_cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN released_at TEXT")
        if "completion_score" not in sub_cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN completion_score REAL NOT NULL DEFAULT 0")
        if "quality_score" not in sub_cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN quality_score REAL")
        if "quality_max_score" not in sub_cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN quality_max_score REAL")
        if "score_policy_version" not in sub_cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN score_policy_version TEXT NOT NULL DEFAULT 'legacy'")
        # Queue metadata is separate from grading evidence.  It lets an
        # operator tell whether a job is merely queued, running, retried, or
        # permanently failed without inspecting Redis manually.
        job_cols = {row[1] for row in conn.execute("PRAGMA table_info(grading_jobs)")}
        for column, ddl in {
            "rq_job_id": "TEXT",
            "queue_attempts": "INTEGER NOT NULL DEFAULT 0",
            "last_enqueued_at": "TEXT",
            "last_started_at": "TEXT",
            "last_error": "TEXT",
        }.items():
            if column not in job_cols:
                conn.execute(f"ALTER TABLE grading_jobs ADD COLUMN {column} {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_grading_jobs_status ON grading_jobs(status, rq_job_id)")
        q_cols = {row[1] for row in conn.execute("PRAGMA table_info(questions)")}
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_learning_traces (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              student_user_id INTEGER NOT NULL,
              submission_id INTEGER NOT NULL,
              question_id INTEGER NOT NULL,
              source_problem_id TEXT,
              mode TEXT NOT NULL,
              qwen_used INTEGER NOT NULL DEFAULT 0,
              teacher_review_needed INTEGER NOT NULL DEFAULT 0,
              handwriting_ocr_failed INTEGER NOT NULL DEFAULT 0,
              latency_ms REAL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_agent_trace_student_created ON agent_learning_traces(student_user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_agent_trace_submission ON agent_learning_traces(submission_id, question_id);
        """)
        trace_cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_learning_traces)")}
        if "agent_trace_id" not in trace_cols:
            conn.execute("ALTER TABLE agent_learning_traces ADD COLUMN agent_trace_id TEXT")
        if "execution_trace_json" not in trace_cols:
            conn.execute("ALTER TABLE agent_learning_traces ADD COLUMN execution_trace_json TEXT NOT NULL DEFAULT '[]'")
        for column, ddl in {
            "qwen_called": "INTEGER NOT NULL DEFAULT 0",
            "qwen_success": "INTEGER NOT NULL DEFAULT 0",
            "qwen_adopted": "INTEGER NOT NULL DEFAULT 0",
            "question_type": "TEXT NOT NULL DEFAULT 'unknown'",
            "learning_outcome": "TEXT",
        }.items():
            if column not in trace_cols:
                conn.execute(f"ALTER TABLE agent_learning_traces ADD COLUMN {column} {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_trace_trace_id ON agent_learning_traces(agent_trace_id)")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS teacher_evaluation_cases (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_submission_id INTEGER NOT NULL,
              question_id INTEGER NOT NULL,
              case_type TEXT NOT NULL,
              question_type TEXT NOT NULL DEFAULT 'calc',
              problem_text TEXT NOT NULL DEFAULT '',
              student_answer TEXT NOT NULL DEFAULT '',
              standard_answer TEXT NOT NULL DEFAULT '',
              ai_initial_correct INTEGER,
              teacher_correct INTEGER NOT NULL,
              expected_route TEXT NOT NULL,
              expected_diagnosis TEXT,
              requires_teacher_review INTEGER NOT NULL DEFAULT 0,
              disagreement_reason TEXT NOT NULL DEFAULT '',
              teacher_note TEXT NOT NULL DEFAULT '',
              label_version TEXT NOT NULL DEFAULT 'teacher-label-v1',
              created_by INTEGER NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(source_submission_id, question_id)
            );
            CREATE INDEX IF NOT EXISTS idx_teacher_eval_cases_created ON teacher_evaluation_cases(created_at DESC);
            CREATE TABLE IF NOT EXISTS agent_learning_attempts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              prior_trace_id TEXT NOT NULL,
              student_user_id INTEGER NOT NULL,
              submission_id INTEGER NOT NULL,
              question_id INTEGER NOT NULL,
              answer_text TEXT NOT NULL,
              verification_json TEXT NOT NULL DEFAULT '{}',
              correct INTEGER,
              input_kind TEXT NOT NULL DEFAULT 'text',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_learning_attempt_prior ON agent_learning_attempts(prior_trace_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_learning_attempt_student ON agent_learning_attempts(student_user_id, created_at DESC);
        """)
        attempt_cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_learning_attempts)")}
        if "input_kind" not in attempt_cols:
            conn.execute("ALTER TABLE agent_learning_attempts ADD COLUMN input_kind TEXT NOT NULL DEFAULT 'text'")
        if "knowledge_points" not in q_cols:
            conn.execute("ALTER TABLE questions ADD COLUMN knowledge_points TEXT NOT NULL DEFAULT ''")

        count = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        if count == 0:
            conn.executemany(
                "INSERT INTO questions(content,chapter,difficulty,question_type,answer,rubric,source_page) VALUES(?,?,?,?,?,?,?)",
                [
                    ("求函数 f(x)=x^3-3x 的单调区间与极值。", "第一章 函数与极限", "基础", "计算题", "极大值为2，极小值为-2", "求导2分；临界点2分；区间与极值2分", 18),
                    ("计算极限 lim_{x→0} (sin x)/x。", "第一章 函数与极限", "基础", "计算题", "1", "使用重要极限，4分", 36),
                    ("证明：连续函数在闭区间上必取得最大值和最小值。", "第一章 函数与极限", "提高", "证明题", "由闭区间上连续函数性质可得。", "说明闭区间与连续性条件各2分；结论2分", 42),
                    ("设 y=x^x(x>0)，求 y'。", "第二章 导数与微分", "基础", "计算题", "x^x(ln x+1)", "对数求导2分；结果2分", 67),
                    ("求曲线 y=x^3-3x 在区间[-2,2]上的最大值和最小值。", "第二章 导数与微分", "提高", "应用题", "最大值2，最小值-2", "驻点2分；端点与驻点比较3分；结论1分", 73),
                ],
            )


def queue_due_grading(now: str) -> int:
    """Create each due submission's grading job once."""
    with connection() as conn:
        rows = conn.execute("""SELECT s.id FROM submissions s JOIN assignments a ON a.id=s.assignment_id
          LEFT JOIN grading_jobs j ON j.submission_id=s.id WHERE a.due_at<=? AND j.id IS NULL""", (now,)).fetchall()
        conn.executemany("INSERT INTO grading_jobs(submission_id) VALUES(?)", [(r["id"],) for r in rows])
        return len(rows)


def claim_grading_job():
    with connection() as conn:
        job = conn.execute("SELECT id, submission_id FROM grading_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not job:
            return None
        changed = conn.execute("UPDATE grading_jobs SET status='running' WHERE id=? AND status='queued'", (job["id"],)).rowcount
        return dict(job) if changed else None
