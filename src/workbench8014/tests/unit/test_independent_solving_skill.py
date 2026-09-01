from pathlib import Path
import sys

WORKBENCH = Path(__file__).resolve().parents[2]
if str(WORKBENCH) not in sys.path:
    sys.path.insert(0, str(WORKBENCH))

from skills.schemas import IndependentSolveInput
from skills import independent_solving as module


def test_qwen_invalid_response_is_safe(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b"[]"

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    result = module.independent_solving(IndependentSolveInput(problem_text="求 lim x->0 sin(x)/x"))
    assert result.success is False
    assert result.error_code == "INVALID_MODEL_RESPONSE"


def test_qwen_request_never_contains_standard_answer(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"std_answer":"1","confidence":0.9,"full_solution":"use important limit"}'

    def fake_open(request, **kwargs):
        captured["body"] = request.data.decode("utf-8")
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_open)
    result = module.independent_solving(IndependentSolveInput(problem_text="test question"))
    assert result.success is True
    assert result.answer == "1"
    assert "standard_answer" not in captured["body"]
