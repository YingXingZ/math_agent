"""Deprecated compatibility launcher for the canonical 8014 launcher.

Use ``src/workbench8014/run_workbench_8014.py`` for all new deployments.
"""
from pathlib import Path
import runpy


CANONICAL_LAUNCHER = Path(__file__).resolve().parents[1] / "workbench8014" / "run_workbench_8014.py"
runpy.run_path(str(CANONICAL_LAUNCHER), run_name="__main__")
