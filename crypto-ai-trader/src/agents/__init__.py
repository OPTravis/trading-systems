"""
Specialist agents for market scoring.

Each agent wraps a subset of the MarketScanner factor calculations
into an independently testable component.
"""

from .base import SpecialistResult
from .technical_agent import TechnicalAgent
from .trend_agent import TrendAgent
from .volume_agent import VolumeAgent
from .sentiment_agent import SentimentAgent
from .onchain_agent import OnChainAgent
from .market_sentiment_agent import MarketSentimentAgent
from .prepump_agent import PrePumpAgent

__all__ = [
    'SpecialistResult',
    'TechnicalAgent',
    'TrendAgent',
    'VolumeAgent',
    'SentimentAgent',
    'OnChainAgent',
    'MarketSentimentAgent',
    'PrePumpAgent',
]
