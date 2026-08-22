import sqlite3, sys, time
db = sys.argv[1]
time.sleep(2)  # let A grab the lock first
c = sqlite3.connect(db, timeout=30)
c.execute("PRAGMA journal_mode=DELETE")
c.execute("PRAGMA synchronous=FULL")
c.execute("PRAGMA busy_timeout=30000")
t0 = time.time()
try:
    c.execute("INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) VALUES ('X','SELL',1,1,0,?)", (time.time(),))
    c.commit()
    print(f"B: INSERT+commit OK after {time.time()-t0:.1f}s (silent-success path)", flush=True)
except Exception as e:
    print(f"B: RAISED after {time.time()-t0:.1f}s: {type(e).__name__}: {e}", flush=True)
