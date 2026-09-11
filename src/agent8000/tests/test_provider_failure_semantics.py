import httpx

from app.llm_provider import LLMProviderError, normalize_provider_error


def test_timeout_is_retryable_and_stable():
    error = normalize_provider_error(httpx.ReadTimeout("slow upstream"))
    assert error.error_code == "UPSTREAM_TIMEOUT"
    assert error.retryable is True
    assert str(error).startswith("UPSTREAM_TIMEOUT:")


def test_connection_failure_is_retryable_and_stable():
    request = httpx.Request("POST", "http://vlm.internal/grade-homework")
    error = normalize_provider_error(httpx.ConnectError("offline", request=request))
    assert error.error_code == "UPSTREAM_UNAVAILABLE"
    assert error.retryable is True


def test_http_500_is_retryable_but_bad_request_is_not():
    request = httpx.Request("POST", "http://vlm.internal/grade-homework")
    unavailable = normalize_provider_error(httpx.HTTPStatusError("bad gateway", request=request, response=httpx.Response(502, request=request)))
    rejected = normalize_provider_error(httpx.HTTPStatusError("bad request", request=request, response=httpx.Response(400, request=request)))
    assert (unavailable.error_code, unavailable.retryable) == ("UPSTREAM_ERROR", True)
    assert (rejected.error_code, rejected.retryable) == ("UPSTREAM_REJECTED", False)


def test_existing_semantic_error_is_not_reclassified():
    original = LLMProviderError("invalid schema", error_code="MALFORMED_RESPONSE")
    assert normalize_provider_error(original) is original