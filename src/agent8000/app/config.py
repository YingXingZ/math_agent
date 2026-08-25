from pathlib import Path
from pydantic_settings import BaseSettings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]


class Settings(BaseSettings):
    # Resolve local storage from the project root, not the process working
    # directory. Environment variables can still override these defaults.
    database_path: str = str(PROJECT_ROOT / "data" / "homework.db")
    upload_dir: str = str(PROJECT_ROOT / "data" / "uploads")
    dify_api_url: str = ""
    dify_api_key: str = ""
    dify_workflow_id: str = ""
    # The existing 8014 workbench is the authoritative evidence store for
    # textbooks, answer PDFs, Qwen results and symbolic verification.
    evidence_api_url: str = "http://127.0.0.1:8014/api"
    qwen_grading_url: str = "http://127.0.0.1:18080/grade-homework"
    qwen_pdf_max_pages: int = 12
    qwen_pdf_render_dpi: int = 144
    pdf_renderer_path: str = ""
    # Operator-managed evidence files used by the teacher editing workflow.
    # Keep usable repository defaults while allowing deployments to place data
    # outside the source tree.
    garble_audit_csv: str = str(REPOSITORY_ROOT / "docs" / "garble_audit.csv")
    workbench_db_path: str = str(REPOSITORY_ROOT / "api.workbench.db")

    class Config:
        env_file = ".env"

    def prepare_dirs(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.upload_dir).mkdir(parents=True, exist_ok=True)


settings = Settings()
