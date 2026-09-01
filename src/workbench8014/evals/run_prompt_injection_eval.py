from __future__ import annotations

import json
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = SRC_ROOT / "agent8000"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.prompt_security import inspect_untrusted_text

CASES = Path(__file__).with_name("prompt_injection_redteam.jsonl")


def run() -> int:
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    failures: list[str] = []
    true_positive = false_positive = malicious = benign = 0
    for case in cases:
        result = inspect_untrusted_text(case["text"])
        expected = bool(case["expected_suspicious"])
        if expected:
            malicious += 1
            true_positive += int(result.suspicious)
        else:
            benign += 1
            false_positive += int(result.suspicious)
        if result.suspicious != expected:
            failures.append(f"{case['id']}: suspicious expected {expected}, got {result.suspicious}")
        reason = case.get("expected_reason")
        if reason and reason not in result.reasons:
            failures.append(f"{case['id']}: missing reason {reason}, got {result.reasons}")
    report = {
        "case_count": len(cases),
        "malicious_detection_rate": round(true_positive / malicious, 3) if malicious else None,
        "benign_false_positive_rate": round(false_positive / benign, 3) if benign else None,
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
