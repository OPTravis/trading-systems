#!/usr/bin/env python3
"""Resident crypto scan — Cloud-Travis (2026-09-15).
The ONE executor & scanner. CodeAct ticket becomes bridge/report-only.
Gate shared with cron_scan.py ticket (same cloud-drive file).
Writes scan result -> cloud-drive live_scan/latest.json for the bridge.
"""
import json, os, pathlib, subprocess, sys, time

GATE_FILE = "/Coze/Drive/Crypto_Trading_Monitor/.last_scan_gate"
GATE_SEC = 20 * 60            # reside cadence: 20 min
LIVE_DIR = "/Coze/Drive/Crypto_Trading_Monitor/live_scan"
LATEST = os.path.join(LIVE_DIR, "latest.json")
BACKUP_DIR = "/Coze/Drive/Crypto_Trading_Monitor/state_backup"
# self-locating: script lives in <repo>/scripts/, repo root is parent.
# Survives reclone/rename; crontab points here, zero edits after pull.
REPO = str(pathlib.Path(__file__).resolve().parent.parent)
DB = "/root/trading-state/state.db"          # data stays outside repo
LOG = os.path.join(REPO, "logs", "reside_scan.log")

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    try:
        sz = os.path.getsize(LOG) if os.path.exists(LOG) else 0
        with open(LOG, "a") as f:
            f.write(line + "\n")
        if sz > 5_000_000:
            os.replace(LOG, LOG + ".old")
    except Exception:
        pass

def gate_open():
    # WO-017-3: cron-phase jitter (flock startup + log IO) can leave the
    # gate file age at 1199.x s on the due tick — a strict `<` comparison
    # then skips the round and pushes cadence out a full cycle ("gate:
    # skip (0s remaining)" 9/22 03:30). A <2s residual counts as expired.
    try:
        age = time.time() - os.path.getmtime(GATE_FILE)
        if age < GATE_SEC - 2:
            log(f"gate: skip ({max(GATE_SEC - age, 0):.0f}s remaining)")
            return False
    except FileNotFoundError:
        pass
    return True

def touch_gate():
    pathlib.Path(GATE_FILE).touch()

def backup_db():
    try:
        pathlib.Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M")
        dst = os.path.join(BACKUP_DIR, f"state.db.{ts}")
        subprocess.run(["cp", DB, dst], check=True, timeout=30)
        baks = sorted(pathlib.Path(BACKUP_DIR).glob("state.db.*"))
        for old in baks[:-3]:
            old.unlink(missing_ok=True)
        log(f"db backup -> {os.path.basename(dst)}")
        return os.path.basename(dst)
    except Exception as e:
        log(f"db backup FAIL: {e}")
        return None

def _load_repo_env():
    """Expose repo .env vars to os.environ without overriding existing."""
    try:
        with open(os.path.join(REPO, ".env")) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip(chr(34)).strip(chr(39))
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        log(f"repo env load fail: {e}")


def event_tick():
    """Lightweight rapid-change check (Phase 1 event-driven layer).

    Runs inside this 10min cron round — no new resident process. Returns
    a trigger reason string (also logged as EVENT_TRIGGER) or None. Any
    failure fails open to the normal scheduled flow.
    """
    try:
        sys.path.insert(0, REPO)
        _load_repo_env()
        import sqlite3
        from src.binance_client import BinanceClient
        from src.event_trigger import EventTriggerEngine, BTC_SYMBOL

        cli = BinanceClient(testnet=False)

        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT symbol FROM portfolio WHERE quantity > 0").fetchall()
        conn.close()
        holdings = sorted(r[0] for r in rows)

        prices = {}
        for s in sorted(set([BTC_SYMBOL] + holdings)):
            try:
                px = cli.get_ticker_price(s)
                if px:
                    prices[s] = float(px)
            except Exception as e:
                log(f"event tick price fail {s}: {e}")

        # (c) HMM regime flip — channel inactive while no trained model
        regime = None
        try:
            from src.hmm_regime import HMMRegimeDetector
            det = HMMRegimeDetector()
            if det._trained or det._load_training_state():
                # 1000 bars = ~42 daily samples -> 22 valid rows after BB20
                # warmup (min 20). 500 bars starved _compute_features and the
                # channel silently returned None (kv stuck at train-time snap).
                kl = cli.get_klines("BTCUSDT", interval="1h", limit=1000)
                res = det.predict(kl)
                regime = res.get("regime") if res else None
        except Exception as e:
            log(f"event tick regime fail: {e}")

        # (d) fills on holdings (covers OCO fills; reconciler already
        # re-lists TP/SL each scan — event value = faster full re-scan)
        trades = {}
        for s in holdings:
            try:
                tr = cli.get_my_trades(s, limit=10)
                if tr:
                    trades[s] = max(int(t["id"]) for t in tr)
            except Exception:
                pass

        eng = EventTriggerEngine()
        trig = eng.record_and_check(time.time(), prices, holdings,
                                    regime_now=regime, trade_ids_now=trades)
        if trig:
            reason = f"{trig['type']}:{trig['symbol']} {trig['detail']}"
            log(f"EVENT_TRIGGER: {reason}")
            return reason
        return None
    except Exception as e:
        log(f"event tick FAIL (fail-open to normal scan): {e}")
        return None


def main():
    pathlib.Path(LIVE_DIR).mkdir(parents=True, exist_ok=True)
    ev_reason = event_tick()
    if ev_reason is None and not gate_open():
        return 0
    touch_gate()
    log("=== reside scan start ===" if not ev_reason
        else f"=== reside scan start (EVENT_TRIGGER: {ev_reason}) ===")
    t0 = time.time()
    log("=== reside scan start ===")
    try:
        scan_env = dict(os.environ)
        if ev_reason:
            scan_env["EVENT_TRIGGER"] = ev_reason
        r = subprocess.run(
            ["bash", "run_cron.sh", "cron-scan"],
            cwd=REPO, capture_output=True, text=True, timeout=900,
            env=scan_env,
        )
        stdout, rc, elapsed = r.stdout, r.returncode, time.time() - t0
    except subprocess.TimeoutExpired:
        stdout, rc, elapsed = "SCAN TIMEOUT after 900s", 124, 900
    except Exception as e:
        stdout, rc, elapsed = f"SCAN LAUNCH FAIL: {e}", 125, time.time() - t0

    bak = backup_db()
    # scan output lands in logs/cron-scan.log — read tail as the report body
    try:
        log_path = os.path.join(REPO, "logs", "cron-scan.log")
        with open(log_path, errors="replace") as f:
            report = f.read()[-6000:]
    except Exception:
        report = stdout
    if len(stdout.strip()) > len(report.strip()):
        report = stdout
    # extract key signals from report
    trades = [l.strip() for l in report.splitlines()
              if ("BUY " in l or "SELL " in l) and "@" in l and "USDT" in l][:5]
    health = [l.strip() for l in report.splitlines() if "WARN" in l or "🛑" in l][:5]
    opp = ""
    for l in report.splitlines():
        if "opportunities after adapted threshold" in l:
            opp = l.strip()
    verdict = {
        "version": 1,
        "host": "cloud-travis-resident",
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(elapsed, 1),
        "exit_code": rc,
        "opportunities_line": opp,
        "trades": trades,
        "health_warns": health,
        "db_backup": bak,
        "stdout_tail": report,
    }
    tmp = LATEST + ".tmp"
    with open(tmp, "w") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=1)
    os.replace(tmp, LATEST)
    log(f"=== done rc={rc} {elapsed:.0f}s, latest.json written ===")
    return 0

if __name__ == "__main__":
    sys.exit(main())
