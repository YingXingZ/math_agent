"""Isolated Pix2Text worker for one suspicious question crop.

This worker is deliberately database-free.  It returns a review candidate and
never edits the authoritative question text or publication state.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import traceback


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    try:
        image = Path(args.image)
        if not image.is_file():
            raise FileNotFoundError(f"question crop missing: {image}")
        from pix2text import Pix2Text

        recognizer = Pix2Text.from_config(
            enable_formula=True,
            enable_table=False,
            device="cpu",
        )
        candidate = str(
            recognizer.recognize(str(image), file_type="text_formula")
        ).strip()
        result = {
            "status": "review_candidate" if candidate else "unavailable",
            "provider": "pix2text",
            "candidate_text": candidate,
            "confidence": 0.45,
            "auto_publish_allowed": False,
            "notes": "Pix2Text 仅作为可疑公式的二层补救候选，必须重新校验并由教师确认。",
        }
        output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return 0 if candidate else 2
    except Exception:
        output.write_text(json.dumps({
            "status": "unavailable",
            "provider": "pix2text",
            "candidate_text": "",
            "confidence": 0.0,
            "auto_publish_allowed": False,
            "error": traceback.format_exc()[-2000:],
        }, ensure_ascii=False), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
