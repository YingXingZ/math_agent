from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from math_agent_graph import proof_step_assessment, run_math_agent
PART1_ONLY = "g(-x)=f(-x)+f(x)=g(x); h(-x)=f(-x)-f(x)=-h(x)."
COMPLETE = PART1_ONLY + " f(x)=1/2 g(x)+1/2 h(x)，所以 f(x) 是一个偶函数与一个奇函数的和。"
def test_proof_part2_missing_is_not_teacher_review():
    a=proof_step_assessment(PART1_ONLY)
    assert a["part2_status"]=="missing" and a["subquestions"][1]["status"]=="missing"
    r=run_math_agent(PART1_ONLY,"proof",problem_text="证明 h(x) 是奇函数，f(x) 可表示为偶函数与奇函数的和",question_type="proof")
    assert r["action"]=="teach_student" and "未识别到第（2）问" in r["response"] and "教师确认" not in r["response"]
def test_proof_part2_complete_is_accepted():
    a=proof_step_assessment(COMPLETE)
    assert a["part2_status"]=="confirmed" and a["subquestions"][1]["status"]=="correct"
def test_proof_unclear_or_conflicting_record_needs_teacher_review():
    assert proof_step_assessment("未识别到作答")["part2_status"]=="uncertain"
    assert proof_step_assessment(COMPLETE,"第（2）问错误：缺少分解公式")["teacher_feedback_conflict"] is True
