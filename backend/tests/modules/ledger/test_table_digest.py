"""Pin the per-table ledger digest (S1-4b): canonical bytes, order, watermark, column sets.

Mutations this file kills (each applied alone to ``modules/ledger/table_digest.py``):

* drop a column from a ``CANONICAL_COLUMNS`` entry: ``test_canonical_columns_cover_the_orm``
  and the golden hex of that table fail.
* add a generated column to a canonical list: ``test_canonical_columns_cover_the_orm`` fails.
* ``format(value.normalize(), "f")`` for Numeric: ``test_decimal_trailing_zeros_are_significant``
  and the golden hexes of tables with Numeric cells fail.
* sort rows by insertion order instead of ``_order_key``: ``test_order_is_independent_of_input``.
* drop ``COLLATE "C"`` from text order keys: ``test_select_is_explicit_ordered_and_text_keys_use_byte_collation``
  (the test PostgreSQL is C-collated, so only the SQL shape can be pinned).
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

import pytest

from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger import table_digest as td
from bfx_funding_bot.modules.ledger.tables import LEDGER_TABLES, CapitalAuthorityEpochRow

ALL_TABLES = (*LEDGER_TABLES, CapitalAuthorityEpochRow.__table__)


def _sample(table: str, variant: int = 0) -> dict[str, object]:
    """A deterministic row: every cell non-null, values derived from table/column position."""
    index = td.DIGEST_TABLES.index(table)
    row: dict[str, object] = {}
    for position, (column, kind) in enumerate(
        zip(td.CANONICAL_COLUMNS[table], td._KINDS[table], strict=True)
    ):
        seed = 1 + position + 100 * variant
        if kind == "uuid":
            row[column] = UUID(int=(index << 16) + seed)
        elif kind == "text":
            row[column] = f"{column}-{variant}"
        elif kind == "int":
            row[column] = seed
        elif kind == "bool":
            row[column] = variant % 2 == 0
        elif kind == "decimal":
            row[column] = Decimal(seed) + Decimal("0.50")
        else:
            row[column] = {"z": seed, "a": "值"}
    return row


GOLDEN: dict[str, str] = {
    "capital_command_clock": (
        "988471536cdfb5992e20be07c66b3e71ed50874a36297e380396efcdbaa16615"
    ),
    "ledger_observation_query": (
        "2c3fb0ed4c77cc77016ffe63602c91941f7bb26f58c2591f517055b481c9bd92"
    ),
    "ledger_observation": (
        "3bb767d9c3b25d4caaee8f014f11d714bbf940421554ed85cbe7d4e0b5268d63"
    ),
    "ledger_observation_wallet": (
        "01d913e24ea83ad2054772a6c92d4c9eb4a4af27a274f322df6ddef475163f9b"
    ),
    "ledger_observation_offer": (
        "c5cbd1a3c6550cb71e23c49a2390c1ca06480e34b3422d0a95807549c35fc740"
    ),
    "ledger_observation_credit": (
        "8c0ccbf2da91abf6def39411d12668bf3dd7937eba9c53557776190e85cf2e7a"
    ),
    "ledger_observation_offer_history": (
        "95e6b751189ca149af0ac2907a2fdc8786256767f9f6e36013f3c2ccb546164d"
    ),
    "ledger_observation_credit_history": (
        "e339a18c3f77f14499f820242ab9cfe72bc0183d3144f15d8f30fb657edf38c9"
    ),
    "ledger_observation_trade": (
        "6d114deefabd06f3eeed4331dc66d813e203e339d5c7217e50faaae80011feb9"
    ),
    "venue_offer_mirror": (
        "e07aacf1e6ff8798faf1a0e39eafc72adb61c8106767408bb2f760c69d11089c"
    ),
    "venue_credit_mirror": (
        "e0d4afa9b444c6cf2a487bfecb15a37722803cb09e150633d3ac6f16ce26e506"
    ),
    "submission_attempt_journal": (
        "bfc1eff765642da681f6a3eaf80d65e9dbdc10a631c6dc41c9b2dfc9ccd8d4ab"
    ),
    "transport_outcome_journal": (
        "f178043600b5799c7827c935cda2f77633d1c3ed1b1521396a38f83727647f7b"
    ),
    "quarantine_opening": (
        "ce406b6f5cda0a0f197e6efa9e0b22640b43e86d95e773536053278d9efe668a"
    ),
    "quarantine_member": (
        "e9d4d9fe91327c41dd39af5e8d40b283691bfd8c1e9affb96410b5c69015be35"
    ),
    "execution_resolution_journal": (
        "9164fec4760ba737ccd128b6a1c6dbe099987a3b7b96cb2e40254e8b5b6f77bc"
    ),
    "accepted_capital_basis": (
        "0ec343a19d847bbdfcc57792c66d5bac18c04315da2f2e95998db23f32f4013b"
    ),
    "accepted_capital_basis_symbol": (
        "2353b0ed7e77145e5d24de63a7c5428912fc240031a370cc151a8fb40f4b48e0"
    ),
    "accepted_capital_basis_cell": (
        "a131a8f1d7e025349ed12f4e13913faf0c26d1d7b9ef99b67d71756028e08088"
    ),
    "accepted_capital_basis_credit": (
        "7b74b6ad046afc920ab2b80b658d159420510e6aaa5db0a001c7c290ca8d96aa"
    ),
    "accepted_capital_basis_credit_cell": (
        "838107abbbd2b1b3c944d699f471d0a6ed5c22397913dfd9eab3997b48b52487"
    ),
    "accepted_capital_basis_attempt": (
        "a80ddf7812cf6187166fd04b97e6ad3b8944f765c829c58346d376c570cf4e64"
    ),
    "accepted_capital_basis_quarantine": (
        "58af16b994b8c6abc28725a9ab9f02b138dd1ee74d3eef263e56924bd637bb78"
    ),
    "capital_authority_epoch": (
        "06b26d95086b4cf56459a1016ffb7c3e4a9248bd3ad8e573d67ecf983d63b754"
    ),
}


def test_tables_cover_the_ledger_and_the_epoch() -> None:
    assert tuple(table.name for table in ALL_TABLES) == td.DIGEST_TABLES
    assert len(td.DIGEST_TABLES) == 24
    assert set(td.CANONICAL_COLUMNS) == set(td.ORDER_BY) == set(td.WATERMARK_COLUMN)
    assert set(td.CANONICAL_COLUMNS) == set(td.DIGEST_TABLES)
    assert set(td._SCOPE_PARENT) == set(td.DIGEST_TABLES) - {"capital_authority_epoch"}
    assert {"capital_command_clock", "venue_offer_mirror", "venue_credit_mirror"} == td.MUTABLE_TABLES


@pytest.mark.parametrize("table", ALL_TABLES, ids=lambda table: table.name)
def test_canonical_columns_cover_the_orm(table) -> None:
    """A new ORM column fails here until it gets a canonical (or generated) decision."""
    generated = {column.name for column in table.columns if column.computed is not None}
    assert set(td.GENERATED_COLUMNS.get(table.name, ())) == generated
    canonical = td.CANONICAL_COLUMNS[table.name]
    assert len(set(canonical)) == len(canonical)
    assert set(canonical) == {column.name for column in table.columns} - generated
    assert not set(canonical) & generated


@pytest.mark.parametrize("table", ALL_TABLES, ids=lambda table: table.name)
def test_order_is_total_and_watermark_is_an_integer_column(table) -> None:
    name = table.name
    order = td.ORDER_BY[name]
    assert set(order) <= set(td.CANONICAL_COLUMNS[name])
    assert {column.name for column in table.primary_key.columns} <= set(order)
    kinds = dict(zip(td.CANONICAL_COLUMNS[name], td._KINDS[name], strict=True))
    assert all(kinds[column] in ("uuid", "text", "int") for column in order)
    watermark = td.WATERMARK_COLUMN[name]
    assert watermark is None or kinds[watermark] == "int"
    parent = td._SCOPE_PARENT.get(name)
    if parent is not None:
        foreign_key, parent_name, parent_column = parent
        assert foreign_key in table.c
        parent_table = td._TABLES[parent_name]
        assert {"exchange_account_id", "deployment_environment", parent_column} <= set(
            parent_table.c.keys()
        )
    elif name in td._SCOPE_PARENT:
        assert {"exchange_account_id", "deployment_environment"} <= set(table.c.keys())


@pytest.mark.parametrize("table", td.DIGEST_TABLES)
def test_golden_digest_per_table(table: str) -> None:
    rows = [_sample(table, 0), _sample(table, 1)]
    digest = td.digest_rows(table, rows)
    assert digest.count == 2
    assert digest.sha256 == GOLDEN[table]


def test_empty_table_digest_is_the_header_hash() -> None:
    digest = td.digest_rows("transport_outcome_journal", [])
    header = (
        "bfx-ledger-table-digest/v1\ntransport_outcome_journal\n"
        "attempt_id,kind,venue_offer_id,reason,completed_at_ms,evidence\n"
    )
    assert digest.count == 0
    assert digest.sha256 == sha256(header.encode()).hexdigest()
    assert digest.watermark is None


def test_row_bytes_are_pinned_independently() -> None:
    row = {
        "observation_id": UUID("00000000-0000-0000-0000-00000000a002"),
        "wallet_type": "funding",
        "currency": "UST",
        "available": Decimal("10.0"),
        "balance": Decimal("1E+2"),
        "symbol": None,
    }
    expected = (
        b'["00000000-0000-0000-0000-00000000a002","funding","UST","10.0","100",null]'
    )
    assert td.canonical_row_bytes("ledger_observation_wallet", row) == expected
    header = (
        "bfx-ledger-table-digest/v1\nledger_observation_wallet\n"
        "observation_id,wallet_type,currency,available,balance,symbol\n"
    )
    manual = sha256(header.encode() + expected + b"\n").hexdigest()
    assert td.digest_rows("ledger_observation_wallet", [row]).sha256 == manual


def test_decimal_trailing_zeros_are_significant() -> None:
    """F1 ruling: ``format(v, "f")`` without normalize; the wire/stored digits are the identity."""
    def wallet(available: Decimal) -> dict[str, object]:
        return {**_sample("ledger_observation_wallet"), "available": available}

    one_ten = td.digest_rows("ledger_observation_wallet", [wallet(Decimal("1.10"))])
    one_one = td.digest_rows("ledger_observation_wallet", [wallet(Decimal("1.1"))])
    assert one_ten.sha256 != one_one.sha256
    # Exponent notation is the same number with the same scale: same bytes.
    assert (
        td.digest_rows("ledger_observation_wallet", [wallet(Decimal("1E+2"))]).sha256
        == td.digest_rows("ledger_observation_wallet", [wallet(Decimal("100"))]).sha256
    )
    assert td._cell("t", "c", "decimal", Decimal("1.10")) == b'"1.10"'
    assert td._cell("t", "c", "decimal", Decimal("1E+2")) == b'"100"'


def test_negative_zero_encodes_as_the_zero_postgres_stores() -> None:
    assert td._cell("t", "c", "decimal", Decimal("-0.00")) == b'"0.00"'


def test_rows_sharing_a_primary_key_are_refused() -> None:
    row = _sample("ledger_observation_wallet")
    with pytest.raises(td.TableDigestRowInvalid, match="duplicate primary key"):
        td.digest_rows("ledger_observation_wallet", [row, dict(row)])
    assert td._cell("t", "c", "decimal", Decimal("-0.50")) == b'"-0.50"'


def test_json_cells_use_the_ledger_canonical_payload() -> None:
    assert td._cell("t", "c", "json", {"b": [1, "值"], "a": None}) == '{"a":null,"b":[1,"值"]}'.encode()
    assert td._cell("t", "c", "json", None) == b"null"
    assert td._cell("t", "c", "json", 3) == b"3"
    assert td._cell("t", "c", "json", {"a": 1, "b": 2}) == td._cell("t", "c", "json", {"b": 2, "a": 1})
    with pytest.raises(td.TableDigestRowInvalid):
        td._cell("t", "c", "json", {"a": Decimal("1")})
    with pytest.raises(td.TableDigestRowInvalid):
        td._cell("t", "c", "json", {"a": float("nan")})


def test_scalar_cells_are_strict() -> None:
    ident = UUID("00000000-0000-0000-0000-00000000a001")
    assert td._cell("t", "c", "uuid", ident) == b'"00000000-0000-0000-0000-00000000a001"'
    assert td._cell("t", "c", "uuid", str(ident).upper()) == td._cell("t", "c", "uuid", ident)
    assert td._cell("t", "c", "text", "值\n") == '"值\\n"'.encode()
    assert td._cell("t", "c", "int", -5) == b"-5"
    assert td._cell("t", "c", "bool", False) == b"false"
    for kind, value in (
        ("int", True),
        ("int", "1"),
        ("bool", 1),
        ("decimal", 1),
        ("decimal", 1.5),
        ("decimal", Decimal("NaN")),
        ("text", b"x"),
        ("uuid", "not-a-uuid"),
        ("uuid", 5),
    ):
        with pytest.raises(td.TableDigestRowInvalid):
            td._cell("t", "c", kind, value)  # type: ignore[arg-type]


def test_row_must_have_exactly_the_canonical_columns() -> None:
    row = _sample("quarantine_member")
    td.canonical_row_bytes("quarantine_member", row)
    with pytest.raises(td.TableDigestRowInvalid):
        td.canonical_row_bytes("quarantine_member", {k: v for k, v in row.items() if k != "amount_at_join"})
    with pytest.raises(td.TableDigestRowInvalid):
        td.canonical_row_bytes("quarantine_member", {**row, "extra": 1})
    attempt = _sample("submission_attempt_journal")
    with pytest.raises(td.TableDigestRowInvalid):
        td.canonical_row_bytes("submission_attempt_journal", {**attempt, "match_rate": Decimal("1")})


def test_order_is_independent_of_input() -> None:
    same_scope = {
        key: _sample("venue_offer_mirror")[key] for key in ("exchange_account_id", "deployment_environment")
    }
    rows = [
        {**_sample("venue_offer_mirror", variant), **same_scope, "venue_offer_id": offer_id}
        for variant, offer_id in enumerate(("a", "B", "é", "b"))
    ]
    digests = {
        td.digest_rows("venue_offer_mirror", list(order)).sha256
        for order in itertools.permutations(rows)
    }
    assert len(digests) == 1
    # Byte order, not locale: "B" < "a" < "b" < "é".
    ordered = sorted(rows, key=lambda row: td._order_key("venue_offer_mirror", row))
    assert [row["venue_offer_id"] for row in ordered] == ["B", "a", "b", "é"]
    assert td.digest_rows("venue_offer_mirror", rows[:2]).sha256 not in digests


def test_order_keys_follow_the_documented_keys() -> None:
    first = {**_sample("submission_attempt_journal"), "attempt_seq": 2}
    second = {**_sample("submission_attempt_journal", 1), "attempt_seq": 10}
    # attempt_seq sorts numerically (2 < 10), within the same scope.
    second["exchange_account_id"] = first["exchange_account_id"]
    second["deployment_environment"] = first["deployment_environment"]
    assert td._order_key("submission_attempt_journal", first) < td._order_key(
        "submission_attempt_journal", second
    )
    low = UUID(int=1)
    high = UUID(int=2**127)
    wallet = {**_sample("ledger_observation_wallet"), "observation_id": low}
    assert td._order_key("ledger_observation_wallet", wallet) < td._order_key(
        "ledger_observation_wallet", {**wallet, "observation_id": high}
    )


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("capital_command_clock", "revision"),
        ("ledger_observation_query", "query_revision"),
        ("submission_attempt_journal", "attempt_seq"),
        ("quarantine_opening", "opened_revision"),
        ("capital_authority_epoch", "epoch_seq"),
    ],
)
def test_watermark_is_the_column_maximum(table: str, column: str) -> None:
    rows = [{**_sample(table, variant), column: value} for variant, value in enumerate((7, 30, 4))]
    # Distinct keys so the rows stay distinct whatever the order columns are.
    assert td.digest_rows(table, rows).watermark == 30
    assert td.digest_rows(table, []).watermark is None


def test_tables_without_a_watermark_report_none() -> None:
    assert td.digest_rows("ledger_observation_wallet", [_sample("ledger_observation_wallet")]).watermark is None
    assert {name for name, column in td.WATERMARK_COLUMN.items() if column} == {
        "capital_command_clock",
        "ledger_observation_query",
        "submission_attempt_journal",
        "quarantine_opening",
        "capital_authority_epoch",
    }


def test_result_marks_mutable_tables_and_scope() -> None:
    from bfx_funding_bot.modules.ledger import Scope

    scope = Scope(UUID(int=1), "ci")
    mirror = td.digest_rows("venue_credit_mirror", [], scope=scope)
    assert mirror.mutable and mirror.scope == scope
    assert not td.digest_rows("submission_attempt_journal", []).mutable
    # The epoch is global: a scope label is dropped.
    assert td.digest_rows("capital_authority_epoch", [], scope=scope).scope is None


def test_select_is_explicit_ordered_and_text_keys_use_byte_collation() -> None:
    """The test database is C-collated, production is en_US: pin the SQL, not the data."""
    from sqlalchemy.dialects import postgresql

    def sql(name: str, scope=None) -> str:
        statement, _ = td._statement(name, scope)
        return str(statement.compile(dialect=postgresql.dialect()))

    wallet = sql("ledger_observation_wallet")
    assert "SELECT *" not in wallet and "ctid" not in wallet
    assert "ORDER BY ledger_observation_wallet.observation_id, " in wallet
    assert 'ledger_observation_wallet.wallet_type COLLATE "C"' in wallet
    assert 'ledger_observation_wallet.currency COLLATE "C"' in wallet
    attempts = sql("submission_attempt_journal")
    assert 'deployment_environment COLLATE "C"' in attempts
    assert "match_rate" not in attempts and "intended_amount" not in attempts
    scoped = sql("transport_outcome_journal", Scope(UUID(int=1), "ci"))
    assert "JOIN submission_attempt_journal" in scoped
