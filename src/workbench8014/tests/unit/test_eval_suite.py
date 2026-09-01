from pathlib import Path
import json, sys
ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from evals.run_eval import run
def test_eval_set_has_fixed_30_case_mix():
    cases=[json.loads(line) for line in (ROOT/"evals/math_agent_eval_cases.jsonl").read_text().splitlines()]
    assert len(cases)==30
    assert sum(c["category"]=="equivalent" for c in cases)==10
    assert sum(c["expected_diagnosis"] is not None for c in cases)>=6
def test_offline_eval_regression_passes(capsys):
    assert run()==0
    assert '"case_count": 30' in capsys.readouterr().out
