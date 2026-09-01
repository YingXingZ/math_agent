import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.config import settings
from app.evidence_client import headers, url

def test_evidence_client_adds_internal_key(monkeypatch):
    monkeypatch.setattr(settings, "evidence_api_key", "test-internal-secret")
    monkeypatch.setattr(settings, "evidence_api_url", "http://evidence.internal:8014/api/")
    request_headers = headers()
    assert request_headers["X-Internal-API-Key"] == "test-internal-secret"
    assert request_headers["X-Request-ID"]
    assert url("/problems/42") == "http://evidence.internal:8014/api/problems/42"

def test_evidence_client_omits_empty_key(monkeypatch):
    monkeypatch.setattr(settings, "evidence_api_key", "")
    assert "X-Internal-API-Key" not in headers()
