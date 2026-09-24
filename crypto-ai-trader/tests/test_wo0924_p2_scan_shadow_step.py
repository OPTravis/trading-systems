"""WO-0924 P2 followup (9/24 22:36): shadow_diff must run on every scan exit path.

Production evidence: gate-skip short rounds (1s "no opportunities") hit the
early-return in cmd_cron_scan and silently skipped _step_ledger_shadow_diff.
Stats sat at round 3 (22:03) while scan rounds kept ticking at 22:20 — the
P2 observation cadence starved. These tests pin the fix: both short-round
returns and the full pipeline path run the shadow step exactly once.
"""

from unittest import mock

import pytest

import src.scan_orchestrator as so


@pytest.fixture()
def _isolated_scan_lock(monkeypatch, tmp_path):
    """Point the flock at a per-test file so test runs never collide on
    /tmp/crypto-trader-scan.lock."""
    monkeypatch.setenv("SCAN_LOCK_FILE", str(tmp_path / "scan.lock"))
    yield


class TestShadowStepOnEveryExitPath:

    def test_no_op_round_still_runs_shadow_diff(self, _isolated_scan_lock):
        """Short round (no opportunities) must not starve the observation clock."""
        with mock.patch.object(so, "_step_scan_opportunities",
                               return_value=None), \
             mock.patch.object(so, "_append_scan_summary") as summary, \
             mock.patch.object(so, "_step_ledger_shadow_diff") as shadow:
            so.cmd_cron_scan()
        summary.assert_called_once_with(None)
        shadow.assert_called_once_with(None)

    def test_research_short_round_still_runs_shadow_diff(self,
                                                         _isolated_scan_lock):
        """ctx alive entering research but research bailed: the shadow step
        is still owed. Note ctx is reassigned by research, so the summary
        and shadow step see the (None) post-research value — original
        semantics preserved."""
        ctx = {"opportunities": ["X"]}
        with mock.patch.object(so, "_step_scan_opportunities",
                               return_value=ctx), \
             mock.patch.object(so, "_step_research_top_n", return_value=None), \
             mock.patch.object(so, "_append_scan_summary") as summary, \
             mock.patch.object(so, "_step_ledger_shadow_diff") as shadow:
            so.cmd_cron_scan()
        summary.assert_called_once_with(None)
        shadow.assert_called_once_with(None)

    def test_full_round_runs_shadow_diff_exactly_once(self, _isolated_scan_lock):
        """Full pipeline path: exactly one shadow step per round."""
        with mock.patch.object(so, "_step_scan_opportunities",
                               return_value={"ok": 1}), \
             mock.patch.object(so, "_step_research_top_n",
                               side_effect=lambda c: c), \
             mock.patch.object(so, "_step_event_driven_adjustment"), \
             mock.patch.object(so, "_step_kv_preflight", return_value=True), \
             mock.patch.object(so, "_step_execute_trades"), \
             mock.patch.object(so, "_step_reconcile_portfolio"), \
             mock.patch.object(so, "_step_defense_sweep"), \
             mock.patch.object(so, "_step_ledger_shadow_diff") as shadow, \
             mock.patch.object(so, "_step_evolve_strategies"), \
             mock.patch.object(so, "_append_scan_summary"):
            so.cmd_cron_scan()
        assert shadow.call_count == 1

    def test_shadow_step_internal_crash_never_blocks_short_round(
            self, _isolated_scan_lock):
        """Fail-open contract with the REAL step: a ledger.shadow_diff crash
        is swallowed inside _step_ledger_shadow_diff and the short round
        returns normally."""
        import src.ledger as ledger_mod
        with mock.patch.object(so, "_step_scan_opportunities",
                               return_value=None), \
             mock.patch.object(so, "_append_scan_summary"), \
             mock.patch.object(ledger_mod, "get_mode", return_value="shadow"), \
             mock.patch.object(ledger_mod, "shadow_diff",
                               side_effect=RuntimeError("boom")):
            so.cmd_cron_scan()  # must not raise


class TestShadowRoundCli:
    """`python -m src.ledger shadow-round` — the gate-skip cadence keeper."""

    def test_shadow_round_runs_one_diff_round(self):
        import io
        import contextlib
        import sys
        from src.state_db import StateDB
        from src import ledger

        db = StateDB()
        ledger.set_mode(db, "shadow")
        ledger.bootstrap_shadow(db)
        before = ledger.get_stats(db).get("rounds", 0)
        with mock.patch.object(sys, "argv", ["ledger", "shadow-round"]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            ledger._cli()
        line = out.getvalue().strip()
        assert "ledger shadow round" in line
        after = ledger.get_stats(db)
        assert after["rounds"] == before + 1
        # one history row per round (P2-④ round ledger)
        rows = db._get_conn().execute(
            "SELECT COUNT(*) FROM ledger_shadow_rounds").fetchone()[0]
        assert rows >= 1

    def test_shadow_round_off_mode_prints_skip(self):
        import io
        import contextlib
        import sys
        from src.state_db import StateDB
        from src import ledger

        db = StateDB()
        ledger.set_mode(db, "off")
        before = ledger.get_stats(db).get("rounds", 0)
        with mock.patch.object(sys, "argv", ["ledger", "shadow-round"]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            ledger._cli()
        assert "mode=off" in out.getvalue()
        assert ledger.get_stats(db).get("rounds", 0) == before
