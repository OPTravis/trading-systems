"""bug#34 (2026-08-31): 上报前一致性校验器（pre-report validator）。

背景：8/30 21:14 轮误报「交易所挂单 0」——根因：执行 session 直连被墙 +
ccxt get_open_orders 网络失败 3 次后 return []，失败与真 0 不可区分；
8/31 凌晨多轮重复通报历史已平仓事件。

修复：
  1) BinanceClient: ccxt requests session 显式代理 127.0.0.1:17890 +
     trust_env=True（BINANCE_PROXY 可覆盖/off 关闭）。
  2) scripts/report_validator.py：push_notifications 输出前逐条断言：
     ① 声称持仓 ⇔ DB portfolio 实查；② 声称挂单数 ⇔ 交易所实查（请求失败
     ⇒ 阻断而非放行；空列表须请求成功才算真 0）；③ 声称余额 ⇔ DB cash 且
     交易所差 < $0.01；④ 持仓变化主事件与已通报历史去重。
     失败 ⇒ 阻断该条（不输出、标记 blocked）+ 写诊断工单 jsonl。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import pytest

from scripts.report_validator import ReportValidator, ExchangeFacts


class FakeDB:
    """portfolio rows + kv cash_balance."""
    def __init__(self, positions=None, cash=399.26):
        self.positions = positions or []
        self.cash = cash

    def _get_conn(self):
        return _Conn(self.positions, self.cash)


class _Conn:
    def __init__(self, positions, cash):
        self._p, self._c = positions, cash

    def execute(self, sql, params=()):
        u = sql.strip().upper()
        if u.startswith("SELECT SYMBOL"):
            return _Res([{"symbol": p["symbol"]} for p in self._p])
        if "FROM KV" in u and "CASH_BALANCE" in u:
            return _Res([{"value": str(self._c)}])
        return _Res([])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Res:
    def __init__(self, rows):
        self._rows = rows
    def fetchall(self):
        return self._rows
    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeFacts:
    """ok / orders / balance controlled per-test."""
    def __init__(self, ok=True, orders=None, balance=399.26):
        self.fact = {"ok": ok, "open_orders": orders, "usdt_balance": balance}
        self.collect_calls = 0
    def collect(self):
        self.collect_calls += 1
        return dict(self.fact)


def _mk(db=None, facts=None):
    return ReportValidator(db=db or FakeDB(), facts=facts or FakeFacts(orders=[]),
                           now_ts=1756630000.0)


def _notif(body, ntype="scan_summary", nid="n1"):
    return {"id": nid, "timestamp": "2026-08-31T10:00:00", "type": ntype,
            "title": "", "body": body, "pushed": False}


# ① 持仓断言
def test_claimed_position_not_in_db_blocked():
    v = _mk(db=FakeDB(positions=[]))
    verdict, reasons, _ = v.validate(_notif("📈 BTCUSDT 持仓 0.001 开多"))
    assert verdict == "block" and any("BTCUSDT" in r for r in reasons)


def test_empty_position_claim_matches_db_pass():
    v = _mk(db=FakeDB(positions=[]))
    verdict, reasons, _ = v.validate(_notif("🔍 扫描完成\n当前无持仓，挂单 0 单\n余额 $399.26"))
    assert verdict == "pass", reasons


def test_empty_position_claim_but_db_has_position_blocked():
    v = _mk(db=FakeDB(positions=[{"symbol": "BTCUSDT"}]))
    verdict, reasons, _ = v.validate(_notif("🔍 扫描完成\n当前无持仓"))
    assert verdict == "block"
    assert any("position-claim-mismatch" in r and "BTCUSDT" in r for r in reasons)


# ② 挂单断言
def test_orders_claim_zero_but_exchange_unreachable_blocked():
    v = _mk(facts=FakeFacts(ok=False, orders=None))
    verdict, reasons, _ = v.validate(_notif("扫描完成\n挂单 0 单"))
    assert verdict == "block" and any("unreachable" in r or "失败" in r for r in reasons)


def test_orders_claim_zero_exchange_zero_pass():
    v = _mk(facts=FakeFacts(ok=True, orders=[], balance=399.26))
    verdict, _, _ = v.validate(_notif("扫描完成\n挂单 0 单"))
    assert verdict == "pass"


def test_orders_claim_mismatch_blocked():
    v = _mk(facts=FakeFacts(ok=True, orders=[{"id": "o1"}]))
    verdict, reasons, _ = v.validate(_notif("扫描完成\n挂单 0 单"))
    assert verdict == "block" and any("orders-claim-mismatch" in r for r in reasons)


# ③ 余额断言
def test_balance_divergence_over_cent_blocked():
    v = _mk(db=FakeDB(cash=398.00), facts=FakeFacts(ok=True, orders=[], balance=399.26))
    verdict, reasons, _ = v.validate(_notif("扫描完成\n余额 $399.26"))
    assert verdict == "block" and any("balance-claim-mismatch" in r for r in reasons)


def test_balance_consistent_pass():
    v = _mk(db=FakeDB(cash=399.26), facts=FakeFacts(ok=True, orders=[], balance=399.2595))
    verdict, _, _ = v.validate(_notif("扫描完成\n余额 $399.26"))
    assert verdict == "pass"


# ④ 去重
def test_duplicate_close_event_blocked(tmp_path):
    hist = tmp_path / "pending_notifications.json"
    hist.write_text(json.dumps([{
        "id": "old", "timestamp": "2026-08-31T09:00:00", "type": "trade",
        "title": "", "body": "🔴 ENSOUSDT 平仓 STOP_LOSS @ 0.826", "pushed": True,
    }]))
    v = ReportValidator(db=FakeDB(), facts=FakeFacts(ok=True, orders=[]),
                        history_file=str(hist), now_ts=1756630000.0)
    verdict, reasons, _ = v.validate(_notif("🔴 ENSOUSDT 平仓 STOP_LOSS @ 0.826"))
    assert verdict == "block" and any("重复" in r or "duplicate" in r.lower() for r in reasons)


def test_distinct_event_pass(tmp_path):
    hist = tmp_path / "pending_notifications.json"
    hist.write_text(json.dumps([{
        "id": "old", "timestamp": "2026-08-31T09:00:00", "type": "trade",
        "title": "", "body": "🔴 ENSOUSDT 平仓 STOP_LOSS @ 0.826", "pushed": True,
    }]))
    v = ReportValidator(db=FakeDB(positions=[{"symbol": "UNIUSDT"}]),
                        facts=FakeFacts(ok=True, orders=[]),
                        history_file=str(hist), now_ts=1756630000.0)
    verdict, reasons, _ = v.validate(_notif("🟢 UNIUSDT 开仓 BUY @ 5.2796"))
    assert verdict == "pass", reasons


# 诊断工单
def test_block_writes_diagnostic_ticket(tmp_path):
    fail_file = tmp_path / "report_validator_failures.jsonl"
    v = ReportValidator(db=FakeDB(), facts=FakeFacts(ok=False, orders=None),
                        failures_file=str(fail_file))
    v.validate(_notif("扫描完成\n挂单 0 单"))
    assert fail_file.exists()
    rec = json.loads(fail_file.read_text().strip().splitlines()[-1])
    assert rec["verdict"] == "block" and rec["notif_id"] == "n1"
    assert "root_cause_hypothesis" in rec and "suggested_fix" in rec


# BinanceClient session 加固
def test_apply_session_proxy_pins_session():
    from scripts.report_validator import apply_session_proxy

    class FakeSess:
        trust_env = False
        proxies = {}
    class FakeEx:
        session = FakeSess()
    ex = FakeEx()
    apply_session_proxy(ex)
    assert ex.session.trust_env is True
    assert ex.session.proxies["http"] == "http://127.0.0.1:17890"
    assert ex.session.proxies["https"] == "http://127.0.0.1:17890"


def test_apply_session_proxy_off_env(monkeypatch):
    from scripts.report_validator import apply_session_proxy
    monkeypatch.setenv("BINANCE_PROXY", "off")

    class FakeSess:
        trust_env = False
        proxies = {}
    ex = type("E", (), {"session": FakeSess()})()
    apply_session_proxy(ex)
    assert ex.session.proxies == {}  # untouched
