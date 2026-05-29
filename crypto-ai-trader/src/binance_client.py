"""
Binance SPOT API Client — Proxy Module

Set USE_CCXT=1 in .env to use the ccxt-based client (better retry, rate limiting, endpoint fallback).
Default: python-binance SDK (legacy).

All existing code does `from src.binance_client import BinanceClient` —
this module transparently returns the right implementation.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env early so USE_CCXT is available
_project_root = Path(__file__).parent.parent
_env_file = _project_root / ".env"
if _env_file.exists():
    load_dotenv(_env_file, override=False)

_use_ccxt = os.environ.get("USE_CCXT", "").strip().lower() in ("1", "true", "yes")

if _use_ccxt:
    from src.ccxt_client import BinanceClient  # noqa: F401
else:
    from src._binance_sdk_client import BinanceClient  # noqa: F401
