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
    # Switch model backends at deployment time. local_qwen keeps the current
    # A100-compatible HTTP service; qwen_api calls a Qwen-compatible API.
    llm_provider: str = "local_qwen"
    local_qwen_model: str = "local-grade-service"
    qwen_grading_url: str = "http://127.0.0.1:18080/grade-homework"
    qwen_api_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_api_key: str = ""
    qwen_api_model: str = "qwen-plus"
    qwen_api_grading_url: str = ""
    llm_request_timeout_seconds: int = 1800
    qwen_pdf_max_pages: int = 12
    qwen_pdf_render_dpi: int = 144
    pdf_renderer_path: str = ""
    # Inline is safe for local development. Production Compose sets rq and
    # provides Redis so web requests never execute long Qwen calls themselves.
    task_queue_mode: str = "inline"
    redis_url: str = ""
    rq_queue_name: str = "grading"
    rq_job_timeout_seconds: int = 2100
    rq_retry_max: int = 3
    # Operator-managed evidence files used by the teacher editing workflow.
    # Keep usable repository defaults while allowing deployments to place data
    # outside the source tree.
    garble_audit_csv: str = str(REPOSITORY_ROOT / "docs" / "garble_audit.csv")
    workbench_db_path: str = str(REPOSITORY_ROOT / "workbench8014" / "api.workbench.db")
    # The public teaching platform always requires an authenticated account.
    # Local development can explicitly set AUTH_REQUIRED=false when needed.
    auth_required: bool = True
    session_days: int = 14
    session_cookie_name: str = "math_agent_session"
    cookie_secure: bool = False
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    student_invite_days: int = 14
    submission_retention_days: int = 365
    audit_retention_days: int = 730

    class Config:
        env_file = ".env"

    def prepare_dirs(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.upload_dir).mkdir(parents=True, exist_ok=True)


settings = Settings()
