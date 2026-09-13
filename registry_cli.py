#!/usr/bin/env python3
"""Compatibility entrypoint; implementation lives in the installed package."""
from bootstrap import activate
activate()
from local_llm_deploy.registry import main

if __name__ == "__main__":
    raise SystemExit(main())
