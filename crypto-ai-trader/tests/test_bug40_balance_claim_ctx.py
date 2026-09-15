"""bug#40 (2026-09-05): balance-claim 误把成交价当余额声明。

背景：signal 通知格式「BUY NEARUSDT @ $2.133」——balance-claim 正则的
USDT 锚点把币名后缀 USDT 吞进匹配，拿 @ 后的成交价 $2.133 对比钱包
$393 必然报错。9/5 当日误拦 5 次（NEAR/ASTER/ZKP/GIGGLE/ENA 全中招）。

修复：
  1) 排除成交价上下文——含交易动词 + @价格 的行不参与余额声明匹配；
  2) 真余额声明检测加强——补英文 balance 锚点（balance: x / Balance: $x /
     balance is x）。
原 3f87c12 (9/5 15:50) 未 push 随容器丢失，本文件为按规格重建版。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.report_validator import (
    ReportValidator, ExchangeFacts, _balance_claim_value,
)

# ---------- 复现样例 14 例（9/5 五连误拦 + 边界 + 英文锚点 + 中文保护） ----------

def test_near_buy_price_not_claim():
    assert _balance_claim_value("BUY NEARUSDT @ $2.133") is None

def test_aster_sell_price_not_claim():
    assert _balance_claim_value("SELL ASTERUSDT @ $1.85") is None

def test_zkp_buy_price_not_claim():
    assert _balance_claim_value("BUY ZKPUSDT @ $0.42") is None

def test_giggle_sell_price_not_claim():
    assert _balance_claim_value("SELL GIGGLEUSDT @ $0.117") is None

def test_ena_buy_price_not_claim():
    assert _balance_claim_value("BUY ENAUSDT @ $0.66") is None

def test_trade_line_skipped_but_real_claim_kept():
    body = "BUY NEARUSDT @ $2.133\n当前余额 $393.5"
    assert _balance_claim_value(body) == 393.5

def test_at_without_space_still_trade_line():
    assert _balance_claim_value("BUY NEARUSDT@$2.133") is None

def test_price_with_thousands_separator():
    assert _balance_claim_value("SELL ENAUSDT @ $1,234.5") is None

def test_chinese_verb_trade_line():
    assert _balance_claim_value("买入 NEARUSDT @ $2.133") is None

def test_trade_line_with_trailing_text():
    assert _balance_claim_value("卖出 GIGGLEUSDT @ $0.117 手续费已扣") is None

def test_english_balance_colon_plain_number():
    assert _balance_claim_value("USDT balance: 393.5") == 393.5

def test_english_balance_dollar():
    assert _balance_claim_value("Balance: $393.5") == 393.5

def test_english_balance_is():
    assert _balance_claim_value("total balance is 393.5") == 393.5

def test_chinese_anchor_unaffected():
    assert _balance_claim_value("当前余额 $393.5") == 393.5


# ---------- validator 端到端：交易行不再触发余额断言，真声明照常校验 ----------

class FakeDB:
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
        if "SELECT MAX(TIMESTAMP) FROM TRADES" in u or ("MAX" in u and "TRADES" in u):
            return _Res([{"max_ts": None}])
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
    def __init__(self, ok=True, orders=None, balance=399.26):
        self.fact = {"ok": ok, "open_orders": orders, "usdt_balance": balance}
    def collect(self):
        return dict(self.fact)

def _mk(db=None, facts=None):
    return ReportValidator(db=db or FakeDB(), facts=facts or FakeFacts(orders=[]),
                           now_ts=1756630000.0)

def _notif(body):
    return {"id": "n40", "timestamp": "2026-09-05T15:50:00", "type": "main_event",
            "title": "", "body": body, "pushed": False}

def test_trade_line_only_body_passes():
    v = _mk()
    verdict, reasons, _ = v.validate(
        _notif("BUY NEARUSDT @ $2.133\nSELL ASTERUSDT @ $1.85"))
    assert verdict == "pass", reasons
    assert not any("balance-claim" in r for r in reasons)

def test_english_claim_consistent_passes():
    v = _mk(db=FakeDB(cash=399.26), facts=FakeFacts(orders=[], balance=399.26))
    verdict, reasons, _ = v.validate(
        _notif("BUY NEARUSDT @ $2.133\nBalance: $399.26"))
    assert verdict == "pass", reasons

def test_english_claim_mismatch_blocked():
    v = _mk(db=FakeDB(cash=399.26), facts=FakeFacts(orders=[], balance=399.26))
    verdict, reasons, _ = v.validate(
        _notif("BUY NEARUSDT @ $2.133\nBalance: $100.00"))
    assert verdict == "block"
    assert any("balance-claim-mismatch" in r for r in reasons)
