from pathlib import Path

from src.agent8000.app.config import PROJECT_ROOT, REPOSITORY_ROOT, Settings


def test_operator_data_defaults_are_repository_relative() -> None:
    settings = Settings()

    assert REPOSITORY_ROOT == PROJECT_ROOT.parents[1]
    assert Path(settings.garble_audit_csv) == REPOSITORY_ROOT / "docs" / "garble_audit.csv"
    assert Path(settings.workbench_db_path) == REPOSITORY_ROOT / "api.workbench.db"


def test_operator_data_paths_can_be_overridden_by_environment(monkeypatch) -> None:
    monkeypatch.setenv("GARBLE_AUDIT_CSV", "E:/operator/garble_audit.csv")
    monkeypatch.setenv("WORKBENCH_DB_PATH", "E:/operator/api.workbench.db")

    settings = Settings()

    assert settings.garble_audit_csv == "E:/operator/garble_audit.csv"
    assert settings.workbench_db_path == "E:/operator/api.workbench.db"
