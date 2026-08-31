#!/usr/bin/env python3
"""bug#39 (2026-08-31): self-heal pipeline skeleton.

异常分类器 → 工单生成 → fail-safe safe_mode 开关 → 显式解除。

数据流：
  logs/cron_failures.jsonl (+ logs/report_validator_failures.jsonl 镜像)
    → classify() 按记录特征归入类别并定级
    → 窗口内同类达到阈值（默认 3 / 24h；critical 级 1 次即触发）
        · 生成 open 工单 → logs/self_heal_tickets.jsonl（同类别去重）
        · 拉起 safe_mode → logs/safe_mode.json

safe_mode 生效期：
  · 生产脚本调用 is_safe_mode() 即可感知；fail-safe 语义：开关文件
    存在但损坏/不可读时一律视为 ON（宁可停，不可裸奔）。
  · 本工具自身的扫描/分类异常只会以退出码 2 报错，绝不解除、也不会
    误开 safe_mode（开关文件保持原样）。

解除：
  · 仅 `python3 scripts/self_heal.py --lift "<理由>"` 显式解除；
    理由连同解除前状态写入工单审计行；无理由拒绝执行。

建议接入点（后续 bug 逐个接入，本骨架不主动改生产行为）：
  scripts/push_notifications.py main() 开头:
      from scripts.self_heal import is_safe_mode
      if is_safe_mode(): 阻断推送并告警
  scan 调度 wrapper: if is_safe_mode(): 只跑行情 fetch/观测，不开新仓。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"


def _env_path(env_key: str, default: Path) -> Path:
    v = os.environ.get(env_key)
    return Path(v) if v else default


def _cron_failures_file() -> Path:
    return _env_path("CRON_FAILURES_FILE", LOGS_DIR / "cron_failures.jsonl")


def _tickets_file() -> Path:
    return _env_path("SELF_HEAL_TICKETS_FILE", LOGS_DIR / "self_heal_tickets.jsonl")


def _safe_mode_file() -> Path:
    return _env_path("SAFE_MODE_FILE", LOGS_DIR / "safe_mode.json")


def _extra_failure_files() -> List[Path]:
    # report_validator_failures.jsonl mirrors validator blocks (same channel)
    return [_env_path("REPORT_VALIDATOR_FAILURES_FILE",
                      LOGS_DIR / "report_validator_failures.jsonl")]


_RE_NETWORK = re.compile(
    r"(proxy|timeout|timed\s*out|connection|network|ssl|getaddrinfo|"
    r"failed\s*after|unreachable|直连|代理)", re.I)
_RE_BALANCE = re.compile(r"(balance|mismatch|余额)", re.I)
_RE_BLOCKED = re.compile(r"(blocked|position-claim|order-claim|balance-claim|duplicate)", re.I)


def classify(entry: dict) -> Tuple[str, str]:
    """异常分类器：entry -> (category, severity)。entry 的纯函数。"""
    detail = str(entry.get("detail", ""))
    etype = str(entry.get("type", ""))
    job = str(entry.get("job", ""))
    if etype == "db_write_failure":
        return "db_write_failure", "high"
    if job == "report_validator" or _RE_BLOCKED.search(detail):
        return "report_block", "high"
    if _RE_BALANCE.search(detail):
        return "balance_mismatch", "critical"
    if _RE_NETWORK.search(detail) or _RE_NETWORK.search(job):
        return "network_proxy", "medium"
    try:
        if int(entry.get("exit_code", 0)) != 0:
            return "cron_exit", "medium"
    except (TypeError, ValueError):
        pass
    return "unknown", "low"


def _read_jsonl(path: Path) -> List[dict]:
    rows: List[dict] = []
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    rows.append({"job": "unparsable", "type": "unparsable",
                                 "detail": line[:500]})
    except OSError:
        pass
    return rows


def _parse_ts(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def is_safe_mode() -> Optional[dict]:
    """safe_mode 感知 API。engaged ⇒ 返回状态 dict；损坏/不可读 ⇒ 视为
    engaged（fail-safe）；未启用 ⇒ None。"""
    f = _safe_mode_file()
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("enabled"):
            return d
    except Exception:
        return {"enabled": True, "ts": None, "reason": "safe_mode file "
                "unreadable/corrupt — fail-safe engaged"}
    return None


def _open_ticket_categories() -> set:
    cats = set()
    for t in _read_jsonl(_tickets_file()):
        if t.get("kind") == "ticket" and t.get("status") == "open":
            cats.add(t.get("category"))
    return cats


def generate_ticket(category: str, severity: str, count: int, sample: str) -> dict:
    ticket = {"kind": "ticket", "ts": _now_iso(),
              "ticket_id": f"TK-{int(time.time()*1000)}",
              "category": category, "severity": severity, "status": "open",
              "evidence_count": count, "sample_detail": sample[:300]}
    _append_jsonl(_tickets_file(), ticket)
    return ticket


def enable_safe_mode(categories: List[str]) -> dict:
    state = {"enabled": True, "ts": _now_iso(),
             "reason": f"self-heal threshold tripped: {','.join(categories)}",
             "categories": categories}
    f = _safe_mode_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    _append_jsonl(_tickets_file(), {"kind": "audit", "event": "safe_mode_on",
                                    "ts": state["ts"], "categories": categories})
    return state


def lift_safe_mode(reason: str) -> Tuple[bool, str]:
    """显式解除。无理由拒绝；解除写审计行并删除开关文件。"""
    if not reason or not str(reason).strip():
        return False, "拒绝解除：--lift 必须附带非空理由（审计要求）"
    f = _safe_mode_file()
    if not f.exists():
        return True, "safe_mode 未启用，无需解除"
    prev = None
    try:
        prev = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        prev = {"corrupt": True}
    _append_jsonl(_tickets_file(), {"kind": "audit", "event": "safe_mode_lifted",
                                    "ts": _now_iso(), "reason": str(reason)[:500],
                                    "previous": prev})
    try:
        f.unlink()
    except OSError as e:
        return False, f"解除失败：无法删除开关文件 {f}: {e}"
    return True, f"safe_mode 已解除（理由已审计）: {reason}"


def scan(window_s: Optional[int] = None, threshold: Optional[int] = None) -> int:
    """扫描故障通道 → 分类计数 → 工单 + safe_mode。返回退出码。"""
    now = time.time()
    win = window_s if window_s is not None else int(
        os.environ.get("SELF_HEAL_WINDOW_S", 24 * 3600))
    thr = threshold if threshold is not None else int(
        os.environ.get("SELF_HEAL_THRESHOLD", 3))

    entries: List[dict] = []
    for e in _read_jsonl(_cron_failures_file()):
        entries.append(e)
    for p in _extra_failure_files():
        for r in _read_jsonl(p):
            # normalize validator tickets into the cron-failure shape
            entries.append({"timestamp": r.get("ts") or r.get("timestamp"),
                            "job": "report_validator", "exit_code": 1,
                            "detail": "; ".join(
                                r.get("reasons", [str(r.get("detail", ""))]))})

    counts: Dict[str, dict] = {}
    for e in entries:
        ts = _parse_ts(e.get("timestamp") or e.get("ts"))
        if ts is None or now - ts > win:  # unparsable/out-of-window: ignored
            continue
        cat, sev = classify(e)
        c = counts.setdefault(cat, {"count": 0, "severity": sev, "sample": ""})
        c["count"] += 1
        if not c["sample"]:
            c["sample"] = str(e.get("detail", ""))[:300]

    open_cats = _open_ticket_categories()
    tripped: List[str] = []
    for cat, info in sorted(counts.items()):
        need = 1 if info["severity"] == "critical" else thr
        if info["count"] >= need:
            tripped.append(cat)
            if cat not in open_cats:
                generate_ticket(cat, info["severity"], info["count"], info["sample"])

    engaged = is_safe_mode()
    if tripped and not engaged:
        enable_safe_mode(tripped)
        print(f"🛑 self-heal: safe_mode ENGAGED，触发类别: {tripped} "
              f"（工单见 {_tickets_file().name}；解除: --lift \"<理由>\"）")
    elif tripped:
        print(f"🛑 self-heal: safe_mode 已处于启用状态，本轮触发类别: {tripped}")
    else:
        print(f"✅ self-heal: 窗口内无触发（{len(counts)} 个类别，"
              f"阈值 critical=1 / 其余={thr}，窗口 {win}s）")
    return 0


def status() -> int:
    engaged = is_safe_mode()
    if engaged:
        print(f"🛑 safe_mode: ON  ts={engaged.get('ts')} "
              f"reason={engaged.get('reason')}")
    else:
        print("🟢 safe_mode: OFF")
    tickets = [t for t in _read_jsonl(_tickets_file()) if t.get("kind") == "ticket"]
    open_t = [t for t in tickets if t.get("status") == "open"]
    print(f"tickets: {len(open_t)} open / {len(tickets)} total "
          f"（{_tickets_file()}）")
    return 0


def main(argv: List[str]) -> int:
    try:
        if "--lift" in argv:
            i = argv.index("--lift")
            reason = argv[i + 1] if i + 1 < len(argv) else ""
            ok, msg = lift_safe_mode(reason)
            print(("✅ " if ok else "🛑 ") + msg)
            return 0 if ok else 2
        if "--status" in argv:
            return status()
        return scan()
    except Exception as e:  # fail-safe: never touch the switch on our own errors
        print(f"🛑 self-heal internal error: {e} — safe_mode 开关保持原样",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
