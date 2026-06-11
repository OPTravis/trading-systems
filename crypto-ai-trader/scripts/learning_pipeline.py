#!/usr/bin/env python3
"""
Weekly Learning Pipeline — chains 4 learning steps sequentially.

Steps:
1. Factor weight learning (online_learner + strategy_evolver)
2. Concept drift detection
3. Sector clustering
4. Parameter optimization (grid search + walk-forward)

Run via cron (no_agent) every Sunday 09:00.
Output: JSON summary of each step.
"""
import glob as _glob
import json
import subprocess
import sys
import time
from pathlib import Path

# no_agent scripts need venv site-packages
_venv_dir = Path.home() / 'trading-systems' / 'crypto-ai-trader' / '.venv'
if sys.platform == 'win32':
    _venv_site = str(_venv_dir / 'Lib' / 'site-packages')
else:
    _venv_site = str(_venv_dir / 'lib' / 'python*' / 'site-packages')
_matches = _glob.glob(_venv_site)
if _matches:
    sys.path.insert(0, _matches[-1])

PROJECT = Path.home() / "trading-systems" / "crypto-ai-trader"
PYTHON = str(PROJECT / ".venv" / "bin" / "python")


def run_step(name: str, cmd: list, timeout: int = 600) -> dict:
    """Run a step and capture result."""
    start = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(PROJECT)
        )
        elapsed = time.time() - start
        return {
            "step": name,
            "status": "ok" if result.returncode == 0 else "error",
            "returncode": result.returncode,
            "elapsed_sec": round(elapsed, 1),
            "output": result.stdout.strip()[:500],
            "error": result.stderr.strip()[:300] if result.returncode != 0 else "",
        }
    except subprocess.TimeoutExpired:
        return {"step": name, "status": "timeout", "elapsed_sec": timeout}
    except Exception as e:
        return {"step": name, "status": "error", "error": str(e)[:200]}


def main():
    steps = []

    # Step 1: Factor weight learning
    print("Step 1/4: Factor weight learning...", flush=True)
    r1 = run_step("weight_learning", [PYTHON, "scripts/learn_weights.py"])
    steps.append(r1)

    # Step 2: Concept drift detection
    print("Step 2/4: Concept drift detection...", flush=True)
    r2 = run_step("concept_drift", [
        PYTHON, "-c",
        "from src.concept_drift import ConceptDriftDetector; "
        "d = ConceptDriftDetector(); r = d.detect_drift(); "
        "print(f'drift_severity={r.get(\"severity\",\"none\")}'); "
        "print(d.format_report(r))"
    ])
    steps.append(r2)

    # Step 3: Sector clustering
    print("Step 3/4: Sector clustering...", flush=True)
    r3 = run_step("sector_clustering", [
        PYTHON, "-m", "src.sector_clustering"
    ], timeout=300)
    steps.append(r3)

    # Step 4: Parameter optimization (longest step)
    print("Step 4/4: Parameter optimization...", flush=True)
    r4 = run_step("param_optimization", [PYTHON, "scripts/optimize_params.py"], timeout=900)
    steps.append(r4)

    # Summary
    summary = {
        "pipeline": "weekly_learning",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "steps": steps,
        "total_elapsed_sec": round(sum(s.get("elapsed_sec", 0) for s in steps), 1),
        "all_ok": all(s.get("status") == "ok" for s in steps),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
