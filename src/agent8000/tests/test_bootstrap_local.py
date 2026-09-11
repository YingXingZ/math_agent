from pathlib import Path

from src.agent8000.scripts.bootstrap_local import venv_python


def test_bootstrap_uses_platform_specific_venv_python(tmp_path: Path):
    path = venv_python(tmp_path / ".venv")

    assert path.parent.name in {"bin", "Scripts"}
    assert path.name in {"python", "python.exe"}
