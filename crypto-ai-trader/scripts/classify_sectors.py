#!/usr/bin/env python3
"""Batch-classify missing sector symbols using DeepSeek LLM."""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from sector_classifier import SectorClassifier

sc = SectorClassifier()

# Check env
api_key = os.environ.get("DEEPSEEK_API_KEY", "")
print(f"DEEPSEEK_API_KEY: {'set (' + api_key[:8] + '...)' if api_key else 'MISSING'}")

# All symbols from scan watchlist
missing = []
for sym in list(sc.BASE_SECTORS.keys()):
    pass  # skip

# Find all NOT_FOUND symbols
all_symbols = ['ONDO', 'PENGU', 'JTO', 'EUR', 'KSM', 'CFG', 'LINK', 'WLFI', 'RLUSD',
               'AR', 'PSG', 'BIO', 'D', 'TON', 'PENDLE', 'BANANAS31', 'ZBT',
               'FARTCOIN', 'AI16Z', 'AIXBT', 'GRIFT', 'ZEC', 'DASH', 'ICP', 'AVAX',
               'MNT', 'GAS', 'CRO', 'MORPHO', 'SAFE', 'LQTY', 'KMNO', 'OMNI',
               'TRUMP', 'STX', 'BMT', 'GRIFFAIN', 'SPEC', 'COOKIE']

missing = [s for s in all_symbols if s not in sc._symbol_to_sector]
print(f"\nClassifying {len(missing)} missing symbols...")

for sym in missing:
    sector = sc.get_sector(sym)
    print(f"  {sym:12s} -> {sector}")
    time.sleep(0.5)  # Rate limit

sc._save_classifications()
print(f"\nSaved {len(sc._classifications)} classifications")

# Print all
print("\n=== All Classifications ===")
for sym, sector in sorted(sc._classifications.items()):
    print(f"  {sym:12s} -> {sector}")
