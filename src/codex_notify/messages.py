"""Human-readable Feishu messages."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _clock(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%m-%d %H:%M:%S")


def _duration(seconds: int) -> str:
    minutes, seconds = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}小时{minutes}分{seconds}秒"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"


def render_message(event_type: str, payload: dict[str, Any]) -> str:
    project = payload.get("project") or "未知项目"
    summary = payload.get("summary") or "（未提供摘要）"
    occurred_at = float(payload.get("occurred_at") or 0)
    turn_id = str(payload.get("turn_id") or "-")
    short_turn_id = turn_id[:12]
    event_id = str(payload.get("event_id") or short_turn_id)
    if event_type == "started":
        return (
            "▶️ Codex Turn 开始\n"
            f"项目：{project}\n"
            f"时间：{_clock(occurred_at)}\n"
            f"任务：{summary}\n"
            f"Turn：{short_turn_id}\n"
            f"事件：{event_id}"
        )
    if event_type == "completed":
        duration = _duration(int(payload.get("duration_seconds") or 0))
        lifecycle_note = (
            "\n生命周期：未观测到对应启动事件"
            if payload.get("incomplete_lifecycle")
            else ""
        )
        children = payload.get("child_results")
        child_block = ""
        if isinstance(children, list) and children:
            lines = []
            for child in children:
                if not isinstance(child, dict):
                    continue
                agent_type = str(child.get("agent_type") or "subagent")
                child_summary = str(child.get("summary") or "（无可用结果）")
                lines.append(f"- {agent_type}：{child_summary}")
            omitted = int(payload.get("omitted_child_results") or 0)
            if omitted > 0:
                lines.append(f"- 另有 {omitted} 个已确认子任务未展开")
            if lines:
                child_block = "\n子任务结果：\n" + "\n".join(lines) + "\n"
        return (
            "✅ Codex Turn 结束\n"
            f"项目：{project}\n"
            f"时间：{_clock(occurred_at)}\n"
            f"耗时：{duration}\n"
            f"结果：{summary}\n"
            f"{child_block}"
            f"Turn：{short_turn_id}\n"
            f"事件：{event_id}"
            f"{lifecycle_note}"
        )
    if event_type == "test":
        return f"🔔 Codex Notify 测试成功\n飞书通知链路工作正常。\n事件：{event_id}"
    raise ValueError(f"未知事件类型：{event_type}")
