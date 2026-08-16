CREATE TABLE ai_stem_candidates (
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

CREATE TABLE assignment_questions (
  assignment_id INTEGER NOT NULL, question_id INTEGER NOT NULL, sort_order INTEGER NOT NULL,
  score INTEGER NOT NULL, original_no TEXT, PRIMARY KEY(assignment_id, question_id)
);

CREATE TABLE assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, chapter TEXT NOT NULL,
  class_name TEXT NOT NULL, due_at TEXT NOT NULL, total_score INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'published', created_at TEXT DEFAULT CURRENT_TIMESTAMP
, semester TEXT NOT NULL DEFAULT '');

CREATE TABLE grading_experiences (
  id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL,
  assignment_id INTEGER NOT NULL, confirmed_score REAL,
  teacher_feedback TEXT, evidence_json TEXT NOT NULL,
  confirmed_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE grading_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'queued', result_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE mineru_pending_sync (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  section_no TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'mineru_review',
  reason TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  synced_at TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE mineru_review_audit_log (
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

CREATE TABLE mineru_review_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
  source_problem_id TEXT, section_no TEXT NOT NULL, question_no TEXT NOT NULL,
  subquestion_no TEXT, candidate_text TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0,
  quality_json TEXT NOT NULL DEFAULT '{}', evidence_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending', review_note TEXT DEFAULT '', reviewed_at TEXT
);

CREATE TABLE mineru_review_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, document_name TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, chapter TEXT NOT NULL,
  difficulty TEXT NOT NULL CHECK(difficulty IN ('基础','提高','综合')),
  question_type TEXT NOT NULL DEFAULT '计算题', answer TEXT, rubric TEXT,
  source_page INTEGER, review_status TEXT NOT NULL DEFAULT 'published', created_at TEXT DEFAULT CURRENT_TIMESTAMP
, source_problem_id TEXT, source_evidence_json TEXT, source_problem_no TEXT, knowledge_points TEXT NOT NULL DEFAULT '');

CREATE TABLE sqlite_sequence(name,seq);

CREATE TABLE submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id INTEGER NOT NULL, student_no TEXT NOT NULL,
  student_name TEXT, file_path TEXT NOT NULL, submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
  status TEXT NOT NULL DEFAULT 'submitted', score REAL, feedback TEXT, needs_review INTEGER DEFAULT 1
, handwriting_score REAL);

