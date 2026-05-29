#!/home/travis/crypto-ai-trader/.venv/bin/python3
"""
Self-healing diagnostic for crypto cron outputs.
Scans recent crypto-scan outputs for repeating error patterns.
When the same error appears 3+ consecutive times, investigates root cause.

Usage: python3 self_heal_check.py [--fix]
  --fix  Auto-fix detected issues (without flag, report only)
"""

import os
import sys
import re
import glob
from pathlib import Path
from datetime import datetime
from collections import Counter

CRON_OUTPUT_DIR = Path.home() / ".hermes" / "cron" / "output"
CRYPTO_SCAN_JOB_ID = "28cda1d17ae5"


def get_recent_scan_outputs(n=10):
    """Get the N most recent crypto-scan output files."""
    scan_dir = CRON_OUTPUT_DIR / CRYPTO_SCAN_JOB_ID
    if not scan_dir.exists():
        return []
    files = sorted(scan_dir.glob("*.md"), reverse=True)
    return files[:n]


def extract_errors_from_output(filepath):
    """Extract error messages from a scan output file (Response section only)."""
    errors = []
    try:
        content = filepath.read_text()
        # Only look at the Response section (after "## Response")
        if "## Response" in content:
            content = content.split("## Response", 1)[1]
        lines = content.split('\n')
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('**'):
                continue
            # Match error patterns in actual output
            if any(pattern in line.lower() for pattern in [
                'price anomaly', 'price_deviation', 'anomaly',
                '偏離', '未執行', '掛單失敗', 'auto-execute failed',
                '❌', 'blocked by', '攔截',
            ]):
                # Exclude prompt template lines and success markers
                if any(skip in line for skip in ['✅', 'scan complete', 'cron-scan', '**', '```']):
                    continue
                errors.append(line)
    except Exception:
        pass
    return errors


def detect_repeating_errors(outputs):
    """Detect if the same error appears in 3+ consecutive outputs."""
    all_errors = []
    for f in outputs:
        errors = extract_errors_from_output(f)
        all_errors.append((f.name, errors))

    # Find error messages that appear in 3+ outputs
    error_counts = Counter()
    error_examples = {}
    for fname, errors in all_errors:
        for err in errors:
            # Normalize: remove prices, timestamps, specific values
            normalized = re.sub(r'\$[\d.]+', '$XXX', err)
            normalized = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}', 'TIME', normalized)
            error_counts[normalized] += 1
            if normalized not in error_examples:
                error_examples[normalized] = err

    return {k: (v, error_examples[k]) for k, v in error_counts.items() if v >= 3}


def investigate_price_deviation():
    """Investigate if _check_price_deviation is working correctly."""
    sys.path.insert(0, str(Path.home() / "crypto-ai-trader"))
    sys.path.insert(0, str(Path.home() / "crypto-ai-trader" / "src"))

    try:
        from src.binance_client import BinanceClient
        from src.trade_executor import _check_price_deviation
        import numpy as np

        client = BinanceClient(testnet=False)
        test_symbols = ['BTCUSDT', 'PENGUUSDT', 'BIOUSDT', 'ORDIUSDT']

        results = []
        for sym in test_symbols:
            try:
                klines = client.get_klines(sym, "1h", limit=14)
                if not klines:
                    results.append((sym, "SKIP", "no klines data"))
                    continue

                k = klines[0]
                if isinstance(k, dict):
                    fmt = "dict"
                elif isinstance(k, list):
                    fmt = "list"
                else:
                    fmt = f"unknown({type(k).__name__})"

                closes = [float(k['close']) if isinstance(k, dict) else float(k[4]) for k in klines]
                mean = np.mean(closes)
                std = np.std(closes)
                price = closes[-1]
                z = abs(price - mean) / std if std > 0 else 0

                check_result = _check_price_deviation(client, sym, price)
                status = "PASS" if check_result else "BLOCKED"

                results.append((sym, status, f"format={fmt} z={z:.1f} price=${price:.4f} mean=${mean:.4f}"))
            except Exception as e:
                results.append((sym, "ERROR", str(e)))

        return results
    except Exception as e:
        return [("IMPORT", "ERROR", str(e))]


def auto_fix_price_deviation():
    """Auto-fix klines dict/list format mismatch in trade_executor.py."""
    trade_executor_path = Path.home() / "crypto-ai-trader" / "src" / "trade_executor.py"
    if not trade_executor_path.exists():
        return False, "trade_executor.py not found"

    content = trade_executor_path.read_text()

    # Check if already fixed
    if "k['close']" in content and "k[4]" not in content.split("def _check_price_deviation")[1].split("def ")[0]:
        return True, "Already fixed — uses k['close']"

    # Check if broken
    func_start = content.find("def _check_price_deviation")
    if func_start == -1:
        return False, "Function not found"

    func_end = content.find("\ndef ", func_start + 1)
    func_body = content[func_start:func_end] if func_end != -1 else content[func_start:]

    if "float(k[4])" in func_body:
        # Fix it
        new_content = content[:func_start]
        fixed_body = func_body.replace("float(k[4])", "float(k['close'])")
        new_content += fixed_body
        if func_end != -1:
            new_content += content[func_end:]
        trade_executor_path.write_text(new_content)
        return True, "Fixed: k[4] → k['close']"

    return True, "No fix needed (no k[4] found)"


def main():
    # Default: no_agent + fix mode (for cron). Override with --verbose or --report-only
    fix_mode = "--report-only" not in sys.argv
    no_agent = "--verbose" not in sys.argv

    if not no_agent:
        print("=== Crypto Self-Healing Diagnostic ===")
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Mode: {'AUTO-FIX' if fix_mode else 'REPORT ONLY'}")
        print()

    # Step 1: Scan recent outputs
    outputs = get_recent_scan_outputs(10)
    if not outputs:
        if not no_agent:
            print("No recent crypto-scan outputs found.")
        return

    if not no_agent:
        print(f"Scanned {len(outputs)} recent outputs:")
        for f in outputs[:3]:
            print(f"  {f.name}")
        print()

    # Step 2: Detect repeating errors
    repeating = detect_repeating_errors(outputs)
    if not repeating:
        if not no_agent:
            print("✅ No repeating error patterns detected.")
        return

    print(f"⚠️  Found {len(repeating)} repeating error pattern(s):")
    for pattern, (count, example) in repeating.items():
        print(f"  [{count}x] {example[:100]}")
    print()

    # Step 3: Investigate root cause
    if any('price anomaly' in p.lower() or '偏離' in p or 'deviation' in p.lower() for p in repeating):
        print("--- Investigating _check_price_deviation ---")
        results = investigate_price_deviation()
        for sym, status, detail in results:
            print(f"  {sym}: {status} — {detail}")

        # Check if there's a format mismatch
        has_bug = any("format=list" in detail and "BLOCKED" in status for sym, status, detail in results)
        has_blocked = any(status == "BLOCKED" for sym, status, detail in results)
        has_error = any(status == "ERROR" for sym, status, detail in results)

        if has_bug or has_error:
            print()
            print("🐛 Root cause: klines format mismatch or API error")
            if fix_mode:
                success, msg = auto_fix_price_deviation()
                print(f"  Auto-fix: {'✅' if success else '❌'} {msg}")
                if success:
                    print("  → Verifying fix...")
                    verify = investigate_price_deviation()
                    for sym, status, detail in verify:
                        print(f"    {sym}: {status} — {detail}")
            else:
                print("  → Run with --fix to auto-repair")
        elif has_blocked:
            print()
            print("ℹ️  Price deviation blocks may be legitimate (volatile market).")
            print("  Check if ALL symbols are blocked (system bug) vs specific ones (market signal).")
        else:
            print()
            print("✅ _check_price_deviation working correctly.")
    print()
    print("=== Done ===")


if __name__ == "__main__":
    main()
