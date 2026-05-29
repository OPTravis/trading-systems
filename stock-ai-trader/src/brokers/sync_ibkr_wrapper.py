"""Synchronous wrapper around ib_insync IB for use with StockDataFeed and other sync code."""
from ib_insync import IB, Stock, Forex, util
from typing import Optional, Dict, Any, List
import logging

logger = logging.getLogger(__name__)


class SyncIBKRWrapper:
    """Synchronous wrapper around ib_insync.IB that provides
    sync versions of the methods StockDataFeed/StockResearcher need."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4001, client_id: int = 1):
        self._ib = IB()
        self._host = host
        self._port = port
        self._client_id = client_id
        self._connected = False

    def connect(self) -> None:
        if not self._connected:
            self._ib.connect(self._host, self._port, clientId=self._client_id, timeout=15)
            self._connected = True
            # Request delayed market data (type 3) since we don't have live subscription
            self._ib.reqMarketDataType(3)

    def disconnect(self) -> None:
        if self._connected:
            self._ib.disconnect()
            self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self._ib.isConnected()

    def get_market_data(self, symbol: str) -> Dict[str, Any]:
        """Get real-time quote for a symbol. Returns dict with price, volume, etc."""
        try:
            contract = Stock(symbol, 'SMART', 'USD')
            self._ib.qualifyContracts(contract)
            [ticker] = self._ib.reqTickers(contract)

            price = ticker.marketPrice()
            if price != price or price == 0:  # NaN or zero (market closed)
                price = ticker.close if ticker.close == ticker.close else 0

            return {
                "symbol": symbol,
                "price": price,
                "bid": ticker.bid,
                "ask": ticker.ask,
                "volume": ticker.volume,
                "high": ticker.high,
                "low": ticker.low,
                "close": ticker.close,
                "open": ticker.open,
                "change": ticker.close - ticker.open if ticker.close == ticker.close and ticker.open == ticker.open else 0,
                "change_pct": ((ticker.close / ticker.open) - 1) * 100 if ticker.open and ticker.open == ticker.open else 0,
            }
        except Exception as e:
            logger.warning("get_market_data failed for %s: %s", symbol, e)
            return {"symbol": symbol, "price": 0, "volume": 0}

    def get_historical_bars(self, symbol: str, duration: str = "1 M", bar_size: str = "1 day") -> list:
        """Get historical bars. Returns list of OHLCV dicts."""
        try:
            contract = Stock(symbol, 'SMART', 'USD')
            self._ib.qualifyContracts(contract)
            bars = self._ib.reqHistoricalData(
                contract,
                endDateTime='',
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow='TRADES',
                useRTH=True,
            )
            return [
                {
                    "date": str(bar.date),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": int(bar.volume),
                }
                for bar in bars
            ]
        except Exception as e:
            logger.warning("get_historical_bars failed for %s: %s", symbol, e)
            return []

    def get_account(self):
        """Get account summary."""
        from src.brokers.broker_protocol import AccountSummary
        acct = self._ib.accountSummary()
        fields = {}
        for item in acct:
            tag = item.tag
            try:
                val = float(item.value)
            except (ValueError, TypeError):
                continue  # Skip non-numeric fields like 'INDIVIDUAL'
            if tag == "TotalCashValue":
                fields["total_cash"] = val
                fields["currency"] = item.currency
            elif tag == "NetLiquidation":
                fields["net_liquidation"] = val
            elif tag == "AvailableFunds":
                fields["available_funds"] = val
            elif tag == "BuyingPower":
                fields["buying_power"] = val
            elif tag == "GrossPositionValue":
                fields["gross_position_value"] = val
            elif tag == "UnrealizedPnL":
                fields["unrealized_pnl"] = val

        accounts = self._ib.managedAccounts()
        return AccountSummary(
            account_id=accounts[0] if accounts else "unknown",
            net_liquidation=fields.get("net_liquidation", 0),
            total_cash=fields.get("total_cash", 0),
            available_funds=fields.get("available_funds", 0),
            buying_power=fields.get("buying_power", 0),
            gross_position_value=fields.get("gross_position_value", 0),
            unrealized_pnl=fields.get("unrealized_pnl", 0),
            currency=fields.get("currency", "HKD"),
        )

    def get_portfolio(self) -> list:
        """Get portfolio positions."""
        from src.brokers.broker_protocol import Position as BPPosition, Contract as BPContract
        positions = self._ib.positions()
        result = []
        for p in positions:
            result.append(BPPosition(
                contract=BPContract(
                    symbol=p.contract.symbol,
                    exchange=p.contract.exchange or "SMART",
                    currency=p.contract.currency or "USD",
                    contract_id=p.contract.conId,
                ),
                quantity=p.position,
                avg_cost=p.avgCost,
                market_value=p.position * p.avgCost,
                unrealized_pnl=0,
            ))
        return result
