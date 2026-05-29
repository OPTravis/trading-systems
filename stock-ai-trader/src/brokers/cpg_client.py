"""Client Portal Gateway (CPG) REST API client for querying IBKR live account.

CPG runs on localhost:5000 (HTTPS, self-signed cert). User must login via
browser first to authenticate. Session lasts ~24 hours.

API docs: https://interactivebrokers.github.io/cpwebapi/
"""

import logging
import os
import requests
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# Disable SSL warnings for self-signed cert
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class CPGClient:
    """REST API client for IBKR Client Portal Gateway."""

    def __init__(self, base_url: str = "https://localhost:5000"):
        self._base = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.verify = False
        self._session.headers.update({"Accept": "application/json"})

    def _get(self, path: str) -> Optional[Any]:
        """GET request to CPG API. Returns parsed JSON or None on error."""
        url = f"{self._base}{path}"
        try:
            resp = self._session.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 302:
                logger.error("CPG session expired — need browser re-login")
                return None
            else:
                logger.warning("CPG GET %s returned %d: %s",
                               path, resp.status_code, resp.text[:200])
                return None
        except requests.ConnectionError:
            logger.error("CPG not running at %s", self._base)
            return None
        except Exception as e:
            logger.error("CPG request failed: %s", e)
            return None

    def is_session_active(self) -> bool:
        """Check if CPG session is still authenticated."""
        data = self._get("/v1/api/iserver/accounts")
        return data is not None and "accounts" in data

    def get_accounts(self) -> List[str]:
        """List available account IDs."""
        data = self._get("/v1/api/iserver/accounts")
        if data and "accounts" in data:
            return data["accounts"]
        return []

    def get_account_summary(self, account_id: str) -> Optional[Dict[str, float]]:
        """Get account balances.

        Returns dict with keys: total_cash, net_liquidation, buying_power,
        available_funds, gross_position_value, unrealized_pnl, currency.
        """
        data = self._get(f"/v1/api/portfolio/{account_id}/summary")
        if not data:
            return None

        result = {"account_id": account_id, "currency": "HKD"}
        field_map = {
            "totalcashvalue": "total_cash",
            "netliquidation": "net_liquidation",
            "buyingpower": "buying_power",
            "availablefunds": "available_funds",
            "grosspositionvalue": "gross_position_value",
            "unrealizedpnl": "unrealized_pnl",
            "excessliquidity": "excess_liquidity",
            "equitywithloanvalue": "equity_with_loan",
        }
        for cpg_key, our_key in field_map.items():
            entry = data.get(cpg_key, {})
            if entry and not entry.get("isNull", True):
                result[our_key] = entry.get("amount", 0.0)
                cur = entry.get("currency")
                if cur:
                    result["currency"] = cur
        return result

    def get_positions(self, account_id: str) -> List[Dict[str, Any]]:
        """Get portfolio positions.

        Returns list of dicts with: symbol, quantity, avg_cost, market_value, currency.
        """
        data = self._get(f"/v1/api/portfolio/{account_id}/positions/0")
        if not data:
            return []

        positions = []
        for p in data:
            qty = p.get("position", 0)
            if abs(qty) < 0.001:
                continue
            positions.append({
                "symbol": p.get("contractDesc", p.get("ticker", "?")),
                "con_id": p.get("conid"),
                "quantity": qty,
                "avg_cost": p.get("avgCost", 0),
                "market_value": p.get("marketValue", 0),
                "unrealized_pnl": p.get("unrealizedPnL", 0),
                "currency": p.get("currency", "USD"),
            })
        return positions

    def get_live_status(self, account_id: Optional[str] = None) -> Optional[Dict]:
        """Convenience: get full status (summary + positions) for live account.

        Account ID from CPG_ACCOUNT_ID env var or parameter.
        Returns dict with 'summary' and 'positions' keys, or None if session expired.
        """
        if account_id is None:
            account_id = os.environ.get("CPG_ACCOUNT_ID", "")
        if not account_id:
            logger.error("CPG_ACCOUNT_ID not set — cannot query live account")
            return None
        summary = self.get_account_summary(account_id)
        if summary is None:
            return None
        positions = self.get_positions(account_id)
        return {"summary": summary, "positions": positions}
