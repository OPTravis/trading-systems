"""WO-1009-③ (10/2): exit dust pre-flight — never cancel resting legs for
a position that cannot clear minNotional in the first place.

Evidence (10/2 03:40-08:41 PENGU, audit ids 1125-1260 + cron-scan log):
every exit event BYPASSed the scan gate, execute_exit cancelled the
resting OCO legs FIRST, then the dust guard aborted the market sell
(428 × $0.0094 ≈ $4.03 < minNotional $5) — leaving the position NAKED
with the message "no legs cancelled harmfully" (they had been). The
protection_guardian rebuilt the OCO on the same/next sweep, the next
exit event tore it down again: the NAKED→TP_ONLY→OCO loop ran ~15
rounds. Meanwhile cmd_trailing_check's bug#32 guard skipped the symbol
because trade_outcomes shows open_cnt=0 for the synced-only slice, so
nothing broke the cycle.

Fix under test: a minNotional pre-flight BEFORE the cancel, judged on
the decision qty × decision price (free balance is ~0 while a full-qty
OCO locks the base — it is not a valid dust yardstick pre-cancel).
Dust → skip with a stable (timestamp-free) outbox id so a permanently
dust-locked exit pages once, not once per loop iteration.
"""

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.state_db import get_state_db  # noqa: E402
from src.exit_check import execute_exit  # noqa: E402
from tests.test_wo0931_exit_check import ExitFakeClient  # noqa: E402

NOW = 1790900000.0


@pytest.fixture
def db():
    d = get_state_db()
    d.kv_set("ledger:shadow:bootstrap_ts", time.time() - 86400)
    yield d


OCO_LEGS = [
    {"symbol": "PENGUUSDT", "side": "SELL", "status": "NEW",
     "orderId": 111, "type": "LIMIT_MAKER"},
    {"symbol": "PENGUUSDT", "side": "SELL", "status": "NEW",
     "orderId": 112, "type": "STOP_LOSS_LIMIT"},
]


def _dust_decision():
    # 428 × 0.0094 ≈ $4.03 < minNotional 5.0 (the live PENGU shape)
    return {"symbol": "PENGUUSDT", "kind": "stop_loss", "qty": 428.0,
            "entry_price": 0.010237, "price": 0.0094, "pnl_pct": -8.2,
            "held_hours": 30.0, "sell_pct": 100, "reason": "sl crossed",
            "ts": NOW}


def _tradable_decision():
    return {"symbol": "PENGUUSDT", "kind": "stop_loss", "qty": 10.0,
            "entry_price": 100.0, "price": 100.0, "pnl_pct": -7.5,
            "held_hours": 30.0, "sell_pct": 100, "reason": "sl crossed",
            "ts": NOW}


class TestExitDustPreflight:

    def test_dust_position_never_cancels_legs(self, db):
        """The loop fix: resting OCO must survive a dust-locked exit."""
        cli = ExitFakeClient(open_orders=list(OCO_LEGS),
                             balances={"PENGU": 428.386})
        out = execute_exit(cli, db, _dust_decision(), now=NOW)
        assert out["status"] == "aborted"
        assert "dust pre-flight" in out["detail"]
        # NOTHING was touched on the exchange — no cancel, no sell,
        # no emergency SL: the OCO floor is the only protection a dust
        # position can have
        assert cli.cancel_calls == []
        assert cli.sell_calls == []
        assert cli.emergency_sl_calls == []

    def test_dust_skip_notifies_with_stable_id(self, db):
        """Same notif_id every round → outbox dedupes the loop spam."""
        cli = ExitFakeClient(open_orders=list(OCO_LEGS),
                             balances={"PENGU": 428.386})
        execute_exit(cli, db, _dust_decision(), now=NOW)
        conn = db._get_conn()
        rows = conn.execute(
            "SELECT notif_id FROM notification_outbox "
            "WHERE notif_id = 'exit:PENGUUSDT:stop_loss:dust_preflight'"
        ).fetchall()
        assert len(rows) == 1
        # second round (cooldown window aside) must not add a new row
        execute_exit(cli, db, _dust_decision(), now=NOW + 1200)
        rows = conn.execute(
            "SELECT COUNT(*) FROM notification_outbox "
            "WHERE notif_id = 'exit:PENGUUSDT:stop_loss:dust_preflight'"
        ).fetchone()
        assert rows[0] == 1

    def test_tradable_exit_keeps_cancel_then_sell_chain(self, db):
        """Non-dust exits keep the exact legacy sequence."""
        cli = ExitFakeClient(open_orders=list(OCO_LEGS),
                             balances={"PENGU": 10.0})
        out = execute_exit(cli, db, _tradable_decision(), now=NOW)
        assert out["status"] == "ok"
        assert cli.cancel_calls == [111, 112]
        assert cli.sell_calls == [("PENGUUSDT", 10.0)]

    def test_preflight_filter_error_falls_back_to_legacy(self, db):
        """Filters unavailable → old order (cancel first), not a skip."""

        class ErrClient(ExitFakeClient):
            # only the pre-flight fetch blows up; the post-cancel path
            # (step-2 filters for step flooring) works normally, so the
            # run proves the pre-flight fallback, not a cascade
            def __init__(self, **kw):
                super().__init__(**kw)
                self._first_filters = True

            def get_symbol_filters(self, symbol):
                if self._first_filters:
                    self._first_filters = False
                    raise RuntimeError("filters endpoint down")
                return {"stepSize": 0.001, "minNotional": 5.0}

        cli = ErrClient(open_orders=list(OCO_LEGS), balances={"PENGU": 10.0})
        out = execute_exit(cli, db, _tradable_decision(), now=NOW)
        assert out["status"] == "ok"
        assert cli.cancel_calls == [111, 112]
        assert cli.sell_calls == [("PENGUUSDT", 10.0)]
