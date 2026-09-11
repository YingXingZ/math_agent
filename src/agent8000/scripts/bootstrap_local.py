#!/usr/bin/env python3
"""Bootstrap a safe local development environment for agent8000.

Usage:
    python scripts/bootstrap_local.py          # create venv, install deps, copy .env
    python scripts/bootstrap_local.py --run    # then start the local API on :8001
    python scripts/bootstrap_local.py --no-install
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
VENV_ROOT = APP_ROOT / ".venv"


def venv_python(venv_root: Path = VENV_ROOT) -> Path:
    return venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=APP_ROOT, check=True)


def prepare_environment(*, install: bool) -> Path:
    python = venv_python()
    if not python.exists():
        run([sys.executable, "-m", "venv", str(VENV_ROOT)])
    if install:
        run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
        run([str(python), "-m", "pip", "install", "-r", "requirements.txt"])
    env_file = APP_ROOT / ".env"
    env_example = APP_ROOT / ".env.example"
    if not env_file.exists():
        shutil.copyfile(env_example, env_file)
        print("Created .env from .env.example. Set bootstrap credentials before a real login.")
    return python


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap the local Math Agent API.")
    parser.add_argument("--run", action="store_true", help="start uvicorn after bootstrap")
    parser.add_argument("--no-install", action="store_true", help="reuse an existing venv without pip install")
    args = parser.parse_args()
    python = prepare_environment(install=not args.no_install)
    if args.run:
        os.execv(str(python), [str(python), "-m", "uvicorn", "app.main:app", "--reload", "--port", "8001"])
    print(f"Ready: {python}")
    print("Start the API with: python scripts/bootstrap_local.py --run")


if __name__ == "__main__":
    main()
