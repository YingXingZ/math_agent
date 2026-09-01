from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from evals.run_real_case_eval import run
def test_real_case_protocol_is_safe_and_baseline_passes(capsys):
    assert (ROOT / "evals/real_cases/README.md").is_file()
    assert run() == 0
    assert '"real_case_count": 30' in capsys.readouterr().out
