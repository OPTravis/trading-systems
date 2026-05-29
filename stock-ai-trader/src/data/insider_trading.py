"""
Insider trading data from SEC Form 4 filings via FMP API.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"


class InsiderTrading:
    """
    SEC Form 4 insider transaction data from Financial Modeling Prep.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("FMP_API_KEY", "")
        self._cache: dict = {}

    def _get(self, endpoint: str, params: Optional[dict] = None) -> any:
        p = params or {}
        p["apikey"] = self.api_key
        resp = requests.get(f"{FMP_BASE_URL}/{endpoint}", params=p, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_insider_trades(self, symbol: str, days: int = 90) -> list[dict]:
        """
        Get recent insider trades for a symbol.

        Args:
            symbol: Ticker symbol.
            days: Look-back window in days.

        Returns:
            List of dicts with keys: date, insider_name, title, transaction_type,
            shares, price, value, security_name.
        """
        cache_key = f"insider|{symbol}|{days}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        logger.info("Fetching insider trades for %s (last %d days)", symbol, days)
        data = self._get(f"insider-trading/{symbol}", {"limit": 100})

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        trades = []
        for item in data:
            try:
                trade_date_str = item.get("transactionDate", item.get("date", ""))
                if not trade_date_str:
                    continue
                trade_date = datetime.strptime(trade_date_str[:10], "%Y-%m-%d")
                if trade_date < cutoff:
                    continue

                shares = int(item.get("securitiesTransacted", 0) or 0)
                price = float(item.get("price", 0) or 0)

                trades.append({
                    "date": trade_date_str,
                    "insider_name": item.get("reportingName", item.get("insiderName", "")),
                    "title": item.get("typeOfOwner", item.get("title", "")),
                    "transaction_type": item.get("transactionType", ""),
                    "shares": shares,
                    "price": price,
                    "value": shares * price,
                    "security_name": item.get("securityName", ""),
                    "acquisition": item.get("acquisitionOrDisposition", ""),
                })
            except (ValueError, TypeError) as exc:
                logger.debug("Skipping insider trade entry: %s", exc)
                continue

        self._cache[cache_key] = trades
        return trades

    def get_insider_summary(self, symbol: str, days: int = 90) -> dict:
        """
        Get a summary of insider activity.

        Returns:
            Dict with keys: total_buys, total_sells, net_shares, total_value,
            num_transactions.
        """
        trades = self.get_insider_trades(symbol, days)

        BUY_TYPES = {"P-PURCHASE", "PURCHASE", "P"}
        SELL_TYPES = {"S-SALE", "SALE", "S"}

        buys = [t for t in trades if t.get("transaction_type", "").upper() in BUY_TYPES]
        sells = [t for t in trades if t.get("transaction_type", "").upper() in SELL_TYPES]

        total_buy_shares = sum(t["shares"] for t in buys)
        total_sell_shares = sum(t["shares"] for t in sells)
        total_value = sum(t["value"] for t in trades)

        return {
            "total_buys": len(buys),
            "total_sells": len(sells),
            "net_shares": total_buy_shares - total_sell_shares,
            "total_value": total_value,
            "num_transactions": len(trades),
        }
