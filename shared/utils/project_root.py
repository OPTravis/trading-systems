"""
Project root utilities — shared helpers for locating the project root directory.
"""
from pathlib import Path


def get_project_root() -> Path:
    """Return the project root directory.

    Searches upward from this file's location until it finds a directory
    containing a 'data/' subdirectory or a '.git' directory. Falls back
    to the parent of this file's parent if nothing is found.
    """
    current = Path(__file__).resolve().parent.parent
    for parent in [current, *current.parents]:
        if (parent / "data").is_dir() or (parent / ".git").is_dir():
            return parent
    # Fallback: two levels up from utils/
    return Path(__file__).resolve().parent.parent.parent
