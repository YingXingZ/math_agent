from fastapi.testclient import TestClient

from src.agent8000.app import main
from src.agent8000.app.db import init_db


def test_teacher_class_isolation_and_student_invite_activation(tmp_path):
    old = (main.settings.database_path, main.settings.upload_dir, main.settings.auth_required,
           main.settings.bootstrap_admin_username, main.settings.bootstrap_admin_password)
    main.settings.database_path = str(tmp_path / "tenant.db")
    main.settings.upload_dir = str(tmp_path / "uploads")
    main.settings.auth_required = True
    main.settings.bootstrap_admin_username = "admin"
    main.settings.bootstrap_admin_password = "admin-password-123"
    try:
        init_db()
        with TestClient(main.app) as admin:
            assert admin.get("/api/classes").status_code == 401
            assert admin.post("/api/auth/login", json={"username": "admin", "password": "admin-password-123"}).status_code == 200
            for username in ("teacher_a", "teacher_b"):
                response = admin.post("/api/admin/teachers", json={
                    "username": username, "display_name": username, "temporary_password": f"{username}-password",
                })
                assert response.status_code == 201
        with TestClient(main.app) as teacher_a, TestClient(main.app) as teacher_b:
            assert teacher_a.post("/api/auth/login", json={"username": "teacher_a", "password": "teacher_a-password"}).status_code == 200
            assert teacher_b.post("/api/auth/login", json={"username": "teacher_b", "password": "teacher_b-password"}).status_code == 200
            class_id = teacher_a.post("/api/classes", json={"name": "甲班", "semester": "2026-1"}).json()["id"]
            assert teacher_a.post(f"/api/classes/{class_id}/students/import", json={"students": [{"student_no": "S001", "name": "学生甲"}]}).status_code == 200
            assert teacher_b.get(f"/api/classes/{class_id}/students").status_code == 404
            invite = teacher_a.post(f"/api/classes/{class_id}/invites", json={"max_uses": 1}).json()["invite_code"]
        with TestClient(main.app) as student:
            activated = student.post("/api/auth/student-activate", json={
                "invite_code": invite, "student_no": "S001", "name": "学生甲",
                "username": "student_a", "password": "student-a-password",
            })
            assert activated.status_code == 201
            assert activated.json()["user"]["role"] == "student"
    finally:
        (main.settings.database_path, main.settings.upload_dir, main.settings.auth_required,
         main.settings.bootstrap_admin_username, main.settings.bootstrap_admin_password) = old
