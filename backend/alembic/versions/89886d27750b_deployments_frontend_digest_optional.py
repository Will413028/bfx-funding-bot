"""A deployment may have no frontend image: ``deployments.frontend_digest`` becomes nullable.

Quality plan 9e-2 (security/9d-tailscale-ingress-brief.md §5 "9e"): bfx-deploy derives the
services it deploys from the release's own compose file. From 9d-5 that file has no
frontend service and CI builds no frontend image, so the ledger records NULL for such a
release. A release that still has a frontend keeps recording its digest, so a rollback to
one carries it.

- ``frontend_digest`` drops NOT NULL;
- ``ck_deployments_digests`` accepts a NULL frontend digest; a present one must still be a
  ``sha256:`` digest, and the backend digest stays required.

``check_deployment_attempt`` compares the started and terminal rows with ``IS DISTINCT
FROM``, which treats two NULLs as equal and NULL against a digest as different, so it is
unchanged. No row is rewritten (no data assertion needed, docs/adr/2026-10-08-migrations-
assert-their-data-and-deploy-rehearses-them.md D1): every existing row has a digest and
satisfies the wider CHECK, which PostgreSQL validates when it is added.

The previous bfx-deploy (9e-1) always writes a digest, so it keeps working on this schema;
the deployer that writes NULL is installed only after this release deployed.

Forward-only (docs/adr/2026-10-08-forward-only-migrations.md).

Revision ID: 89886d27750b
Revises: e489261c10b1
"""
from alembic import op

revision = "89886d27750b"
down_revision = "e489261c10b1"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

_DIGEST_RE = "'^sha256:[0-9a-f]{64}$'"


def upgrade() -> None:
    op.alter_column("deployments", "frontend_digest", nullable=True)
    op.drop_constraint("ck_deployments_digests", "deployments", type_="check")
    op.create_check_constraint(
        "ck_deployments_digests", "deployments",
        f"backend_digest ~ {_DIGEST_RE} AND (frontend_digest IS NULL OR frontend_digest ~ {_DIGEST_RE})",
    )


def downgrade() -> None:
    raise NotImplementedError("forward-only: docs/adr/2026-10-08-forward-only-migrations.md")
