import asyncio

from src.agent8000.app import llm_provider


def test_local_vlm_request_sends_internal_key(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return Response()

    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", Client)
    monkeypatch.setattr(llm_provider.settings, "llm_provider", "local_qwen")
    monkeypatch.setattr(llm_provider.settings, "qwen_grading_url", "http://vlm.internal/grade-homework")
    monkeypatch.setattr(llm_provider.settings, "vlm_internal_api_key", "test-vlm-secret")

    result = asyncio.run(llm_provider.grade_homework(
        ["image-base64"],
        [{"problem_id": "p1", "problem_no": "1", "problem_text": "求 1+1", "std_answer": "2", "max_score": 10}],
    ))

    assert result == []
    assert captured["url"] == "http://vlm.internal/grade-homework"
    assert captured["headers"] == {"X-Internal-API-Key": "test-vlm-secret"}
    assert captured["payload"]["security_policy"] == "untrusted_data_only"


def test_vlm_security_contract_is_production_gated():
    source = open("src/vlm18080/server_vlm_service.py", encoding="utf-8").read()

    assert 'VLM_MODE == "production" and not INTERNAL_API_KEY' in source
    assert 'X-Internal-API-Key' in source
    assert 'internal service authentication required' in source


def test_agent_cors_is_opt_in_and_never_wildcarded():
    source = open("src/agent8000/app/main.py", encoding="utf-8").read()

    assert "settings.cors_origins" in source
    assert "allow_credentials=True" in source
    assert 'allow_origins=["*"]' not in source
