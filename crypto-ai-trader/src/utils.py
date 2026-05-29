"""
Project utilities - shared helpers for crypto-ai-trader.
"""
from pathlib import Path


def get_project_root() -> Path:
    """Return the project root directory (where src/ and data/ live).
    
    Works regardless of where the script is run from.
    """
    # This file is in src/, so parent is project root
    return Path(__file__).parent.parent
