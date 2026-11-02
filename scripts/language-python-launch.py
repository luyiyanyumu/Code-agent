"""Run one bundled Python LSP without changing embedded Python's ._pth file."""
from pathlib import Path
import os
import runpy
import sys

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in {"pylsp", "ruff"}:
        raise SystemExit("Expected a bundled Python language service: pylsp or ruff")
    module = sys.argv.pop(1)
    sys.path.insert(0, str(Path(__file__).resolve().parent / "site-packages"))
    if module == "pylsp" and os.environ.get("CODE_AGENT_LSP_CACHE"):
        import jedi
        jedi.settings.cache_directory = str(Path(os.environ["CODE_AGENT_LSP_CACHE"]) / "jedi")
    runpy.run_module(module, run_name="__main__", alter_sys=True)
