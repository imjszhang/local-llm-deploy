#!/usr/bin/env python3
"""Compatibility entrypoint; implementation lives in the installed package."""
from _bootstrap import activate
activate()
from local_llm_deploy.artifacts.download import main

if __name__ == "__main__":
    raise SystemExit(main())
