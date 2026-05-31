"""
Centralized secrets management for crypto-ai-trader.
All secrets file paths and loading logic in one place.
"""

import os

SECRETS_DIR = os.path.join(os.path.expanduser("~"), ".config", "crypto-ai-trader")
CRYPTO_SECRETS = os.path.join(SECRETS_DIR, "crypto-secrets.env")
GENERAL_SECRETS = os.path.join(SECRETS_DIR, "secrets.env")


def check_file_permissions(filepath: str) -> None:
    """Raise if secrets file has overly permissive permissions."""
    mode = os.stat(filepath).st_mode
    if mode & 0o077:  # group or world readable
        raise PermissionError(
            f"CRITICAL: Secrets file {filepath} has overly permissive permissions "
            f"({oct(mode & 0o777)}). Fix: chmod 600 {filepath}"
        )


def load_secret_file(path: str) -> dict:
    """Load key=value pairs from a secrets file.

    Supports 'export K=V' and 'K=V' lines. Skips blanks and comments.
    """
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return {}

    check_file_permissions(path)

    secrets = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                if line.startswith("export "):
                    line = line[7:]
                k, v = line.split("=", 1)
                secrets[k.strip()] = v.strip().strip('"').strip("'")
    return secrets
