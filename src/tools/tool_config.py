"""Shared, environment-first paths for maintenance tools.

Maintenance commands must never rely on a particular developer workstation.
All defaults resolve from the repository root and may be overridden by the
same environment variables used by the running services.
"""
from __future__ import annotations

import os
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def workbench_db() -> Path:
    return Path(os.environ.get("WORKBENCH_DB", REPOSITORY_ROOT / "api.workbench.db"))


def image_root() -> Path:
    return Path(os.environ.get("IMAGE_ROOT", REPOSITORY_ROOT / "extract_img"))


def agent_dir() -> Path:
    return Path(os.environ.get("AGENT_DIR", REPOSITORY_ROOT / "src" / "agent8000"))


def agent_db() -> Path:
    return Path(os.environ.get("AGENT_DB", agent_dir() / "data" / "homework.db"))
