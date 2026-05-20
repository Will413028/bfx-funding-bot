#!/usr/bin/env python
"""G1 deployment-gate smoke check (CLI wrapper).

Business logic in bfx_funding_bot.smoke.g1. This file kept for backwards-compatible
invocation via `uv run python scripts/g1_smoke_check.py`.
"""
from bfx_funding_bot.smoke.g1 import main

if __name__ == "__main__":
    main()
