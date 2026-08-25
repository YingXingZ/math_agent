"""Run the preserved integration regressions through pytest."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "validate_agent_reports.py",
    "regress_student_submit.py",
    "regression_where_clause.py",
)


def test_regression_scripts() -> None:
    """Each script creates its own database and must succeed from a clean clone."""
    for script_name in SCRIPTS:
        result = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "src" / "tools" / script_name)],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"{script_name} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
