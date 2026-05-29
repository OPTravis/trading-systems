#!/usr/bin/env python3
"""Grade all `except Exception` blocks in src/ by risk level.

CRITICAL: except Exception: pass (silent swallow, no log)
HIGH:     except Exception: return default_value (swallow + invalid downstream)
MEDIUM:   except Exception: logger.debug/warning (logged but barely visible)
LOW:      except Exception: logger.error(..., exc_info=True) (properly logged)
OK:       except SpecificError: (specific exception caught)
"""

import ast
import pathlib

SRC = pathlib.Path("/home/travis/crypto-ai-trader/src")

def grade_handler(body: list[ast.stmt]) -> tuple[str, str]:
    """Grade an except handler body. Returns (grade, reason)."""
    if not body:
        return ("CRITICAL", "empty body (pass)")

    text = ast.unparse(body[0]) if hasattr(ast, 'unparse') else ""
    first = body[0]

    # CRITICAL: bare pass
    if isinstance(first, ast.Pass):
        return ("CRITICAL", "bare pass — silently ignores all errors")

    # Check for return with default value
    if isinstance(first, ast.Return):
        if first.value is None:
            return ("HIGH", "return None — downstream gets None")
        if isinstance(first.value, ast.Constant) and first.value.value in (0, 0.0, False, 50, 50.0):
            return ("HIGH", f"return {first.value.value} — downstream gets fake valid data")
        return ("HIGH", "return default — downstream gets invalid data")

    # Check for assignment with default
    if isinstance(first, ast.Assign):
        for target in first.targets:
            if isinstance(target, ast.Name):
                val = first.value
                if isinstance(val, ast.Constant):
                    if val.value == 0 or val.value == 0.0:
                        return ("HIGH", f"assign {target.id}=0 — downstream gets zero")
                    if val.value in (50, 50.0):
                        return ("HIGH", f"assign {target.id}=50 — downstream gets midpoint")
                    if val.value is None:
                        return ("HIGH", f"assign {target.id}=None")
        return ("MEDIUM", "assign default value")

    # Check for logging
    all_stmts = " ".join(ast.unparse(s) if hasattr(ast, 'unparse') else "" for s in body)
    if "logger.error" in all_stmts and "exc_info" in all_stmts:
        return ("LOW", "logged with exc_info=True — traceable")
    if "logger.error" in all_stmts:
        return ("MEDIUM", "logged without exc_info — no traceback")
    if "logger.warning" in all_stmts:
        return ("MEDIUM", "warning level — easy to miss")
    if "logger.debug" in all_stmts:
        return ("MEDIUM", "debug level — invisible in production")
    if "logger.info" in all_stmts:
        return ("MEDIUM", "info level — no stack trace")

    return ("MEDIUM", "unclassified — needs review")


def scan_file(filepath: pathlib.Path) -> list[dict]:
    """Scan one file for except Exception blocks."""
    results = []
    try:
        tree = ast.parse(filepath.read_text())
    except SyntaxError as e:
        return [{"file": str(filepath), "line": 0, "grade": "ERROR", "reason": f"syntax: {e}"}]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if handler.type is None:
                # bare except: — worst case
                grade, reason = grade_handler(handler.body)
                results.append({
                    "file": str(filepath.relative_to(SRC.parent)),
                    "line": handler.lineno,
                    "grade": "CRITICAL",
                    "reason": f"bare except: {reason}",
                    "snippet": ast.unparse(handler).split('\n')[0][:80] if hasattr(ast, 'unparse') else "",
                })
            elif isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
                grade, reason = grade_handler(handler.body)
                results.append({
                    "file": str(filepath.relative_to(SRC.parent)),
                    "line": handler.lineno,
                    "grade": grade,
                    "reason": reason,
                    "snippet": ast.unparse(handler).split('\n')[0][:80] if hasattr(ast, 'unparse') else "",
                })
    return results


def main():
    all_results = []
    for f in sorted(SRC.glob("**/*.py")):
        all_results.extend(scan_file(f))

    # Sort by severity
    grade_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "OK": 4, "ERROR": 5}
    all_results.sort(key=lambda r: (grade_order.get(r["grade"], 99), r["file"], r["line"]))

    # Summary
    grades = {}
    for r in all_results:
        grades[r["grade"]] = grades.get(r["grade"], 0) + 1

    print("=" * 60)
    print("except Exception BLOCK AUDIT")
    print("=" * 60)
    for g in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "ERROR"]:
        if g in grades:
            print(f"  {g}: {grades[g]}")

    total = sum(grades.values())
    print(f"  TOTAL: {total}")
    print()

    # Critical path files
    critical_files = {
        "src/market_scanner.py", "src/scan_orchestrator.py",
        "src/trade_executor.py", "src/risk_manager.py",
        "src/strategy_registry.py", "src/strategy_adaptor.py",
        "src/market_researcher.py", "src/data_feed.py",
        "src/multi_timeframe.py", "src/binance_client.py",
        "src/dynamic_coin_pool.py",
    }

    print("=" * 60)
    print("CRITICAL+HIGH in trading pipeline")
    print("=" * 60)
    critical_count = 0
    for r in all_results:
        if r["grade"] in ("CRITICAL", "HIGH") and r["file"] in critical_files:
            print(f"  [{r['grade']}] {r['file']}:{r['line']} — {r['reason']}")
            if r["snippet"]:
                print(f"         {r['snippet']}")
            critical_count += 1

    if critical_count == 0:
        print("  (none)")
    print()

    # All CRITICAL+HIGH
    print("=" * 60)
    print("ALL CRITICAL+HIGH (non-pipeline)")
    print("=" * 60)
    for r in all_results:
        if r["grade"] in ("CRITICAL", "HIGH") and r["file"] not in critical_files:
            print(f"  [{r['grade']}] {r['file']}:{r['line']} — {r['reason']}")

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"CRITICAL: {grades.get('CRITICAL', 0)}")
    print(f"HIGH:     {grades.get('HIGH', 0)}")
    print(f"MEDIUM:   {grades.get('MEDIUM', 0)}")
    print(f"LOW:      {grades.get('LOW', 0)}")
    print(f"TOTAL:    {sum(grades.values())}")
    print(f"Pipeline CRITICAL+HIGH to fix: {critical_count}")


if __name__ == "__main__":
    main()
