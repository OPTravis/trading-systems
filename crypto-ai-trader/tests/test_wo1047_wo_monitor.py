"""WO-1047 工单状态监控链路测试。

覆盖 scripts/wo_status_scanner.py：
- 四条告警规则（watcher 停摆 / 执行卡住 / 验收超时 / 交付物缺失）+ 入站工单
- delivered 时间推断链（缺陷②：顶层字段缺失时从 history / updated_at / mtime 推断，
  显示不再出现 '?'）
- 30min 验收超时阈值 + ≥2h 升级（escalated）
- 告警去重窗口（1h）与升级重发间隔（2h）
- heartbeat 留痕（有/无告警都落盘；含扫描时间+发现项+notified 标志——工单需求④）
- deliverables/{wo_id}/ 子目录检索（工单需求③）
- 集成：模拟 delivered 超时工单触发告警的完整链路（验收口径）
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.wo_status_scanner import (  # noqa: E402
    TZ_SHANGHAI,
    append_heartbeat,
    build_alert_message,
    build_forward_instruction,
    check_deliverables,
    infer_active_since,
    infer_delivered_at,
    scan_work_orders,
)

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=TZ_SHANGHAI)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def write_wo(wo_dir: Path, wo_id: str, status: str, *, to: str = "编程专家",
             created_at: str | None = None, extra: dict | None = None,
             history: list | None = None, mtime: datetime | None = None) -> Path:
    """写一个模拟工单 JSON 到 wo_dir。"""
    wo = {"wo_id": wo_id, "id": wo_id, "to": to, "status": status,
          "created_at": created_at or iso(NOW - timedelta(hours=3)),
          "task": f"{wo_id} 模拟任务"}
    if extra:
        wo.update(extra)
    if history is not None:
        wo["history"] = history
    fp = wo_dir / f"{wo_id}.json"
    fp.write_text(json.dumps(wo, ensure_ascii=False, indent=2), encoding="utf-8")
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(fp, (ts, ts))
    return fp


# ────────────────────────────────────────────────────────────────────
# 时间推断链（缺陷②）
# ────────────────────────────────────────────────────────────────────

class TestDeliveredAtInference:
    def test_top_level_delivered_at(self, tmp_path):
        fp = write_wo(tmp_path, "WO-X", "delivered",
                      extra={"delivered_at": iso(NOW - timedelta(hours=1))})
        assert infer_delivered_at(json.loads(fp.read_text()), fp) == NOW - timedelta(hours=1)

    def test_history_note_fallback(self, tmp_path):
        """WO-1046 系列惯例：只写 history note（顶层无 delivered_at）→ 不再显示 ?。"""
        history = [
            {"ts": iso(NOW - timedelta(hours=5)), "by": "x", "note": "ack 并开工"},
            {"ts": iso(NOW - timedelta(hours=2)), "by": "x", "note": "delivered：commit abc 已 push"},
        ]
        fp = write_wo(tmp_path, "WO-X", "delivered", history=history)
        assert infer_delivered_at(json.loads(fp.read_text()), fp) == NOW - timedelta(hours=2)

    def test_history_time_field_alias(self, tmp_path):
        """Travis 回写风格用 time 字段。"""
        history = [{"time": iso(NOW - timedelta(hours=1)), "by": "T",
                    "action": "delivered", "note": "x"}]
        fp = write_wo(tmp_path, "WO-X", "delivered", history=history)
        assert infer_delivered_at(json.loads(fp.read_text()), fp) == NOW - timedelta(hours=1)

    def test_updated_at_fallback(self, tmp_path):
        fp = write_wo(tmp_path, "WO-X", "delivered",
                      extra={"updated_at": iso(NOW - timedelta(minutes=40))})
        assert infer_delivered_at(json.loads(fp.read_text()), fp) == NOW - timedelta(minutes=40)

    def test_mtime_fallback(self, tmp_path):
        """四层全缺 → mtime 兜底，绝不返回 None/显示 ?。"""
        fp = write_wo(tmp_path, "WO-X", "delivered",
                      mtime=NOW - timedelta(minutes=50))
        assert infer_delivered_at(json.loads(fp.read_text()), fp) == NOW - timedelta(minutes=50)

    def test_active_since_chain(self, tmp_path):
        history = [{"ts": iso(NOW - timedelta(hours=3)), "by": "x", "note": "acked"}]
        fp = write_wo(tmp_path, "WO-X", "in_progress", history=history)
        assert infer_active_since(json.loads(fp.read_text()), fp) == NOW - timedelta(hours=3)


# ────────────────────────────────────────────────────────────────────
# 规则 1/2：watcher 停摆 + 执行卡住（含工单要求③ in_progress>2h）
# ────────────────────────────────────────────────────────────────────

class TestStallAndStuck:
    def test_pending_over_45min_alerts(self, tmp_path):
        write_wo(tmp_path, "WO-S1", "pending",
                 created_at=iso(NOW - timedelta(minutes=46)))
        r = scan_work_orders(tmp_path, NOW)
        assert len(r["alerts"]["rule1_watcher_stall"]) == 1
        assert r["has_alerts"]

    def test_pending_fresh_no_alert(self, tmp_path):
        write_wo(tmp_path, "WO-S2", "pending",
                 created_at=iso(NOW - timedelta(minutes=20)))
        r = scan_work_orders(tmp_path, NOW)
        assert r["alerts"]["rule1_watcher_stall"] == []
        assert not r["has_alerts"]

    def test_in_progress_stuck_over_2h(self, tmp_path):
        """工单需求①：status=in_progress 滞留>2h → 告警（旧挂账）。"""
        write_wo(tmp_path, "WO-S3", "in_progress",
                 extra={"started_at": iso(NOW - timedelta(hours=2, minutes=5))})
        r = scan_work_orders(tmp_path, NOW)
        assert len(r["alerts"]["rule2_execution_stuck"]) == 1

    def test_acked_stuck_over_2h(self, tmp_path):
        write_wo(tmp_path, "WO-S4", "acked",
                 extra={"acked_at": iso(NOW - timedelta(hours=3))})
        r = scan_work_orders(tmp_path, NOW)
        assert len(r["alerts"]["rule2_execution_stuck"]) == 1

    def test_active_fresh_no_alert(self, tmp_path):
        write_wo(tmp_path, "WO-S5", "in_progress",
                 extra={"started_at": iso(NOW - timedelta(hours=1))})
        r = scan_work_orders(tmp_path, NOW)
        assert r["alerts"]["rule2_execution_stuck"] == []


# ────────────────────────────────────────────────────────────────────
# 规则 3/4：验收超时（30min 阈值 + 2h 升级）+ deliverables 检索
# ────────────────────────────────────────────────────────────────────

class TestAcceptOverdue:
    def test_delivered_over_30min_alerts(self, tmp_path):
        """验收主路径：delivered>30min 无 accepted → 高优告警。"""
        write_wo(tmp_path, "WO-A1", "delivered",
                 extra={"delivered_at": iso(NOW - timedelta(minutes=35))})
        r = scan_work_orders(tmp_path, NOW)
        assert len(r["alerts"]["rule3_accept_overdue"]) == 1
        entry = r["alerts"]["rule3_accept_overdue"][0]
        assert entry["since_min"] == 35
        assert not entry["escalated"]

    def test_delivered_under_30min_silent(self, tmp_path):
        write_wo(tmp_path, "WO-A2", "delivered",
                 extra={"delivered_at": iso(NOW - timedelta(minutes=25))})
        r = scan_work_orders(tmp_path, NOW)
        assert r["alerts"]["rule3_accept_overdue"] == []
        assert not r["has_alerts"]

    def test_delivered_over_2h_escalated(self, tmp_path):
        write_wo(tmp_path, "WO-A3", "delivered",
                 extra={"delivered_at": iso(NOW - timedelta(hours=3))})
        r = scan_work_orders(tmp_path, NOW)
        assert len(r["alerts"]["rule3_accept_overdue"]) == 1
        assert r["alerts"]["rule3_accept_overdue"][0]["escalated"] is True
        assert r["escalated"]

    def test_history_only_delivered_overdue_alerts(self, tmp_path):
        """缺陷②联动：无顶层 delivered_at 的历史工单也能算出超时。"""
        history = [{"ts": iso(NOW - timedelta(minutes=45)), "by": "x",
                    "note": "delivered：commit b2c6b07 已 push"}]
        write_wo(tmp_path, "WO-A4", "delivered", history=history)
        r = scan_work_orders(tmp_path, NOW)
        assert len(r["alerts"]["rule3_accept_overdue"]) == 1
        assert r["alerts"]["rule3_accept_overdue"][0]["delivered_at"] != "?"

    def test_accepted_silent_and_state_pruned(self, tmp_path):
        state = {"WO-A5": {"status": "delivered", "escalated": True,
                           "last_alert_ts": iso(NOW - timedelta(minutes=30))}}
        write_wo(tmp_path, "WO-A5", "accepted")
        r = scan_work_orders(tmp_path, NOW, alert_state=state)
        assert r["alerts"]["rule3_accept_overdue"] == []
        assert "WO-A5" not in r["alert_state"]


class TestDeliverablesCheck:
    def test_missing_deliverables_dir_alerts(self, tmp_path):
        write_wo(tmp_path, "WO-D1", "delivered",
                 extra={"delivered_at": iso(NOW - timedelta(minutes=10))})
        r = scan_work_orders(tmp_path, NOW,
                             deliverables_dir=tmp_path / "deliverables")
        assert len(r["alerts"]["rule4_deliverables"]) == 1
        assert "deliverables" in r["alerts"]["rule4_deliverables"][0]["hint"]

    def test_present_deliverables_no_rule4(self, tmp_path):
        write_wo(tmp_path, "WO-D2", "delivered",
                 extra={"delivered_at": iso(NOW - timedelta(minutes=10))})
        d = tmp_path / "deliverables" / "WO-D2"
        d.mkdir(parents=True)
        (d / "README.md").write_text("x", encoding="utf-8")
        r = scan_work_orders(tmp_path, NOW,
                             deliverables_dir=tmp_path / "deliverables")
        assert r["alerts"]["rule4_deliverables"] == []

    def test_overdue_alert_carries_deliverables_path(self, tmp_path):
        """工单需求③：告警信息携带 deliverables/ 检索路径与文件数。"""
        write_wo(tmp_path, "WO-D3", "delivered",
                 extra={"delivered_at": iso(NOW - timedelta(minutes=40))})
        d = tmp_path / "deliverables" / "WO-D3"
        d.mkdir(parents=True)
        (d / "README.md").write_text("x", encoding="utf-8")
        (d / "report.md").write_text("y", encoding="utf-8")
        r = scan_work_orders(tmp_path, NOW,
                             deliverables_dir=tmp_path / "deliverables")
        entry = r["alerts"]["rule3_accept_overdue"][0]
        assert entry["deliverables"]["files"] == 2
        assert "WO-D3" in entry["deliverables"]["path"]

    def test_check_deliverables_none_dir(self):
        assert check_deliverables(None, "WO-X") is None


# ────────────────────────────────────────────────────────────────────
# 入站工单（rule5）
# ────────────────────────────────────────────────────────────────────

class TestInbound:
    def test_inbound_pending_alerts_once(self, tmp_path):
        write_wo(tmp_path, "TI-1013-01", "pending", to="Travis",
                 extra={"assignee": "Travis", "created_by": "编程专家"})
        r = scan_work_orders(tmp_path, NOW)
        assert len(r["alerts"]["rule5_inbound"]) == 1
        assert r["inbound_state"]["TI-1013-01"] == "pending"
        # 二轮（状态未变）不再重复
        r2 = scan_work_orders(tmp_path, NOW + timedelta(minutes=30),
                              inbound_state=r["inbound_state"])
        assert r2["alerts"]["rule5_inbound"] == []

    def test_outbound_to_travis_not_inbound(self, tmp_path):
        write_wo(tmp_path, "WO-T1", "pending", to="Travis")
        r = scan_work_orders(tmp_path, NOW)
        assert r["alerts"]["rule5_inbound"] == []


# ────────────────────────────────────────────────────────────────────
# 去重与升级节奏
# ────────────────────────────────────────────────────────────────────

class TestDedupAndEscalation:
    def _delivered_wo(self, tmp_path, minutes_ago):
        write_wo(tmp_path, "WO-E1", "delivered",
                 extra={"delivered_at": iso(NOW - timedelta(minutes=minutes_ago))})

    def test_same_state_no_repeat_within_window(self, tmp_path):
        self._delivered_wo(tmp_path, 40)
        r1 = scan_work_orders(tmp_path, NOW)
        assert len(r1["alerts"]["rule3_accept_overdue"]) == 1
        # 30min 后二扫：仍在 1h 去重窗口内 → 静默
        r2 = scan_work_orders(tmp_path, NOW + timedelta(minutes=30),
                              alert_state=r1["alert_state"])
        assert r2["alerts"]["rule3_accept_overdue"] == []
        assert r2["has_alerts"] is False

    def test_repeat_after_window(self, tmp_path):
        self._delivered_wo(tmp_path, 40)
        r1 = scan_work_orders(tmp_path, NOW)
        # 70min 后（>1h 窗口）→ 重发
        r2 = scan_work_orders(tmp_path, NOW + timedelta(minutes=70),
                              alert_state=r1["alert_state"])
        assert len(r2["alerts"]["rule3_accept_overdue"]) == 1

    def test_escalation_latches(self, tmp_path):
        """首次 35min 普通告警 → 持续未验收 → 2h+ 轮次升级且带 escalated 标记。"""
        self._delivered_wo(tmp_path, 35)
        r1 = scan_work_orders(tmp_path, NOW)
        assert r1["escalated"] is False
        # 时间推进到 delivered 后 2.5h（NOW + 115min，delivered 是 NOW-35min）
        r2 = scan_work_orders(tmp_path, NOW + timedelta(minutes=115),
                              alert_state=r1["alert_state"])
        assert len(r2["alerts"]["rule3_accept_overdue"]) == 1
        assert r2["alerts"]["rule3_accept_overdue"][0]["escalated"] is True
        assert r2["escalated"] is True

    def test_escalated_repeat_interval_2h(self, tmp_path):
        self._delivered_wo(tmp_path, 130)
        r1 = scan_work_orders(tmp_path, NOW)
        assert r1["escalated"]
        # 升级后 30min → 不重复（2h 间隔）
        r2 = scan_work_orders(tmp_path, NOW + timedelta(minutes=30),
                              alert_state=r1["alert_state"])
        assert r2["alerts"]["rule3_accept_overdue"] == []
        # 升级后 2h+ → 重发升级告警
        r3 = scan_work_orders(tmp_path, NOW + timedelta(minutes=125),
                              alert_state=r1["alert_state"])
        assert len(r3["alerts"]["rule3_accept_overdue"]) == 1
        assert r3["escalated"]


# ────────────────────────────────────────────────────────────────────
# heartbeat 留痕（工单需求④）
# ────────────────────────────────────────────────────────────────────

class TestHeartbeat:
    def test_heartbeat_written_even_without_alerts(self, tmp_path):
        write_wo(tmp_path, "WO-H1", "accepted")
        hb = tmp_path / "heartbeat" / "scan_log.jsonl"
        r = scan_work_orders(tmp_path, NOW, heartbeat_path=hb,
                             source="pnl_live_monitor")
        assert not r["has_alerts"]
        assert hb.exists()
        lines = [json.loads(ln) for ln in hb.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        entry = lines[0]
        assert entry["source"] == "pnl_live_monitor"
        assert entry["orders_scanned"] == 1
        assert entry["notified"] is False
        assert entry["scan_time"]
        assert entry["findings"]["rule3_accept_overdue"] == 0

    def test_heartbeat_appends_and_counts(self, tmp_path):
        write_wo(tmp_path, "WO-H2", "delivered",
                 extra={"delivered_at": iso(NOW - timedelta(minutes=40))})
        hb = tmp_path / "heartbeat" / "scan_log.jsonl"
        scan_work_orders(tmp_path, NOW, heartbeat_path=hb)
        scan_work_orders(tmp_path, NOW + timedelta(minutes=5), heartbeat_path=hb)
        lines = [json.loads(ln) for ln in hb.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2
        assert lines[0]["notified"] is True
        assert lines[0]["findings"]["rule3_accept_overdue"] == 1
        assert lines[0]["total_alerts"] >= 1

    def test_heartbeat_rollover(self, tmp_path):
        hb = tmp_path / "hb.jsonl"
        scan_meta = {"orders_scanned": 1, "parse_failures": 0}
        for i in range(2050):
            t = NOW + timedelta(minutes=i)
            append_heartbeat(hb, t, scan_meta, {"rule1_watcher_stall": 0},
                             notified=False)
        lines = [ln for ln in hb.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2000

    def test_bad_json_counted_not_fatal(self, tmp_path):
        (tmp_path / "WO-BAD.json").write_text("{not json", encoding="utf-8")
        write_wo(tmp_path, "WO-OK", "accepted")
        r = scan_work_orders(tmp_path, NOW)
        assert r["scan_meta"]["orders_scanned"] == 1
        assert r["scan_meta"]["parse_failures"] == 1

    def test_missing_dir_returns_meta_error(self, tmp_path):
        r = scan_work_orders(tmp_path / "nope", NOW)
        assert "error" in r["scan_meta"]
        assert not r["has_alerts"]


# ────────────────────────────────────────────────────────────────────
# 集成：模拟 delivered 超时工单触发告警（WO-1047 验收口径）
# ────────────────────────────────────────────────────────────────────

class TestIntegrationDeliveredOverdueScenario:
    def test_full_chain_alert_to_escalation(self, tmp_path):
        """端到端：交付（history-only 时间戳）→ 35min 首告（含 deliverables 路径）
        → 30min 二扫静默 → 2h+ 升级告警 → accepted 后全静默+状态清除。
        复刻 WO-1046g 事故时间线（无顶层 delivered_at 的真实写法）。"""
        wo_dir = tmp_path / "work_orders_pull"
        wo_dir.mkdir()
        hb = wo_dir / "heartbeat" / "scan_log.jsonl"
        deliv_dir = wo_dir / "deliverables"

        t0 = datetime(2026, 10, 13, 1, 50, 0, tzinfo=TZ_SHANGHAI)  # 复刻 1046g 交付时刻
        history = [{"ts": iso(t0), "by": "编程专家",
                    "note": "delivered：commit b2c6b07 已 push（无顶层时间戳的真实写法）"}]
        write_wo(wo_dir, "WO-INT", "delivered",
                 created_at=iso(t0 - timedelta(hours=1)), history=history)
        # 交付物目录缺失（复刻事故：告警须同时报 rule4）
        state = {}

        # T+35min：首轮告警（rule3 + rule4）
        t1 = t0 + timedelta(minutes=35)
        r1 = scan_work_orders(wo_dir, t1, deliverables_dir=deliv_dir,
                              alert_state=state, heartbeat_path=hb)
        assert len(r1["alerts"]["rule3_accept_overdue"]) == 1
        assert r1["alerts"]["rule3_accept_overdue"][0]["since_min"] == 35
        assert not r1["escalated"]
        assert len(r1["alerts"]["rule4_deliverables"]) == 1
        state = r1["alert_state"]

        # T+65min：仍在去重窗口 → 静默，但 heartbeat 有痕
        t2 = t0 + timedelta(minutes=65)
        r2 = scan_work_orders(wo_dir, t2, deliverables_dir=deliv_dir,
                              alert_state=state, heartbeat_path=hb)
        assert r2["alerts"]["rule3_accept_overdue"] == []
        assert not r2["has_alerts"]

        # T+2h：升级（escalated）——执行壳须 at Leo
        t3 = t0 + timedelta(hours=2)
        r3 = scan_work_orders(wo_dir, t3, deliverables_dir=deliv_dir,
                              alert_state=state, heartbeat_path=hb)
        assert r3["escalated"] is True
        assert r3["alerts"]["rule3_accept_overdue"][0]["escalated"] is True
        state = r3["alert_state"]

        # 文案：告警正文 + 转发指令（notify 唤醒不写死日志）
        msg = build_alert_message(r3)
        assert "验收超时升级" in msg
        assert "WO-INT" in msg
        instr = build_forward_instruction(r3)
        assert "at Leo" in instr and "Travis" in instr

        # T+3h：Travis 回写 accepted → 全静默 + 状态清除
        wo = json.loads((wo_dir / "WO-INT.json").read_text())
        wo["status"] = "accepted"
        wo["accepted_at"] = iso(t0 + timedelta(hours=3))
        (wo_dir / "WO-INT.json").write_text(
            json.dumps(wo, ensure_ascii=False, indent=2), encoding="utf-8")
        t4 = t0 + timedelta(hours=3, minutes=5)
        r4 = scan_work_orders(wo_dir, t4, deliverables_dir=deliv_dir,
                              alert_state=state, heartbeat_path=hb)
        assert not r4["has_alerts"]
        assert "WO-INT" not in r4["alert_state"]

        # heartbeat 4 轮全留痕：2 轮 notified + 2 轮静默
        lines = [json.loads(ln) for ln in hb.read_text().splitlines() if ln.strip()]
        assert len(lines) == 4
        assert [ln["notified"] for ln in lines] == [True, False, True, False]

    def test_message_builders(self, tmp_path):
        write_wo(tmp_path, "WO-M1", "delivered",
                 extra={"delivered_at": iso(NOW - timedelta(minutes=45))})
        r = scan_work_orders(tmp_path, NOW)
        msg = build_alert_message(r)
        assert "验收超时" in msg and "WO-M1" in msg
        assert build_alert_message(
            {"alerts": {k: [] for k in ("rule1_watcher_stall", "rule2_execution_stuck",
                                        "rule3_accept_overdue", "rule4_deliverables",
                                        "rule5_inbound")}}) == ""
        instr = build_forward_instruction(r)
        assert "at_agent" in instr and "7642016183574905126" in instr
