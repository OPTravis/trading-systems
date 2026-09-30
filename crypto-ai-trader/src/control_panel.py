"""
WO-1001-1: remote control panel — REST + Telegram thin adapters over a
single command handler. Sidecar process: if it dies the trading loop is
unaffected (fail-open by architecture).

Security model (approved 2026-09-30, Travis review):
  - dual tokens: CTRL_RO_TOKEN (read-only) / CTRL_RW_TOKEN (all commands)
  - IP allowlist: 127.0.0.1 by default; public bind needs
    CTRL_BIND_PUBLIC=1 AND a non-empty CTRL_ALLOWED_IPS (else refuse start)
  - Telegram: chat-id allowlist + update_id dedup (replay defence)
  - every RW command audited to ledger_events (source=control_panel,
    token short-fingerprint) and logs/control_panel.log
  - /forceexit requires a 60s CONFIRM round-trip and, as an explicit human
    order, overrides exit kill-switch "off" (audited override_exit_mode)
  - rate limit 10 cmd/min per token

Run: scripts/run_control_panel.sh (see ops notes at repo docs/CONTROL_PANEL.md)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

RO_TOKEN = os.environ.get("CTRL_RO_TOKEN", "")
RW_TOKEN = os.environ.get("CTRL_RW_TOKEN", "")
ALLOWED_IPS = {
    ip.strip() for ip in os.environ.get("CTRL_ALLOWED_IPS", "127.0.0.1").split(",")
    if ip.strip()
}
BIND_PUBLIC = os.environ.get("CTRL_BIND_PUBLIC", "") == "1"
RATE_LIMIT_PER_MIN = 10
FORCEEXIT_CONFIRM_S = 60.0

_rate: Dict[str, Deque[float]] = {}
_rate_lock = threading.Lock()
_pending_force: Dict[str, Tuple[float, str]] = {}   # actor -> (ts, spec)
_pending = _pending_force  # alias (module attr patched in tests)
_pending_lock = threading.Lock()
_seen_tg_updates: Deque[int] = deque(maxlen=500)


def _fp(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def _rate_ok(actor: str) -> bool:
    now = time.time()
    with _rate_lock:
        q = _rate.setdefault(actor, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            return False
        q.append(now)
        return True


def _audit(db: Any, actor: str, cmd: str, detail: str, extra: Optional[Dict] = None) -> None:
    payload = {"actor": actor, "cmd": cmd, "detail": detail, **(extra or {})}
    try:
        conn = db._get_conn()
        conn.execute(
            "INSERT INTO ledger_events (ts, type, symbol, qty, price, source, "
            "payload_json) VALUES (?,?,?,?,?,?,?)",
            (time.time(), "CTRL_CMD", detail.split()[0] if detail else "-", 0.0,
             0.0, "control_panel", json.dumps(payload, default=str)),
        )
        conn.commit()
    except Exception as exc:
        logger.warning("control_panel audit write failed: %s", exc)
    logger.info("CTRL %s %s :: %s", actor, cmd, detail)


def _token_role(token: str) -> Optional[str]:
    if not token:
        return None
    if RW_TOKEN and token == RW_TOKEN:
        return "rw"
    if RO_TOKEN and token == RO_TOKEN:
        return "ro"
    return None


# ── command handler (single source for both adapters) ────────────────

def handle_command(
    cmd_line: str, *, role: str, actor: str, db: Any,
    confirm: Optional[str] = None,
) -> Dict[str, Any]:
    """cmd_line like '/status', '/forceexit 0GUSDT'. Pure dispatcher —
    transport adapters (REST/TG) parse and call this."""
    if role not in ("ro", "rw"):
        return {"ok": False, "http": 401, "msg": "unauthorized"}
    parts = cmd_line.strip().split()
    cmd = (parts[0] if parts else "").lstrip("/").lower()
    arg = parts[1] if len(parts) > 1 else ""
    ro_cmds = {"status"}
    rw_cmds = {"pause", "resume", "forceexit", "exit-mode"}

    if cmd in ro_cmds and role not in ("ro", "rw"):
        return {"ok": False, "http": 403, "msg": "forbidden"}
    if cmd in rw_cmds and role != "rw":
        return {"ok": False, "http": 403, "msg": "forbidden: read-only token"}
    if cmd not in ro_cmds | rw_cmds:
        return {"ok": False, "http": 400, "msg": f"unknown command: {cmd}"}
    if not _rate_ok(actor):
        return {"ok": False, "http": 429, "msg": "rate limit exceeded"}

    if cmd == "status":
        return _cmd_status(db)
    if cmd == "pause":
        return _cmd_pause(db, actor)
    if cmd == "resume":
        return _cmd_resume(db, actor)
    if cmd == "forceexit":
        return _cmd_forceexit(db, actor, arg.upper(), confirm)
    if cmd == "exit-mode":
        return _cmd_exit_mode(db, actor, arg.lower())
    return {"ok": False, "http": 400, "msg": "unhandled"}


def _cmd_status(db: Any) -> Dict[str, Any]:
    try:
        conn = db._get_conn()
        holdings = conn.execute(
            "SELECT symbol, quantity, entry_price, strategy FROM portfolio "
            "WHERE quantity > 0").fetchall()
        locks = {k: json.loads(v) if isinstance(v, str) else v
                 for k, v in (db.kv_get_prefix("prot:lock:") or {}).items()}
        mode = db.kv_get("exit:mode") or "auto"
        halted = os.environ.get("NEW_POSITIONS_HALTED", "1")
        # SSOT view if the store is live for this process
        try:
            from src.config_store import cfg_get
            halted = cfg_get("NEW_POSITIONS_HALTED", halted)
        except Exception:
            pass
        return {"ok": True, "http": 200, "positions": [
            {"symbol": h[0], "qty": h[1], "entry": h[2], "strategy": h[3]}
            for h in holdings],
            "protection_locks": locks, "exit_mode": mode,
            "new_positions_halted": str(halted) in ("1", "true", "yes", "on"),
            "ts": time.time()}
    except Exception as exc:
        return {"ok": False, "http": 500, "msg": f"status failed: {exc}"}


def _cmd_pause(db: Any, actor: str) -> Dict[str, Any]:
    from src.config_store import set_override
    set_override("NEW_POSITIONS_HALTED", "1")
    _audit(db, actor, "pause", "NEW_POSITIONS_HALTED=1 (no new entries)")
    return {"ok": True, "http": 200, "msg": "paused — no new entries; exits/trailing unaffected"}


def _cmd_resume(db: Any, actor: str) -> Dict[str, Any]:
    from src.config_store import clear_override, set_override
    set_override("NEW_POSITIONS_HALTED", "0")
    _audit(db, actor, "resume", "NEW_POSITIONS_HALTED=0 (entries resumed)")
    return {"ok": True, "http": 200, "msg": "resumed"}


def _cmd_forceexit(db: Any, actor: str, spec: str, confirm: Optional[str]) -> Dict[str, Any]:
    from src.exit_check import execute_exit, evaluate_exits
    from src.paper_trader import get_trading_client
    if spec not in ("ALL",) and not spec.endswith("USDT"):
        return {"ok": False, "http": 400, "msg": "usage: /forceexit SYMBOLUSDT|ALL"}
    now = time.time()
    with _pending_lock:
        pend = _pending.get(actor)
    if pend and confirm and confirm.upper() == "CONFIRM" and spec == pend[1] \
            and now - pend[0] <= FORCEEXIT_CONFIRM_S:
        with _pending_lock:
            _pending.pop(actor, None)
    elif pend and now - pend[0] > FORCEEXIT_CONFIRM_S:
        with _pending_lock:
            _pending.pop(actor, None)
        pend = None
    if not (confirm and confirm.upper() == "CONFIRM" and pend and spec == pend[1]
            and now - pend[0] <= FORCEEXIT_CONFIRM_S):
        # stage 1: echo the blast radius and require confirmation
        try:
            sql = ("SELECT symbol, quantity FROM portfolio WHERE quantity > 0"
                   + (" AND symbol=?" if spec != "ALL" else ""))
            args: tuple = () if spec == "ALL" else (spec,)
            holdings = [{"symbol": r[0], "qty": r[1]}
                        for r in db._get_conn().execute(sql, args).fetchall()]
        except Exception:
            holdings = []
        with _pending_lock:
            _pending[actor] = (now, spec)
        return {"ok": True, "http": 200, "confirm_required": True,
                "will_exit": holdings,
                "msg": f"reply CONFIRM within {int(FORCEEXIT_CONFIRM_S)}s to "
                       f"force-exit {spec}"}

    # stage 2: execute. Explicit human order overrides exit-mode kill-switch
    # (audited) — an emergency exit must not be blocked by 'off'.
    override_mode = False
    mode = db.kv_get("exit:mode") or "auto"
    if mode == "off":
        override_mode = True
    client = get_trading_client()
    results = []
    targets = [r[0] for r in db._get_conn().execute(
        "SELECT symbol FROM portfolio WHERE quantity > 0"
        + (" AND symbol=?" if spec != "ALL" else ""),
        () if spec == "ALL" else (spec,)).fetchall()]
    for sym in targets:
        try:
            px = float(client.get_ticker_price(sym) or 0.0)
        except Exception:
            px = 0.0
        decision = {"kind": "manual_forceexit", "symbol": sym,
                    "reason": "control panel manual force exit",
                    "qty": _qty_of(db, sym), "sell_pct": 100, "price": px}
        r = execute_exit(client, db, decision, bypass_cooldown=True)
        r["symbol"] = sym
        results.append(r)
    _audit(db, actor, "forceexit", f"{spec} exit_mode={mode}",
           {"override_exit_mode": override_mode, "results": results})
    return {"ok": True, "http": 200, "override_exit_mode": override_mode,
            "results": results}


def _qty_of(db: Any, symbol: str) -> float:
    try:
        row = db._get_conn().execute(
            "SELECT quantity FROM portfolio WHERE symbol=?", (symbol,)).fetchone()
        return float(row[0]) if row and row[0] else 0.0
    except Exception:
        return 0.0


def _cmd_exit_mode(db: Any, actor: str, mode: str) -> Dict[str, Any]:
    if mode not in ("auto", "notify", "off"):
        return {"ok": False, "http": 400,
                "msg": "usage: /exit-mode auto|notify|off"}
    db.kv_set("exit:mode", mode)
    _audit(db, actor, "exit-mode", f"exit:mode={mode}")
    return {"ok": True, "http": 200, "msg": f"exit:mode -> {mode}"}


# ── REST adapter ─────────────────────────────────────────────────────

class _RESTHandler(BaseHTTPRequestHandler):
    server_version = "ctrl/1.0"

    def log_message(self, fmt, *args):  # route to std logging
        logger.info("rest %s %s", self.address_string(), fmt % args)

    def _client_ip(self) -> str:
        return self.client_address[0]

    def _reply(self, code: int, body: Dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # GET /status  (read-only surface)
        if self._client_ip() not in ALLOWED_IPS:
            return self._reply(403, {"ok": False, "msg": "ip not allowed"})
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        role = _token_role(token)
        if role is None:
            return self._reply(401, {"ok": False, "msg": "bad token"})
        from src.state_db import get_state_db
        if self.path.split("?")[0].strip("/") != "status":
            return self._reply(405, {"ok": False, "msg": "GET only supports /status"})
        out = handle_command("status", role=role, actor=f"rest:{_fp(token)}",
                             db=get_state_db())
        self._reply(out.pop("http", 200), out)

    def do_POST(self):  # POST /cmd {"cmd": "/pause", "confirm": "CONFIRM"}
        if self._client_ip() not in ALLOWED_IPS:
            return self._reply(403, {"ok": False, "msg": "ip not allowed"})
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        role = _token_role(token)
        if role is None:
            return self._reply(401, {"ok": False, "msg": "bad token"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._reply(400, {"ok": False, "msg": "bad json"})
        from src.state_db import get_state_db
        out = handle_command(str(body.get("cmd", "")), role=role,
                             actor=f"rest:{_fp(token)}", db=get_state_db(),
                             confirm=body.get("confirm"))
        self._reply(out.pop("http", 200), out)


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    if not (RO_TOKEN or RW_TOKEN):
        raise SystemExit("refusing to start: no CTRL_RO_TOKEN/CTRL_RW_TOKEN set")
    if host not in ("127.0.0.1", "localhost") and not (BIND_PUBLIC and ALLOWED_IPS - {"127.0.0.1"}):
        raise SystemExit("refusing public bind: set CTRL_BIND_PUBLIC=1 and "
                         "CTRL_ALLOWED_IPS with real addresses")
    srv = ThreadingHTTPServer((host, port), _RESTHandler)
    logger.info("control panel listening on %s:%s", host, port)
    srv.serve_forever()


# ── Telegram adapter (enabled when CTRL_TG_BOT_TOKEN is set) ─────────

def tg_poll_once(bot_token: str, allowed_chats: set) -> List[Dict[str, Any]]:
    """Single getUpdates round; returns handled command replies."""
    import urllib.request
    url = (f"https://api.telegram.org/bot{bot_token}/getUpdates"
           f"?timeout=0&allowed_updates=[\"message\"]")
    out: List[Dict[str, Any]] = []
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception as exc:
        logger.warning("tg poll failed: %s", exc)
        return out
    for upd in data.get("result", []):
        uid = upd.get("update_id")
        if uid is None or uid in _seen_tg_updates:
            continue
        _seen_tg_updates.append(uid)
        msg = upd.get("message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if chat not in allowed_chats or not text.startswith("/"):
            continue  # silently drop non-allowlisted chats
        # command auth: chat allowlist == RO; RW requires the RW token inline
        role, actor = "ro", f"tg:{chat}"
        if RW_TOKEN and text.startswith("/auth "):
            token = text.split(maxsplit=1)[1].strip()
            if token == RW_TOKEN:
                role, actor = "rw", f"tg:{chat}:{_fp(token)}"
                text = "/status"
        from src.state_db import get_state_db
        parts = text.split()
        confirm = "CONFIRM" if "CONFIRM" in text.upper().split() else None
        res = handle_command(parts[0], role=role, actor=actor,
                             db=get_state_db(), confirm=confirm)
        out.append({"chat_id": chat, "reply": json.dumps(res, default=str)[:3500]})
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    host = os.environ.get("CTRL_BIND", "127.0.0.1")
    port = int(os.environ.get("CTRL_PORT", "8787"))
    bot_token = os.environ.get("CTRL_TG_BOT_TOKEN", "")
    tg_chats = {c.strip() for c in
                os.environ.get("CTRL_TG_ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
    if bot_token:
        threading.Thread(target=_tg_loop, args=(bot_token, tg_chats),
                         daemon=True).start()
    serve(host, port)


def _tg_loop(bot_token: str, chats: set) -> None:
    import urllib.request
    while True:
        for reply in tg_poll_once(bot_token, chats):
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    data=json.dumps({
                        "chat_id": reply["chat_id"], "text": reply["reply"]
                    }).encode(),
                    headers={"Content-Type": "application/json"}), timeout=10)
            except Exception as exc:
                logger.warning("tg send failed: %s", exc)


if __name__ == "__main__":
    main()
