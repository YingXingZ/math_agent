"""Create a safe batch PDF-evidence manifest for the current live 8000 queue."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tool_config import agent_db, image_root, workbench_db

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent8000.app.pdf_evidence_pipeline import build_evidence_manifest  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a read-only PDF evidence plan for live garble candidates")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--agent-db", type=Path, default=agent_db())
    parser.add_argument("--workbench-db", type=Path, default=workbench_db())
    parser.add_argument("--image-root", type=Path, default=image_root())
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    if not args.agent_db.is_file() or not args.workbench_db.is_file():
        raise SystemExit("Agent database or 8014 database was not found; no database was changed.")
    manifest = build_evidence_manifest(args.agent_db, args.workbench_db, args.image_root, args.repository_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest["summary"], ensure_ascii=False)); print(f"manifest={args.out}")


if __name__ == "__main__":
    main()
