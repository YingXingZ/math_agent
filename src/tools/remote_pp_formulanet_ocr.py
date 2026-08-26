"""Run PP-FormulaNet_plus-L on the configured A100 and emit a review candidate.

The model output is evidence for teacher review only.  It is deliberately not
treated as a calibrated whole-question confidence score: FormulaNet recognises
formula regions, while the input crop can also contain prose.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SSH = ROOT / "src" / "tools" / "server_ssh.py"
MODEL_DIR = "/opt/math-vlm/models/PP-FormulaNet_plus-L"


def call(*args: str, timeout: int = 300) -> str:
    result = subprocess.run([sys.executable, str(SSH), *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-1500:])
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    tag = "ocr-repair-formula-" + uuid.uuid4().hex
    remote_image = f"/tmp/{tag}.png"
    remote_out = f"/tmp/{tag}-out"
    try:
        call("put", str(args.image), remote_image)
        output = call(
            "run", "180",
            f"/opt/math-vlm/bin/paddleocr formula_recognition -i {remote_image} "
            f"--model_name PP-FormulaNet_plus-L --model_dir {MODEL_DIR} --device gpu:0 "
            f"--save_path {remote_out} 2>&1",
        )
        line = next((item for item in output.splitlines() if "'rec_formula':" in item), "")
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        payload = ast.literal_eval(line[line.index("{'res':"):]) if "{'res':" in line else {}
        latex = str((payload.get("res") or {}).get("rec_formula") or "").strip()
        if not latex:
            raise RuntimeError("PP-FormulaNet returned no rec_formula")
        print(json.dumps({
            "provider": "pp_formulanet",
            "latex_text": latex,
            "confidence": 0.0,
            "risks": ["FormulaNet 未提供可校准的整题置信度；输入含题干文字，必须对照原图复核"],
        }, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"provider": "pp_formulanet", "latex_text": "", "confidence": 0.0,
                          "risks": [str(exc)[:1000]], "error": str(exc)[:1000]}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
