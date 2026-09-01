"""Single authenticated client for the private 8014 evidence service."""
from __future__ import annotations
import uuid
import httpx
from .config import settings

def headers() -> dict[str,str]:
    data={"X-Request-ID":uuid.uuid4().hex}
    if settings.evidence_api_key: data["X-Internal-API-Key"]=settings.evidence_api_key
    return data

def client(*, timeout:float=30) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, headers=headers())
def url(path:str)->str:
    return settings.evidence_api_url.rstrip("/")+"/"+path.lstrip("/")
