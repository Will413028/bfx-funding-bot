"""Validate canned Bitfinex funding API responses match hand-curated schema.

Acts as drift detector for mock responses used by FillTracker tests.
"""
import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = Path(__file__).parent / "bitfinex_funding_api_schema.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


# Canned response samples (representative shapes from Bitfinex docs).
# Update when schema is updated; CI catches mismatch.
OFFERS_SAMPLE = [
    [
        41215275, "fUSD", 1573289848000, 1573289848000,
        1000.0, 1000.0, "LIMIT",
        None, None, None, "ACTIVE",
        None, None, None,
        0.0001, 2, 0, 0, None, 0, 12345,
    ],
]

CREDITS_SAMPLE = [
    [
        26222883, "fUSD", "LEND", 1574077528000, 1574077528000,
        500.0, None, "ACTIVE", 0,
        None, None,
        0.0002, 7, 1574077528000, 1574077528000,
        0, 0, None, 0, 0.0002, 0, "BTCUSD",
    ],
]


def test_offers_sample_matches_schema(schema: dict) -> None:
    validator = jsonschema.Draft202012Validator(
        {"$ref": "#/definitions/FundingOffersResponse", **schema}
    )
    errors = list(validator.iter_errors(OFFERS_SAMPLE))
    assert not errors, f"schema drift: {errors}"


def test_credits_sample_matches_schema(schema: dict) -> None:
    validator = jsonschema.Draft202012Validator(
        {"$ref": "#/definitions/FundingCreditsResponse", **schema}
    )
    errors = list(validator.iter_errors(CREDITS_SAMPLE))
    assert not errors, f"schema drift: {errors}"


def test_malformed_offer_rejected(schema: dict) -> None:
    bad = [["not_an_int"]]
    validator = jsonschema.Draft202012Validator(
        {"$ref": "#/definitions/FundingOffersResponse", **schema}
    )
    errors = list(validator.iter_errors(bad))
    assert errors
