"""
Broker client implementations for the stock research & analysis tool.

Provides:
- BrokerProtocol: Abstract interface for all broker clients
- IBKRClient: Interactive Brokers implementation via ib_async
- PaperClient: Simulated market data client (analysis only, no order execution)
- SyncIBKRWrapper: Synchronous wrapper for CLI use
- CPGClient: Read-only live account status via IBKR Client Portal Gateway
"""

from .broker_protocol import BrokerProtocol
from .ibkr_client import IBKRClient
from .paper_client import PaperClient

__all__ = ["BrokerProtocol", "IBKRClient", "PaperClient"]
