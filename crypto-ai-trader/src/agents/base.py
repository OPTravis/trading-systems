"""
Base types for specialist agent modules.
"""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class SpecialistResult:
    """Result returned by every specialist agent.

    Attributes:
        score: Numeric score in the 0-100 range.
        signals: Human-readable signal descriptions.
        data: Arbitrary sub-scores and diagnostics for downstream consumers.
        confidence: Qualitative confidence label ('high', 'medium', 'low').
    """
    score: float = 0.0
    signals: List[str] = field(default_factory=list)
    data: Dict = field(default_factory=dict)
    confidence: str = 'medium'
