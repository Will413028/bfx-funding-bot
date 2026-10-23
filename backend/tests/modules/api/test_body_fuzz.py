"""Fuzz what the webapi's write endpoints parse: an arbitrary JSON body is either a valid
model or a pydantic ValidationError (FastAPI's 422), and an arbitrary /fe-events body is
either forwarded or refused, never an unhandled exception (a 500)."""
from __future__ import annotations

import contextlib
import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from bfx_funding_bot.modules.api.ingress import sentry_envelope_target
from bfx_funding_bot.modules.api.schemas import StrategyConfigBody
from bfx_funding_bot.modules.api.trading_control import ControlBody

_SCALARS = st.one_of(
    st.none(), st.booleans(), st.integers(), st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=40),
)
_JSON = st.recursive(
    _SCALARS,
    lambda inner: st.lists(inner, max_size=5) | st.dictionaries(st.text(max_size=12), inner, max_size=6),
    max_leaves=30,
)
# Bodies close to valid reach the field and model validators, not only the type check.
_RANGE = st.fixed_dictionaries({"min": _SCALARS, "max": _SCALARS})
_CONFIG_LIKE = st.fixed_dictionaries(
    {"currency": _SCALARS, "amount": st.one_of(_RANGE, _JSON), "rate": st.one_of(_RANGE, _JSON),
     "period": st.one_of(_RANGE, _JSON), "autoRenew": _SCALARS},
    optional={"auto_renew": _SCALARS, "extra": _JSON},
)
_CONTROL_LIKE = st.fixed_dictionaries({"reason": _SCALARS}, optional={"extra": _JSON})

BODIES: dict[str, tuple[type[BaseModel], st.SearchStrategy[Any]]] = {
    "control": (ControlBody, st.one_of(_JSON, _CONTROL_LIKE)),
    "strategy_config": (StrategyConfigBody, st.one_of(_JSON, _CONFIG_LIKE)),
}


@pytest.mark.parametrize("name", BODIES)
@given(data=st.data())
def test_write_bodies_raise_only_validation_errors(name: str, data: st.DataObject) -> None:
    model, bodies = BODIES[name]
    body = data.draw(bodies)
    with contextlib.suppress(ValidationError):
        model.model_validate(body)


_DSN = "https://publickey@o1.ingest.us.sentry.io/42"


@given(header=_JSON, rest=st.binary(max_size=64))
def test_fe_events_envelope_header_never_raises(header: Any, rest: bytes) -> None:
    sentry_envelope_target(json.dumps(header).encode() + b"\n" + rest, _DSN)


@given(envelope=st.binary(max_size=200), dsn=st.one_of(st.just(_DSN), st.text(max_size=60)))
def test_fe_events_raw_bytes_never_raise(envelope: bytes, dsn: str) -> None:
    sentry_envelope_target(envelope, dsn)
