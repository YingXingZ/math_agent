"""Temporary MinerU staging adapter; it never writes the teaching database."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = ROOT / "tools"
for path in (ROOT, TOOLS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

def _pipeline():
    """Load the optional MinerU adapter only when its staging API is used."""
    try:
        from mineru_knowledge_pipeline import build_document, match
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "MinerU staging is unavailable; provide src/tools/mineru_knowledge_pipeline.py "
            "or install the optional MinerU integration."
        ) from exc
    return build_document, match


def stage_markdown(markdown: str, role: str, name: str) -> dict:
    """Stage Markdown via a temporary file, returning JSON only to the caller."""
    temp_dir = ROOT / "tmp" / "api-mineru-staging"
    temp_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(char for char in name if char.isalnum() or char in "-_") or "document"
    source = temp_dir / f"{safe_name}.md"
    source.write_text(markdown, encoding="utf-8")
    build_document, _ = _pipeline()
    return build_document(source, role, name)


def match_staged(textbook: dict, answer_book: dict) -> dict:
    _, match = _pipeline()
    return match(textbook, answer_book)
