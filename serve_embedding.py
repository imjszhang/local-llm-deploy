#!/usr/bin/env python3
"""Compatibility entry point; implementation lives in local_llm_deploy.services."""
from bootstrap import activate

activate()
from local_llm_deploy.services.embedding import main

if __name__ == "__main__":
    raise SystemExit(main())
