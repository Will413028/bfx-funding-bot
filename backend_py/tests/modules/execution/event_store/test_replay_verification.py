"""Captured-row validation must fail before touching a database."""

import importlib
import subprocess
import sys
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_store.serialization import serialize_event
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved

ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440081")


def runtime():
    return importlib.import_module(
        "bfx_funding_bot.modules.execution.event_store.replay_verification"
    )


def test_runtime_import_does_not_load_operator_scripts():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; "
         "from bfx_funding_bot.modules.execution.event_store.replay_verification "
         "import replay_captured_rows; "
         "assert not any(n == 'scripts' or n.startswith('scripts.') for n in sys.modules)"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("fault,match", [
    ("account", "scope"), ("environment", "scope"),
    ("missing_sequence", "missing event sequence"), ("zero", "non-positive"),
    ("negative", "non-positive"), ("duplicate", "sequence ordering"),
    ("reversed", "sequence ordering"), ("identity", "upcaster"),
    ("upcaster", "upcaster"), ("projector", "unsupported projector"),
    ("blank_projector", "missing projector"), ("hash", "event hash mismatch"),
    ("malformed_snapshot", "upcaster"), ("malformed_payload", "upcaster"),
    ("schema", "upcaster"),
])
async def test_invalid_captured_stream_fails_before_database_access(fault, match):
    kernel = runtime()
    # Native snapshots need no historical provenance. Legacy rows are exercised
    # as real persistent ORM rows in the PG tests, never forged as persistent.
    rows = []
    for seq in (1, 2):
        snapshot = VenueSnapshotObserved(
            account_id=str(ACCOUNT), environment="ci", event_id=UUID(int=seq),
            query_started_at_ms=seq * 1000, query_finished_at_ms=seq * 1000 + 1,
            offers=(), credits=(), wallet_available={},
            coverage=SnapshotCoverage(True, True, True),
        )
        rows.append(EventLogRow(
            event_seq=seq, account_id=str(ACCOUNT), exchange_account_id=ACCOUNT,
            deployment_environment="ci", event_type="VENUE_SNAPSHOT_OBSERVED",
            payload=serialize_event(snapshot), event_id=snapshot.event_id,
            schema_version=3, occurred_at_ms=snapshot.query_finished_at_ms,
        ))
    options = {}
    if fault == "account":
        rows[0].exchange_account_id = UUID(int=99)
    elif fault == "environment":
        rows[0].deployment_environment = "other"
    elif fault == "missing_sequence":
        rows[0].event_seq = None
    elif fault in {"zero", "negative"}:
        rows[0].event_seq = 0 if fault == "zero" else -1
    elif fault == "duplicate":
        rows[1].event_seq = 1
    elif fault == "reversed":
        rows.reverse()
    elif fault == "identity":
        rows[0].payload.update(__schema_version__=3, event_id="invalid-uuid")
        rows[0].schema_version = 3
    elif fault == "upcaster":
        rows[0].event_type = "UNSUPPORTED_EVENT"
        rows[0].payload["__event_type__"] = "UNSUPPORTED_EVENT"
    elif fault == "malformed_snapshot":
        rows[0].payload["query_finished_at_ms"] = 0
    elif fault == "malformed_payload":
        rows[0].payload = []
    elif fault == "schema":
        rows[0].payload["__schema_version__"] = 999
        rows[0].schema_version = 999
    elif fault == "projector":
        options["projector_version"] = "unsupported"
    elif fault == "blank_projector":
        options["projector_version"] = " "
    elif fault == "hash":
        options["expected_event_hash"] = "0" * 64
    async with AsyncSession() as session:
        with pytest.raises(kernel.ReplayVerificationError, match=match):
            await kernel.replay_captured_rows(
                session, rows=rows, account_id=ACCOUNT, environment="ci", **options,
            )
        assert not session.in_transaction()
