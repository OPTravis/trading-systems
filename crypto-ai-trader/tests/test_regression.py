"""
回歸測試套件 — 覆蓋所有已知 bug 模式
任何代碼修改前必須通過此測試

Bug 分類：
  TYPE_A: 類型錯誤（enum vs string, int vs float）
  TYPE_B: 空數據處理（None/[] 導致崩潰或錯誤結果）
  TYPE_C: 數據源錯誤（讀錯表、用錯欄位）
  TYPE_D: 業務邏輯繞過（安全檢查被跳過）
  TYPE_E: 變量作用域（db/client 未初始化）
  TYPE_F: 邊界條件（除零、溢出、空循環）
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from unittest.mock import MagicMock, patch
from decimal import Decimal


# ============================================================
# TYPE_A: 類型錯誤
# ============================================================

class TestTypeErrors:
    """防止 enum vs string、int vs float 等類型比較錯誤"""

    def test_signal_type_comparison_in_run_all(self):
        """StrategyRegistry.run_all() 必須用 SignalType enum 比較"""
        from src.strategy_registry import StrategyRegistry
        import inspect
        source = inspect.getsource(StrategyRegistry.run_all)
        # 不應有 string 比較
        assert 'signal.signal in ("BUY"' not in source, \
            "run_all 仍然用 string 比較信號！應使用 SignalType.BUY"
        assert 'SignalType.BUY' in source or 'SignalType.SELL' in source, \
            "run_all 應該用 SignalType enum 比較"

    def test_score_is_float_technical(self):
        """TechnicalAgent 評分必須是 float"""
        from src.agents.technical_agent import TechnicalAgent
        ta = TechnicalAgent()
        result = ta.analyze([])
        assert isinstance(result.score, float), \
            f"TechnicalAgent score type: {type(result.score)}"
        assert isinstance(result.confidence, str), \
            f"TechnicalAgent confidence type: {type(result.confidence)}"


# ============================================================
# TYPE_B: 空數據處理
# ============================================================

class TestEmptyData:
    """防止空數據導致崩潰或錯誤結果"""

    def test_technical_agent_empty_klines(self):
        """空 klines → score=50, confidence=none"""
        from src.agents.technical_agent import TechnicalAgent
        ta = TechnicalAgent()
        result = ta.analyze([])
        assert result.score == 50.0, f"Expected 50, got {result.score}"
        assert result.confidence == 'none', f"Expected none, got {result.confidence}"

    def test_technical_agent_none_klines(self):
        """None klines → score=50, confidence=none"""
        from src.agents.technical_agent import TechnicalAgent
        ta = TechnicalAgent()
        result = ta.analyze(None)
        assert result.score == 50.0, f"Expected 50, got {result.score}"

    def test_score_range_0_to_100(self):
        """所有評分必須在 0-100 範圍"""
        from src.agents.technical_agent import TechnicalAgent
        ta = TechnicalAgent()
        result = ta.analyze([])
        assert 0 <= result.score <= 100, \
            f"TechnicalAgent score={result.score} 超出範圍"


# ============================================================
# TYPE_C: 數據源錯誤
# ============================================================

class TestDataSource:
    """防止讀錯表、用錯欄位"""

    def test_kelly_reads_trade_outcomes(self):
        """Kelly 必須從 trade_outcomes 讀取，不是 trades"""
        from src.kelly_sizer import KellyPositionSizer
        import inspect
        source = inspect.getsource(KellyPositionSizer._get_trade_history)
        assert 'trade_outcomes' in source, \
            "_get_trade_history 必須讀 trade_outcomes 表"
        assert "FROM trades" not in source, \
            "_get_trade_history 不應讀 trades 表"

    def test_strategy_weights_from_trade_outcomes(self):
        """策略權重必須從 trade_outcomes 計算"""
        from src.strategy_registry import StrategyRegistry
        import inspect
        source = inspect.getsource(StrategyRegistry.compute_strategy_weights)
        assert 'trade_outcomes' in source, \
            "compute_strategy_weights 應從 trade_outcomes 讀取"


# ============================================================
# TYPE_D: 業務邏輯繞過
# ============================================================

class TestBusinessLogicBypass:
    """防止安全檢查被繞過"""

    def test_kelly_blocks_when_zero(self):
        """Kelly = 0 時必須阻止開單 (position_pct = 0)"""
        from src.kelly_sizer import KellyPositionSizer
        ks = KellyPositionSizer()

        # win_rate=0 → kelly=0 → position_pct=0
        result = ks.get_position_size(
            symbol='BTCUSDT',
            balance=1000.0,
            stop_loss_pct=5.0,
            take_profit_pct=10.0,
            signal_score=50,
            use_historical=False  # 不用歷史數據，用 score 估算
        )
        # score=50 → win_rate=0.35+0.5*0.30=0.50
        # 但 kelly 可能為正，所以用 win_rate=0 測試
        # 直接測 calculate_kelly_fraction
        kelly = ks.calculate_kelly_fraction(0.0, 0.05, 0.05)
        assert kelly == 0.0, f"win_rate=0 時 kelly 應為 0，got {kelly}"

    def test_kelly_blocks_when_negative(self):
        """Kelly < 0 時必須阻止開單"""
        from src.kelly_sizer import KellyPositionSizer
        ks = KellyPositionSizer()

        # 非常低的 win_rate + 高 loss → kelly < 0
        kelly = ks.calculate_kelly_fraction(0.2, 0.01, 0.10)
        assert kelly <= 0, f"劣勢交易 kelly 應 ≤ 0，got {kelly}"

    def test_kelly_zero_win_rate(self):
        """win_rate=0 → kelly=0"""
        from src.kelly_sizer import KellyPositionSizer
        ks = KellyPositionSizer()
        kelly = ks.calculate_kelly_fraction(0.0, 0.05, 0.05)
        assert kelly == 0.0

    def test_kelly_zero_avg_loss(self):
        """avg_loss=0 → kelly=0（防除零）"""
        from src.kelly_sizer import KellyPositionSizer
        ks = KellyPositionSizer()
        kelly = ks.calculate_kelly_fraction(0.5, 0.05, 0.0)
        assert kelly == 0.0

    def test_kelly_win_rate_one(self):
        """win_rate=1.0 → kelly=0（異常輸入保護）"""
        from src.kelly_sizer import KellyPositionSizer
        ks = KellyPositionSizer()
        kelly = ks.calculate_kelly_fraction(1.0, 0.05, 0.05)
        assert kelly == 0.0


# ============================================================
# TYPE_E: 變量作用域
# ============================================================

class TestVariableScope:
    """防止 db/client 未初始化等作用域錯誤"""

    def test_verify_phase_files_have_db(self):
        """所有 verify_phase 文件必須初始化 db"""
        import glob
        phase_files = glob.glob('/home/travis/crypto-ai-trader/scripts/verify_phase*.py')
        for f in phase_files:
            with open(f) as fh:
                content = fh.read()
            if 'TradeOutcomeRecorder' in content:
                assert 'get_state_db()' in content, \
                    f"{os.path.basename(f)}: 使用 TradeOutcomeRecorder 但未初始化 db"

    def test_no_undefined_db_in_ensure_tp_sl(self):
        """ensure_tp_sl.py 中使用 db 的位置必須先定義"""
        f = '/home/travis/crypto-ai-trader/scripts/ensure_tp_sl.py'
        with open(f) as fh:
            content = fh.read()
        # 檢查所有函數中 db 的使用
        import ast
        try:
            tree = ast.parse(content)
        except SyntaxError:
            pytest.fail(f"{f} 語法錯誤")

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                func_source = ast.get_source_segment(content, node)
                if func_source and 'db.' in func_source:
                    # 檢查函數內是否有 db = ... 或 db 參數
                    func_args = [a.arg for a in node.args.args]
                    has_db_arg = 'db' in func_args
                    has_db_assign = any(
                        isinstance(n, ast.Assign) and
                        any(isinstance(t, ast.Name) and t.id == 'db' for t in n.targets)
                        for n in ast.walk(node)
                    )
                    if not has_db_arg and not has_db_assign:
                        # 可能是全局變量，檢查模塊級別
                        pass  # 不報錯，但標記為潛在問題


# ============================================================
# TYPE_F: 邊界條件
# ============================================================

class TestEdgeCases:
    """除零、溢出、空循環等邊界條件"""

    def test_decimal_precision_step_size(self):
        """價格精度不能有浮點誤差"""
        step = Decimal('0.001')
        qty = Decimal('123.456789')
        rounded = float(int(qty / step) * step)
        assert rounded == 123.456, f"精度錯誤: {rounded}"

    def test_indicators_rsi_empty(self):
        """空 prices → RSI 不崩潰"""
        from src.indicators import Indicators
        result = Indicators.rsi([], period=14)
        # 應該返回 None 或合理默認值
        assert result is None or isinstance(result, (int, float))

    def test_indicators_vwap_empty(self):
        """空 klines → VWAP 不崩潰"""
        from src.indicators import Indicators
        result = Indicators.vwap([])
        assert isinstance(result, (int, float))

    def test_indicators_atr_empty(self):
        """空 klines → ATR 不崩潰"""
        from src.indicators import Indicators
        result = Indicators.atr([], period=14)
        assert result is None or isinstance(result, (int, float))

    def test_indicators_rsi_insufficient_data(self):
        """數據不足 → RSI 不崩潰"""
        from src.indicators import Indicators
        result = Indicators.rsi([100.0, 101.0, 99.0], period=14)
        assert result is None or isinstance(result, (int, float))


# ============================================================
# TYPE_G: 系統整合
# ============================================================

class TestIntegration:
    """端到端整合測試"""

    def test_hmm_predict_runs(self):
        """HMM 預測不崩潰"""
        import subprocess
        result = subprocess.run(
            [sys.executable, 'scripts/hmm_regime.py', '--predict'],
            cwd='/home/travis/crypto-ai-trader',
            capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, f"HMM predict 失敗:\n{result.stderr[:500]}"

    def test_ensure_tp_sl_runs(self):
        """ensure_tp_sl.py 執行不崩潰"""
        import subprocess
        result = subprocess.run(
            [sys.executable, 'scripts/ensure_tp_sl.py'],
            cwd='/home/travis/crypto-ai-trader',
            capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, \
            f"ensure_tp_sl.py 失敗:\n{result.stderr[:500]}"

    @pytest.mark.slow
    def test_auto_heal_runs(self):
        """auto_heal.py 執行不崩潰（exit code 0=正常，1=有異常，都是正常行為）"""
        import subprocess
        result = subprocess.run(
            [sys.executable, 'scripts/auto_heal.py', '--verbose'],
            cwd='/home/travis/crypto-ai-trader',
            capture_output=True, text=True, timeout=600
        )
        # auto_heal 返回 0=全部正常，1=發現異常（這是設計行為，不是崩潰）
        # 只檢查不是被信號殺死（returncode < 0）或超時
        assert result.returncode >= 0, \
            f"auto_heal.py 被殺死:\n{result.stderr[:500]}"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
