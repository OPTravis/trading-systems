"""
Broker client implementations for the stock AI trading system.

Provides:
- BrokerProtocol: Abstract interface for all broker clients
- IBKRClient: Interactive Brokers implementation via ib_async
- PaperClient: Paper trading simulation client
- AlpacaClient: Alpaca Markets backup client (stub)
"""

from .broker_protocol import BrokerProtocol
from .ibkr_client import IBKRClient
from .paper_client import PaperClient
from .alpaca_client import AlpacaClient

__all__ = ["BrokerProtocol", "IBKRClient", "PaperClient", "AlpacaClient"]
