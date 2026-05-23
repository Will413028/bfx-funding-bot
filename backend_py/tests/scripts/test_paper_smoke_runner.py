"""D6 — paper_smoke_runner exit code logic.

Smoke runner has 3 outcomes; this tests the counter/decision logic
in isolation. Real subprocess run is post-deploy chaos (Gate 1).
"""


class TestSmokeRunnerDecision:
    def test_exit_0_when_count_ge_n(self):
        from bfx_funding_bot.scripts.paper_smoke_runner import (
            _decide_exit_code,
        )
        rc = _decide_exit_code(count=11, n_required=11, elapsed_s=300)
        assert rc == 0

    def test_exit_1_when_timeout_and_zero(self):
        from bfx_funding_bot.scripts.paper_smoke_runner import (
            _decide_exit_code,
        )
        rc = _decide_exit_code(count=0, n_required=11, elapsed_s=5500)
        assert rc == 1

    def test_exit_2_when_timeout_and_partial(self):
        from bfx_funding_bot.scripts.paper_smoke_runner import (
            _decide_exit_code,
        )
        rc = _decide_exit_code(count=5, n_required=11, elapsed_s=5500)
        assert rc == 2

    def test_continue_polling_under_timeout(self):
        """Returning -1 (sentinel) means "keep polling"."""
        from bfx_funding_bot.scripts.paper_smoke_runner import (
            _decide_exit_code,
        )
        rc = _decide_exit_code(count=5, n_required=11, elapsed_s=600)
        assert rc == -1
