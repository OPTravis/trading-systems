"""WO-1057 ②: P3 anti-chase paper rule (record-only, never gates).

Backtest context (worklogs/20261014_BT_P3P4_*.md §2.2, Travis 拍板
2026-10-14): the dominant NEUTRAL failure shape is chasing an alt that
already ran vs BTC — block line diff >= +8pp, PAPER ONLY until clean
NEUTRAL sample >=50. The rule must:
  1. measure alt vs BTC 24h change on the last CLOSED 1h bar with a
     common time anchor (same discipline as the backtest);
  2. exclude the forming bar (close_time based);
  3. record every NEUTRAL fresh entry's metric (chase_block flag marks
     the >=+8pp subset) into paper_signals for the future §2.2 recompute;
  4. never raise into the trading path (fail-open).
"""
import logging
import time

import pytest

from src import entry_governor as gov
from src.trade_executor import _neutral_paper_metrics

HOUR_MS = 3600 * 1000
NOW_MS = 1_791_950_000_000  # fixed anchor — tests are time-independent


class _KlineClient:
    """Fake client serving synthetic 1h klines (dict rows, SDK format).

    `series` maps symbol → (closes, last_open_ms): hourly bars generated
    backward from last_open_ms with the given closes.
    """

    def __init__(self, series, exc=None):
        self._series = series
        self._exc = exc

    def get_klines(self, symbol, interval="1h", limit=500, **kw):
        if self._exc is not None:
            raise self._exc
        closes, last_open = self._series[symbol]
        bars = []
        for i, c in enumerate(closes):
            open_ms = last_open - (len(closes) - 1 - i) * HOUR_MS
            bars.append({
                "open_time": open_ms,
                "open": c, "high": c, "low": c, "close": c,
                "volume": 1.0,
                "close_time": open_ms + HOUR_MS - 1,
                "quote_volume": c, "trades": 1, "is_closed": True,
            })
        return bars


def _flat(n=30, price=100.0):
    return [price] * n


# ────────────────────────────────────────────────────────────────────
# _neutral_paper_metrics（executor 侧取数：锚点口径与回测一致）
# ────────────────────────────────────────────────────────────────────

class TestNeutralPaperMetrics:
    def test_diff_and_anchor(self):
        """25% alt vs 1% BTC on the common last-closed bar → diff 24pp。"""
        alt = _flat()
        btc = _flat()
        alt[4], alt[28] = 100.0, 125.0    # bar4 = anchor-24h, bar28 = anchor
        btc[4], btc[28] = 100.0, 101.0
        # last bar (index 29) is forming: open 10min before NOW → excluded
        client = _KlineClient({"ALTUSDT": (alt, NOW_MS - 10 * 60 * 1000),
                                "BTCUSDT": (btc, NOW_MS - 10 * 60 * 1000)})
        m = _neutral_paper_metrics(client, "ALTUSDT", now=NOW_MS / 1000)
        assert m is not None
        alt_24h, btc_24h, anchor = m
        assert alt_24h == pytest.approx(25.0)
        assert btc_24h == pytest.approx(1.0)
        # anchor = bar28 open = NOW - 10min - 1h（最后已收 bar）
        assert anchor == NOW_MS - 10 * 60 * 1000 - HOUR_MS

    def test_forming_bar_excluded(self):
        """成形中的当前 bar 不参与：+25% 只出现在最后一根（未收）→ diff≈0。"""
        alt = _flat()
        btc = _flat()
        alt[4], alt[28], alt[29] = 100.0, 100.0, 125.0  # 125 在成形 bar 上
        btc[4], btc[28] = 100.0, 101.0
        client = _KlineClient({"ALTUSDT": (alt, NOW_MS - 10 * 60 * 1000),
                                "BTCUSDT": (btc, NOW_MS - 10 * 60 * 1000)})
        m = _neutral_paper_metrics(client, "ALTUSDT", now=NOW_MS / 1000)
        assert m is not None
        assert m[0] == pytest.approx(0.0)   # 形成 bar 被剔除
        assert m[1] == pytest.approx(1.0)

    def test_misaligned_series_use_common_anchor(self):
        """两条序列最后已收 bar 不同小时 → anchor=min，双侧同锚度量。"""
        alt = _flat()
        btc = _flat()
        # alt 序列整体早 2h：其最后（已收）bar open = NOW-130min
        alt_last_open = NOW_MS - 130 * 60 * 1000
        btc_last_open = NOW_MS - 10 * 60 * 1000
        # anchor = alt 的最后已收 bar open（更旧者）
        anchor = alt_last_open
        # 在 anchor 与 anchor-24h 上布置价格
        alt[29], alt[5] = 110.0, 100.0      # 最后 bar=anchor；anchor-24h=bar5
        btc[27], btc[3] = 104.0, 100.0      # btc 在同 anchor 的 bar 与 24h 前
        client = _KlineClient({"ALTUSDT": (alt, alt_last_open),
                                "BTCUSDT": (btc, btc_last_open)})
        m = _neutral_paper_metrics(client, "ALTUSDT", now=NOW_MS / 1000)
        assert m is not None
        assert m[0] == pytest.approx(10.0)
        assert m[1] == pytest.approx(4.0)
        assert m[2] == anchor

    def test_fetch_failure_returns_none(self):
        client = _KlineClient({}, exc=RuntimeError("network down"))
        assert _neutral_paper_metrics(client, "X", now=NOW_MS / 1000) is None

    def test_insufficient_history_returns_none(self):
        """不足 25 根已收 bar（缺 anchor-24h 的 bar）→ None。"""
        client = _KlineClient({
            "ALTUSDT": (_flat(n=5), NOW_MS - 10 * 60 * 1000),
            "BTCUSDT": (_flat(n=30), NOW_MS - 10 * 60 * 1000),
        })
        assert _neutral_paper_metrics(client, "ALTUSDT",
                                      now=NOW_MS / 1000) is None


# ────────────────────────────────────────────────────────────────────
# note_neutral_paper（governor 侧记录：NEUTRAL 才记、fail-open、可统计）
# ────────────────────────────────────────────────────────────────────

def _paper_rows(symbol):
    from src.state_db import get_state_db
    db = get_state_db()
    return db._get_conn().execute(
        "SELECT * FROM paper_signals WHERE symbol = ? ORDER BY id",
        (symbol,)).fetchall()


class TestNoteNeutralPaper:
    def test_chase_candidate_recorded_and_logged(self, caplog):
        """diff >= +8pp → paper_signals 落行 chase_block=1 + PAPER_CANDIDATE 日志。"""
        with caplog.at_level(logging.INFO, logger="src.entry_governor"):
            gov.note_neutral_paper("PAPER1", 25.0, 1.0,
                                   anchor_open_ms=12345,
                                   market_regime="NEUTRAL",
                                   source="trade_executor")
        rows = _paper_rows("PAPER1")
        assert len(rows) == 1
        r = rows[0]
        assert r["diff_pp"] == pytest.approx(24.0)
        assert r["chase_block"] == 1
        assert r["alt_24h_pct"] == pytest.approx(25.0)
        assert r["btc_24h_pct"] == pytest.approx(1.0)
        assert r["anchor_open_ms"] == 12345
        assert r["rule"] == "chase_block_neutral"
        assert r["source"] == "trade_executor"
        assert any("PAPER_CANDIDATE" in rec.getMessage()
                   and "PAPER1" in rec.getMessage() for rec in caplog.records)

    def test_below_block_line_recorded_without_candidate_log(self, caplog):
        """diff < +8pp → 仍落行（供 >=50 样本后全分布重算）但 chase_block=0。"""
        with caplog.at_level(logging.INFO, logger="src.entry_governor"):
            gov.note_neutral_paper("PAPER2", 3.0, 1.0,
                                   market_regime="NEUTRAL")
        rows = _paper_rows("PAPER2")
        assert len(rows) == 1
        assert rows[0]["chase_block"] == 0
        assert rows[0]["diff_pp"] == pytest.approx(2.0)
        assert not any("PAPER_CANDIDATE" in rec.getMessage()
                       for rec in caplog.records)

    def test_non_neutral_regime_not_recorded(self):
        """非 NEUTRAL 体制不记录（P3 规则仅定义在 NEUTRAL fresh entry 上）。"""
        gov.note_neutral_paper("PAPER3", 25.0, 1.0, market_regime="GREED")
        gov.note_neutral_paper("PAPER4", 25.0, 1.0, market_regime="FEAR")
        assert _paper_rows("PAPER3") == []
        assert _paper_rows("PAPER4") == []

    def test_kv_snapshot_regime_path(self):
        """无 executor 提示时走 kv 快照口径（与 WO-1053 同源）。"""
        from src.state_db import get_state_db
        db = get_state_db()
        db.kv_set("gov:market_regime",
                  {"regime": "NEUTRAL", "fng": 57, "ts": time.time()})
        gov.note_neutral_paper("PAPER5", 20.0, 1.0)
        assert len(_paper_rows("PAPER5")) == 1

    def test_fail_open_never_raises(self, monkeypatch):
        """DB 故障时吞异常（paper 绝不打断交易路径）。"""
        def _boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(gov, "_db", _boom)
        gov.note_neutral_paper("PAPER6", 25.0, 1.0,
                               market_regime="NEUTRAL")  # 不应抛出

    def test_boundary_exactly_8pp_is_chase(self):
        """边界：diff == +8.0pp 恰好命中 block 线（>= 语义）。"""
        gov.note_neutral_paper("PAPER7", 9.0, 1.0, market_regime="NEUTRAL")
        rows = _paper_rows("PAPER7")
        assert rows[0]["diff_pp"] == pytest.approx(8.0)
        assert rows[0]["chase_block"] == 1


class TestPaperSignalsStore:
    def test_insert_and_read_back(self):
        """state_db.insert_paper_signal 直写直读全字段。"""
        from src.state_db import get_state_db
        db = get_state_db()
        db.insert_paper_signal(123.0, "SYM", alt_24h_pct=10.0,
                               btc_24h_pct=2.0, diff_pp=8.0,
                               chase_block=1, anchor_open_ms=999,
                               source="unit")
        rows = db._get_conn().execute(
            "SELECT * FROM paper_signals WHERE symbol = ?", ("SYM",)
        ).fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r["ts"] == 123.0
        assert r["rule"] == "chase_block_neutral"
        assert r["chase_block"] == 1
        assert r["anchor_open_ms"] == 999
        assert r["created_at"] is not None
