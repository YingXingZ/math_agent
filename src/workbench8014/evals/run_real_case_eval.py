"""运行脱敏真实案例回归；没有案例时返回 0，但明确报告样本为 0。"""
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from skills.schemas import SymbolicVerificationInput, MisconceptionDiagnosisInput
from skills.symbolic_verification import symbolic_verification
from skills.misconception_diagnosis import misconception_diagnosis
from math_agent_graph import proof_step_assessment
CASES=Path(__file__).with_name("real_cases")/"sanitized_cases.jsonl"

def route(c): return "diagnose_misconception" if (c or 0)>=0.85 else "independent_solve"
def run():
    rows=[json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    failures=[]
    for x in rows:
        required={"id","source","consent_or_anonymization","student_answer","standard_answer","expected_correct","expected_route","teacher_verified"}
        if not required <= set(x) or x["consent_or_anonymization"]!="anonymized" or x["teacher_verified"] is not True:
            failures.append(x.get("id","unknown")+": invalid governance fields"); continue
        v=symbolic_verification(SymbolicVerificationInput(student_answer=x["student_answer"],standard_answer=x["standard_answer"]))
        # Proof cases are not valid inputs for symbolic-expression equivalence;
        # their expected truth is checked by the critical-step regression below.
        if x.get("regression") != "proof_key_evidence_must_not_be_called_missing" and v.correct is not x["expected_correct"]: failures.append(x["id"]+": correctness mismatch")
        if route(v.confidence)!=x["expected_route"]: failures.append(x["id"]+": route mismatch")
        if x.get("expected_diagnosis"):
            d=misconception_diagnosis(MisconceptionDiagnosisInput(student_answer=x["student_answer"],standard_answer=x["standard_answer"],problem_text=x.get("problem_text",""),verification_correct=v.correct))
            if x["expected_diagnosis"] not in {i.code for i in d.diagnoses}: failures.append(x["id"]+": diagnosis mismatch")
        if x.get("regression") == "proof_key_evidence_must_not_be_called_missing":
            assessment = proof_step_assessment(x["student_answer"], "学生没有正确地证明 h(x) 是否为奇函数")
            if not assessment["key_evidence_present"]:
                failures.append(x["id"]+": key proof evidence was missed")
            if assessment["subquestions"][0]["status"] != "correct":
                failures.append(x["id"]+": first subquestion was not recognised correct")
            if not assessment["teacher_feedback_conflict"]:
                failures.append(x["id"]+": stale teacher-feedback conflict was not raised")
    print(json.dumps({"real_case_count":len(rows),"failures":failures},ensure_ascii=False,indent=2))
    return 1 if failures else 0
if __name__=="__main__": raise SystemExit(run())
