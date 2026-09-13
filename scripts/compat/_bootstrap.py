"""Activate the checkout for compatibility scripts invoked from any directory."""
from pathlib import Path
import sys


def activate():
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    from bootstrap import activate as activate_checkout
    activate_checkout()
