"""Give the operator's tailnet node user ID the memberships its current id holds.

Quality plan 9d (security/9d-tailscale-ingress-brief.md, W3): after 9d-1 the daemon's
authority is membership alone, and from 9d-5 the web API and the CLI name the operator by
the Tailscale node user ID. This copies each membership of ``--from-user`` (today's
``BFX_OPERATOR_USER_ID``) to ``--node-user-id`` with the same role. The old rows stay
until 9d-7 deletes them, so either id is authorized in between.

  python -m scripts.grant_operator_membership --from-user OLD_ID --node-user-id 1234567

Idempotent: a membership that already exists is left alone. In one transaction, refused
and rolled back unless:

- the node user ID is digits only (the form ``tailscale whois`` reports);
- the connected role owns the membership table (only the owner may write it);
- ``--from-user`` holds at least one membership;
- afterwards the authorized users are exactly the old id and the node id, so a typo in
  either cannot hand the daemon's authority to anyone else.

On the VM run it as a one-shot of the deployed backend image with
/opt/bfx/runtime/migrate.env (the owner role). DATABASE_URL must be supplied explicitly;
this command does not load .env.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import make_async_engine_from_url, make_session_factory

NODE_USER_ID = re.compile(r"^[0-9]+$")


class GrantRefusedError(Exception):
    pass


async def grant(session: AsyncSession, *, from_user: str, node_user_id: str) -> dict[str, Any]:
    """Copy ``from_user``'s memberships to ``node_user_id``; the caller commits."""
    if not NODE_USER_ID.fullmatch(node_user_id):
        raise GrantRefusedError("node_user_id_not_numeric")
    owner = await session.scalar(text(
        "SELECT pg_has_role(current_user, relowner, 'USAGE') FROM pg_class "
        "WHERE oid = 'public.exchange_account_memberships'::regclass"))
    if not owner:
        raise GrantRefusedError("not_the_table_owner")
    await session.execute(text(
        "LOCK TABLE public.exchange_account_memberships, public.exchange_accounts IN SHARE MODE"))
    source = (await session.execute(text(
        "SELECT exchange_account_id, role FROM public.exchange_account_memberships "
        "WHERE user_id = :u"), {"u": from_user})).all()
    if not source:
        raise GrantRefusedError("from_user_has_no_membership")
    inserted = (await session.execute(text(
        "INSERT INTO public.exchange_account_memberships (exchange_account_id, user_id, role) "
        "SELECT exchange_account_id, :node, role FROM public.exchange_account_memberships "
        "WHERE user_id = :u ON CONFLICT (exchange_account_id, user_id) DO NOTHING "
        "RETURNING exchange_account_id"), {"node": node_user_id, "u": from_user})).all()
    authorized = set((await session.scalars(text(
        "SELECT DISTINCT m.user_id FROM public.exchange_account_memberships m "
        "WHERE public.operator_authorized(m.exchange_account_id, m.user_id)"))).all())
    if not authorized <= {from_user, node_user_id}:
        raise GrantRefusedError("unexpected_authorized_user")
    if node_user_id not in authorized:
        raise GrantRefusedError("node_user_id_not_authorized")
    return {"status": "granted", "accounts": len(source), "inserted": len(inserted),
            "authorized_users": len(authorized)}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    engine = make_async_engine_from_url(os.environ["DATABASE_URL"])
    try:
        async with make_session_factory(engine)() as session:
            try:
                report = await grant(session, from_user=args.from_user,
                                     node_user_id=args.node_user_id)
            except BaseException:
                await session.rollback()
                raise
            await session.commit()
            return report
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-user", required=True,
                        help="the operator's current membership user_id")
    parser.add_argument("--node-user-id", required=True,
                        help="the operator's Tailscale node user ID (digits)")
    args = parser.parse_args()
    logging.disable(sys.maxsize)
    try:
        print(json.dumps(asyncio.run(run(args)), sort_keys=True))
        return 0
    except GrantRefusedError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}), file=sys.stderr)
        return 2
    except Exception:
        # Never expose a connection URL, DB bind values or credentials.
        print('{"status":"blocked","reason":"grant_failed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
