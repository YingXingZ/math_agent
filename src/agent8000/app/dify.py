"""Dify 工作流边界：不在业务路由中耦合具体模型供应商。"""
from pathlib import Path
from typing import Any
import mimetypes
import httpx
from .config import settings


async def upload_file(file_path: str, user: str) -> dict[str, Any]:
    """Upload a private local submission before passing it to Dify as a file input."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"submission file not found: {path}")
    headers = {"Authorization": f"Bearer {settings.dify_api_key}"}
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as stream:
        files = {"file": (path.name, stream, mime_type)}
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(settings.dify_api_url.rstrip("/") + "/files/upload", headers=headers, data={"user": user}, files=files)
            response.raise_for_status()
            return response.json()


async def run_workflow(inputs: dict[str, Any], user: str = "teacher", file_inputs: dict[str, str] | None = None) -> dict[str, Any]:
    if not (settings.dify_api_url and settings.dify_api_key):
        return {"mode": "local-placeholder", "message": "未配置 Dify，已使用本地规则流程。"}
    workflow_inputs = dict(inputs)
    for variable_name, file_path in (file_inputs or {}).items():
        uploaded = await upload_file(file_path, user)
        mime_type = uploaded.get("mime_type", "")
        workflow_inputs[variable_name] = [{
            "transfer_method": "local_file",
            "upload_file_id": uploaded["id"],
            "type": "image" if mime_type.startswith("image/") else "document",
        }]
    headers = {"Authorization": f"Bearer {settings.dify_api_key}", "Content-Type": "application/json"}
    payload = {"inputs": workflow_inputs, "response_mode": "blocking", "user": user}
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(settings.dify_api_url.rstrip("/") + "/workflows/run", headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
