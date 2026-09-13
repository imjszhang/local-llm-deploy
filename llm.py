#!/usr/bin/env python3
"""Compatibility launcher for a checkout."""
from bootstrap import activate

activate()

from local_llm_deploy.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
