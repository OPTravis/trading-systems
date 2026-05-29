import sqlite3
import math
from datetime import datetime

db_path = 'data/state.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.execute('SELECT * FROM trade_outcomes WHERE status = "closed" ORDER BY entry_time ASC')
rows = cur.fetchall()
cols = [d[0] for d in cur.description]

# Map columns to indices
ci = {c: i for i, c in enumerate(cols)}

wins = 0
losses = 0
total_pnl_pct = 0.0
total_pnl_abs = 0.0
pnls = []
for r in rows:
    pnl_pct = r[ci['net_pnl_pct']]
    pnl_abs = r[ci['net_pnl_absolute']]
    is_win = r[ci['is_win']]
    if is_win:
        wins += 1
    else:
        losses += 1
    total_pnl_pct += pnl_pct if pnl_pct else 0
    total_pnl_abs += pnl_abs if pnl_abs else 0
    pnls.append(pnl_pct if pnl_pct else 0)

total = wins + losses
win_rate = (wins / total * 100) if total > 0 else 0
avg_pnl_pct = (total_pnl_pct / total) if total > 0 else 0
avg_pnl_abs = (total_pnl_abs / total) if total > 0 else 0

# Sharpe-like ratio (annualized, assuming ~3 day avg hold)
avg_hold_hours = sum(r[ci['time_held_hours']] for r in rows if r[ci['time_held_hours']]) / total if total > 0 else 0
if len(pnls) > 1:
    mean_pnl = sum(pnls) / len(pnls)
    std_pnl = math.sqrt(sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1))
    # Approximate annualized: trades/year = (24 * 365) / avg_hold
    trades_per_year = (24 * 365) / avg_hold_hours if avg_hold_hours > 0 else 0
    sharpe = (mean_pnl / std_pnl) * math.sqrt(trades_per_year) if std_pnl > 0 else 0
else:
    std_pnl = 0
    sharpe = 0

# Max drawdown from cumulative PnL
cum = 0
peak = 0
max_dd = 0
for p in pnls:
    cum += p
    if cum > peak:
        peak = cum
    dd = peak - cum
    if dd > max_dd:
        max_dd = dd

print(f"=== PERFORMANCE STATS ===")
print(f"Total closed trades: {total}")
print(f"Wins: {wins}, Losses: {losses}")
print(f"Win rate: {win_rate:.1f}%")
print(f"Avg PnL %: {avg_pnl_pct:.2f}%")
print(f"Avg PnL abs: ${avg_pnl_abs:.2f}")
print(f"Total PnL %: {total_pnl_pct:.2f}%")
print(f"Total PnL abs: ${total_pnl_abs:.2f}")
print(f"Avg hold time: {avg_hold_hours:.1f} hours ({avg_hold_hours/24:.1f} days)")
print(f"Sharpe (approx): {sharpe:.2f}")
print(f"Max drawdown (cumulative %): {max_dd:.2f}%")
print(f"PnL std dev: {std_pnl:.2f}%")

# Breakdown by strategy
print(f"\n=== BY STRATEGY ===")
cur.execute('SELECT strategy, COUNT(*), SUM(is_win), AVG(net_pnl_pct), SUM(net_pnl_absolute) FROM trade_outcomes WHERE status="closed" GROUP BY strategy')
for row in cur.fetchall():
    strat, cnt, w, avg_p, total_p = row
    wr = (w / cnt * 100) if cnt > 0 else 0
    print(f"  {strat}: {cnt} trades, WR {wr:.0f}%, avg PnL {avg_p:.2f}%, total ${total_p:.2f}")

# Breakdown by symbol
print(f"\n=== BY SYMBOL (last 5 trades) ===")
cur.execute('SELECT symbol, COUNT(*), SUM(is_win), AVG(net_pnl_pct) FROM trade_outcomes WHERE status="closed" GROUP BY symbol ORDER BY COUNT(*) DESC')
for row in cur.fetchall():
    sym, cnt, w, avg_p = row
    wr = (w / cnt * 100) if cnt > 0 else 0
    print(f"  {sym}: {cnt} trades, WR {wr:.0f}%, avg PnL {avg_p:.2f}%")

# Daily PnL from risk_guard
print(f"\n=== RISK GUARD (daily PnL) ===")
cur.execute('SELECT daily_pnl, streak, last_reset FROM risk_guard')
for row in cur.fetchall():
    print(f"  Daily PnL: ${row[0]:.2f}, streak: {row[1]}")

# Drawdown state
print(f"\n=== DRAWDOWN STATE ===")
cur.execute('SELECT high_watermark, current_drawdown_pct, max_drawdown_pct, tripped_count, tripped_at FROM drawdown')
for row in cur.fetchall():
    print(f"  HWM: ${row[0]:.2f}, current DD: {row[1]:.2f}%, max DD: {row[2]:.2f}%, tripped: {row[3]} times, last trip: {row[4]}")

conn.close()
