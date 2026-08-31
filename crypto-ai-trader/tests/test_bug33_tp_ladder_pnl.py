"""bug#33 (2026-08-31): B 引擎 TP 阶梯平仓记账失真（bug#24 记账失真家族）。

现象（8/30 ZKPUSDT paper_03a8333b4115，只影响 paper 归因、不影响实盘资金）：
  建仓 1031.888901 @ 0.0492246（notional $50.79, fee $0.0508）；
  TP_1R/TP_2R 同轮各卖 1/3 @ 0.0593（各 +$3.45 净）；
  次日 B_ATR_SL 平最后 1/3 @ 0.0515。
  两个失真：
  1) close_position partial 分支不累计 realized_pnl（只写 notes）；full close
     公式 (exit-entry)*剩余qty - 全部费用 只给最后一段定价却扣全部费用 →
     position.realized_pnl = $0.673，真实 ~$7.60，前两段 TP 净收益 ~$6.9 被吞。
     bull_paper_ab_metrics 用该字段算 B 胜率/盈亏 → 归因全面失真。
  2) _arm_scaleouts 调用点未传 original_qty（恒 0）→ _process_scaleouts
     fallback 用"调用时剩余量"。跨轮触发时 stage2 只卖 2/9 而非 1/3。

修复（bug#33）：
  a) partial close：realized_pnl 累计写回 DB；full close：
     total_pnl = 累计 realized_pnl + 本段 pnl - 建仓费（从首笔 BUY leg 取）。
  b) open 路径 arm 时传 original_qty；fallback 链 original_qty → 首笔 BUY qty
     → 快照量（兼容存量 0 值行）。
不变量：全平后 position.realized_pnl == 现金增量（cash_final - cash_start）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.bull_paper_portfolio import BullPaperPortfolio
from src.state_db import StateDB


QTY = 1031.888901
ENTRY = 0.0492246
TP_PX = 0.0593
SL_PX = 0.0515
FEE_RATE = 0.001


@pytest.fixture()
def b_env(tmp_path):
    db = StateDB(str(tmp_path / "state33.db"))
    pf = BullPaperPortfolio(db, start_cash=400.0, group="B")
    yield db, pf
    try:
        db.close()
    except Exception:
        pass


def _open_zkp(pf):
    return pf.open_position(
        symbol="ZKPUSDT", side="core", quantity=QTY, price=ENTRY,
        stop_loss=0.045, notes="bug33 fixture",
    )


def _mk_engine(db, pf):
    """Minimal engine stub exposing only what _process_scaleouts needs."""
    from src.bull_paper_engine_b import BullPaperEngineB
    eng = BullPaperEngineB.__new__(BullPaperEngineB)
    eng.db = db
    eng._b_portfolio = pf  # portfolio is a read-only property over this cache
    return eng


def _arm(db, eng, pos, original_qty=0.0):
    eng._arm_scaleouts(pos.id, "ZKPUSDT", ENTRY, 0.004, atr_entry=0.002,
                       original_qty=original_qty)


def _get_pos(db, pid):
    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM paper_bull_positions WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# 1) partial close 必须累计 realized_pnl
# ---------------------------------------------------------------------------
def test_partial_closes_accumulate_realized_pnl(b_env):
    db, pf = b_env
    pos = _open_zkp(pf)
    seg = QTY / 3.0
    pf.close_position(pos.id, TP_PX, quantity=seg, reason="B_TP_1R")
    pf.close_position(pos.id, TP_PX, quantity=seg, reason="B_TP_2R")

    row = _get_pos(db, pos.id)
    exp_seg = seg * TP_PX * (1 - FEE_RATE) - seg * ENTRY
    assert row["quantity"] == pytest.approx(QTY - 2 * seg, abs=1e-6)
    # bug#33: realized_pnl must include BOTH fired TP legs (was: 0.0)
    assert row["realized_pnl"] == pytest.approx(2 * exp_seg, rel=1e-9), (
        f"partial legs not accumulated: {row['realized_pnl']} != {2*exp_seg}"
    )


# ---------------------------------------------------------------------------
# 2) 全平后 realized_pnl == 现金增量（记账黄金不变量）
# ---------------------------------------------------------------------------
def test_full_close_total_pnl_matches_cash_delta(b_env):
    db, pf = b_env
    cash0 = pf.cash
    pos = _open_zkp(pf)
    seg = QTY / 3.0
    pf.close_position(pos.id, TP_PX, quantity=seg, reason="B_TP_1R")
    pf.close_position(pos.id, TP_PX, quantity=seg, reason="B_TP_2R")
    pf.close_position(pos.id, SL_PX, quantity=None, reason="B_ATR_SL")

    row = _get_pos(db, pos.id)
    cash_delta = pf.cash - cash0

    assert row["status"] == "closed"
    # Golden invariant: booked PnL equals what cash actually did
    assert row["realized_pnl"] == pytest.approx(cash_delta, abs=1e-6), (
        f"booked {row['realized_pnl']} vs cash delta {cash_delta}"
    )
    # And it must NOT be the old last-leg-only figure (~$0.67 on live data)
    assert row["realized_pnl"] > 5.0, "TP-leg gains swallowed again"


# ---------------------------------------------------------------------------
# 3) scaleout 数量：跨轮触发 stage2 必须按原始建仓量的 1/3
# ---------------------------------------------------------------------------
def test_scaleout_stage2_uses_original_qty_across_rounds(b_env):
    db, pf = b_env
    pos = _open_zkp(pf)
    eng = _mk_engine(db, pf)
    _arm(db, eng, pos, original_qty=0.0)  # legacy arm: original_qty unknown

    seg = QTY / 3.0
    # triggers: stage1 = ENTRY+0.004 = 0.0532246; stage2 = ENTRY+0.008 = 0.0572246
    # Round 1: price clears stage1 only (legacy fallback must use entry BUY qty)
    eng._process_scaleouts(pos.__dict__, ENTRY + 0.0045)
    r1 = _get_pos(db, pos.id)
    assert r1["quantity"] == pytest.approx(QTY - seg, abs=1e-6)

    # Round 2: stage2 fires later — must sell 1/3 of ORIGINAL, not 1/3 of remaining
    eng._process_scaleouts(r1, TP_PX)
    r2 = _get_pos(db, pos.id)
    assert r2["quantity"] == pytest.approx(QTY - 2 * seg, abs=1e-6), (
        f"stage2 sold off remaining-qty base: {r2['quantity']}"
    )


# ---------------------------------------------------------------------------
# 4) 无 partial 直接全平：回归（老口径对纯全平本来就对）
# ---------------------------------------------------------------------------
def test_full_close_without_partials_regression(b_env):
    db, pf = b_env
    cash0 = pf.cash
    pos = _open_zkp(pf)
    pf.close_position(pos.id, SL_PX, reason="B_ATR_SL")
    row = _get_pos(db, pos.id)
    # full close books gross exit minus close fee AND entry fee (unchanged by fix)
    exp = QTY * SL_PX * (1 - FEE_RATE) - QTY * ENTRY - QTY * ENTRY * FEE_RATE
    assert row["realized_pnl"] == pytest.approx(exp, rel=1e-9)
    assert pf.cash - cash0 == pytest.approx(exp, abs=1e-6)
