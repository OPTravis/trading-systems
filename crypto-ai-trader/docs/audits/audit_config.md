# Config & Dependencies Audit Report

**Date:** 2026-04-04  
**Auditor:** config-auditor  
**Project:** `/home/travis/.openclaw/workspace/crypto-ai-trader/`

---

## 1. Secrets Management

### 🟡 Warning — crypto-secrets.env not in project .gitignore

- **File:** `.gitignore` — excludes `.env` but NOT `crypto-secrets.env`
- The actual secrets file lives at `~/.openclaw/crypto-secrets.env` (outside project dir), so it's not tracked currently — but if someone copies it into the project, it would not be ignored.
- **Fix:** Add `crypto-secrets.env` and `*secrets.env` patterns to `.gitignore`

### 🟡 Warning — Secrets file permissions allow group read

- **File:** `~/.openclaw/crypto-secrets.env` — mode `664` (rw-rw-r--)
- Group members can read the file.
- **Fix:** `chmod 600 ~/.openclaw/crypto-secrets.env`

### 🔵 Info — secrets.py is well-designed

- `src/secrets.py` centralizes all secret loading, has permission checking, and supports both `.env` files. No hardcoded keys found in the codebase.
- Secrets are loaded from `~/.openclaw/crypto-secrets.env` and `~/.openclaw/secrets.env` — outside the project dir. Good pattern.

### 🔵 Info — No hardcoded secrets found

- Grep across all `.py`, `.json`, `.yaml`, `.sh` files found zero hardcoded API keys, passwords, or private keys.

---

## 2. Dependencies

### 🔵 Info — requirements.txt is minimal and reasonable

```
binance-connector>=3.12.0
pandas>=2.0.0
numpy>=1.24.0
requests>=2.31.0
PyYAML>=6.0
backtrader>=1.9.78
ta>=0.10.0
python-dotenv>=1.0.0
```

- No unnecessary deps. All are actively used.
- Versions use `>=` which is normal; pin hashes (`pip install --require-hashes`) would be more secure for production.

### 🟡 Warning — No lock file (no `requirements.lock` or `Pipfile.lock`)

- Reproducible builds not guaranteed.
- **Fix:** Run `pip freeze > requirements-lock.txt` or use `pip-tools` / `poetry`.

### 🟡 Warning — No version upper bounds

- `>=` allows breaking major version bumps.
- **Fix:** Pin to `~=` or `<next_major` ranges.

---

## 3. Environment Config

### 🟡 Warning — No environment flag; all scripts default to production (testnet=False)

- `main.py` has **8 instances** of `BinanceClient(testnet=False)` hardcoded.
- `check_msg.py`, `handle_confirmation.py`, `run_copy_trading.py`, `run_dca.py`, `ai4trade_integration.py` all hardcode `testnet=False`.
- No env var (e.g. `BINANCE_TESTNET=1`) to switch environments.
- **Fix:** Read from env/config: `BinanceClient(testnet=os.getenv('BINANCE_TESTNET', 'false').lower() == 'true')`

### 🔵 Info — Config files are clean YAML

- `config/risk_limits.yaml` and `config/strategies.yaml` contain only risk parameters and strategy configs. No secrets or hardcoded addresses.

---

## 4. File Permissions

### 🟡 Warning — data/portfolio_state.json is overly restrictive

- Mode `600` — only owner read/write. This is actually fine for secrets, but for shared data files it may cause issues if other processes need access.

### 🟡 Warning — data/dca_state.json mode 664

- Group-writable. If this file contains trade state, group modification is risky.

---

## 5. Git Safety

### 🔴 Critical — `src/secrets.py` was committed to git history

- Commit `f0bbda9` added `src/secrets.py` to the repo.
- While the file itself doesn't contain secrets (it loads from external files), the **naming convention** and its presence in history makes it a target for credential-scanning tools and could leak infrastructure details.
- **Fix:** If the repo is or will be public, consider removing from history: `git filter-branch` or `BFG Repo-Cleaner`. If private-only, document why it's acceptable.

### 🟡 Warning — Remote origin exists — verify it's private

- `git remote origin` is configured. Ensure the remote repo is **private**.
- **Fix:** `git remote get-url origin` to verify. If public, secrets history is exposed.

### 🔵 Info — .gitignore covers basics

- Ignores `__pycache__/`, `*.pyc`, `.venv/`, `.env`, `*.log`, `.DS_Store`.
- Missing: `*.env*`, `data/` (contains state files), `config/` (if sensitive).

---

## Summary

| Severity | Count | Key Issues |
|----------|-------|------------|
| 🔴 Critical | 1 | secrets.py in git history |
| 🟡 Warning | 6 | File permissions, no env separation, no lock file, remote repo privacy |
| 🔵 Info | 4 | Good secrets architecture, clean configs, no hardcoded keys |

### Top 3 Fixes (priority order)

1. **Verify remote repo is private** — if public, scrub `secrets.py` from history immediately
2. **`chmod 600 ~/.openclaw/crypto-secrets.env`** — restrict secrets file
3. **Add `BINANCE_TESTNET` env var support** — avoid hardcoded prod mode in 8+ locations
