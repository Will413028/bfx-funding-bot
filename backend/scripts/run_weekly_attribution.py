"""E3 (b) — per-cell weekly fee-adjusted realized interest → attribution_weekly,
reconciled against the venue ledger. Thin CLI over
modules/live_validation/attribution_loader.py (inputs, matching and the
reconciliation window are documented there).

Run from backend/ (env: DATABASE_URL / BFX_EXCHANGE_ACCOUNT_ID /
BFX_DEPLOYMENT_ENV):
  uv run python -m scripts.run_weekly_attribution [--weeks 8] [--out FILE]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings, require_deployment_environment
from bfx_funding_bot.modules.accounts.exchange_accounts import account_id_canonical
from bfx_funding_bot.modules.live_validation.attribution_loader import (
    load_and_compute,
    persist_rows,
    render_reconciliation,
)

log = logging.getLogger(__name__)


async def _amain(args: argparse.Namespace) -> int:
    settings = Settings()
    engine = make_engine(settings)
    sf = make_session_factory(engine)
    raw_account_id = os.environ.get("BFX_EXCHANGE_ACCOUNT_ID", "").strip()
    if not raw_account_id:
        raise RuntimeError("BFX_EXCHANGE_ACCOUNT_ID is required")
    account_id = account_id_canonical(raw_account_id)
    env = require_deployment_environment()
    try:
        result = await load_and_compute(
            sf, account_id=account_id, deployment_environment=env,
            reconcile_weeks=args.weeks,
        )
        if not result.has_credit_history:
            # The bot's CreditHistorySync has not filled the table yet: keep the
            # previous rows instead of replacing them with nothing.
            print("WARN attribution_weekly unchanged: funding_credit_history is empty "
                  "(CreditHistorySync not run yet)")
            return 0
        n = await persist_rows(sf, result.rows, account_id=account_id,
                               deployment_environment=env)
    finally:
        await engine.dispose()
    unattributed = sum(1 for r in result.rows if r.cell == "unattributed")
    print(f"attribution_weekly upserted={n} unattributed_rows={unattributed}")
    report = render_reconciliation(result)
    if args.out:
        Path(args.out).write_text(f"# Attribution vs ledger\n\n{report}")
    print(report, end="")
    flagged = [r for r in result.reconciliations if r.flagged]
    if flagged:
        log.warning("attribution_reconciliation_flagged weeks=%d", len(flagged))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--weeks", type=int, default=8,
                        help="complete weeks to reconcile against the ledger")
    parser.add_argument("--out", help="write the reconciliation as markdown here")
    sys.exit(asyncio.run(_amain(parser.parse_args())))
