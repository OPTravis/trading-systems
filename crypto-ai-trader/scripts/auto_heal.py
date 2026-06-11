#!/home/travis/crypto-ai-trader/.venv/bin/python3
"""
Auto-heal: System-wide anomaly detection by PROBLEM TYPE.

Scans the ENTIRE crypto-ai-trader system for 6 categories of bugs:
  A. CODE_PATTERNS — source code patterns that cause bugs (all .py files)
  B. STATE_INTEGRITY — DB record consistency (all tables)
  C. RUNTIME_VERIFY — execute code to catch runtime failures
  D. OUTPUT_ANALYSIS — scan cron outputs for failure patterns
  E. MODEL_INTEGRITY — ML model state validation
  F. REGRESSION_TESTS — run pytest regression tests for known bug patterns

Each scanner is GENERIC — it detects the same problem type across ALL modules,
not hardcoded checks for specific files.

Usage:
  python3 auto_heal.py              # diagnose + fix, silent when OK
  python3 auto_heal.py --verbose    # always output
  python3 auto_heal.py --report     # report only, no auto-fix
"""

import os
import sys
import re
import json
import glob as _glob
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

# no_agent cron uses system Python — add venv site-packages for project deps
_venv_dir = Path.home() / 'trading-systems' / 'crypto-ai-trader' / '.venv'
if sys.platform == 'win32':
    _venv_site = str(_venv_dir / 'Lib' / 'site-packages')
else:
    _venv_site = str(_venv_dir / 'lib' / 'python*' / 'site-packages')
_matches = _glob.glob(_venv_site)
if _matches:
    sys.path.insert(0, _matches[-1])

HERMES_DIR = Path.home() / ".hermes"
CRON_OUTPUT_DIR = HERMES_DIR / "cron" / "output"
CRYPTO_DIR = Path.home() / "trading-systems" / "crypto-ai-trader"

# Cron job IDs
JOB_IDS = {
    "scan": "28cda1d17ae5",
    "monitor": "d9b1b53cd740",
    "trailing": "097c3e3afcc6",
    "optimize": "ecc9e39afedc",
    "heal": "44e31e87f072",
    "dust": "d7f8b0b0a59f",
}


class Anomaly:
    """A detected anomaly with diagnosis and fix action."""
    def __init__(self, source, pattern, detail, fixable=False, fix_fn=None):
        self.source = source
        self.pattern = pattern
        self.detail = detail
        self.fixable = fixable
        self.fix_fn = fix_fn
        self.fixed = False
        self.fix_result = None

    def attempt_fix(self, dry_run=False):
        if not self.fixable or not self.fix_fn:
            return
        if dry_run:
            self.fix_result = "DRY-RUN"
            return
        try:
            self.fix_result = self.fix_fn()
            self.fixed = bool(self.fix_result)
        except Exception as e:
            self.fix_result = f"FIX-ERROR: {e}"

    def __str__(self):
        status = "✅ FIXED" if self.fixed else ("🔧 FIXABLE" if self.fixable else "⚠️ MANUAL")
        fix_info = f" → {self.fix_result}" if self.fix_result else ""
        return f"[{self.source}] {status} {self.pattern}: {self.detail}{fix_info}"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_recent_outputs(job_id, n=5, max_age_hours=6):
    job_dir = CRON_OUTPUT_DIR / job_id
    if not job_dir.exists():
        return []
    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    files = []
    for f in sorted(job_dir.glob("*.md"), reverse=True):
        try:
            ts_str = f.stem[:15]
            ts = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            if ts >= cutoff:
                files.append(f)
        except (ValueError, IndexError):
            files.append(f)
        if len(files) >= n:
            break
    return files


def grep_code(pattern, include_scripts=True):
    """grep across src/ (and optionally scripts/) for a regex pattern. Returns list of (file, line_num, text)."""
    dirs = [str(CRYPTO_DIR / 'src')]
    if include_scripts:
        dirs.append(str(CRYPTO_DIR / 'scripts'))
    try:
        result = subprocess.run(
            ['grep', '-rn', '-E', pattern] + dirs + ['--include=*.py'],
            capture_output=True, text=True, timeout=15
        )
        matches = []
        for line in result.stdout.strip().split('\n'):
            if not line or 'test' in line or '__pycache__' in line:
                continue
            parts = line.split(':', 2)
            if len(parts) >= 3:
                matches.append((parts[0], int(parts[1]), parts[2]))
        return matches
    except Exception:
        return []


def read_file_content(path):
    try:
        return Path(path).read_text()
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# SCANNER A: CODE_PATTERNS — scan ALL .py files for known bad patterns
# ─────────────────────────────────────────────────────────────────────────────

def check_code_patterns():
    """Scan ALL source code for patterns that cause runtime bugs.

    Checks:
    1. KLINES_FORMAT — k[N] on dict output (should be k['close'])
    2. WRONG_API — get_balance('USDT') instead of get_balance('cash')
    3. WRONG_API — get_account() fields that don't exist
    4. MISSING_GUARD — trade paths missing safety checks
    5. TYPE_MISMATCH — enum vs string comparison
    6. WRONG_DATA_SOURCE — reading from stale tables
    7. BUSINESS_LOGIC_BYPASS — safety thresholds without guard
    8. EMPTY_DATA — analyze() without null/empty check
    9. VARIABLE_SCOPE — db used before assignment
    """
    anomalies = []

    src_dir = CRYPTO_DIR / 'src'
    scripts_dir = CRYPTO_DIR / 'scripts'

    # Scan ALL .py files once, check all patterns per file
    all_files = list(src_dir.glob('*.py')) + list(src_dir.glob('agents/*.py'))
    if scripts_dir.exists():
        all_files += list(scripts_dir.glob('*.py'))

    for py_file in all_files:
        if 'test' in py_file.name or '__pycache__' in str(py_file):
            continue
        # Skip self (auto_heal.py) to avoid false positives from pattern strings
        if py_file.name == 'auto_heal.py':
            continue
        content = read_file_content(str(py_file))
        if not content:
            continue
        lines = content.split('\n')
        rel_path = py_file.relative_to(CRYPTO_DIR)

        for i, line in enumerate(lines, 1):
            # 1. KLINES_FORMAT: k[N] on dict output (but skip legitimate fallback patterns)
            if re.search(r'k\[\d\]', line) and 'get_klines' in content:
                ctx = '\n'.join(lines[max(0, i-15):i])
                # Skip if k[N] is used as fallback after isinstance(k, dict) check
                if 'isinstance(k, dict)' in line or 'isinstance(k, dict)' in ctx:
                    continue
                if 'get_klines' in ctx and 'result.append' not in ctx and 'klines = [' not in ctx:
                    anomalies.append(Anomaly("CODE", "klines_format",
                        f"{rel_path}:{i} uses k[N] on get_klines() output — should be k['close']",
                        fixable=False))
                    break  # one per file

            # 2. WRONG_API: get_balance('USDT')
            _bad_api = "get_balance(" + "'USDT'" + ")"
            if _bad_api in line and 'def ' not in line:
                anomalies.append(Anomaly("CODE", "wrong_api",
                    f"{rel_path}:{i} uses {_bad_api} — should be get_balance('cash')",
                    fixable=False))
                break

            # 3. WRONG_API: get_account() invalid fields
            if 'get_account()' in content:
                for field in ['lastPrice', 'last_price', 'currentPrice']:
                    if f"b.get('{field}'" in line or f"b['{field}']" in line:
                        anomalies.append(Anomaly("CODE", "wrong_api",
                            f"{rel_path}:{i} uses '{field}' on balance dict — get_account() only has asset/free/locked",
                            fixable=False))
                        break

        # 4. MISSING_GUARD: trade paths missing safety checks
        if py_file.name == 'trade_executor.py':
            content_tf = read_file_content(str(py_file))
            for guard, label in [
                ('_check_price_deviation', 'Price deviation'),
                ('CircuitBreaker', 'Circuit breaker'),
                ('daily_loss', 'Daily loss breaker'),
            ]:
                if guard not in content_tf:
                    anomalies.append(Anomaly("CODE", "missing_guard",
                        f"{rel_path}: missing {label} ({guard}) in execute_auto_trade",
                        fixable=False))

        # 5. TYPE_MISMATCH: signal in ("BUY", "SELL") — enum vs string
        _type_bad = "signal" + '.signal in ("BUY"'
        if _type_bad in content or ("signal" + ".signal in ('BUY'") in content:
            for i, line in enumerate(lines, 1):
                if _type_bad in line or ("signal" + ".signal in ('BUY'") in line:
                    anomalies.append(Anomaly("CODE", "type_mismatch",
                        f"{rel_path}:{i} compares SignalType enum with string",
                        fixable=False))
                    break

        # 6. WRONG_DATA_SOURCE: trade_get_recent when trade_outcomes has real data
        if 'trade_get_recent' in content and 'trade_outcomes' not in content and 'def trade_get_recent' not in content:
            anomalies.append(Anomaly("CODE", "wrong_data_source",
                f"{rel_path}: reads trades table (pnl=0) instead of trade_outcomes",
                fixable=False))

        # 7. BUSINESS_LOGIC_BYPASS: MIN threshold used as floor without ≤0 guard
        # Pattern: variable = MIN_* (forcing minimum) without checking value ≤ 0 first
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Skip definitions, comments, constants
            if stripped.startswith('#') or stripped.startswith('def ') or stripped.startswith('class '):
                continue
            # Look for: var = MIN_* or var = max(var, MIN_*) — forcing a floor
            # Skip clamps: max(MIN, min(MAX, val)) — these are range validations, not bypasses
            is_floor_assignment = (
                re.search(r'\w+\s*=\s*MIN_', stripped) or
                re.search(r'\w+\s*=\s*max\(.*MIN_', stripped)
            )
            # Skip clamp patterns: max(MIN, min(MAX, ...))
            if is_floor_assignment and 'min(' in stripped.lower() and 'max(' in stripped.lower():
                continue
            if is_floor_assignment:
                # Check if there's a ≤0 guard in the 15 lines before
                has_guard = any(
                    kw in lines[j]
                    for j in range(max(0, i-16), i-1)
                    for kw in ['<= 0', '< 0', '<=0', '<0', 'if not kelly', 'kelly <= 0']
                )
                if not has_guard:
                    anomalies.append(Anomaly("CODE", "business_logic_bypass",
                        f"{rel_path}:{i} applies MIN floor without ≤0 guard",
                        fixable=False))
                    break  # one per file

        # 8. EMPTY_DATA: analyze() without null/empty check
        if 'def analyze(' in content and py_file.name.endswith('_agent.py'):
            has_guard = any(kw in content for kw in [
                'early return for empty', 'No data available',
                'len(klines) == 0', 'klines is None', 'if not klines',
                'if not klines_1h', 'if not data', 'if not coin_data',
                'if not onchain_score', 'if not fng_value',
                'is None:', 'if not ',
            ])
            if not has_guard:
                anomalies.append(Anomaly("CODE", "empty_data_false_signal",
                    f"{rel_path}: analyze() may return high score for empty/None input",
                    fixable=False))

        # 9. VARIABLE_SCOPE: db used before assignment
        for i, line in enumerate(lines, 1):
            if 'TradeOutcomeRecorder(db=db)' in line:
                db_assigned = any(
                    ('db = get_state_db()' in lines[j] or 'db = StateDB(' in lines[j])
                    for j in range(max(0, i-11), i-1)
                )
                if not db_assigned:
                    anomalies.append(Anomaly("CODE", "variable_scope",
                        f"{rel_path}:{i} db used before assignment in TradeOutcomeRecorder",
                        fixable=False))
                    break

    return anomalies


# ─────────────────────────────────────────────────────────────────────────────
# SCANNER B: STATE_INTEGRITY — scan ALL DB tables for inconsistencies
# ─────────────────────────────────────────────────────────────────────────────

def check_state_integrity():
    """Scan ALL DB tables for orphaned records and stale data.

    Checks:
    1. ORPHANED_STATE — symbol-keyed records without portfolio entry
    2. STALE_TRAILING — trailing_stop for closed positions
    3. FALSE_BREAKER — DailyLossBreaker triggered but real loss < threshold
    4. DEAD_METRICS — exporter metrics stuck at 0
    """
    anomalies = []

    try:
        sys.path.insert(0, str(CRYPTO_DIR))
        from src.state_db import get_state_db
        db = get_state_db()
        conn = db._get_conn()

        # Get active portfolio symbols
        portfolio_syms = set(
            r[0] for r in conn.execute(
                'SELECT symbol FROM portfolio WHERE quantity > 0'
            ).fetchall()
        )

        # 1. ORPHANED_STATE: scan ALL symbol-keyed tables
        symbol_tables = ['trailing_stop', 'grid_state', 'dca_state']
        for table in symbol_tables:
            try:
                rows = conn.execute(f'SELECT symbol FROM {table}').fetchall()
                for row in rows:
                    sym = row[0]
                    if sym not in portfolio_syms:
                        def fix_orphan(t=table, s=sym):
                            c = db._get_conn()
                            c.execute(f'DELETE FROM {t} WHERE symbol = ?', (s,))
                            c.commit()
                            return f"Deleted orphaned {t} for {s}"
                        anomalies.append(Anomaly("STATE", "orphaned_record",
                            f"{table}[{sym}]: record exists but no portfolio position",
                            fixable=True, fix_fn=fix_orphan))
            except Exception as e:
                anomalies.append(Anomaly("STATE", "orphaned_scan_failed", f"{table}: {e}"))

        # 2. FALSE_BREAKER: DailyLossBreaker triggered but real loss < threshold
        try:
            from src.binance_client import BinanceClient
            from src.daily_loss_breaker import DailyLossBreaker
            client = BinanceClient(testnet=False)
            dlb = DailyLossBreaker()
            status = dlb.get_status()

            if status.get('current_tier', 0) >= 3:
                # Calculate real total
                total_value = float(client.get_free_balance('USDT'))
                _account = client.get_account()
                for b in _account.get('balances', []):
                    _asset = b['asset']
                    _qty = float(b.get('free', 0)) + float(b.get('locked', 0))
                    if _qty > 0 and _asset not in ('USDT', 'NTRN'):
                        try:
                            _price = client.get_ticker_price(symbol=f"{_asset}USDT")
                            total_value += _qty * _price
                        except Exception:
                            pass
                start_bal = status.get('daily_start_balance', 0)
                if start_bal > 0:
                    daily_pnl_pct = (total_value - start_bal) / start_bal * 100
                    if daily_pnl_pct > -3.0:
                        def fix_breaker():
                            dlb._current_tier = 0
                            dlb._daily_start_balance = 0.0
                            dlb._halt_until = None
                            dlb._save_state()
                            return f"Reset breaker. total=${total_value:.2f}"
                        anomalies.append(Anomaly("STATE", "false_breaker",
                            f"DailyLossBreaker TIER {status['current_tier']} but real PnL={daily_pnl_pct:.2f}%",
                            fixable=True, fix_fn=fix_breaker))
        except Exception as e:
            anomalies.append(Anomaly("STATE", "breaker_check_failed", str(e)))

        # 3. DEAD_METRICS: exporter metrics stuck at 0
        try:
            import urllib.request
            resp = urllib.request.urlopen("http://localhost:8000/metrics", timeout=5)
            content = resp.read().decode()
            for metric, label in [('trades_pnl_total_usdt', 'PnL'), ('trades_total', 'Trade count')]:
                vals = []
                for line in content.split('\n'):
                    if line.startswith(metric + ' ') or line.startswith(metric + '{'):
                        try:
                            vals.append(float(line.split()[-1]))
                        except ValueError:
                            pass
                if vals and all(v == 0 for v in vals):
                    anomalies.append(Anomaly("STATE", "dead_metric",
                        f"{label} ({metric}) = 0 — record_*() not called or wrong data source",
                        fixable=False))
        except Exception:
            pass  # Exporter down — not a state issue

    except Exception as e:
        anomalies.append(Anomaly("STATE", "init_failed", str(e)))

    return anomalies


# ─────────────────────────────────────────────────────────────────────────────
# SCANNER C: RUNTIME_VERIFY — execute code to catch runtime failures
# ─────────────────────────────────────────────────────────────────────────────

def check_runtime():
    """Execute critical code paths to catch runtime failures.

    Checks:
    1. API_CONNECTIVITY — Binance API reachable
    2. PRICE_DEVIATION — _check_price_deviation runs without error
    3. KLINE_FORMAT — get_klines() returns expected format
    4. POSITION_SYNC — portfolio sync works
    """
    anomalies = []

    try:
        sys.path.insert(0, str(CRYPTO_DIR))
        from src.binance_client import BinanceClient
        client = BinanceClient(testnet=False)

        # 1. API connectivity
        try:
            account = client.get_account()
            if not account or 'balances' not in account:
                anomalies.append(Anomaly("RUNTIME", "api_connectivity",
                    "Binance get_account() returned unexpected format", fixable=False))
        except Exception as e:
            anomalies.append(Anomaly("RUNTIME", "api_connectivity",
                f"Binance API unreachable: {e}", fixable=False))
            return anomalies  # Can't continue without API

        # 2. Price deviation check
        try:
            from src.trade_executor import _check_price_deviation
            for sym in ['BTCUSDT', 'ENAUSDT']:
                try:
                    klines = client.get_klines(sym, "1h", limit=14)
                    if klines:
                        price = float(klines[-1]['close'])
                        _check_price_deviation(client, sym, price)
                except KeyError as e:
                    anomalies.append(Anomaly("RUNTIME", "key_error",
                        f"{sym}: KeyError {e} in _check_price_deviation — klines format bug",
                        fixable=False))
                except Exception as e:
                    anomalies.append(Anomaly("RUNTIME", "unexpected_error",
                        f"{sym}: {type(e).__name__}: {e}", fixable=False))
        except ImportError:
            anomalies.append(Anomaly("RUNTIME", "import_failed",
                "Cannot import _check_price_deviation", fixable=False))

        # 3. Kline format verification
        try:
            klines = client.get_klines('BTCUSDT', '1h', limit=5)
            if klines:
                k = klines[0]
                if not isinstance(k, dict):
                    anomalies.append(Anomaly("RUNTIME", "kline_format",
                        "get_klines() returns list instead of dict", fixable=False))
                elif 'close' not in k:
                    anomalies.append(Anomaly("RUNTIME", "kline_format",
                        f"get_klines() dict missing 'close' key: {list(k.keys())}", fixable=False))
        except Exception as e:
            anomalies.append(Anomaly("RUNTIME", "kline_format",
                f"get_klines() failed: {e}", fixable=False))

        # 4. Position sync
        try:
            from src.portfolio import PortfolioManager
            pm = PortfolioManager(binance_client=client)
            pm.sync_from_binance(client)
        except Exception as e:
            anomalies.append(Anomaly("RUNTIME", "position_sync",
                f"Portfolio sync failed: {e}", fixable=False))

    except Exception as e:
        anomalies.append(Anomaly("RUNTIME", "init_failed", str(e)))

    return anomalies


# ─────────────────────────────────────────────────────────────────────────────
# SCANNER D: OUTPUT_ANALYSIS — scan cron outputs for failure patterns
# ─────────────────────────────────────────────────────────────────────────────

def check_outputs():
    """Scan ALL recent cron outputs for failure patterns.

    Checks:
    1. EXECUTION_FAILURE — trade/scan/optimizer failures
    2. DB_ERROR — StateDB binding errors
    3. ANOMALY_DETECTED — error keywords in outputs
    """
    anomalies = []

    for job_name, job_id in JOB_IDS.items():
        outputs = get_recent_outputs(job_id, n=3, max_age_hours=6)
        for f in outputs:
            try:
                content = f.read_text(encoding='utf-8', errors='replace')
                if "## Response" in content:
                    content = content.split("## Response", 1)[1]

                # 1. EXECUTION_FAILURE
                for keyword, pattern_name in [
                    ('Auto-execute failed', 'auto_execute_failed'),
                    ('切換失敗', 'switch_failure'),
                    ('偏離.*未執行', 'price_deviation_block'),
                ]:
                    if re.search(keyword, content):
                        # Skip expected failures (locked balance)
                        if 'insufficient balance' in content.lower() or '餘額不足' in content:
                            continue
                        sym_match = re.search(r'(\w+USDT)', content)
                        symbol = sym_match.group(1) if sym_match else "UNKNOWN"
                        anomalies.append(Anomaly("OUTPUT", pattern_name,
                            f"{job_name}/{f.name}: {symbol} — {pattern_name}",
                            fixable=False))
                        break

                # 2. DB_ERROR
                if "type 'dict' is not supported" in content:
                    anomalies.append(Anomaly("OUTPUT", "statedb_dict_binding",
                        f"{job_name}/{f.name}: StateDB dict binding error — nested dict not serialized",
                        fixable=False))

                # 3. ANOMALY_DETECTED — generic error keywords
                error_keywords = ['❌', 'failed', 'error', '異常', '失敗', 'ANOMALY']
                for line in content.split('\n'):
                    line = line.strip()
                    if any(kw in line for kw in error_keywords):
                        if any(skip in line for skip in ['✅', 'scan complete', '```']):
                            continue
                        anomalies.append(Anomaly("OUTPUT", "error_in_output",
                            f"{job_name}/{f.name}: {line[:100]}",
                            fixable=False))
                        break

            except Exception:
                pass

    return anomalies


# ─────────────────────────────────────────────────────────────────────────────
# SCANNER E: MODEL_INTEGRITY — ML model state validation
# ─────────────────────────────────────────────────────────────────────────────

def check_model_integrity():
    """Validate ML models can load and predict.

    Checks:
    1. HMM_STATE — covars shape, predict capability
    2. PREDICTION_STALE — predictions older than 24h
    """
    anomalies = []

    try:
        sys.path.insert(0, str(CRYPTO_DIR))
        import time as _time
        import numpy as np
        from src.state_db import get_state_db

        db = get_state_db()
        conn = db._get_conn()

        # 1. HMM model state
        row = conn.execute("SELECT value FROM kv WHERE key = 'hmm_model_state'").fetchone()
        if not row:
            anomalies.append(Anomaly("MODEL", "hmm_no_state",
                "HMM: no model state in DB — run --train first", fixable=False))
            return anomalies

        state = json.loads(row["value"])
        covars = np.array(state.get("covars", []))
        n_components = len(state.get("means", []))
        n_features = len(state["means"][0]) if n_components > 0 else 0

        if covars.ndim == 3:
            def fix_covars():
                covars_diag = np.array([np.diag(covars[i]) for i in range(covars.shape[0])])
                state["covars"] = covars_diag.tolist()
                c = db._get_conn()
                c.execute(
                    "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES ('hmm_model_state', ?, ?)",
                    (json.dumps(state), _time.time()),
                )
                c.commit()
                return f"Fixed: {covars.shape} → {covars_diag.shape}"
            anomalies.append(Anomaly("MODEL", "hmm_covars_shape",
                f"HMM covars shape {covars.shape} → should be ({n_components},{n_features})",
                fixable=True, fix_fn=fix_covars))
        elif covars.ndim == 2 and covars.shape == (n_components, n_features):
            try:
                from hmmlearn.hmm import GaussianHMM
                model = GaussianHMM(n_components=n_components, covariance_type="diag", n_iter=0)
                model.n_features = n_features
                model.means_ = np.array(state["means"])
                model.covars_ = covars
                model.startprob_ = np.array(state["startprob"])
                model.transmat_ = np.array(state["transmat"])
                dummy = (np.random.randn(5, n_features) - np.array(state["mean"])) / np.array(state["std"])
                model.predict_proba(dummy)
            except Exception as e:
                anomalies.append(Anomaly("MODEL", "hmm_predict_failed",
                    f"HMM predict() failed: {e}", fixable=False))

        # 2. Prediction staleness
        row_pred = conn.execute("SELECT value FROM kv WHERE key = 'hmm_regime'").fetchone()
        if row_pred:
            pred_data = json.loads(row_pred["value"])
            age_hours = (_time.time() - pred_data.get("timestamp", 0)) / 3600
            if age_hours > 24:
                anomalies.append(Anomaly("MODEL", "prediction_stale",
                    f"HMM prediction {age_hours:.0f}h old (>24h stale)", fixable=False))

    except ImportError as e:
        anomalies.append(Anomaly("MODEL", "import_failed", f"Import failed: {e}", fixable=False))
    except Exception as e:
        anomalies.append(Anomaly("MODEL", "check_crashed", f"Check crashed: {e}", fixable=False))

    return anomalies


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def check_regression_tests() -> list:
    """Scanner F: Run regression tests to catch known bug patterns."""
    anomalies = []
    test_file = CRYPTO_DIR / "tests" / "test_regression.py"
    if not test_file.exists():
        return anomalies

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file), "-v", "--tb=short", "-x", "-m", "not slow"],
            cwd=str(CRYPTO_DIR),
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            # Parse failed tests
            failed_lines = [l for l in result.stdout.split('\n') if 'FAILED' in l]
            for line in failed_lines:
                parts = line.strip().split('::')
                test_name = parts[-1].split(' ')[0] if len(parts) > 1 else line.strip()
                anomalies.append(Anomaly(
                    source="REGRESSION_TESTS",
                    detail=f"測試失敗: {test_name}",
                    pattern="regression_test_failure",
                    fixable=False,
                    fix_fn=lambda: "檢查 tests/test_regression.py 並修復對應代碼",
                ))
    except Exception as e:
        anomalies.append(Anomaly(
            source="REGRESSION_TESTS",
            detail=f"測試執行失敗: {e}",
            pattern="regression_test_error",
            fixable=False,
        ))

    return anomalies


def main():
    verbose = "--verbose" in sys.argv
    dry_run = "--report" in sys.argv

    all_anomalies = []

    # Scanner A: Code patterns (source code analysis)
    all_anomalies.extend(check_code_patterns())

    # Scanner B: State integrity (DB consistency)
    all_anomalies.extend(check_state_integrity())

    # Scanner C: Runtime verification (execute code)
    all_anomalies.extend(check_runtime())

    # Scanner D: Output analysis (cron logs)
    all_anomalies.extend(check_outputs())

    # Scanner E: Model integrity (ML models)
    all_anomalies.extend(check_model_integrity())

    # Scanner F: Regression tests (known bug patterns)
    all_anomalies.extend(check_regression_tests())

    # Attempt fixes for fixable anomalies
    for anomaly in [a for a in all_anomalies if a.fixable]:
        anomaly.attempt_fix(dry_run=dry_run)

    # Deduplicate by pattern+detail
    seen = set()
    unique = []
    for a in all_anomalies:
        key = f"{a.pattern}:{a.detail}"
        if key not in seen:
            seen.add(key)
            unique.append(a)
    all_anomalies = unique

    if not all_anomalies:
        if verbose:
            print(f"✅ Auto-heal: All clear ({datetime.now().strftime('%H:%M')})")
        return

    fixed_count = sum(1 for a in all_anomalies if a.fixed)
    fixable_count = sum(1 for a in all_anomalies if a.fixable and not a.fixed)
    manual_count = sum(1 for a in all_anomalies if not a.fixable)

    print(f"🔍 Auto-heal 掃描 ({datetime.now().strftime('%H:%M')})")
    print(f"   發現 {len(all_anomalies)} 個異常 | ✅修復 {fixed_count} | 🔧待修 {fixable_count} | ⚠️需手動 {manual_count}")
    print()

    # Group by source
    by_source = {}
    for a in all_anomalies:
        by_source.setdefault(a.source, []).append(a)

    for source, anomalies in sorted(by_source.items()):
        print(f"  [{source}]")
        for a in anomalies:
            print(f"    {a}")
        print()


if __name__ == "__main__":
    main()
