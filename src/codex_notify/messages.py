"""Human-readable Feishu messages."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .redact import safe_summary


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
    # Re-sanitize at the final delivery boundary so legacy queued payloads
    # created by an older redaction policy cannot bypass current protections.
    project = safe_summary(payload.get("project"), 120) or "未知项目"
    summary = safe_summary(payload.get("summary"), 300) or "（未提供摘要）"
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
        terminal_status = str(payload.get("terminal_status") or "completed")
        title = {
            "completed": "✅ Codex Turn 结束：已完成",
            "failed": "❌ Codex Turn 结束：失败",
            "interrupted": "⏹️ Codex Turn 结束：已中断",
        }.get(terminal_status, "Codex Turn 终态")
        status_label = {
            "completed": "completed",
            "failed": "failed",
            "interrupted": "interrupted",
        }.get(terminal_status, "unknown")
        if not summary or summary == "（未提供摘要）":
            summary = {
                "completed": "Turn 已完成，请回到 Codex 查看结果。",
                "failed": "Turn 执行失败，请回到 Codex 查看。",
                "interrupted": "Turn 已中断，请回到 Codex 查看。",
            }.get(terminal_status, "请回到 Codex 查看。")
        error_category = safe_summary(payload.get("error_category"), 80)
        error_block = (
            f"\n错误类别：{error_category}"
            if terminal_status == "failed" and error_category
            else ""
        )
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
                agent_type = safe_summary(child.get("agent_type"), 80) or "subagent"
                child_summary = (
                    safe_summary(child.get("summary"), 300) or "（无可用结果）"
                )
                lines.append(f"- {agent_type}：{child_summary}")
            omitted = int(payload.get("omitted_child_results") or 0)
            if omitted > 0:
                lines.append(f"- 另有 {omitted} 个已确认子任务未展开")
            if lines:
                child_block = "\n子任务结果：\n" + "\n".join(lines) + "\n"
        return (
            f"{title}\n"
            f"项目：{project}\n"
            "可信度：confirmed\n"
            f"状态：{status_label}\n"
            f"时间：{_clock(occurred_at)}\n"
            f"耗时：{duration}\n"
            f"结果：{summary}\n"
            f"{child_block}"
            f"Turn：{short_turn_id}\n"
            f"事件：{event_id}"
            f"{error_block}"
            f"{lifecycle_note}"
        )
    if event_type == "permission":
        reason = safe_summary(payload.get("reason"), 200)
        reason_block = f"\n原因：{reason}" if reason else ""
        return (
            "⚠️ Codex 权限检查记录\n"
            f"项目：{project}\n"
            "归属可信度：confirmed\n"
            "状态：不代表当前仍在等待人工审批\n"
            f"操作：{summary}\n"
            f"时间：{_clock(occurred_at)}\n"
            f"Turn：{short_turn_id}\n"
            f"事件：{event_id}"
            f"{reason_block}"
        )
    if event_type == "experimental_request_user_input":
        return (
            "❓ Codex 可能需要你回答一个问题\n"
            f"项目：{project}\n"
            "可信度：best_effort\n"
            f"信号来源：{payload.get('signal_source') or 'hook'}\n"
            f"时间：{_clock(occurred_at)}\n"
            "建议回到 Codex 检查。\n"
            f"Turn：{short_turn_id}\n"
            f"信号：{payload.get('signal_id') or event_id}\n"
            f"事件：{event_id}"
        )
    if event_type == "experimental_mcp_auth":
        display_name = (
            safe_summary(payload.get("display_name"), 80) or "某 MCP 服务"
        )
        return (
            "🔐 Codex 全局状态提醒\n"
            f"{display_name} 可能需要重新登录\n"
            "可信度：best_effort\n"
            f"信号来源：{payload.get('signal_source') or 'app_server_status'}\n"
            f"时间：{_clock(occurred_at)}\n"
            "建议回到 Codex 检查。\n"
            f"信号：{payload.get('signal_id') or event_id}\n"
            f"事件：{event_id}"
        )
    if event_type == "experimental_rate_limit":
        return (
            "⏳ Codex 全局状态提醒\n"
            "账户可能达到限流\n"
            "可信度：best_effort\n"
            f"信号来源：{payload.get('signal_source') or 'app_server_status'}\n"
            f"时间：{_clock(occurred_at)}\n"
            "建议回到 Codex 检查。\n"
            f"信号：{payload.get('signal_id') or event_id}\n"
            f"事件：{event_id}"
        )
    if event_type == "test":
        return f"🔔 Codex Notify 测试成功\n飞书通知链路工作正常。\n事件：{event_id}"
    raise ValueError(f"未知事件类型：{event_type}")
