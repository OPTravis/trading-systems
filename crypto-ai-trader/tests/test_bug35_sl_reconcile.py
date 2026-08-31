"""bug#35 (2026-08-31): trailing-check 逐仓 SL 记录自动对账校正。

背景：8/31 发现 DB portfolio 中 ENSO stop_loss 记 $0.83695，交易所实际挂单
stop $0.826（偏差 1.32%）。偏差未被及时发觉，SL 触发价与记录脱节。

修复：cmd_trailing_check 每轮新增 reconcile_sl_prices：
  - 逐仓对比 DB portfolio.stop_loss vs 交易所 open orders 中 STOP 单价格
    （取最紧/最高腿，bug#17 同款口径），交易所为准。
  - 偏差 > 0.5%（或 DB 无记录但交易所有单）→ 以交易所值更新 DB，
    日志/输出 action=sl_price_corrected。
  - bug#32 ledger-freshness-guard 模式：对账任何异常 fail-open（告警日志、
    不阻断 trailing-check 主流程）。
  - 默认 DRY-RUN（SL_RECONCILE_DRYRUN 未设或 != '0'）：只记录不写库；
    显式 SL_RECONCILE_DRYRUN=0 才真实写库（主人切换）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.cmd_trailing_check import _reconcile_sl_prices


class FakeDB:
    def __init__(self, holdings):
        self.holdings = holdings          # {symbol: {..., 'stop_loss': x}}
        self.updates = []                 # (symbol, stop_loss)

    def portfolio_get_all(self):
        return self.holdings

    def _get_conn(self):
        return _Conn(self)


class _Conn:
    def __init__(self, outer):
        self._o = outer
    def execute(self, sql, params=()):
        if sql.strip().upper().startswith("UPDATE PORTFOLIO"):
            # params = (stop_loss, updated_at, symbol) -> record (symbol, sl)
            self._o.updates.append((params[2], params[0]))
        return self
    def commit(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class FakeClient:
    def __init__(self, orders):
        self._orders = orders             # {symbol: [order,...]}
        self._fail = set()                # symbols that raise on fetch
    def get_open_orders(self, symbol):
        if symbol in self._fail:
            raise ConnectionError("proxied network down")
        return self._orders.get(symbol, [])


def _stop(price, qty=1.0):
    return {"type": "STOP_LOSS_LIMIT", "stopPrice": str(price),
            "amount": str(qty), "id": "o1"}


def _mk_db(enso_sl=0.83695):
    return FakeDB({"ENSOUSDT": {"symbol": "ENSOUSDT", "quantity": 100.0,
                                "entry_price": 0.85, "stop_loss": enso_sl}})


# 1) ENSO 案例复现：偏差 1.32% > 0.5% → 记录校正（默认 dry-run，不写库）
def test_enso_divergence_detected_dryrun(monkeypatch):
    monkeypatch.delenv("SL_RECONCILE_DRYRUN", raising=False)
    db = _mk_db()
    client = FakeClient({"ENSOUSDT": [_stop(0.826)]})
    out = _reconcile_sl_prices(client, [{"asset": "ENSO", "symbol": "ENSOUSDT"}], db=db)
    assert len(out) == 1
    r = out[0]
    assert r["action"] == "sl_price_corrected"
    assert r["symbol"] == "ENSOUSDT"
    assert r["db_sl"] == pytest.approx(0.83695)
    assert r["exchange_sl"] == pytest.approx(0.826)
    assert r["deviation_pct"] > 0.5
    assert r["mode"] == "dry-run"
    assert db.updates == []  # dry-run writes nothing


# 2) 显式 SL_RECONCILE_DRYRUN=0 → 真实更新 DB
def test_apply_mode_writes_db(monkeypatch):
    monkeypatch.setenv("SL_RECONCILE_DRYRUN", "0")
    db = _mk_db()
    client = FakeClient({"ENSOUSDT": [_stop(0.826)]})
    out = _reconcile_sl_prices(client, [{"asset": "ENSO", "symbol": "ENSOUSDT"}], db=db)
    assert out[0]["mode"] == "applied"
    assert db.updates == [("ENSOUSDT", 0.826)]


# 3) 无偏差（<0.5%）直通
def test_no_deviation_passes_through(monkeypatch):
    monkeypatch.setenv("SL_RECONCILE_DRYRUN", "0")
    db = _mk_db(enso_sl=0.8263)  # 0.036% off
    client = FakeClient({"ENSOUSDT": [_stop(0.826)]})
    out = _reconcile_sl_prices(client, [{"asset": "ENSO", "symbol": "ENSOUSDT"}], db=db)
    assert out == [] and db.updates == []


# 4) 交易所无 SL 单 → 跳过
def test_no_exchange_sl_leg_skipped(monkeypatch):
    db = _mk_db()
    client = FakeClient({"ENSOUSDT": []})
    out = _reconcile_sl_prices(client, [{"asset": "ENSO", "symbol": "ENSOUSDT"}], db=db)
    assert out == []


# 5) DB 无 SL 记录但交易所有单 → 全偏差，采纳交易所值
def test_db_missing_sl_adopts_exchange(monkeypatch):
    monkeypatch.setenv("SL_RECONCILE_DRYRUN", "0")
    db = FakeDB({"ENSOUSDT": {"symbol": "ENSOUSDT", "quantity": 100.0,
                              "entry_price": 0.85, "stop_loss": None}})
    client = FakeClient({"ENSOUSDT": [_stop(0.826)]})
    out = _reconcile_sl_prices(client, [{"asset": "ENSO", "symbol": "ENSOUSDT"}], db=db)
    assert len(out) == 1 and out[0]["mode"] == "applied"
    assert db.updates == [("ENSOUSDT", 0.826)]


# 6) 单仓对账异常 fail-open，不影响后续仓
def test_fail_open_on_client_error(monkeypatch):
    monkeypatch.setenv("SL_RECONCILE_DRYRUN", "0")
    db = _mk_db()
    db.holdings["SOLUSDT"] = {"symbol": "SOLUSDT", "quantity": 1.0,
                              "entry_price": 100.0, "stop_loss": 101.0}
    client = FakeClient({"SOLUSDT": [_stop(101.2)]})
    client._fail = {"ENSOUSDT": True}
    out = _reconcile_sl_prices(
        client,
        [{"asset": "ENSO", "symbol": "ENSOUSDT"},
         {"asset": "SOL", "symbol": "SOLUSDT"}],
        db=db,
    )
    # ENSO errored (skipped), SOL fine (0.2% < 0.5% → no record either)
    assert out == []
    # and nothing raised — fail-open held


# 7) 多腿 SL 取最紧（最高）价 — bug#17 口径
def test_multiple_sl_legs_tightest_wins(monkeypatch):
    monkeypatch.setenv("SL_RECONCILE_DRYRUN", "0")
    db = _mk_db(enso_sl=0.80)
    client = FakeClient({"ENSOUSDT": [_stop(0.810), _stop(0.826)]})
    out = _reconcile_sl_prices(client, [{"asset": "ENSO", "symbol": "ENSOUSDT"}], db=db)
    assert out[0]["exchange_sl"] == pytest.approx(0.826)
