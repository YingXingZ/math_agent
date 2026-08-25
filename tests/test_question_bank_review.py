from __future__ import annotations

import sqlite3
from pathlib import Path

from src.agent8000.app.question_bank_review import attach_source_image, build_review_queue, source_item_for_problem


def test_review_queue_keeps_source_and_corruption_as_separate_dimensions(tmp_path: Path) -> None:
    db_path = tmp_path / "workbench.db"
    crop_root = tmp_path / "crops"
    crop_root.mkdir()
    (crop_root / "one.png").write_bytes(b"image")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """CREATE TABLE sections (id INTEGER PRIMARY KEY, section_no TEXT);
           CREATE TABLE problems (
             id INTEGER PRIMARY KEY, section_id INTEGER, problem_no TEXT, sub_no TEXT,
             ptype TEXT, difficulty INTEGER, content_text TEXT, std_answer TEXT,
             answer_status TEXT, crop_image_path TEXT);"""
    )
    conn.execute("INSERT INTO sections VALUES(1, '1.1')")
    conn.execute("INSERT INTO problems VALUES(1,1,'1','','calc',1,'','','','one.png')")
    conn.execute("INSERT INTO problems VALUES(2,1,'2','','calc',1,'锟','1','corrupt_ocr','')")
    conn.execute("INSERT INTO problems VALUES(3,1,'3','','calc',1,'ok','1','verified','')")
    conn.commit()
    conn.close()

    queue = build_review_queue(db_path=db_path, crop_root=crop_root)
    assert queue["counts"] == {
        "ready_for_teacher_review": 1,
        "requires_source_image": 1,
        "corrupt": 1,
        "complete": 1,
        "total": 3,
    }
    assert len(build_review_queue("corrupt", db_path=db_path, crop_root=crop_root)["items"]) == 1


def test_teacher_upload_binds_only_the_selected_problem(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "workbench.db"
    image_root = tmp_path / "crops"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """CREATE TABLE sections (id INTEGER PRIMARY KEY, section_no TEXT);
           CREATE TABLE problems (id INTEGER PRIMARY KEY, section_id INTEGER, problem_no TEXT,
             sub_no TEXT, ptype TEXT, difficulty INTEGER, content_text TEXT, std_answer TEXT,
             answer_status TEXT, crop_image_path TEXT);
           INSERT INTO sections VALUES(1, '1.1');
           INSERT INTO problems VALUES(7,1,'2','','calc',1,'','','','');"""
    )
    conn.close()
    monkeypatch.setenv("WORKBENCH_DB", str(db_path))
    monkeypatch.setenv("IMAGE_ROOT", str(image_root))
    saved = attach_source_image(7, "problem.png", b"\x89PNG\r\n\x1a\ncontent")
    assert (image_root / saved["crop_image_path"]).is_file()
    item = source_item_for_problem(7)
    assert item["source_problem_id"] == "7"
    assert item["evidence"]["crop_image_path"] == saved["crop_image_path"]
