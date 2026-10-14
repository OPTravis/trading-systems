#!/usr/bin/env python3
"""wo_status_scanner.py — Pull 底座工单状态扫描器（WO-1047）

纯 stdlib、无 SDK 依赖的可测模块。执行壳复用：
- codeact/scripts/pnl_live_monitor.py（30min Calendar，编程专家侧——WO-1047 接入）
- codeact/scripts/wo_base_patrol.py（Travis 侧 2h 巡检，同源化防逻辑分叉）

规则（相对注入的 now 判定，全部可测）：
  rule1_watcher_stall   : pending > 45min 未 ack → watcher 停摆
  rule2_execution_stuck : acked / in_progress 滞留 > 2h → 执行卡住
  rule3_accept_overdue  : delivered > 30min 仍无 accepted → 验收超时（高优；
                          ≥2h 置 escalated=True，执行壳须 at Leo 升级）
  rule4_deliverables    : delivered 工单交付物三级检索全落空 → 交付物路径缺失
                          （WO-1057 ①：工单 JSON deliverable_path 声明路径优先
                          → deliverables/{wo_id}/ 目录 → worklogs/ 内文件名含
                          该 WO 号；约定：worklogs/=分析报告，deliverables/=
                          脚本与数据产物）
  rule5_inbound         : TI-（to/assignee=Travis）pending → 入站待处理（独立去重）

时间推断链（修复 WO-1047 工单缺陷②「delivered 时间显示 ?」）：
  delivered : delivered_at → history(ts/time) note 含 'delivered' 的最新条目
              → updated_at → 文件 mtime
  执行中    : started_at → in_progress_at → acked_at → history 最新条目
              → updated_at → 文件 mtime
  兼容历史工单四种字段混写（顶层时间戳 / 仅 history / 双缺），显示不再出现 '?'。

去重状态（alert_state，调用方持久化到 codeact/output/wo_alert_state.json）：
  {wo_id: {"status", "first_alert_ts", "last_alert_ts", "escalated"}}
  - 同状态 1h 去重窗口：窗口内不重复告警（仍落 heartbeat 心跳）
  - rule3 delivered ≥2h 未 accepted → escalated=True；升级后每 2h 重发一次
  - 状态变化（accepted 等）→ 条目清除

heartbeat（工单需求④）：每次扫描 append 一行 JSONL 到
  work_orders_pull/heartbeat/scan_log.jsonl
  含扫描时间 + orders_scanned + 发现项（告警计数）+ notified 标志；
  无告警也落痕（监控自身存活的唯一证据——WO-1046g 断点教训）。
  滚动保留最近 MAX_HEARTBEAT_LINES 行。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 常量 ────────────────────────────────────────────────────────────
TZ_SHANGHAI = timezone(timedelta(hours=8))

PENDING_THRESHOLD_MIN = 45        # rule1: pending 超 45 分钟
ACTIVE_THRESHOLD_MIN = 120        # rule2: acked/in_progress 滞留 2 小时
ACCEPT_OVERDUE_MIN = 30           # rule3: delivered 超时阈值（工单规格 30min）
ESCALATE_AFTER_MIN = 120          # rule3: delivered ≥2h 升级
REPEAT_WINDOW_MIN = 60            # 同状态去重窗口
ESCALATED_REPEAT_MIN = 120        # 升级后重发间隔
MAX_HEARTBEAT_LINES = 2000        # heartbeat jsonl 滚动上限（30min/次 ≈ 41 天）

RULE_KEYS = (
    "rule1_watcher_stall",
    "rule2_execution_stuck",
    "rule3_accept_overdue",
    "rule4_deliverables",
    "rule5_inbound",
)


# ── 时间工具 ────────────────────────────────────────────────────────
def now_shanghai() -> datetime:
    return datetime.now(TZ_SHANGHAI)


def parse_iso(s: str | None) -> datetime | None:
    """解析工单时间字段；naive 视为 Asia/Shanghai；非法返回 None。"""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_SHANGHAI)
    return dt


def minutes_since(dt: datetime | None, now: datetime) -> float:
    if dt is None:
        return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_SHANGHAI)
    return (now - dt).total_seconds() / 60.0


def fmt_duration(minutes: float) -> str:
    if minutes == float("inf"):
        return "未知时长"
    if minutes < 60:
        return f"{int(minutes)} 分钟"
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    return f"{hours} 小时" if mins == 0 else f"{hours} 小时 {mins} 分钟"


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    return dt.astimezone(TZ_SHANGHAI).strftime("%m-%d %H:%M")


# ── 工单加载与时间推断 ─────────────────────────────────────────────
def load_wo(filepath: Path) -> dict | None:
    """加载工单 JSON；缺 wo_id/id 或 status 的文件跳过（返回 None）。"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if not (data.get("wo_id") or data.get("id")) or not data.get("status"):
            return None
        return data
    except Exception:
        return None


def _history_ts(wo: dict, keyword: str | None = None) -> datetime | None:
    """取 history 中匹配 note 关键词的最新条目时间（ts/time 双字段兼容）。
    keyword=None → 任意最新条目。"""
    history = wo.get("history")
    if not isinstance(history, list):
        return None
    candidates: list[datetime] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        if keyword is not None:
            note = str(entry.get("note") or "")
            action = str(entry.get("action") or "")
            if (keyword.lower() not in note.lower()
                    and keyword.lower() not in action.lower()):
                continue
        ts = parse_iso(entry.get("ts") or entry.get("time"))
        if ts is not None:
            candidates.append(ts)
    return max(candidates) if candidates else None


def infer_delivered_at(wo: dict, filepath: Path) -> datetime | None:
    """delivered 时间推断链（修复缺陷②：顶层字段缺失时不再显示 ?）。"""
    # 1. 顶层 delivered_at（WO-1029 等）
    dt = parse_iso(wo.get("delivered_at"))
    if dt is not None:
        return dt
    # 2. history 里 note 含 'delivered' 的最新条目（WO-1046 系列惯例）
    dt = _history_ts(wo, "delivered")
    if dt is not None:
        return dt
    # 3. updated_at
    dt = parse_iso(wo.get("updated_at"))
    if dt is not None:
        return dt
    # 4. 文件 mtime 兜底
    try:
        return datetime.fromtimestamp(filepath.stat().st_mtime, tz=TZ_SHANGHAI)
    except OSError:
        return None


def infer_active_since(wo: dict, filepath: Path) -> datetime | None:
    """执行中（acked / in_progress）滞留基准时间推断链。"""
    for key in ("started_at", "in_progress_at", "acked_at"):
        dt = parse_iso(wo.get(key))
        if dt is not None:
            return dt
    dt = _history_ts(wo)  # 最新任意条目（ack/开工 history note）
    if dt is not None:
        return dt
    dt = parse_iso(wo.get("updated_at"))
    if dt is not None:
        return dt
    try:
        return datetime.fromtimestamp(filepath.stat().st_mtime, tz=TZ_SHANGHAI)
    except OSError:
        return None


def is_inbound(filename: str, wo: dict) -> bool:
    if not filename.startswith("TI-"):
        return False
    to_field = (wo.get("to") or "").strip().lower()
    assignee_field = (wo.get("assignee") or "").strip().lower()
    return to_field == "travis" or assignee_field == "travis"


# ── deliverables 检索（工单需求③；WO-1057 ① 三级检索）─────────────
def check_deliverables(deliverables_dir: Path | None, wo_id: str,
                       *, deliverable_path=None,
                       worklogs_dir: Path | None = None,
                       project_root: Path | None = None) -> dict | None:
    """检查 delivered 工单的交付物可定位性（WO-1057 ① 三级检索）。

    优先级（任一命中即算可定位，全部落空才告警 rule4）：
      1. deliverable_path：工单 JSON 声明路径（str 或 list[str]；绝对路径
         原样、相对路径按 project_root 解析）——文件存在或目录非空即命中；
      2. deliverables/{wo_id}/ 目录存在且含文件；
      3. worklogs/ 内文件名含该 WO 号（分析报告类交付的自然约定）。

    deliverables_dir 为 None 时返回 None（rule4 关闭，兼容旧调用契约：
    该参数仍是 rule4 的总开关）。

    返回 {"path", "exists", "files", "source"}，source ∈
    declared / deliverables / worklogs / none（多路命中取优先级最高者）。
    """
    if deliverables_dir is None:
        return None

    declared_list = []
    if isinstance(deliverable_path, str):
        declared_list = [deliverable_path]
    elif isinstance(deliverable_path, (list, tuple)):
        declared_list = [p for p in deliverable_path
                         if isinstance(p, str) and p.strip()]

    def _dir_files(d: Path):
        try:
            return [p for p in d.rglob("*") if p.is_file()]
        except OSError:
            return []

    # 1) 声明路径（声明即契约，优先级最高）
    for raw in declared_list:
        p = Path(raw.strip())
        if not p.is_absolute() and project_root is not None:
            p = project_root / p
        try:
            if p.is_file():
                return {"path": str(p), "exists": True, "files": 1,
                        "source": "declared"}
            if p.is_dir():
                files = _dir_files(p)
                if files:
                    return {"path": str(p), "exists": True,
                            "files": len(files), "source": "declared"}
        except OSError:
            continue

    # 2) deliverables/{wo_id}/
    d = deliverables_dir / wo_id
    files = _dir_files(d)
    if files:
        return {"path": str(d), "exists": True, "files": len(files),
                "source": "deliverables"}

    # 3) worklogs/ 文件名含 WO 号
    if worklogs_dir is not None:
        try:
            matches = [p for p in worklogs_dir.glob(f"*{wo_id}*")
                       if p.is_file()]
        except OSError:
            matches = []
        if matches:
            return {"path": str(matches[0]), "exists": True,
                    "files": len(matches), "source": "worklogs"}

    fallback = (str(d) if deliverables_dir is not None
                else (str(declared_list[0]) if declared_list else ""))
    return {"path": fallback, "exists": False, "files": 0, "source": "none"}


# ── 去重状态 ───────────────────────────────────────────────────────
def _alert_due(state_entry: dict | None, now: datetime, escalated: bool) -> bool:
    """按去重窗口判定本轮是否需要（重新）告警。

    新升级（本轮 escalated=True 而历史未升级）是状态翻转新事件，立即触发，
    不受普通窗口压制——否则 delivered 2h 升级会被首轮告警的 1h 窗口吃掉。
    """
    if state_entry is None:
        return True
    last = parse_iso(state_entry.get("last_alert_ts"))
    if last is None:
        return True
    was_escalated = bool(state_entry.get("escalated"))
    if escalated and not was_escalated:
        return True
    window = ESCALATED_REPEAT_MIN if escalated else REPEAT_WINDOW_MIN
    return minutes_since(last, now) >= window


def _prune_state(alert_state: dict, live_ids: set[str]) -> dict:
    """存在性清除：工单文件消失 → 移除去重条目。

    状态性清除（accepted 等不再告警的状态）在 scan 循环内按工单即时执行：
    live_ids 覆盖所有被扫描到的工单（含 accepted），不能用于状态判定。
    """
    return {k: v for k, v in alert_state.items() if k in live_ids}


# ── heartbeat（工单需求④）─────────────────────────────────────────
def append_heartbeat(
    heartbeat_path: Path,
    now: datetime,
    scan_meta: dict,
    alert_counts: dict,
    notified: bool,
) -> dict:
    """append 一行心跳到 jsonl；返回该行内容。滚动截断保最近 N 行。"""
    entry = {
        "scan_time": now.isoformat(),
        "source": scan_meta.get("source", ""),
        "orders_scanned": scan_meta.get("orders_scanned", 0),
        "parse_failures": scan_meta.get("parse_failures", 0),
        "findings": {k: alert_counts.get(k, 0) for k in RULE_KEYS},
        "total_alerts": sum(alert_counts.get(k, 0) for k in RULE_KEYS),
        "notified": notified,
    }
    try:
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if heartbeat_path.exists():
            with open(heartbeat_path, "r", encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        lines.append(json.dumps(entry, ensure_ascii=False))
        if len(lines) > MAX_HEARTBEAT_LINES:
            lines = lines[-MAX_HEARTBEAT_LINES:]
        tmp = heartbeat_path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, heartbeat_path)
    except OSError:
        # 心跳失败不阻塞扫描主流程（告警优先于留痕）
        pass
    return entry


# ── 核心扫描 ───────────────────────────────────────────────────────
def scan_work_orders(
    wo_dir: Path,
    now: datetime,
    *,
    deliverables_dir: Path | None = None,
    worklogs_dir: Path | None = None,
    alert_state: dict | None = None,
    inbound_state: dict | None = None,
    heartbeat_path: Path | None = None,
    source: str = "",
) -> dict:
    """扫描工单目录，返回告警/状态/心跳。

    参数：
      wo_dir          : work_orders_pull/ 目录
      now             : 注入的当前时刻（tz-aware；测试可控）
      deliverables_dir: work_orders_pull/deliverables（None=关闭 rule4；
                          传入即启用三级检索）
      worklogs_dir     : worklogs/ 报告目录（None=自动取
                          wo_dir.parent/worklogs——WO-1057 ① 回退路径）
      alert_state     : 上一轮去重状态（None={}；调用方持久化返回值）
      inbound_state   : 原 inbound 去重 dict（None={}；调用方持久化）
      heartbeat_path  : heartbeat jsonl 路径（None=不落心跳）

    返回：
      {
        "alerts": {rule: [entries...]},   # 本轮应上报的告警（已过去重窗口）
        "alert_state": {...},             # 新去重状态（含本轮已告警时间戳）
        "inbound_state": {...},           # 新 inbound 去重状态
        "has_alerts": bool,
        "escalated": bool,                # 本轮含升级告警（执行壳 at Leo）
        "heartbeat": {...},               # 本轮心跳条目（含未过窗口的静默项）
        "scan_meta": {"orders_scanned", "parse_failures"},
      }
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ_SHANGHAI)

    # WO-1057 ①: worklogs 报告目录（分析报告类交付的回退检索路径）。
    # 仅在 rule4 开启（deliverables_dir 传入）时推导，保持旧调用契约。
    if deliverables_dir is not None and worklogs_dir is None:
        worklogs_dir = wo_dir.parent / "worklogs"

    alerts: dict[str, list] = {k: [] for k in RULE_KEYS}
    state = dict(alert_state or {})
    inbound = dict(inbound_state or {})
    escalated_any = False
    orders_scanned = 0
    parse_failures = 0
    live_ids: set[str] = set()

    if not wo_dir.exists():
        result_meta = {"orders_scanned": 0, "parse_failures": 0,
                       "error": f"工单目录不存在: {wo_dir}"}
        return {
            "alerts": alerts,
            "alert_state": _prune_state(state, live_ids),
            "inbound_state": inbound,
            "has_alerts": False,
            "escalated": False,
            "heartbeat": None,
            "scan_meta": result_meta,
        }

    for fp in sorted(wo_dir.glob("*.json")):
        wo = load_wo(fp)
        if wo is None:
            parse_failures += 1
            continue
        orders_scanned += 1

        wo_id = wo.get("wo_id") or wo.get("id")
        status = wo.get("status", "")
        to = wo.get("to", "未指定")
        live_ids.add(wo_id)

        # ── rule5: 入站工单（TI- → Travis，pending；独立去重） ──
        if is_inbound(fp.name, wo):
            prev = inbound.get(wo_id)
            if status == "pending" and prev != "pending":
                task = wo.get("task") or wo.get("description") or "无描述"
                if len(task) > 80:
                    task = task[:77] + "..."
                alerts["rule5_inbound"].append({
                    "wo_id": wo_id,
                    "created_by": wo.get("created_by") or wo.get("from") or "未知",
                    "task": task,
                    "created_at": wo.get("created_at", "?"),
                    "priority": wo.get("priority", "normal"),
                })
                inbound[wo_id] = "pending"
            elif prev != status:
                inbound[wo_id] = status
            continue

        # ── rule1: pending → watcher 停摆 ──
        if status == "pending":
            mins = minutes_since(parse_iso(wo.get("created_at")), now)
            if mins > PENDING_THRESHOLD_MIN:
                if _alert_due(state.get(wo_id), now, escalated=False):
                    alerts["rule1_watcher_stall"].append({
                        "wo_id": wo_id, "to": to, "status": status,
                        "since": fmt_duration(mins), "since_min": round(mins),
                        "created_at": wo.get("created_at", "?"),
                    })
                    state[wo_id] = {"status": status,
                                    "first_alert_ts": now.isoformat(),
                                    "last_alert_ts": now.isoformat(),
                                    "escalated": False}
            continue

        # ── rule2: acked / in_progress 滞留 ──
        if status in ("acked", "in_progress"):
            mins = minutes_since(infer_active_since(wo, fp), now)
            if mins > ACTIVE_THRESHOLD_MIN:
                if _alert_due(state.get(wo_id), now, escalated=False):
                    alerts["rule2_execution_stuck"].append({
                        "wo_id": wo_id, "to": to, "status": status,
                        "since": fmt_duration(mins), "since_min": round(mins),
                    })
                    state[wo_id] = {"status": status,
                                    "first_alert_ts": now.isoformat(),
                                    "last_alert_ts": now.isoformat(),
                                    "escalated": False}
            continue

        # ── rule3/4: delivered → 验收超时 + 交付物检索 ──
        if status == "delivered":
            delivered_dt = infer_delivered_at(wo, fp)
            mins = minutes_since(delivered_dt, now)
            deliv = check_deliverables(
                deliverables_dir, wo_id,
                deliverable_path=wo.get("deliverable_path"),
                worklogs_dir=worklogs_dir,
                project_root=wo_dir.parent)

            if mins > ACCEPT_OVERDUE_MIN:
                escalated = bool(
                    state.get(wo_id, {}).get("escalated")
                ) or mins >= ESCALATE_AFTER_MIN
                if _alert_due(state.get(wo_id), now, escalated):
                    entry = {
                        "wo_id": wo_id, "to": to, "status": status,
                        "delivered_at": _fmt_dt(delivered_dt),
                        "since": fmt_duration(mins), "since_min": round(mins),
                        "deliverables": deliv,
                        "escalated": escalated,
                    }
                    alerts["rule3_accept_overdue"].append(entry)
                    state[wo_id] = {
                        "status": status,
                        "first_alert_ts": (state.get(wo_id, {})
                                           .get("first_alert_ts")
                                           or now.isoformat()),
                        "last_alert_ts": now.isoformat(),
                        "escalated": escalated,
                    }
                    if escalated:
                        escalated_any = True

            # rule4 独立于超时判定：delivered 即查交付物（检索路径需求③）
            if deliv is not None and (not deliv["exists"] or deliv["files"] == 0):
                if _alert_due(state.get(f"{wo_id}:deliv"), now, escalated=False):
                    alerts["rule4_deliverables"].append({
                        "wo_id": wo_id, "to": to,
                        "deliverables": deliv,
                        "hint": "工单已交付但三级检索均未命中"
                                "（deliverable_path 声明路径 / deliverables/ "
                                "目录 / worklogs/ 文件名），"
                                "验收方无法定位交付物",
                    })
                    state[f"{wo_id}:deliv"] = {
                        "status": status,
                        "first_alert_ts": now.isoformat(),
                        "last_alert_ts": now.isoformat(),
                        "escalated": False,
                    }
            continue
        # accepted / closed / 其他 → 正常忽略；状态性清除去重条目
        # （工单已验收：rule1/2/3/4 条目全部失效，防止陈旧 state 压制
        #   同 id 新工单——工单 id 不复用，此处主要为 accepted 卫生）
        state.pop(wo_id, None)
        state.pop(f"{wo_id}:deliv", None)

    # 存在性清除：文件已消失的工单条目移除
    state = _prune_state(state, live_ids | {f"{i}:deliv" for i in live_ids})

    alert_counts = {k: len(v) for k, v in alerts.items()}
    has_alerts = any(alert_counts.values())
    scan_meta = {"orders_scanned": orders_scanned,
                 "parse_failures": parse_failures, "source": source}

    heartbeat = None
    if heartbeat_path is not None:
        heartbeat = append_heartbeat(
            heartbeat_path, now, scan_meta, alert_counts, notified=has_alerts)

    return {
        "alerts": alerts,
        "alert_state": state,
        "inbound_state": inbound,
        "has_alerts": has_alerts,
        "escalated": escalated_any,
        "heartbeat": heartbeat,
        "scan_meta": scan_meta,
    }


# ── 告警文案（执行壳用；保持纯函数可测）───────────────────────────
def build_alert_message(result: dict) -> str:
    """把 scan_work_orders 结果组装成结构化告警文本（给主 agent 的唤醒正文）。"""
    alerts = result.get("alerts", {})
    lines = ["🔍 **工单状态巡检（WO-1047 链路）**", ""]
    has_any = False

    r5 = alerts.get("rule5_inbound", [])
    if r5:
        has_any = True
        lines.append(f"📬 **入站工单待处理**（{len(r5)} 单）")
        for item in r5:
            lines.append(f"  - `{item['wo_id']}` | 来自 {item['created_by']} | "
                         f"优先级 {item['priority']} | 任务: {item['task']}")
        lines.append("")

    r1 = alerts.get("rule1_watcher_stall", [])
    if r1:
        has_any = True
        lines.append(f"⚠️ **watcher 疑似停摆**（{len(r1)} 单）")
        for item in r1:
            lines.append(f"  - `{item['wo_id']}` → {item['to']}，pending 滞留 {item['since']}")
        lines.append("")

    r2 = alerts.get("rule2_execution_stuck", [])
    if r2:
        has_any = True
        lines.append(f"⚠️ **执行卡住**（{len(r2)} 单）")
        for item in r2:
            lines.append(f"  - `{item['wo_id']}` → {item['to']}，"
                         f"{item['status']} 滞留 {item['since']}")
        lines.append("")

    r3 = alerts.get("rule3_accept_overdue", [])
    if r3:
        has_any = True
        overdue = [i for i in r3 if not i.get("escalated")]
        esc = [i for i in r3 if i.get("escalated")]
        if overdue:
            lines.append(f"🚨 **验收超时（>30min 无 accepted）**（{len(overdue)} 单）")
            for item in overdue:
                deliv = item.get("deliverables")
                d_str = ""
                if deliv:
                    d_str = (f"，交付物 {deliv['files']} 文件 @ {deliv['path']}"
                             if deliv.get("files") else "，⚠️交付物目录缺失")
                lines.append(f"  - `{item['wo_id']}` → {item['to']}，"
                             f"delivered {item['delivered_at']}"
                             f"（已 {item['since']}）{d_str}")
            lines.append("")
        if esc:
            lines.append(f"🔥 **验收超时升级（≥2h，请 at Leo）**（{len(esc)} 单）")
            for item in esc:
                lines.append(f"  - `{item['wo_id']}` → {item['to']}，"
                             f"delivered {item['delivered_at']}"
                             f"（已 {item['since']}）")
            lines.append("")

    r4 = alerts.get("rule4_deliverables", [])
    if r4:
        has_any = True
        lines.append(f"📦 **交付物路径缺失**（{len(r4)} 单）")
        for item in r4:
            lines.append(f"  - `{item['wo_id']}`：{item['hint']}")
        lines.append("")

    if not has_any:
        return ""
    return "\n".join(lines)


def build_forward_instruction(result: dict) -> str:
    """生成给主 agent 的转发协议指令（WO-1047 工单需求①：notify 唤醒不是只写日志）。"""
    alerts = result.get("alerts", {})
    parts = []
    if alerts.get("rule3_accept_overdue"):
        parts.append(
            "【转发协议】delivered 验收超时：请立即 at_agent request 通知 Travis"
            "（claw_id 7642016183574905126），正文附上方 rule3 清单与交付物路径，"
            "请其验收或回写 accepted。"
        )
    if result.get("escalated"):
        parts.append(
            "【升级协议】delivered 已 ≥2h 无 accepted：本条回复正文必须 at Leo "
            "[用户](at://user:1307735377318496_0) 提请人工关注（cron 会话 at_agent "
            "有静默失败风险，Leo 是必达通道），并同步 at_agent 通知 Travis。"
        )
    if alerts.get("rule1_watcher_stall"):
        parts.append("【处置】watcher 停摆：核对 watchers_registry.md 对应行，走三级派单补救。")
    if alerts.get("rule2_execution_stuck"):
        parts.append("【处置】执行卡住：评估是否催办或重新派单。")
    return "\n".join(parts)
