"""Compatibility entrypoints work from any cwd, before or after package installation."""
from pathlib import Path
import os
import sys


def activate():
    root = Path(__file__).resolve().parent
    os.environ.setdefault("LOCAL_LLM_ROOT", str(root))
    # Prefer this checkout, even if another checkout is installed in the interpreter.
    # This is an absolute compatibility path; package code never relies on cwd.
    source = str(root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
