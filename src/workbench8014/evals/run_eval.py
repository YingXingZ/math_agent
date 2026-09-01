from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from skills.misconception_diagnosis import misconception_diagnosis
from skills.schemas import MisconceptionDiagnosisInput, SymbolicVerificationInput
from skills.symbolic_verification import symbolic_verification
CASES = Path(__file__).with_name("math_agent_eval_cases.jsonl")

def route_for(confidence):
    return "diagnose_misconception" if (confidence or 0) >= 0.85 else "independent_solve"

def run():
    cases=[json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    failures=[]; ct=cp=rp=dt=dp=0
    for case in cases:
        verdict=symbolic_verification(SymbolicVerificationInput(student_answer=case["student_answer"],standard_answer=case["standard_answer"]))
        route=route_for(verdict.confidence)
        if route == case["expected_route"]: rp+=1
        else: failures.append(case["id"]+": route expected "+case["expected_route"]+", got "+route)
        if case["expected_correct"] is not None:
            ct+=1
            if verdict.correct is case["expected_correct"]: cp+=1
            else: failures.append(case["id"]+": correctness mismatch")
        if case["expected_diagnosis"] is not None:
            dt+=1
            d=misconception_diagnosis(MisconceptionDiagnosisInput(student_answer=case["student_answer"],standard_answer=case["standard_answer"],problem_text=case.get("problem_text",""),verification_correct=verdict.correct))
            if case["expected_diagnosis"] in {item.code for item in d.diagnoses}: dp+=1
            else: failures.append(case["id"]+": diagnosis missing")
    print(json.dumps({"case_count":len(cases),"correctness_accuracy":round(cp/ct,3) if ct else None,"route_accuracy":round(rp/len(cases),3),"diagnosis_label_accuracy":round(dp/dt,3) if dt else None,"failures":failures},ensure_ascii=False,indent=2))
    return 1 if failures else 0
if __name__ == "__main__": raise SystemExit(run())
