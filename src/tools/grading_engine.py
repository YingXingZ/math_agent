"""Compatibility export for the canonical src/grading_engine.py.

Do not add logic here: the application and tools share one grading engine.
"""
from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

_CANONICAL = Path(__file__).resolve().parents[1] / "grading_engine.py"
_SPEC = importlib.util.spec_from_file_location("_canonical_grading_engine", _CANONICAL)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("canonical grading engine is unavailable")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
for _name in dir(_MODULE):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_MODULE, _name)
__all__ = [name for name in globals() if not name.startswith("_")]
