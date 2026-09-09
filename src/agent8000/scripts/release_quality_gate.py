"""Single release gate for grading quality and workbench readiness."""
from __future__ import annotations

import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> None:
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


def main() -> int:
    run("-m", "pytest", "-q")
    run("-m", "evals.run_grading_quality_eval")
    manifest = os.getenv("PDF_GOLD_MANIFEST", "").strip()
    if os.getenv("REQUIRE_REAL_PDF_GOLD") == "1" and not manifest:
        raise SystemExit("PDF_GOLD_MANIFEST is required for a production release")
    if manifest:
        run("-m", "evals.run_pdf_gold_eval", manifest, "--min-cases", os.getenv("PDF_GOLD_MIN_CASES", "100"))
    base_url = os.getenv("WORKBENCH_BASE_URL", "").rstrip("/")
    if base_url:
        with urllib.request.urlopen(base_url + "/healthz", timeout=10) as response:
            if response.status != 200:
                raise SystemExit(f"health check failed: {response.status}")
    print("release quality gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
