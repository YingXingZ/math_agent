from pathlib import Path

from fastapi.testclient import TestClient

from src.agent8000.app import main
from src.agent8000.app.db import init_db


def test_demo_package_is_downloadable_and_excluded_from_reports(tmp_path: Path) -> None:
    old = main.settings.database_path, main.settings.upload_dir, main.settings.auth_required
    main.settings.database_path = str(tmp_path / "demo.db")
    main.settings.upload_dir = str(tmp_path / "uploads")
    main.settings.auth_required = False
    try:
        init_db()
        with TestClient(main.app) as client:
            response = client.post("/api/admin/demo-package")
            assert response.status_code == 201
            package = response.json()
            assert package["assignment_id"] and package["student_no"] == "DEMO001"
            sample = client.get(package["sample_download_url"])
            assert sample.status_code == 200
            assert sample.headers["content-type"].startswith("image/png")
            assert client.get("/api/reports/summary").json()["assignment_count"] == 0
    finally:
        main.settings.database_path, main.settings.upload_dir, main.settings.auth_required = old
