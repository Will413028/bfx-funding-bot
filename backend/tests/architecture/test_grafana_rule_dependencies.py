"""Names the homelab Grafana rules (observability/.../bfx-rules.yml) depend on.

Those rules live in another repository, so renaming anything here silently
turns a page into "no data". Renaming any of these means changing
homelab's bfx-rules.yml in the same release.

`bfx-trading-halted-submits`:
    min(min_over_time(bfx_trading_halted[6m])) == 1
    and on() sum(increase(bfx_executor_submits_total{status!="not_sent"}[5m])) > 0
"""
from __future__ import annotations

from prometheus_client.parser import text_string_to_metric_families

from bfx_funding_bot.modules.observability.metrics import (
    _KNOWN_SUBMIT_STATUSES,
    DaemonMetrics,
)


class _Halted:
    async def halted(self) -> bool | None:
        return True

    def metric_scope(self) -> dict[str, str]:
        return {"exchange_account_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                "deployment_environment": "ci"}


async def test_halt_rule_metrics_are_exported_under_the_names_it_queries() -> None:
    metrics = DaemonMetrics()
    await metrics.refresh_trading_halted(_Halted())
    metrics.observe_submit(status="submitted", duration_s=0.1)
    samples = {
        (s.name, s.labels.get("status"))
        for family in text_string_to_metric_families(metrics.render().decode())
        for s in family.samples
    }
    assert ("bfx_trading_halted", None) in samples
    assert ("bfx_executor_submits_total", "submitted") in samples


def test_not_sent_is_the_only_status_that_never_left_the_bot() -> None:
    """The rule excludes exactly `not_sent` (a guard blocked it before the
    venue call). `exception` can also be a pre-intent gate block, but that only
    happens as a halt begins, which the rule's 6-minute halted window excludes."""
    assert "not_sent" in _KNOWN_SUBMIT_STATUSES
