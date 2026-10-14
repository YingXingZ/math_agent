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
    # Empty is valid for a local-only 8014 workbench; production deployments
    # may provide EVIDENCE_API_KEY when the evidence service is protected.
    evidence_api_key: str = ""
    # Default local grader contract. Environment variables may override every value.
    llm_provider: str = "local_qwen"
    local_qwen_model: str = "local-grade-service"
    vlm_internal_api_key: str = ""
    llm_request_timeout_seconds: int = 180
    qwen_api_base_url: str = ""
    qwen_api_grading_url: str = ""
    qwen_api_key: str = ""
    qwen_api_model: str = "qwen"
    qwen_grading_url: str = "http://127.0.0.1:18080/grade-homework"
    local_qwen_urls: str = ""
    formula_ocr_enabled: bool = False
    formula_ocr_url: str = "http://127.0.0.1:18080/recognize-formula"
    formula_ocr_timeout_seconds: int = 90
    deepseek_fallback_enabled: bool = False
    deepseek_api_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_api_model: str = "deepseek-flash"
    deepseek_fallback_confidence: float = 0.55
    deepseek_fallback_timeout_seconds: int = 120
    # One-time OpenAI-compatible solver endpoint. It generates candidate reference
    # answers/rubrics only; a teacher must confirm them before student grading uses them.
    solver_provider: str = "openai_compatible"  # openai_compatible | local_vlm
    solver_api_base_url: str = ""
    solver_api_key: str = ""
    solver_api_model: str = ""
    solver_local_url: str = ""
    solver_api_timeout_seconds: int = 120
    qwen_pdf_max_pages: int = 12
    qwen_pdf_render_dpi: int = 144
    pdf_renderer_path: str = ""
    # Operator-managed evidence files used by the teacher editing workflow.
    # Keep usable repository defaults while allowing deployments to place data
    # outside the source tree.
    garble_audit_csv: str = str(REPOSITORY_ROOT / "docs" / "garble_audit.csv")
    workbench_db_path: str = str(REPOSITORY_ROOT / "api.workbench.db")
    # Authentication is deliberately opt-in for the existing local single-user
    # installation.  Every public deployment must set AUTH_REQUIRED=true.
    auth_required: bool = True
    session_days: int = 14
    session_cookie_name: str = "math_agent_session"
    cookie_secure: bool = False
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    student_invite_days: int = 14
    submission_retention_days: int = 365
    audit_retention_days: int = 730
    # Queue settings. SQLite stays the safe local default; production can set
    # QUEUE_BACKEND=rq and REDIS_URL without changing route code.
    # inline keeps one-host deployments working without Redis; set rq only when Redis is configured.
    task_queue_mode: str = "inline"
    queue_backend: str = "sqlite"
    redis_url: str = ""
    cors_origins: str = ""
    rq_job_timeout_seconds: int = 900
    rq_retry_max: int = 3
    rq_queue_name: str = "grading"
    grading_max_attempts: int = 3
    grading_retry_base_seconds: int = 60
    metrics_retention_days: int = 90
    evidence_cache_ttl_seconds: int = 60
    # Optional MinerU OCR executable for scanned textbook PDFs.  Production can
    # override both values in runtime.env; local installs remain unaffected.
    mineru_bin: str = ""
    mineru_output_dir: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"

    def prepare_dirs(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.upload_dir).mkdir(parents=True, exist_ok=True)


settings = Settings()
