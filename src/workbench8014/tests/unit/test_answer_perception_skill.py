from pathlib import Path
import sys

WORKBENCH = Path(__file__).resolve().parents[2]
if str(WORKBENCH) not in sys.path:
    sys.path.insert(0, str(WORKBENCH))

from skills import answer_perception as module
from skills.schemas import AnswerPerceptionInput


def test_perception_never_sends_standard_answer(monkeypatch):
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"model":"local-qwen","results":[{"recognized_work":"x^2","recognition_confidence":0.88,"formula_regions":[{"bbox":[1,2,3,4],"confidence":0.8}]}]}'
    def fake_open(request, **kwargs):
        captured["body"] = request.data.decode()
        return Response()
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_open)
    result = module.answer_perception(AnswerPerceptionInput(image_base64="a"*100, problem_id="p1", problem_text="求导"))
    assert result.success is True
    assert result.recognized_work == "x^2"
    assert result.formula_regions[0].bbox == [1.0,2.0,3.0,4.0]
    assert "std_answer" not in captured["body"]
    assert "full_solution" not in captured["body"]


def test_empty_recognition_has_explicit_failure(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"results":[{}]}'
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: Response())
    result = module.answer_perception(AnswerPerceptionInput(image_base64="a"*100, problem_id="p1"))
    assert result.success is False
    assert result.error_code == "EMPTY_OCR_RESULT"
