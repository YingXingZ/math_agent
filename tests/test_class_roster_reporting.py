from pathlib import Path
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook

from src.agent8000.app import main
from src.agent8000.app.db import connection, init_db


def test_class_roster_controls_submission_and_real_reports(tmp_path: Path) -> None:
    """A roster-bound class is the only source admitted into live reports."""
    previous_db, previous_upload = main.settings.database_path, main.settings.upload_dir
    main.settings.database_path = str(tmp_path / "homework.db")
    main.settings.upload_dir = str(tmp_path / "uploads")
    try:
        init_db()
        with connection() as conn:
            conn.execute(
                "INSERT INTO questions(content,chapter,difficulty,question_type,answer,rubric) VALUES(?,?,?,?,?,?)",
                ("求极限。", "1.1", "基础", "计算题", "1", "",),
            )
            # This mimics an old assignment: it deliberately has no class_id.
            conn.execute(
                "INSERT INTO assignments(title,chapter,class_name,due_at,total_score,status) VALUES(?,?,?,?,?,?)",
                ("历史演示", "1.1", "演示班", "2026-12-01T00:00:00", 10, "published"),
            )
        with TestClient(main.app) as client:
            created = client.post("/api/classes", json={"name": "高数一班", "semester": "2026-2027-1"})
            assert created.status_code == 201
            class_id = created.json()["id"]

            imported = client.post(
                f"/api/classes/{class_id}/students/import",
                json={"students": [{"student_no": "20260001", "name": "张三"}]},
            )
            assert imported.status_code == 200
            assert imported.json()["inserted"] == 1

            assignment = client.post("/api/assignments", json={
                "title": "第一周作业", "chapter": "1.1", "class_id": class_id,
                "due_at": "2026-12-01T00:00:00", "question_count": 1,
                "basic_ratio": 1, "advanced_ratio": 0,
            })
            assert assignment.status_code == 201
            assignment_id = assignment.json()["id"]

            rejected = client.post(
                f"/api/assignments/{assignment_id}/submissions",
                data={"student_no": "20260002", "student_name": "李四"},
                files={"file": ("work.pdf", b"%PDF-1.4", "application/pdf")},
            )
            assert rejected.status_code == 403

            accepted = client.post(
                f"/api/assignments/{assignment_id}/submissions",
                data={"student_no": "20260001", "student_name": ""},
                files={"file": ("work.pdf", b"%PDF-1.4", "application/pdf")},
            )
            assert accepted.status_code == 201
            submission_id = accepted.json()["id"]

            with connection() as conn:
                conn.execute("UPDATE submissions SET status='graded',score=10,needs_review=0 WHERE id=?", (submission_id,))
            report = client.get("/api/reports/semester-summary")
            assert report.status_code == 200
            assert [row["student_no"] for row in report.json()["students"]] == ["20260001"]
    finally:
        main.settings.database_path, main.settings.upload_dir = previous_db, previous_upload


def test_roster_file_requires_explicit_headers(tmp_path: Path) -> None:
    previous_db = main.settings.database_path
    main.settings.database_path = str(tmp_path / "homework.db")
    try:
        init_db()
        with TestClient(main.app) as client:
            class_id = client.post("/api/classes", json={"name": "高数二班"}).json()["id"]
            bad = client.post(
                f"/api/classes/{class_id}/students/import-file",
                files={"file": ("roster.csv", "编号,学生\n1,张三\n".encode("utf-8"), "text/csv")},
            )
            assert bad.status_code == 422
            good = client.post(
                f"/api/classes/{class_id}/students/import-file",
                files={"file": ("roster.csv", "学号,姓名\n20260002,李四\n".encode("utf-8"), "text/csv")},
            )
            assert good.status_code == 200
            assert good.json()["inserted"] == 1
            book = Workbook()
            sheet = book.active
            sheet.append(["学号", "姓名", "备注"])
            sheet.append(["20260003", "王五", "测试 Excel"])
            stream = BytesIO()
            book.save(stream)
            excel = client.post(
                f"/api/classes/{class_id}/students/import-file",
                files={"file": ("roster.xlsx", stream.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
            assert excel.status_code == 200
            assert excel.json()["inserted"] == 1
    finally:
        main.settings.database_path = previous_db
