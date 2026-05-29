"""
Research modules — LLM-based stock research and macro analysis.

Provides:
- StockResearcher: Deep research on individual stocks using dual LLM verification
- MacroAnalyzer: Macro-economic state analysis (Fed rate, VIX, yield curve, etc.)
"""

from .stock_researcher import StockResearcher, ResearchReport
from .macro_analyzer import MacroAnalyzer, MacroState

__all__ = ["StockResearcher", "ResearchReport", "MacroAnalyzer", "MacroState"]
