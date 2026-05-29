#!/usr/bin/env python3
"""
Health Check - System health monitoring for stock-ai-trader.

Checks:
- Broker connection status
- Data feed availability
- Disk space
- Last scan timestamp
- Memory usage

Usage:
    python scripts/health_check.py
    python scripts/health_check.py --json
    python scripts/health_check.py --notify
"""

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)


class HealthCheck:
    """System health check runner."""

    def __init__(self):
        self.checks = {}
        self.overall_status = "OK"

    def run_all(self) -> dict:
        """Run all health checks and return status dict."""
        self.checks["broker"] = self._check_broker()
        self.checks["data_feed"] = self._check_data_feed()
        self.checks["disk_space"] = self._check_disk_space()
        self.checks["last_scan"] = self._check_last_scan()
        self.checks["memory"] = self._check_memory()

        # Determine overall status
        statuses = [c["status"] for c in self.checks.values()]
        if "CRITICAL" in statuses:
            self.overall_status = "CRITICAL"
        elif "WARNING" in statuses:
            self.overall_status = "WARNING"
        else:
            self.overall_status = "OK"

        return {
            "overall": self.overall_status,
            "timestamp": datetime.now().isoformat(),
            "checks": self.checks,
        }

    def _check_broker(self) -> dict:
        """Check broker connection availability."""
        try:
            # Check if broker config exists
            config_path = PROJECT_ROOT / "config" / "config.yaml"
            if not config_path.exists():
                return {
                    "status": "WARNING",
                    "message": "Config file not found",
                }

            # Check for .env with broker credentials
            env_path = PROJECT_ROOT / ".env"
            if not env_path.exists():
                return {
                    "status": "WARNING",
                    "message": ".env file not found (no broker credentials)",
                }

            # Paper client is always available
            return {
                "status": "OK",
                "message": "Paper client available",
            }
        except Exception as e:
            return {
                "status": "CRITICAL",
                "message": f"Broker check failed: {e}",
            }

    def _check_data_feed(self) -> dict:
        """Check data feed availability."""
        try:
            # Check if data directory exists
            data_dir = PROJECT_ROOT / "data"
            if not data_dir.exists():
                return {
                    "status": "WARNING",
                    "message": "Data directory not found",
                }

            # Check for market data cache
            cache_dir = data_dir / "cache"
            if cache_dir.exists():
                cache_files = list(cache_dir.glob("*.json"))
                if cache_files:
                    newest = max(cache_files, key=lambda f: f.stat().st_mtime)
                    age_hours = (
                        datetime.now().timestamp() - newest.stat().st_mtime
                    ) / 3600
                    if age_hours > 24:
                        return {
                            "status": "WARNING",
                            "message": f"Cache data is {age_hours:.0f}h old",
                        }
                    return {
                        "status": "OK",
                        "message": f"Cache: {len(cache_files)} files, latest {age_hours:.1f}h ago",
                    }

            return {
                "status": "OK",
                "message": "Data directory exists (no cache yet)",
            }
        except Exception as e:
            return {
                "status": "CRITICAL",
                "message": f"Data feed check failed: {e}",
            }

    def _check_disk_space(self) -> dict:
        """Check available disk space."""
        try:
            usage = shutil.disk_usage(PROJECT_ROOT)
            free_gb = usage.free / (1024 ** 3)
            total_gb = usage.total / (1024 ** 3)
            used_pct = (usage.used / usage.total) * 100

            if free_gb < 1.0:
                return {
                    "status": "CRITICAL",
                    "message": f"Low disk: {free_gb:.1f}GB free ({used_pct:.0f}% used)",
                }
            elif free_gb < 5.0:
                return {
                    "status": "WARNING",
                    "message": f"Disk: {free_gb:.1f}GB free ({used_pct:.0f}% used)",
                }
            return {
                "status": "OK",
                "message": f"Disk: {free_gb:.1f}GB / {total_gb:.1f}GB free ({used_pct:.0f}% used)",
            }
        except Exception as e:
            return {
                "status": "WARNING",
                "message": f"Disk check failed: {e}",
            }

    def _check_last_scan(self) -> dict:
        """Check when the last scan was run."""
        try:
            log_dir = PROJECT_ROOT / "data" / "logs"
            if not log_dir.exists():
                return {
                    "status": "WARNING",
                    "message": "No log directory found",
                }

            scan_logs = sorted(log_dir.glob("scan_*.log"))
            if not scan_logs:
                return {
                    "status": "WARNING",
                    "message": "No scan logs found",
                }

            newest = scan_logs[-1]
            # Extract date from filename: scan_YYYYMMDD.log
            date_str = newest.stem.replace("scan_", "")
            scan_date = datetime.strptime(date_str, "%Y%m%d")
            days_ago = (datetime.now() - scan_date).days

            if days_ago > 3:
                return {
                    "status": "WARNING",
                    "message": f"Last scan: {days_ago} days ago ({date_str})",
                }
            return {
                "status": "OK",
                "message": f"Last scan: {date_str} ({days_ago} day(s) ago)",
            }
        except Exception as e:
            return {
                "status": "WARNING",
                "message": f"Scan check failed: {e}",
            }

    def _check_memory(self) -> dict:
        """Check system memory usage."""
        try:
            # Read /proc/meminfo on Linux
            meminfo = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = int(parts[1].strip().split()[0])
                        meminfo[key] = val

            total_mb = meminfo.get("MemTotal", 0) / 1024
            available_mb = meminfo.get("MemAvailable", 0) / 1024
            used_pct = ((total_mb - available_mb) / total_mb) * 100 if total_mb else 0

            if available_mb < 256:
                return {
                    "status": "CRITICAL",
                    "message": f"Low memory: {available_mb:.0f}MB available",
                }
            elif available_mb < 1024:
                return {
                    "status": "WARNING",
                    "message": f"Memory: {available_mb:.0f}MB available ({used_pct:.0f}% used)",
                }
            return {
                "status": "OK",
                "message": f"Memory: {available_mb:.0f}MB / {total_mb:.0f}MB available",
            }
        except FileNotFoundError:
            # Not on Linux
            return {
                "status": "OK",
                "message": "Memory check not available on this OS",
            }
        except Exception as e:
            return {
                "status": "WARNING",
                "message": f"Memory check failed: {e}",
            }


def main():
    parser = argparse.ArgumentParser(description="System health check")
    parser.add_argument(
        "--json", action="store_true", help="Output as JSON"
    )
    parser.add_argument(
        "--notify", action="store_true",
        help="Send results to Feishu if status is not OK",
    )
    args = parser.parse_args()

    checker = HealthCheck()
    result = checker.run_all()

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{'='*50}")
        print(f"  System Health: {result['overall']}")
        print(f"  Time: {result['timestamp']}")
        print(f"{'='*50}")
        for name, check in result["checks"].items():
            status = check["status"]
            emoji = {"OK": "✅", "WARNING": "⚠️", "CRITICAL": "❌"}.get(
                status, "❓"
            )
            print(f"  {emoji} {name}: {status} - {check.get('message', '')}")
        print()

    # Send notification if not OK and --notify flag
    if args.notify and result["overall"] != "OK":
        try:
            from src.notifier import FeishuNotifier
            notifier = FeishuNotifier()
            notifier.send_system_status(result)
        except Exception as e:
            logger.error("Failed to send health check notification: %s", e)

    # Exit with appropriate code
    exit_codes = {"OK": 0, "WARNING": 1, "CRITICAL": 2}
    sys.exit(exit_codes.get(result["overall"], 1))


if __name__ == "__main__":
    main()
