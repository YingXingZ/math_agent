"""Education-document tools used by the Agent Orchestrator.

The three tools mirror the design-doc pipeline (设计二):

1. ``extract_section`` — pull a readable, gated copy of one textbook section
   from the authoritative 8014 evidence workbench into the local cache.
2. ``stratify_difficulty`` — assign problems to 基础 / 提高 / 综合 tiers so
   that assignments can follow the required 3-2-1 distribution.
3. ``assemble_assignment`` — select problems by tier, preserve original problem
   numbers, and render a printable A4 PDF + LaTeX source.
"""
from .extract_section import sync_section
from .stratify_difficulty import stratify_section_difficulty
from .assemble_assignment import assemble

__all__ = ["sync_section", "stratify_section_difficulty", "assemble"]
