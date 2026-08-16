CREATE TABLE answer_candidate_source_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER NOT NULL,
        image_path TEXT DEFAULT '', source_pdf TEXT DEFAULT '', source_page INTEGER DEFAULT 0,
        problem_text TEXT DEFAULT '', ocr_text TEXT DEFAULT '', latex_text TEXT DEFAULT '',
        ai_review_json TEXT DEFAULT '{}', vision_status TEXT DEFAULT '',
        std_answer TEXT DEFAULT '', full_solution TEXT DEFAULT '', answer_status TEXT DEFAULT '',
        match_status TEXT DEFAULT '', created_at TEXT NOT NULL);

CREATE TABLE answer_documents(
        id TEXT PRIMARY KEY, filename TEXT NOT NULL, stored_path TEXT NOT NULL,
        file_size INTEGER DEFAULT 0, page_count INTEGER DEFAULT 0,
        volume TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'uploading',
        index_progress INTEGER DEFAULT 0, index_message TEXT DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE answer_import_candidates (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      problem_id TEXT NOT NULL,
      volume TEXT NOT NULL,
      section_no TEXT NOT NULL,
      problem_no TEXT NOT NULL,
      sub_no TEXT,
      source_pdf TEXT NOT NULL,
      source_page INTEGER NOT NULL,
      ocr_text TEXT NOT NULL,
      ocr_confidence REAL NOT NULL,
      match_status TEXT NOT NULL DEFAULT 'pending',
      match_reason TEXT DEFAULT '',
      content_hash TEXT NOT NULL UNIQUE,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    , reviewed_at TEXT, review_note TEXT DEFAULT '', latex_text TEXT DEFAULT '', vision_status TEXT DEFAULT 'not_queued', vision_confidence REAL, ai_review_json TEXT DEFAULT '{}', ai_review_status TEXT DEFAULT 'not_run', ai_review_model TEXT DEFAULT '', source_updated_at TEXT DEFAULT '', subquestion_count INTEGER DEFAULT 0);

CREATE TABLE answer_page_anchors(
        id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL,
        page_no INTEGER NOT NULL, section_no TEXT DEFAULT '', problem_no TEXT DEFAULT '',
        sub_no TEXT DEFAULT '', bbox_json TEXT DEFAULT '[]', crop_path TEXT DEFAULT '',
        ocr_text TEXT DEFAULT '', ocr_confidence REAL DEFAULT 0,
        problem_id TEXT DEFAULT '', candidate_id INTEGER,
        extraction_status TEXT DEFAULT 'detected', comparison_status TEXT DEFAULT 'not_run',
        comparison_json TEXT DEFAULT '{}', created_at TEXT NOT NULL, teacher_subquestion_count INTEGER DEFAULT 0,
        UNIQUE(document_id,page_no,section_no,problem_no,sub_no,bbox_json));

CREATE TABLE candidate_source_images(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER NOT NULL,
        sort_order INTEGER NOT NULL,
        image_path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(candidate_id, sort_order));

CREATE TABLE classes(
        id TEXT PRIMARY KEY, teacher_id TEXT, name TEXT, term TEXT);

CREATE TABLE homeworks(
        id TEXT PRIMARY KEY, textbook_id TEXT, title TEXT, class_id TEXT,
        section_no TEXT, deadline TEXT, status TEXT DEFAULT 'published',
        problem_ids TEXT DEFAULT '[]', created_at TEXT, points_map TEXT DEFAULT '{}');

CREATE TABLE knowledge_points(
        code TEXT PRIMARY KEY, name TEXT, parent_code TEXT, section_no TEXT);

CREATE TABLE meta(
        key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE problems(
        id TEXT PRIMARY KEY, section_id TEXT, exercise_set TEXT,
        problem_no TEXT, sub_no TEXT, ptype TEXT DEFAULT 'calc',
        crop_image_path TEXT, content_text TEXT,
        difficulty INT DEFAULT 3, knowledge_pts TEXT DEFAULT '',
        extract_status TEXT DEFAULT 'raw', source_page INT, tier TEXT DEFAULT '', std_answer TEXT DEFAULT NULL, grading_steps TEXT DEFAULT NULL, answer_weight REAL DEFAULT 0.6, full_solution TEXT, answer_status TEXT NOT NULL DEFAULT 'unverified', answer_invalid_reason TEXT DEFAULT '');

CREATE TABLE sections(
        id TEXT PRIMARY KEY, textbook_id TEXT, section_no TEXT,
        title TEXT, start_page INT, exercise_page INT);

CREATE TABLE sqlite_sequence(name,seq);

CREATE TABLE students(
        id TEXT PRIMARY KEY, class_id TEXT, student_no TEXT, name TEXT);

CREATE TABLE submission_source_images(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        submission_id TEXT NOT NULL,
        sort_order INTEGER NOT NULL,
        image_path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(submission_id, sort_order));

CREATE TABLE submissions(
        id TEXT PRIMARY KEY, homework_id TEXT, student_no TEXT, student_name TEXT,
        submitted_at TEXT, status TEXT DEFAULT 'pending', score REAL,
        answers TEXT DEFAULT '[]', created_at TEXT, annotations TEXT DEFAULT '[]', grade_detail TEXT DEFAULT '[]', review_log TEXT DEFAULT '[]');

CREATE TABLE textbooks(
        id TEXT PRIMARY KEY, name TEXT, volume TEXT, edition TEXT,
        pdf_path TEXT, page_offset INT DEFAULT 0);

CREATE TABLE vision_recognition_tasks(
        id TEXT PRIMARY KEY, candidate_id INTEGER UNIQUE, problem_id TEXT NOT NULL,
        task_type TEXT NOT NULL DEFAULT 'answer_pdf', status TEXT NOT NULL DEFAULT 'pending',
        provider TEXT NOT NULL DEFAULT 'openai', input_image_path TEXT NOT NULL,
        result_json TEXT DEFAULT '{}', error_message TEXT DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

