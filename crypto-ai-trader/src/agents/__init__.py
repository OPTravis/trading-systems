"""
Specialist agents for market scoring.

Each agent wraps a subset of the MarketScanner factor calculations
into an independently testable component.
"""

from .base import SpecialistResult
from .market_sentiment_agent import MarketSentimentAgent
from .onchain_agent import OnChainAgent
from .prepump_agent import PrePumpAgent
from .sentiment_agent import SentimentAgent
from .technical_agent import TechnicalAgent
from .trend_agent import TrendAgent
from .volume_agent import VolumeAgent

__all__ = [
    "SpecialistResult",
    "TechnicalAgent",
    "TrendAgent",
    "VolumeAgent",
    "SentimentAgent",
    "OnChainAgent",
    "MarketSentimentAgent",
    "PrePumpAgent",
]
