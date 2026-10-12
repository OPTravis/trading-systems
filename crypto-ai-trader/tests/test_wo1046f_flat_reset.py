"""WO-1046f: C+ gated flat auto-reset tests.

Spec (Travis 2026-10-12 23:13 work order, WO-1046d research accepted):
 - four gates AND, fail-closed on any data failure (None -> HOLD,
   daily checkpoint NOT consumed)
 - gate evaluation anchored at the previous UTC day's daily-close bar
   (last completed bar before today 00:00 UTC), at most once per UTC
   day
 - ARM -> veto window (15min, kv drawdown_breaker_flat_veto=true) ->
   FIRE: trip cleared, HWM re-anchored at current equity, chain
   multiplier preserved, 24h 50% ramp, 72h anti-churn window
 - re-trip within retrip_disable_hours (72h) of a flat FIRE -> flat
   auto-reset disabled for the rest of the cycle (manual-only); a
   fresh cycle (re-trip beyond the chain window) re-arms it
 - flat branch only while tripped AND positions_open == 0; legacy
   callers (positions_open=None) never evaluate it
 - three-layer config defaults <- yaml <- kv; kv {"enabled": false}
   rolls the branch back to pure-A manual-only
Gate math is a VERBATIM port of the frozen WO-1046d research
(incl. the research-frozen G4 sub2 candle semantics — pinned by
test_gate_sub2_fork_semantics; see README deviation note).
Rollback: kv drawdown_breaker_flat_reset {"enabled": false}.
"""

import time as _time

import pytest

H = 3600.0
H_MS = 3600 * 1000


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def db():
    from src.state_db import get_state_db
    d = get_state_db()
    yield d
    for k in (
        "drawdown_breaker_cooldown", "drawdown_breaker_cooldown_state",
        "drawdown_breaker_flat_reset", "drawdown_breaker_flat_ramp",
        "drawdown_breaker_flat_veto",
    ):
        d.kv_set(k, None)


@pytest.fixture(autouse=True)
def no_feishu(monkeypatch):
    calls = []

    class FakeNotifier:
        def __init__(self, *a, **k):
            pass

        def send_text(self, msg):
            calls.append(msg)

    import src.notifier as nm
    monkeypatch.setattr(nm, "FeishuNotifier", FakeNotifier)
    return calls


@pytest.fixture
def clock(monkeypatch):
    """Deterministic wall clock (fake now in seconds)."""
    holder = {"now": 1749951000.0}  # 2025-06-15 00:30:00 UTC
    monkeypatch.setattr(_time, "time", lambda: holder["now"])
    return holder


@pytest.fixture
def breaker(db, tmp_path):
    from src.drawdown_breaker import DrawdownBreaker
    b = DrawdownBreaker.__new__(DrawdownBreaker)
    b.client = None
    b._state = {
        "high_watermark": 0.0,
        "current_drawdown_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "tripped_at": None,
        "tripped_count": 0,
        "reset_at": None,
        "history": [],
    }
    return b


# ------------------------------------------------------------------ helpers

class FakeClient:
    """Serves synthetic 1h bars like ccxt_client.get_klines."""

    def __init__(self, bars):
        self.bars = sorted(bars, key=lambda x: x["open_time"])
        self.calls = []

    def get_klines(self, symbol, interval="1h", limit=500,
                   start_time=None, end_time=None, max_retries=5):
        self.calls.append((start_time, end_time, limit))
        out = []
        for b in self.bars:
            ot = b["open_time"]
            if start_time is not None and ot < start_time:
                continue
            if end_time is not None and ot > end_time:
                continue
            out.append({
                "open_time": ot, "open": b["open"], "high": b["high"],
                "low": b["low"], "close": b["close"], "volume": b["volume"],
                "close_time": ot + 3599999, "quote_volume": 0.0,
                "trades": 0, "is_closed": True,
            })
        return out[:limit]  # ccxt semantics: first `limit` from start


class RaisingClient:
    def get_klines(self, *a, **k):
        raise RuntimeError("api down")


def mkbars(t0_ms, closes, lows=None, vol=10.0):
    """Hourly bars; open = previous close, high = max(o,c)*1.001,
    low = explicit list or min(o,c)*0.995."""
    bars = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        l = lows[i] if lows else min(o, c) * 0.995
        bars.append({
            "open_time": t0_ms + i * H_MS,
            "open": o, "high": max(o, c) * 1.001, "low": l,
            "close": c, "volume": vol,
        })
    return bars


def gen_churn(t0_ms, end_ms):
    """+/-3% alternating closes from t0 to end (high realised vol)."""
    bars = []
    t = t0_ms
    c = 100.0
    while t <= end_ms:
        c *= 1.03 if ((t // H_MS) % 2 == 0) else 0.97
        o = bars[-1]["close"] if bars else c
        bars.append({
            "open_time": t, "open": o, "high": max(o, c) * 1.001,
            "low": min(o, c) * 0.995, "close": c, "volume": 10.0,
        })
        t += H_MS
    return bars


def gen_passing(trip_ms, anchor_ms):
    """Churn through the G3 peak window [trip, trip+48h], then a steady
    +1%/bar climb to the anchor — all four gates PASS (sub2 False under
    research-frozen semantics; sub3 supplied via fng)."""
    return (gen_churn(trip_ms - 6 * H_MS, trip_ms + 48 * H_MS)
            + gen_climb(trip_ms + 49 * H_MS, anchor_ms))


def gen_climb(t0_ms, end_ms):
    bars = []
    t = t0_ms
    c = 100.0
    while t <= end_ms:
        c *= 1.01
        o = bars[-1]["close"] if bars else c
        bars.append({
            "open_time": t, "open": o, "high": max(o, c) * 1.001,
            "low": min(o, c) * 0.995, "close": c, "volume": 10.0,
        })
        t += H_MS
    return bars


def _tripped(b, hwm=450.0, balance=400.0, trip_at=None):
    b._state["high_watermark"] = hwm
    b._state["tripped_at"] = trip_at
    b._state["tripped_count"] = 1
    b._state["max_drawdown_pct"] = round((hwm - balance) / hwm * 100, 2)
    return b


def daily_anchor(now):
    """The daily-close anchor bar open-time (ms) for a wall-clock `now`
    (s): the last completed bar BEFORE today 00:00 UTC."""
    today00 = now - now % 86400
    return (today00 - 3600) * 1000


# ------------------------------------------------------------ config layers

class TestFlatCfg:
    def test_defaults(self, breaker):
        fc = breaker._get_flat_cfg()
        assert fc["enabled"] is True
        assert fc["g1_hours"] == 48.0
        assert fc["g2_ret24_min_pct"] == -2.0
        assert fc["g2_low_buffer"] == 1.02
        assert fc["g3_vol_ratio_max"] == 0.80
        assert fc["g4_need"] == 2
        assert fc["ramp_pct"] == 0.5
        assert fc["ramp_hours"] == 24.0
        assert fc["retrip_disable_hours"] == 72.0
        assert fc["veto_minutes"] == 15

    def test_yaml_layer(self, breaker, monkeypatch):
        import src.risk_config as rc
        monkeypatch.setattr(
            rc, "get_section",
            lambda name: ({"flat_reset": {"g1_hours": 72.0}}
                          if name == "drawdown_breaker" else {}))
        fc = breaker._get_flat_cfg()
        assert fc["g1_hours"] == 72.0  # yaml overrode default
        assert fc["veto_minutes"] == 15  # untouched keys keep defaults

    def test_kv_layer_and_rollout(self, breaker, db):
        db.kv_set("drawdown_breaker_flat_reset", {"enabled": False})
        fc = breaker._get_flat_cfg()
        assert fc["enabled"] is False  # kv one-key rollback to pure-A
        db.kv_set("drawdown_breaker_flat_reset", {"g4_need": 3})
        assert breaker._get_flat_cfg()["g4_need"] == 3  # kv beats yaml

    def test_disabled_branch_returns_none(self, breaker, db, clock):
        db.kv_set("drawdown_breaker_flat_reset", {"enabled": False})
        _tripped(breaker, trip_at=clock["now"] - 100 * H)
        r = breaker.check_drawdown(400.0, positions_open=0)
        assert "flat_reset" not in r  # branch fully inert
        assert r["tripped"] is True


# ------------------------------------------------------------- gate math

class TestGateMath:
    """Direct _eval_flat_gates against crafted bar sets. anchor_ms is
    fixed mid-2025 so every bar is completed vs the real clock."""

    ANCHOR = 1750000000000 // H_MS * H_MS  # hour-aligned
    TRIP = ANCHOR - 96 * H_MS

    def _g(self, breaker, bars, anchor, trip_ms, fng_now=40):
        breaker.client = FakeClient(bars)
        return breaker._eval_flat_gates(
            breaker._get_flat_cfg(), {"fng_at_trip": 30},
            trip_ms / 1000.0 + 300, anchor, fng_now)

    def _scenario(self, tail_closes, tail_lows=None, trip=None):
        """Churn at the trip window (peak-vol source) + crafted 30-bar
        tail ending at the anchor."""
        trip = trip or self.TRIP
        churn = gen_churn(trip - 6 * H_MS, trip + 48 * H_MS)
        tail = mkbars(self.ANCHOR - (len(tail_closes) - 1) * H_MS,
                     tail_closes, lows=tail_lows)
        return churn + tail

    def test_all_pass(self, breaker):
        g = self._g(breaker, gen_passing(self.TRIP, self.ANCHOR),
                    self.ANCHOR, self.TRIP)
        assert g is not None
        assert g["ok"] is True
        assert g["g1"] is True and g["g2"] is True
        assert g["g3"] is True and g["g4"] is True
        assert g["g4_subs"] == [True, False, True]  # sub2 fork semantics
        assert g["fng_at_trip"] == 30

    def test_g1_boundary_47h(self, breaker):
        trip = self.ANCHOR - 47 * H_MS
        g = self._g(breaker, gen_passing(trip, self.ANCHOR),
                    self.ANCHOR, trip)
        assert g["g1"] is False and g["ok"] is False

    def test_g1_boundary_48h(self, breaker):
        trip = self.ANCHOR - 48 * H_MS
        g = self._g(breaker, gen_passing(trip, self.ANCHOR),
                    self.ANCHOR, trip)
        assert g["g1"] is True  # >= is inclusive
        # note: the anchor sits INSIDE the peak window here (v_now ==
        # peak), so g3 False is expected — only G1 is under test

    def test_g2_return_floor(self, breaker):
        # r24 exactly -2.0: fails (strict >); -1.9: passes. lows pinned
        # low so only the return floor toggles.
        for c24, expect in ((100.0 / 0.98, False), (100.0 / 0.981, True)):
            closes = [100.0] * 30
            closes[5] = c24  # 24 bars before the anchor close
            g = self._g(breaker, self._scenario(closes, [96.0] * 30),
                        self.ANCHOR, self.TRIP)
            assert g["g2"] is expect, f"c24={c24}"

    def test_g2_low_buffer(self, breaker):
        # flat closes 100, lows pinned at L: close >= L*1.02 boundary
        for low, expect in ((100.0 / 1.02, True), (100.0 / 1.019, False)):
            g = self._g(breaker, self._scenario([100.0] * 30, [low] * 30),
                        self.ANCHOR, self.TRIP)
            assert g["g2"] is expect, f"low={low}"

    def test_g3_ratio(self, breaker):
        # climb tail -> v_now ~0 -> passes; churn all the way to the
        # anchor -> v_now == peak -> fails
        g = self._g(breaker, gen_passing(self.TRIP, self.ANCHOR),
                    self.ANCHOR, self.TRIP)
        assert g["g3"] is True
        g2 = self._g(breaker, gen_churn(self.TRIP - 6 * H_MS, self.ANCHOR),
                     self.ANCHOR, self.TRIP)
        assert g2["g3"] is False

    def test_g4_combinations(self, breaker):
        # rising tail -> sub1 True; fng 40 vs 30 -> sub3 True; sub2 False
        closes = [100.0 * (1.01 ** i) for i in range(30)]
        bars = gen_churn(self.TRIP - 6 * H_MS, self.TRIP + 48 * H_MS) \
            + mkbars(self.ANCHOR - 29 * H_MS, closes)
        g = self._g(breaker, bars, self.ANCHOR, self.TRIP, fng_now=40)
        assert g["g4_subs"] == [True, False, True] and g["g4"] is True
        # fng collapses below the trip value -> only 1 sub -> g4 False
        g = self._g(breaker, bars, self.ANCHOR, self.TRIP, fng_now=29)
        assert g["g4"] is False
        # fng equal to the trip value still counts (>=)
        g = self._g(breaker, bars, self.ANCHOR, self.TRIP, fng_now=30)
        assert g["g4"] is True
        # fng unknown (None) -> sub3 False
        g = self._g(breaker, bars, self.ANCHOR, self.TRIP, fng_now=None)
        assert g["g4"] is False

    def test_gate_sub2_fork_semantics(self, breaker):
        """PIN the research-frozen G4 sub2 semantics (verbatim port of
        the WO-1046d _gate_eval walk: candle[1] = open of the bucket's
        LAST hour, candle[2] = close of the bucket's FIRST hour):
          - steadily rising closes -> sub2 False
          - steadily falling closes -> sub2 False
          - rising bucket-first closes with an intra-bucket pullback
            -> sub2 True
        (Documented deviation from the prose '4H green candles'; see
        README — kept for bit-consistency with WO-1046d.)"""
        rising = [100.0 * (1.01 ** i) for i in range(30)]
        bars = gen_churn(self.TRIP - 6 * H_MS, self.TRIP + 48 * H_MS) \
            + mkbars(self.ANCHOR - 29 * H_MS, rising)
        g = self._g(breaker, bars, self.ANCHOR, self.TRIP)
        assert g["g4_subs"][1] is False
        falling = [100.0 * (0.99 ** i) for i in range(30)]
        g2 = self._g(breaker, self._scenario(falling), self.ANCHOR,
                     self.TRIP)
        assert g2["g4_subs"][1] is False
        # rising bucket-first closes + current-bucket pullback:
        # anchor hour mod 4 == 3 -> b0 = [a-3h..a], b1 = [a-7h..a-4h];
        # A: c(a-3) > o(a)=c(a-2); C: c(a-3) > c(a-7)
        closes = [100.0 * (1.01 ** i) for i in range(22)]  # i0..i21
        closes += [104.0, 104.5, 105.0, 105.5]              # i22..i25
        closes += [106.0, 104.0, 103.0, 103.5]              # i26..i29
        g3 = self._g(breaker, self._scenario(closes), self.ANCHOR,
                     self.TRIP)
        assert g3["g4_subs"][1] is True

    def test_data_fail_returns_none(self, breaker):
        breaker.client = RaisingClient()
        g = breaker._eval_flat_gates(
            breaker._get_flat_cfg(), {"fng_at_trip": 30},
            self.TRIP / 1000.0 + 300, self.ANCHOR, 40)
        assert g is None

    def test_gap_fail_closed(self, breaker):
        bars = [b for b in gen_passing(self.TRIP, self.ANCHOR)
                if b["open_time"] != self.ANCHOR - 10 * H_MS]
        breaker.client = FakeClient(bars)
        g = breaker._eval_flat_gates(
            breaker._get_flat_cfg(), {"fng_at_trip": 30},
            self.TRIP / 1000.0 + 300, self.ANCHOR, 40)
        assert g is None

    def test_missing_anchor_returns_none(self, breaker):
        bars = gen_passing(self.TRIP, self.ANCHOR - 2 * H_MS)
        breaker.client = FakeClient(bars)
        g = breaker._eval_flat_gates(
            breaker._get_flat_cfg(), {"fng_at_trip": 30},
            self.TRIP / 1000.0 + 300, self.ANCHOR, 40)
        assert g is None

    def test_vol6_matches_reference(self, breaker):
        import math
        closes = [100.0 * (1.01 ** i) for i in range(20)]
        v = breaker._vol6(closes, 10)
        rs = [math.log(closes[j] / closes[j - 1]) for j in range(5, 11)]
        m = sum(rs) / 6
        ref = (sum((x - m) ** 2 for x in rs) / 6) ** 0.5
        assert abs(v - ref) < 1e-12
        assert breaker._vol6(closes, 4) is None  # needs 6 bars


# ------------------------------------------------------- state machine

class TestFlatStateMachine:
    """Full check_drawdown flows on a deterministic clock. `now` is
    00:30 UTC on a fixed day; the anchor is yesterday's 23:00 bar."""

    NOW = 1749951000.0  # 2025-06-15 00:30 UTC (day boundary + 30min)

    def setup_method(self):
        self.now = self.NOW
        self.anchor = daily_anchor(self.now)       # yesterday 23:00 bar
        self.trip = self.anchor - 96 * H_MS
        self.trip_at = self.trip / 1000.0 + 300   # 23:05 on trip day

    def _ready(self, breaker, db, clock, bars=None):
        clock["now"] = self.now
        breaker.client = FakeClient(
            bars if bars is not None else gen_passing(self.trip, self.anchor))
        _tripped(breaker, hwm=450.0, balance=400.0, trip_at=self.trip_at)
        # real trips record fng_at_trip in the trip block; injected
        # states must seed it the same way
        db.kv_set("drawdown_breaker_cooldown_state",
                  {"fng_at_trip": 30, "events": []})

    def test_arms_at_daily_close(self, breaker, db, clock, monkeypatch):
        self._ready(breaker, db, clock)
        monkeypatch.setattr(breaker, "_fetch_fng_by_day", lambda day: 40)
        r = breaker.check_drawdown(400.0, positions_open=0)
        assert r["tripped"] is True  # ARM is not FIRE
        assert r["flat_reset"]["state"] == "armed"
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst["flat_reset_armed_at"] == clock["now"]
        assert cst["flat_anchor_open_ms"] == self.anchor
        assert cst["flat_gate_snapshot"]["ok"] is True
        assert cst["flat_last_gate_eval_day"] is not None

    def test_armed_before_veto_window(self, breaker, db, clock, monkeypatch):
        self._ready(breaker, db, clock)
        monkeypatch.setattr(breaker, "_fetch_fng_by_day", lambda day: 40)
        breaker.check_drawdown(400.0, positions_open=0)
        clock["now"] += 5 * 60
        r = breaker.check_drawdown(400.0, positions_open=0)
        assert r["flat_reset"]["state"] == "armed"
        assert r["tripped"] is True

    def test_veto_disables_cycle(self, breaker, db, clock, monkeypatch):
        self._ready(breaker, db, clock)
        monkeypatch.setattr(breaker, "_fetch_fng_by_day", lambda day: 40)
        breaker.check_drawdown(400.0, positions_open=0)  # ARM
        db.kv_set("drawdown_breaker_flat_veto", True)
        clock["now"] += 16 * 60
        r = breaker.check_drawdown(400.0, positions_open=0)
        assert r["tripped"] is True
        assert r["flat_reset"]["state"] == "vetoed_disabled"
        assert db.kv_get("drawdown_breaker_flat_veto") is None  # consumed
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst["flat_auto_disabled"] is True
        assert cst["flat_reset_armed_at"] is None
        clock["now"] += 3600
        r = breaker.check_drawdown(400.0, positions_open=0)
        assert r["flat_reset"]["state"] == "disabled_this_cycle"

    def test_fire_after_veto_window(self, breaker, db, clock, monkeypatch):
        self._ready(breaker, db, clock)
        monkeypatch.setattr(breaker, "_fetch_fng_by_day", lambda day: 40)
        breaker.check_drawdown(400.0, positions_open=0)  # ARM
        clock["now"] += 16 * 60
        r = breaker.check_drawdown(400.0, positions_open=0)  # FIRE
        assert r["action"] == "RESET" and r["tripped"] is False
        assert r["high_watermark"] == 400.0  # re-anchored at equity
        st = breaker._state
        assert st["tripped_at"] is None
        assert st["high_watermark"] == 400.0
        assert st["tripped_count"] == 1  # forensic counter preserved
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst["flat_last_reset_at"] == clock["now"]
        assert cst["flat_reset_armed_at"] is None
        assert cst["last_auto_reset_at"] == clock["now"]
        assert cst["last_chain_reset_at"] == clock["now"]
        ramp = db.kv_get("drawdown_breaker_flat_ramp")
        assert ramp["pct"] == 0.5
        assert ramp["until_ts"] == clock["now"] + 24 * 3600

    def test_fire_preserves_chain_mult(self, breaker, db, clock, monkeypatch):
        self._ready(breaker, db, clock)
        monkeypatch.setattr(breaker, "_fetch_fng_by_day", lambda day: 40)
        db.kv_set("drawdown_breaker_cooldown_state",
                  {"cooldown_multiplier": 4, "fng_at_trip": 30,
                   "events": []})
        breaker.check_drawdown(400.0, positions_open=0)  # ARM
        clock["now"] += 16 * 60
        r = breaker.check_drawdown(400.0, positions_open=0)  # FIRE
        assert r["tripped"] is False
        cst2 = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst2["cooldown_multiplier"] == 4  # anti-churn preserved
        assert any(e["type"] == "flat_auto_reset" for e in cst2["events"])

    def test_fire_gate_recheck_lapse(self, breaker, db, clock, monkeypatch):
        self._ready(breaker, db, clock)
        monkeypatch.setattr(breaker, "_fetch_fng_by_day", lambda day: 40)
        breaker.check_drawdown(400.0, positions_open=0)  # ARM
        breaker.client = RaisingClient()  # data dies mid-veto-window
        clock["now"] += 16 * 60
        r = breaker.check_drawdown(400.0, positions_open=0)
        assert r["tripped"] is True
        assert r["flat_reset"]["state"] == "armed_lapsed"
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst["flat_reset_armed_at"] is None  # disarmed

    def test_daily_checkpoint_once_per_day(self, breaker, db, clock, monkeypatch):
        # steady FALLING tail -> g2 fails -> daily HOLD
        falling = [100.0 * (0.995 ** i) for i in range(30)]
        bars = gen_churn(self.trip - 6 * H_MS, self.trip + 48 * H_MS) \
            + mkbars(self.anchor - 29 * H_MS, falling)
        self._ready(breaker, db, clock, bars=bars)
        monkeypatch.setattr(breaker, "_fetch_fng_by_day", lambda day: 40)
        r1 = breaker.check_drawdown(400.0, positions_open=0)
        assert r1["flat_reset"]["state"] == "hold"
        calls_after_1 = len(breaker.client.calls)
        r2 = breaker.check_drawdown(400.0, positions_open=0)
        assert r2["flat_reset"]["state"] == "await_next_close"
        assert len(breaker.client.calls) == calls_after_1  # no re-fetch

    def test_data_fail_does_not_consume_checkpoint(
            self, breaker, db, clock, monkeypatch):
        self._ready(breaker, db, clock)
        breaker.client = RaisingClient()
        monkeypatch.setattr(breaker, "_fetch_fng_by_day", lambda day: 40)
        r = breaker.check_drawdown(400.0, positions_open=0)
        assert r["flat_reset"]["state"] == "data_fail_hold"
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst.get("flat_last_gate_eval_day") is None  # not consumed
        # data recovers the SAME day -> full gate evaluation happens
        breaker.client = FakeClient(gen_passing(self.trip, self.anchor))
        r = breaker.check_drawdown(400.0, positions_open=0)
        assert r["flat_reset"]["state"] == "armed"

    def test_positions_open_blocks_branch(self, breaker, db, clock):
        self._ready(breaker, db, clock)
        r = breaker.check_drawdown(400.0, positions_open=2)
        assert "flat_reset" not in r
        r = breaker.check_drawdown(400.0)  # legacy call signature
        assert "flat_reset" not in r

    def test_armed_stale_expiry(self, breaker, db, clock, monkeypatch):
        self._ready(breaker, db, clock)
        monkeypatch.setattr(breaker, "_fetch_fng_by_day", lambda day: 40)
        db.kv_set("drawdown_breaker_cooldown_state", {
            "flat_reset_armed_at": self.now - 25 * 3600,
            "flat_anchor_open_ms": self.anchor, "events": []})
        r = breaker.check_drawdown(400.0, positions_open=0)
        assert r["flat_reset"]["state"] == "armed_expired"
        cst2 = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst2["flat_reset_armed_at"] is None


# ------------------------------------------------- re-trip & disable rules

class TestRetripDisable:
    def _fire(self, breaker, db, clock, monkeypatch, now):
        clock["now"] = now
        anchor = daily_anchor(now)
        trip_ms = anchor - 96 * H_MS
        breaker.client = FakeClient(gen_passing(trip_ms, anchor))
        _tripped(breaker, hwm=450.0, balance=400.0,
                 trip_at=trip_ms / 1000.0 + 300)
        db.kv_set("drawdown_breaker_cooldown_state",
                  {"fng_at_trip": 30, "events": []})
        monkeypatch.setattr(breaker, "_fetch_fng_by_day", lambda day: 40)
        breaker.check_drawdown(400.0, positions_open=0)  # ARM
        clock["now"] += 16 * 60
        r = breaker.check_drawdown(400.0, positions_open=0)  # FIRE
        assert r["action"] == "RESET"
        return clock["now"]

    def test_retrip_within_72h_disables(self, breaker, db, clock, monkeypatch):
        fire_at = self._fire(breaker, db, clock, monkeypatch,
                             1749951000.0)
        clock["now"] = fire_at + 10 * 3600  # re-trip 10h after FIRE
        r = breaker.check_drawdown(340.0, positions_open=0)  # -15% of HWM
        assert r["tripped"] is True
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst["flat_auto_disabled"] is True
        assert cst["cooldown_multiplier"] == 2  # anti-churn chain fired
        r = breaker.check_drawdown(340.0, positions_open=0)
        assert r["flat_reset"]["state"] == "disabled_this_cycle"

    def test_retrip_fresh_cycle_reenables(self, breaker, db, clock, monkeypatch):
        fire_at = self._fire(breaker, db, clock, monkeypatch,
                             1749951000.0)
        clock["now"] = fire_at + 80 * 3600  # beyond the 72h window
        r = breaker.check_drawdown(340.0, positions_open=0)
        assert r["tripped"] is True
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst["flat_auto_disabled"] is False  # fresh cycle re-armed
        # next daily checkpoint: flat evaluation active again
        clock["now"] += 86400
        anchor = daily_anchor(clock["now"])
        trip_ms = anchor - 96 * H_MS
        breaker.client = FakeClient(gen_passing(trip_ms, anchor))
        r = breaker.check_drawdown(340.0, positions_open=0)
        assert r["flat_reset"]["state"] in ("armed", "hold")

    def test_manual_reset_clears_flat(self, breaker, db, clock, monkeypatch):
        fire_at = self._fire(breaker, db, clock, monkeypatch,
                             1749951000.0)
        assert db.kv_get("drawdown_breaker_flat_ramp") is not None
        clock["now"] = fire_at + 10 * 3600  # re-trip then human resets
        breaker.check_drawdown(340.0, positions_open=0)
        breaker.reset(340.0, actor="leo", reason="human triage")
        assert breaker._state["tripped_at"] is None  # reset() mutates state
        assert breaker._state["high_watermark"] == 340.0
        cst = db.kv_get("drawdown_breaker_cooldown_state")
        assert cst["flat_auto_disabled"] is False
        assert cst["flat_reset_armed_at"] is None
        assert cst["flat_last_reset_at"] is None
        assert db.kv_get("drawdown_breaker_flat_ramp") is None  # ramp off


# ------------------------------------------- flat / tiered mutual exclusion

class TestFlatTieredExclusion:
    """While flat+tripped, dd sits frozen at >= hard stop, so the tiered
    dd-narrowing branch never fires (needs dd < 8) — the flat branch
    owns the outcome; legacy callers keep pure tiered semantics."""

    def test_flat_locked_tiered_never_fires(self, breaker, db, clock, monkeypatch):
        now = 1749951000.0
        clock["now"] = now
        anchor = daily_anchor(now)
        trip_ms = anchor - 200 * H_MS
        breaker.client = FakeClient(gen_passing(trip_ms, anchor))
        _tripped(breaker, hwm=450.0, balance=400.0,
                 trip_at=trip_ms / 1000.0 + 300)
        db.kv_set("drawdown_breaker_cooldown_state",
                  {"fng_at_trip": 30, "events": []})
        monkeypatch.setattr(breaker, "_fetch_fng_by_day", lambda day: 40)
        # 200h since trip (past tier1 48h / tier2 72h) but dd = 11.1%
        r = breaker.check_drawdown(400.0, positions_open=0)
        assert r["tripped"] is True  # tiered reset did NOT fire
        assert "flat_reset" in r

    def test_nonflat_tiered_path_untouched(self, breaker, db):
        # same clock age, no positions arg -> pure tiered semantics
        _tripped(breaker, hwm=100.0, trip_at=_time.time() - 49 * 3600)
        r = breaker.check_drawdown(95.0)  # dd 5% -> tier1 reset
        assert r["action"] == "RESET" and r["tripped"] is False
        assert "flat_reset" not in r


# -------------------------------------------------- risk_manager wiring

class FakeBinanceClient:
    def __init__(self, usdt, holdings):
        self.usdt = usdt
        self.holdings = holdings

    def get_account(self):
        bals = [{"asset": "USDT", "free": self.usdt, "locked": 0}]
        for a, q, _ in self.holdings:
            bals.append({"asset": a, "free": q, "locked": 0})
        return {"balances": bals}

    def get_24hr_stats(self):
        return [{"symbol": f"{a}USDT", "last_price": p}
                for a, _, p in self.holdings]


class SpyBreaker:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def check_drawdown(self, balance, positions_open=None):
        self.calls.append((balance, positions_open))
        return self.result


class TestRiskManagerWiring:
    def _rm(self, holdings, result=None):
        from src.risk_manager import RiskManager
        rm = RiskManager.__new__(RiskManager)
        rm.client = FakeBinanceClient(100.0, holdings)
        rm.correlation_risk = None  # section 3 attr is unguarded
        rm.drawdown_breaker = SpyBreaker(result or {
            "tripped": False, "drawdown_pct": 0.0})
        return rm

    def test_positions_open_flat(self):
        rm = self._rm([])  # USDT only, no holdings
        rm.pre_trade_check("BTCUSDT", 50000.0, 1.0, strategy="dca")
        (bal, po), = rm.drawdown_breaker.calls
        assert bal == 100.0
        assert po == 0

    def test_positions_open_counts_dollar_positions(self):
        rm = self._rm([("BTC", 0.001, 50000.0),   # $50 -> counts
                       ("ETH", 0.0002, 3000.0)])  # $0.60 -> dust
        rm.pre_trade_check("BTCUSDT", 50000.0, 1.0, strategy="dca")
        (bal, po), = rm.drawdown_breaker.calls
        assert bal == pytest.approx(100.0 + 0.001 * 50000.0 + 0.0002 * 3000.0)
        assert po == 1  # dust does not block flat detection

    def test_ramp_sizing_applied(self, db, clock):
        db.kv_set("drawdown_breaker_flat_ramp", {
            "until_ts": clock["now"] + 7200, "pct": 0.5,
            "ramp_hours": 24.0})
        rm = self._rm([])
        r = rm.pre_trade_check("BTCUSDT", 50000.0, 1.0, strategy="dca")
        # trend-filter attr missing -> fail-open 0.5; ramp x0.5 -> 0.25
        assert r["adjustments"]["size_multiplier"] == 0.25
        assert any("FLAT RESET RAMP" in x for x in r["reasons"])


class TestRampHelper:
    def test_active(self, db, clock):
        from src.risk_manager import flat_ramp_active
        db.kv_set("drawdown_breaker_flat_ramp", {
            "until_ts": clock["now"] + 100, "pct": 0.5,
            "ramp_hours": 24.0})
        assert flat_ramp_active() == {"pct": 0.5, "ramp_hours": 24.0}

    def test_expired(self, db, clock):
        from src.risk_manager import flat_ramp_active
        db.kv_set("drawdown_breaker_flat_ramp", {
            "until_ts": clock["now"] - 1, "pct": 0.5, "ramp_hours": 24.0})
        assert flat_ramp_active() is None

    def test_missing_or_corrupt(self, db, clock):
        from src.risk_manager import flat_ramp_active
        assert flat_ramp_active() is None
        db.kv_set("drawdown_breaker_flat_ramp", "garbage")
        assert flat_ramp_active() is None
