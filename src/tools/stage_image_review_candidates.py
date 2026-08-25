"""Stage VLM readings of available source crops for teacher review only.

This command never writes to 8014, the question cache, or publication state.
With ``--stage`` it stores readable VLM outputs in ``ai_stem_candidates`` with
status ``pending``. A teacher must inspect the source image and explicitly use
the existing approve/reject endpoint before any source question can change.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import httpx

from tool_config import agent_db, agent_dir


def call_vlm(url: str, image_path: Path, item: dict) -> dict:
    payload = {
        "image_base64": base64.b64encode(image_path.read_bytes()).decode("ascii"),
        "section_no": item["section_no"],
        "problem_no": str(item["problem_no"]),
    }
    response = httpx.post(url.rstrip("/") + "/solve-from-image", json=payload, timeout=600)
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage readable crop recognitions for teacher review")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--agent-db", type=Path, default=agent_db())
    parser.add_argument("--vlm-url", default=os.environ.get("MATH_VLM_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--stage", action="store_true", help="write pending candidates; otherwise dry-run")
    parser.add_argument("--offset", type=int, default=0, help="skip this many review-ready entries")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    items = [entry for entry in manifest["candidates"] if entry["disposition"] == "ready_for_teacher_review"]
    items = items[args.offset:]
    if args.limit:
        items = items[:args.limit]
    if not items:
        print("No crop-backed candidates to stage.")
        return

    if args.stage:
        os.environ["DATABASE_PATH"] = str(args.agent_db)
        sys.path.insert(0, str(agent_dir()))
        from app.ai_stem_review import store_pending_candidate
        from app.db import init_db
        init_db()

    staged = unavailable = 0
    for item in items:
        image_path = Path(manifest["image_root"]) / item["crop_image_path"].replace("\\", "/")
        label = f"S{item['section_no']}#{item['problem_no']}{item['sub_no']}"
        try:
            vision = call_vlm(args.vlm_url, image_path, item)
        except Exception as exc:  # noqa: BLE001
            unavailable += 1
            print(f"UNAVAILABLE {label}: {str(exc)[:160]}")
            continue
        text = str(vision.get("problem_text") or "").strip()
        if len(text) < 6:
            unavailable += 1
            print(f"UNREADABLE {label}: VLM returned no reviewable stem")
            continue
        print(f"CANDIDATE {label}: confidence={float(vision.get('confidence') or 0):.2f}")
        if not args.stage:
            continue
        candidate = {
            "problem_text": text,
            "ptype": vision.get("ptype") or item["ptype"],
            "std_answer": vision.get("std_answer") or "",
            "full_solution": vision.get("full_solution") or "",
            "confidence": float(vision.get("confidence") or 0),
            "agreement": {"equal": False, "confidence": 0.0, "method": "single-image review staging"},
            "vision": vision,
            "independent": None,
        }
        source_item = {
            "source_problem_id": str(item["problem_id"]),
            "problem_no": str(item["problem_no"]),
            "sub_no": item["sub_no"],
            "difficulty": item["difficulty"],
            "evidence": {"section_no": item["section_no"], "crop_image_path": item["crop_image_path"]},
        }
        result = store_pending_candidate(source_item, candidate)
        if result.get("stored"):
            staged += 1
        print(f"  queue={result}")
    print(json.dumps({"considered": len(items), "staged": staged, "unavailable": unavailable, "mode": "stage" if args.stage else "dry-run"}))


if __name__ == "__main__":
    main()
