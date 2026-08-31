#!/usr/bin/env python3
"""bug#34 (2026-08-31): 上报前一致性校验器（pre-report validator）。

在任何 scan_summary / 告警通报经 push_notifications 输出前，逐条强制断言：
  ① 报告声称的持仓集合 == state.db portfolio 表实查；
  ② 声称的挂单数 == 交易所 fetch_open_orders 实查（session 显式代理 +
     trust_env=True；空列表必须区分「真 0」与「请求失败」，失败 ⇒ 阻断）；
  ③ 报告声称的余额 == DB kv.cash_balance 且与交易所差 < $0.01；
  ④ 报告主事件若为持仓变化，与最近已通报事件去重（重复 ⇒ 降级拦截）。

任何断言失败 ⇒ 阻断该条通报（不输出、标记 blocked），写
logs/report_validator_failures.jsonl 诊断工单（含根因假设与修复建议），
并追加 cron_failures.jsonl 供自愈管线（bug#35）识别。

校验语义（避免盲区）：
  - 只校验通报 body 中「声称」的事实。无持仓/挂单/余额声称的通报
    （如「扫描完成未发现机会」）不做交易所可达性要求——否则网络故障
    会连「网络故障告警」本身也发不出去。
  - 交易所不可达时：含挂单/余额声称的通报一律阻断（宁缺勿错）。

以拦截器方式接入 scripts/push_notifications.py，不改通知产生方（src/notifier.py）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
SIGNALS_DIR = BASE_DIR / "signals"
NOTIFICATIONS_FILE = SIGNALS_DIR / "pending_notifications.json"
LOGS_DIR = BASE_DIR / "logs"
FAILURES_FILE = LOGS_DIR / "report_validator_failures.jsonl"
CRON_FAILURES = LOGS_DIR / "cron_failures.jsonl"

DEFAULT_PROXY = "http://127.0.0.1:17890"
BALANCE_TOL_USD = 0.01
DEDUP_WINDOW_S = 24 * 3600

# --- body 文本模式 ---
_RE_CLAIMED_POSITION = re.compile(
    r"(?im)^\s*[^\n]{0,24}?([A-Z]{2,10}USDT)[^\n]{0,12}?(持仓|开仓|持有|已开)")
_RE_CLAIM_NO_POSITION = re.compile(r"(无持仓|0\s*持仓|空仓|持仓\s*0\s*[单仓位个只]|未持仓)")
_RE_CLAIM_ORDERS = re.compile(r"挂单\s*([0-9]+)\s*[单条个笔]")
_RE_CLAIM_BALANCE = re.compile(r"(?:余额|USDT)[^\n]{0,10}?\$\s*([0-9]+(?:\.[0-9]+)?)")
_RE_MAIN_EVENT = re.compile(
    r"([A-Z]{2,10}USDT)[^\n]{0,8}?(开仓|平仓|OPEN|CLOSE)[^\n]{0,40}?@\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE)


def apply_session_proxy(exchange, proxy: Optional[str] = None) -> bool:
    """bug#34: pin the ccxt requests session to the local proxy.

    Must be explicit — in cron shells HTTP(S)_PROXY env vars are absent, so a
    default trust_env session goes direct and dies on the GFW (8/30 ghost
    "0 open orders"). BINANCE_PROXY=off disables; BINANCE_PROXY=<url> overrides.
    """
    val = proxy if proxy is not None else os.environ.get("BINANCE_PROXY", DEFAULT_PROXY)
    if val.strip().lower() == "off":
        return False
    sess = getattr(exchange, "session", None)
    if sess is None:
        return False
    sess.trust_env = True
    sess.proxies = {"http": val, "https": val}
    return True


class ExchangeFacts:
    """Collects exchange ground truth with a proxied, failure-explicit read.

    open_orders is None ⇔ the request FAILED (never treat as zero).
    """

    def __init__(self, client=None, proxy: Optional[str] = None):
        self._client = client
        self._proxy = proxy

    def collect(self) -> Dict:
        out: Dict = {"ok": False, "open_orders": None, "usdt_balance": None,
                     "errors": []}
        client = self._client
        if client is None:
            try:
                from src.ccxt_client import BinanceClient
                client = BinanceClient()
            except Exception as e:
                out["errors"].append(f"client-init: {e}")
                return out
        try:
            pinned = apply_session_proxy(client.exchange, self._proxy)
            out["proxy_pinned"] = pinned
        except Exception as e:
            out["errors"].append(f"proxy-pin: {e}")
        try:
            orders = client.get_open_orders()
            out["open_orders"] = list(orders or [])
        except Exception as e:
            out["errors"].append(f"open-orders: {e}")
        try:
            out["usdt_balance"] = float(client.get_balance("USDT"))
        except Exception as e:
            out["errors"].append(f"balance: {e}")
        out["ok"] = (out["open_orders"] is not None
                     and out["usdt_balance"] is not None)
        return out


class ReportValidator:
    def __init__(self, db=None, facts=None, history_file=None,
                 failures_file=None, now_ts: Optional[float] = None,
                 cron_failures_file=None):
        self._db = db
        self._facts_obj = facts
        self._history_file = Path(history_file) if history_file else NOTIFICATIONS_FILE
        self._failures_file = Path(failures_file) if failures_file else FAILURES_FILE
        # injectable so unit tests never touch the real cron_failures.jsonl
        self._cron_failures_file = (Path(cron_failures_file)
                                    if cron_failures_file else CRON_FAILURES)
        self._now = now_ts if now_ts is not None else time.time()

    # ---- ground truth ----
    def _db_positions(self) -> List[str]:
        if self._db is None:
            try:
                from src.state_db import StateDB
                self._db = StateDB()
            except Exception as e:
                logger.error("validator: db init failed: %s", e)
                return []
        try:
            with self._db._get_conn() as conn:
                rows = conn.execute("SELECT symbol FROM portfolio").fetchall()
            return [r["symbol"] if isinstance(r, dict) else r[0] for r in rows]
        except Exception as e:
            logger.error("validator: portfolio read failed: %s", e)
            return []

    def _db_cash(self) -> Optional[float]:
        if self._db is None:
            try:
                from src.state_db import StateDB
                self._db = StateDB()
            except Exception as e:
                logger.error("validator: db init failed: %s", e)
                return None
        try:
            with self._db._get_conn() as conn:
                row = conn.execute(
                    "SELECT value FROM kv WHERE key='cash_balance'").fetchone()
            if not row:
                return None
            v = row["value"] if isinstance(row, dict) else row[0]
            return float(v)
        except Exception as e:
            logger.error("validator: cash read failed: %s", e)
            return None

    def _facts(self) -> Dict:
        if self._facts_obj is None:
            self._facts_obj = ExchangeFacts()
        return self._facts_obj.collect()

    # ---- dedup history ----
    def _recent_pushed_bodies(self) -> List[Tuple[float, str]]:
        try:
            if not self._history_file.exists():
                return []
            data = json.loads(self._history_file.read_text())
        except Exception:
            return []
        out = []
        for n in data if isinstance(data, list) else []:
            if not n.get("pushed"):
                continue
            try:
                ts = _parse_ts(n.get("timestamp", ""))
            except Exception:
                continue
            if self._now - ts <= DEDUP_WINDOW_S:
                out.append((ts, n.get("body", "")))
        return out

    # ---- core ----
    def validate(self, notif: Dict) -> Tuple[str, List[str], Optional[Dict]]:
        body = notif.get("body", "") or ""
        reasons: List[str] = []
        facts = None

        # ① positions
        db_pos = self._db_positions()
        claimed = set(m.group(1).upper() for m in _RE_CLAIMED_POSITION.finditer(body))
        for sym in sorted(claimed):
            if sym not in db_pos:
                reasons.append(
                    f"position-claim-mismatch: body claims {sym} position but DB portfolio has none")
        claims_no_pos = bool(_RE_CLAIM_NO_POSITION.search(body))
        if claims_no_pos and db_pos:
            reasons.append(
                f"position-claim-mismatch: body claims no positions but DB portfolio has {sorted(set(db_pos))}")

        # ② orders (only if claimed; exchange ground truth on demand)
        m_orders = _RE_CLAIM_ORDERS.search(body)
        if m_orders:
            facts = facts or self._facts()
            claimed_n = int(m_orders.group(1))
            if facts["open_orders"] is None:
                reasons.append(
                    "exchange-unreachable: open-orders claim unverifiable (request failed) — blocking rather than publishing")
            else:
                actual = len(facts["open_orders"])
                if actual != claimed_n:
                    reasons.append(
                        f"orders-claim-mismatch: body claims {claimed_n} open orders, exchange returns {actual}")

        # ③ balance (only if claimed)
        m_bal = _RE_CLAIM_BALANCE.search(body)
        if m_bal:
            claimed_bal = float(m_bal.group(1))
            db_cash = self._db_cash()
            if db_cash is None or abs(claimed_bal - db_cash) >= BALANCE_TOL_USD:
                reasons.append(
                    f"balance-claim-mismatch: body claims ${claimed_bal}, DB cash = {db_cash}")
            facts = facts or self._facts()
            if facts["usdt_balance"] is None:
                reasons.append(
                    "exchange-unreachable: balance claim unverifiable (request failed) — blocking rather than publishing")
            elif abs(claimed_bal - float(facts["usdt_balance"])) >= BALANCE_TOL_USD:
                reasons.append(
                    f"balance-claim-mismatch: body claims ${claimed_bal}, exchange = ${facts['usdt_balance']}")

        # ④ dedup of main event
        m_evt = _RE_MAIN_EVENT.search(body)
        if m_evt:
            sym, action, px = m_evt.group(1).upper(), m_evt.group(2).upper(), float(m_evt.group(3))
            for _ts, hist_body in self._recent_pushed_bodies():
                for hm in _RE_MAIN_EVENT.finditer(hist_body):
                    if (hm.group(1).upper() == sym
                            and hm.group(2).upper() == action
                            and abs(float(hm.group(3)) - px) <= max(1e-6, px * 0.002)):
                        reasons.append(
                            f"duplicate-event: {action} {sym} @ {px} already reported within dedup window")
                        break
                if any(r.startswith("duplicate-event") for r in reasons):
                    break

        verdict = "block" if reasons else "pass"
        ticket = None
        if verdict == "block":
            ticket = self._write_ticket(notif, reasons, facts)
            self._append_cron_failure(notif, reasons)
        return verdict, reasons, ticket

    # ---- diagnostics ----
    def _write_ticket(self, notif, reasons, facts) -> Dict:
        kind = _classify(reasons)
        ticket = {
            "ts": datetime_iso(),
            "verdict": "block",
            "notif_id": notif.get("id"),
            "reasons": reasons,
            "category": kind,
            "root_cause_hypothesis": _hypothesis(kind, reasons, facts),
            "suggested_fix": _suggestion(kind),
            "body_excerpt": (notif.get("body", "") or "")[:400],
        }
        try:
            self._failures_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._failures_file, "a") as f:
                f.write(json.dumps(ticket, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("validator: failed to write ticket: %s", e)
        return ticket

    def _append_cron_failure(self, notif, reasons):
        try:
            rec = {"timestamp": datetime_iso(), "job": "report_validator",
                   "exit_code": 1,
                   "detail": f"blocked notif {notif.get('id')}: {reasons[0]}"}
            with open(self._cron_failures_file, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _classify(reasons: List[str]) -> str:
    joined = "; ".join(reasons)
    if "exchange-unreachable" in joined:
        return "transient_network_or_proxy"
    if "duplicate-event" in joined:
        return "duplicate_notification"
    if "position-claim-mismatch" in joined:
        return "db_state_divergence"
    if "orders-claim-mismatch" in joined:
        return "exchange_vs_report_divergence"
    if "balance-claim-mismatch" in joined:
        return "cash_ledger_divergence"
    return "unknown"


def _hypothesis(kind, reasons, facts) -> str:
    if kind == "transient_network_or_proxy":
        return ("session direct-connect blocked (no proxy env in cron shell) or "
                "exchange read failed; get_open_orders returns [] on failure so "
                "'0 orders' claims are unverifiable")
    if kind == "duplicate_notification":
        return ("re-scan of an already-booked event; notifier re-fired the same "
                "OPEN/CLOSE within the 24h window")
    if kind == "db_state_divergence":
        return ("report text produced from a stale/parallel state snapshot; DB "
                "portfolio is authoritative")
    if kind == "exchange_vs_report_divergence":
        return ("report composed before/after an exchange state change, or a "
                "stale cache served the report")
    if kind == "cash_ledger_divergence":
        return ("DB kv.cash_balance and exchange wallet drifted (missing fee "
                "booking, unbooked fill, or stale report figure)")
    return "; ".join(reasons)[:300]


def _suggestion(kind) -> str:
    if kind == "transient_network_or_proxy":
        return ("retry with pinned proxy (apply_session_proxy), verify "
                "127.0.0.1:17890 reachable, then re-run push_notifications")
    if kind == "duplicate_notification":
        return ("drop duplicate; keep only first occurrence; check notifier "
                "re-fire source")
    return ("reconcile DB vs exchange (see trailing-check bug#33-style "
            "reconcile), fix source data, then manually re-queue the report")


def _parse_ts(s: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(s).timestamp()


def datetime_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat()


def validate_all(notifs: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Split unpushed notifs into (allowed, blocked). Blocked ones get marked."""
    validator = ReportValidator()
    allowed, blocked = [], []
    for n in notifs:
        if n.get("pushed"):
            continue
        verdict, reasons, _ = validator.validate(n)
        if verdict == "pass":
            allowed.append(n)
        else:
            n["pushed"] = True  # never re-validated/re-pushed
            n["blocked"] = True
            n["blocked_reason"] = "; ".join(reasons)
            blocked.append(n)
    return allowed, blocked
