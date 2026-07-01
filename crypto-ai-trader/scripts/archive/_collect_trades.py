from src.state_db import StateDB
import sqlite3

db = StateDB()
conn = sqlite3.connect(db.db_path)
cur = conn.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
print('=== Tables ===')
for t in tables:
    print(f'  {t}')

if 'trade_outcomes' in tables:
    cur.execute('SELECT * FROM trade_outcomes ORDER BY rowid DESC LIMIT 50')
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    print(f'\n=== trade_outcomes (last {len(rows)} rows) ===')
    print(f'Columns: {cols}')
    for r in rows:
        print(r)

    cur.execute('SELECT COUNT(*) FROM trade_outcomes')
    total = cur.fetchone()[0]
    print(f'\nTotal trades: {total}')

    for col in ['pnl', 'profit', 'realized_pnl', 'profit_loss', 'result']:
        try:
            cur.execute(f'SELECT AVG({col}), MIN({col}), MAX({col}), COUNT({col}) FROM trade_outcomes')
            avg, mn, mx, cnt = cur.fetchone()
            print(f'{col}: avg={avg}, min={mn}, max={mx}, count={cnt}')
        except:
            pass

    for col in ['is_win', 'result', 'outcome', 'status', 'side']:
        try:
            cur.execute(f'SELECT {col}, COUNT(*) FROM trade_outcomes GROUP BY {col}')
            vals = cur.fetchall()
            print(f'{col} distribution: {vals}')
        except:
            pass

    # Show schema
    cur.execute('PRAGMA table_info(trade_outcomes)')
    print('\n=== Schema ===')
    for row in cur.fetchall():
        print(row)
else:
    print('trade_outcomes table not found')

# Also check for other useful tables
for table in tables:
    if table != 'trade_outcomes':
        cur.execute(f'SELECT COUNT(*) FROM [{table}]')
        cnt = cur.fetchone()[0]
        print(f'\nTable {table}: {cnt} rows')
        cur.execute(f'SELECT * FROM [{table}] LIMIT 3')
        cols = [d[0] for d in cur.description]
        print(f'  Columns: {cols}')
        for r in cur.fetchall():
            print(f'  {r}')

conn.close()
