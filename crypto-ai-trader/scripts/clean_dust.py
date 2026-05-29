#!/usr/bin/env python3
"""
Auto-clean dust positions (< $1) by converting to BNB.
Binance dust transfer API: 1 request/hour hard limit.
Run via cron: every 6 hours (script stays silent when nothing to do).

Exit codes:
  0 = success (converted or nothing to do)
  1 = error
stdout is delivered in no_agent mode — only output when there's something to report.
"""
import sys
import os
import json
import logging
from pathlib import Path

# Ensure project root is in sys.path (works from any directory)
_project_root = Path.home() / 'crypto-ai-trader'
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Ensure venv site-packages is in sys.path (for binance, dotenv, etc.)
import glob as _glob
_venv_site = str(Path.home() / 'crypto-ai-trader' / '.venv' / 'lib' / 'python*' / 'site-packages')
_matches = _glob.glob(_venv_site)
if _matches:
    _site = _matches[-1]  # latest python version
    if _site not in sys.path:
        sys.path.insert(0, _site)

# Load .env from project root
_env_file = _project_root / '.env'
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=False)

from src.binance_client import BinanceClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

DUST_THRESHOLD = 1.0  # USD
SKIP_ASSETS = {'USDT', 'USDC', 'BUSD', 'FDUSD', 'DAI', 'TUSD', 'USDP', 'EUR', 'RLUSD', 'NTRN', 'BNB'}


def get_dust_candidates(client: BinanceClient) -> list:
    """Find dust positions with free balance > 0 and USDT pair."""
    acct = client.get_account()
    if not acct or 'balances' not in acct:
        logger.error("Failed to fetch account balances (API error or network issue)")
        return []
    candidates = []

    for b in acct['balances']:
        asset = b['asset']
        if asset in SKIP_ASSETS:
            continue
        free = float(b['free'])
        if free <= 0:
            continue

        try:
            price = client.get_ticker_price(symbol=f'{asset}USDT')
            value = free * price
            if 0 < value < DUST_THRESHOLD:
                candidates.append({
                    'asset': asset,
                    'qty': free,
                    'price': price,
                    'value': round(value, 4)
                })
        except Exception:
            pass  # No USDT pair

    return sorted(candidates, key=lambda x: -x['value'])


def convert_all_dust(client: BinanceClient, assets: list) -> dict:
    """Convert all dust assets in one API call (hourly limit)."""
    if not assets:
        return {'converted': [], 'total_value': 0}

    asset_list = [a['asset'] for a in assets]
    logger.info(f"Converting {len(asset_list)} assets: {asset_list}")

    try:
        result = client.transfer_dust(asset=asset_list)
        converted = []
        for r in result.get('transferResult', []):
            converted.append({
                'asset': r.get('fromAsset'),
                'amount': r.get('transferedAmount'),
                'charge': r.get('serviceChargeAmount'),
            })
        total = float(result.get('totalTransfered', 0))
        return {'converted': converted, 'total_value': round(total, 4)}
    except Exception as e:
        err_str = str(e)
        if '32110' in err_str or 'once within 1 hour' in err_str:
            logger.warning("Rate limited (1 req/hour) — will retry next run")
            return {'rate_limited': True, 'converted': [], 'total_value': 0}
        logger.error(f"Dust transfer failed: {e}")
        return {'error': err_str, 'converted': [], 'total_value': 0}


def clean_state_db_dust():
    """Remove dust positions from StateDB that no longer exist on Binance."""
    try:
        from src.state_db import get_state_db
        db = get_state_db()
        positions = db.portfolio_get_all()
        removed = []
        for sym, pos in positions.items():
            qty = float(pos.get('quantity', 0))
            if qty > 0:
                asset = sym.replace('USDT', '')
                # Check if this is a tiny amount (dust)
                try:
                    from src.binance_client import BinanceClient
                    c = BinanceClient()
                    acct = c.get_account()
                    for b in acct['balances']:
                        if b['asset'] == asset and float(b['free']) + float(b['locked']) <= 0:
                            db.portfolio_remove(sym)
                            removed.append(sym)
                            break
                except Exception:
                    pass
        return removed
    except Exception as e:
        logger.warning(f"StateDB cleanup failed: {e}")
        return []


def main():
    client = BinanceClient()

    # Step 1: Find dust
    dust = get_dust_candidates(client)
    if not dust:
        return  # Silent — no output

    total_value = sum(d['value'] for d in dust)

    # Step 2: Convert
    result = convert_all_dust(client, dust)

    if result.get('rate_limited'):
        print(f"⏳ Rate limited. {len(dust)} assets (${total_value:.2f}) pending. Retry in ~1h.")
        return

    if result.get('error'):
        print(f"❌ Dust conversion failed: {result['error']}")
        sys.exit(1)

    # Step 3: Report
    if result['converted']:
        print(f"🧹 Converted {len(result['converted'])} dust assets → BNB")
        for c in result['converted']:
            print(f"  {c['asset']}: {c['amount']} (fee: {c['charge']})")
        print(f"Total BNB: ~${result['total_value']:.4f}")

        # Step 4: Clean StateDB
        removed = clean_state_db_dust()
        if removed:
            print(f"🗄️ Removed from StateDB: {', '.join(removed)}")
    else:
        remaining = get_dust_candidates(client)
        if remaining:
            print(f"⚠️ {len(remaining)} dust positions remain: {', '.join(d['asset'] for d in remaining[:5])}")


if __name__ == '__main__':
    main()
