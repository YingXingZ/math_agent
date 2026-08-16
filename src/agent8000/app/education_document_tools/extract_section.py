"""Tool 1: extract / sync one textbook section from the evidence source."""
from __future__ import annotations

from typing import Any


async def sync_section(section_no: str, limit: int = 80) -> dict[str, Any]:
    """Pull one section from the 8014 evidence workbench through the quality gate.

    The heavy lifting lives in ``main._sync_section_into_local_cache`` (quality
    gate, Pix2Text rescue, Qwen fallback, DB upsert).  We lazy-import it so that
    the ``education_document_tools`` package can be imported from ``main.py``
    without creating an import cycle.
    """
    from ..main import _sync_section_into_local_cache
    return await _sync_section_into_local_cache(section_no, limit)
