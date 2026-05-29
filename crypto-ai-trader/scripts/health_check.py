#!/home/travis/crypto-ai-trader/.venv/bin/python3
"""
System-wide health check — scans ALL cron job outputs for ACTIVE errors.

Run as no_agent cron every 30min. Non-empty stdout = issues found.
Empty stdout = healthy (SILENT).

Only detects ACTUAL errors, not AI summaries mentioning past fixes.
Output format: errors grouped by destination group for routing.
"""
import glob
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

CRON_OUTPUT_DIR = Path.home() / ".hermes" / "cron" / "output"
ERRORS_LOG = Path.home() / ".hermes" / "logs" / "errors.log"
LOOKBACK_MINUTES = 120

# Job ID → (name, destination group chat_id)
# All 17 active cron jobs
JOB_ROUTES = {
    # Crypto → oc_07180818c3ebfa6a7c996ee9ccad456a
    "28cda1d17ae5": ("crypto-scan", "crypto"),
    "c24657946ed4": ("crypto-unified-monitor", "crypto"),
    "f4b4a5d9dc6e": ("crypto-report", "crypto"),
    "a99f478ad599": ("crypto-dust-cleanup", "crypto"),
    "a94ede635175": ("crypto-code-quality", "crypto"),
    "ffdfbe13c07c": ("crypto-metrics-update", "crypto"),
    "9c917837dc0e": ("sector-clustering-weekly", "crypto"),
    "52e6393dcc6a": ("crypto-health-check", "crypto"),
    "ec3509712399": ("crypto-weekly-backtest", "crypto"),
    "a2ab573f500c": ("crypto-weight-learning", "crypto"),
    "6a13f7c4927a": ("crypto-param-optimizer", "crypto"),
    "be09200182e2": ("hmm-regime-retrain", "crypto"),
    # Work → oc_941865251bc7f1b0ee2affd91e2643ab
    "fbe954069068": ("outlook-email-summary", "work"),
    "f40347faf14f": ("nas-inbox-scan", "work"),
    "1626a7c654d0": ("work-daily-brief", "work"),
    # News → oc_f02678dc42d92afc541836f5a9c8cdaa
    "a3ecb8dc71cc": ("logistics-news-monitor-4h", "news"),
    # Home → oc_bacf3348ee8d8bbef157aec47848f017
    "53a31fef6821": ("system-backup", "home"),
}

# Feishu group chat IDs
GROUP_IDS = {
    "crypto": "oc_07180818c3ebfa6a7c996ee9ccad456a",
    "work": "oc_941865251bc7f1b0ee2affd91e2643ab",
    "news": "oc_f02678dc42d92afc541836f5a9c8cdaa",
    "home": "oc_bacf3348ee8d8bbef157aec47848f017",
}

# ACTIVE error patterns
ACTIVE_ERROR_PATTERNS = [
    (r"Traceback \(most recent call last\):", "Python traceback"),
    (r'^\s+File ".*", line \d+', "Stack frame"),
    (r"(?:^|\s)TypeError:", "TypeError"),
    (r"(?:^|\s)NameError:", "NameError"),
    (r"(?:^|\s)AttributeError:", "AttributeError"),
    (r"(?:^|\s)KeyError:", "KeyError"),
    (r"(?:^|\s)JSONDecodeError:", "JSONDecodeError"),
    (r"(?:^|\s)ImportError:", "ImportError"),
    (r"(?:^|\s)ModuleNotFoundError:", "ModuleNotFoundError"),
    (r"(?:^|\s)RuntimeError:", "RuntimeError"),
    (r"^SWITCH_FAILED:", "SWITCH_FAILED"),
    (r"^❌ EXECUTE_FAILED", "EXECUTE_FAILED"),
    (r"exit_code: [1-9]", "Script failed"),
    (r"last_status.*error", "Cron job error"),
]

SKIP_PATTERNS = [
    r"已修復", r"已修好", r"修復完成", r"FIXED", r"需後續修復", r"已解決", r"待做",
]


def is_ai_prose(line: str) -> bool:
    ai_markers = ["ℹ️", "✅", "🔍", "⚡", "📈", "📋", "🐻", "🎯", "💰", "📊", "🚫"]
    for marker in ai_markers:
        if line.strip().startswith(marker):
            return True
    for pattern in SKIP_PATTERNS:
        if re.search(pattern, line):
            return True
    return False


def scan_cron_outputs() -> dict:
    """Scan all cron outputs, return {group: [issues]}."""
    issues_by_group = {"crypto": [], "work": [], "news": [], "home": []}
    now = time.time()
    since = now - (LOOKBACK_MINUTES * 60)

    if not CRON_OUTPUT_DIR.exists():
        return issues_by_group

    for job_dir in CRON_OUTPUT_DIR.iterdir():
        if not job_dir.is_dir():
            continue

        job_id = job_dir.name
        route = JOB_ROUTES.get(job_id)
        if not route:
            continue  # unknown job, skip

        job_name, group = route
        md_files = sorted(job_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not md_files:
            continue

        md_file = md_files[0]
        if md_file.stat().st_mtime < since:
            continue

        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        response_section = content
        if "## Response" in content:
            response_section = content.split("## Response", 1)[1]

        for line in response_section.split("\n"):
            line_stripped = line.strip()
            if not line_stripped:
                continue
            if is_ai_prose(line_stripped):
                continue

            for pattern, label in ACTIVE_ERROR_PATTERNS:
                if re.search(pattern, line_stripped):
                    issues_by_group[group].append({
                        "job": job_name,
                        "pattern": label,
                        "detail": line_stripped[:120],
                    })
                    break

    return issues_by_group


def scan_errors_log() -> list:
    """Scan errors.log for recent issues."""
    issues = []
    if not ERRORS_LOG.exists():
        return issues

    now = time.time()
    since = now - (LOOKBACK_MINUTES * 60)

    try:
        size = ERRORS_LOG.stat().st_size
        with open(ERRORS_LOG, "r", encoding="utf-8", errors="replace") as f:
            if size > 30000:
                f.seek(size - 30000)
                f.readline()
            content = f.read()
    except Exception:
        return issues

    for line in content.split("\n"):
        ts_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if not ts_match:
            continue
        try:
            line_ts = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if line_ts < since:
            continue
        if re.search(r"ERROR", line, re.IGNORECASE):
            issues.append({
                "job": "errors_log",
                "pattern": "Runtime error",
                "detail": line.strip()[:120],
            })

    return issues


def format_output(issues_by_group: dict, log_issues: list) -> str:
    """Format output grouped by destination.

    Format:
    [crypto]
    - crypto-scan: TypeError: ... detail
    [work]
    - outlook-email-summary: ImportError: ... detail
    """
    output_lines = []

    for group in ["crypto", "work", "news", "home"]:
        group_issues = issues_by_group.get(group, [])
        if not group_issues:
            continue

        # Deduplicate
        seen = set()
        unique = []
        for item in group_issues:
            key = (item["job"], item["pattern"], item["detail"][:60])
            if key not in seen:
                seen.add(key)
                unique.append(item)

        if unique:
            output_lines.append(f"[{group}]")
            for item in unique:
                output_lines.append(f"- {item['job']}: {item['pattern']}: {item['detail']}")
            output_lines.append("")

    if log_issues:
        output_lines.append("[home]")
        for item in log_issues[:5]:  # cap at 5
            output_lines.append(f"- {item['job']}: {item['pattern']}: {item['detail']}")
        output_lines.append("")

    return "\n".join(output_lines).strip()


def main():
    issues_by_group = scan_cron_outputs()
    log_issues = scan_errors_log()

    output = format_output(issues_by_group, log_issues)
    if output:
        print(output)
    # Empty = SILENT


if __name__ == "__main__":
    main()
