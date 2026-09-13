#!/usr/bin/env python3
"""Compatibility entry point for the modular model API gateway."""
from bootstrap import activate

activate()
from local_llm_deploy.gateway.app import main

if __name__ == '__main__':
    raise SystemExit(main())
