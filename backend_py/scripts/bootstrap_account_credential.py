"""Bootstrap one existing UUID account from operator-injected environment secrets.

Requires BFX_BOOTSTRAP_API_KEY, BFX_BOOTSTRAP_API_SECRET, BFX_VAULT_KEK and
DATABASE_URL. Default is rollback-only; --apply explicitly commits. No secret
arguments, environment dumps, remote error text, or traceback are emitted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import NoReturn
from uuid import UUID


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError("bootstrap_arguments_invalid")


async def _run(args: argparse.Namespace) -> dict[str, str | bool]:
    import httpx

    from bfx_funding_bot.core.crypto import load_kek
    from bfx_funding_bot.core.db import make_async_engine_from_url, make_session_factory
    from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
    from bfx_funding_bot.modules.accounts.credential_bootstrap import bootstrap_credential

    # Explicit environment only: no automatic dotenv fallback to another DB.
    api_key = os.environ["BFX_BOOTSTRAP_API_KEY"]
    api_secret = os.environ["BFX_BOOTSTRAP_API_SECRET"]
    kek = load_kek()
    if not api_key.strip() or not api_secret.strip():
        raise ValueError("bootstrap_input_invalid")
    engine = make_async_engine_from_url(os.environ["DATABASE_URL"])
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            return await bootstrap_credential(
                make_session_factory(engine),
                exchange_account_id=args.exchange_account_id,
                owner_user_id=args.owner_user_id,
                api_key=api_key,
                api_secret=api_secret,
                kek=kek,
                client=BitfinexAuthREST(http=http),
                apply=args.apply,
            )
    finally:
        await engine.dispose()


def main() -> int:
    # This dedicated operator process must not expose SQL bind parameters or
    # HTTP error responses via dependency loggers, including debug settings.
    logging.disable(sys.maxsize)
    try:
        parser = SafeParser(description=__doc__)
        parser.add_argument("--exchange-account-id", type=UUID, required=True)
        parser.add_argument("--owner-user-id", required=True)
        parser.add_argument(
            "--apply", action="store_true", help="commit after permission verification"
        )
        result = asyncio.run(_run(parser.parse_args()))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (Exception, KeyboardInterrupt):
        print('{"status":"bootstrap_failed","outcome":"unconfirmed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
