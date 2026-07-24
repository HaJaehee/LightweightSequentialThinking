"""The single response builder.

Every handler returns through `build()`. That guarantees the four fields the system
prompt teaches the model to rely on - ok, plan_status, next_action, next_action_hint -
are present on every response from every tool, including error paths.
"""

from __future__ import annotations

from typing import Any

from .models import ErrorCode, Plan, PlanStatus
from .state_machine import resolve_next_action


def build(
    plan: Plan | None,
    *,
    ok: bool = True,
    error_code: ErrorCode | None = None,
    message: str | None = None,
    notes: list[str] | None = None,
    qualify: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    action, hint = resolve_next_action(plan, error_code, qualify=qualify)
    payload: dict[str, Any] = {
        "ok": ok,
        "plan_id": plan.plan_id if plan else None,
        "plan_status": plan.plan_status if plan else PlanStatus.NONE.value,
        "next_action": action,
        "next_action_hint": hint,
    }
    if error_code is not None:
        payload["error_code"] = error_code.value
    if message:
        payload["message"] = message
    payload.update({k: v for k, v in extra.items() if v is not None})
    if notes:
        # Surfaced so the model can learn the correct shape, but never as an error.
        payload["input_notes"] = notes
    return payload


def error(
    plan: Plan | None,
    code: ErrorCode,
    message: str,
    *,
    notes: list[str] | None = None,
    qualify: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    return build(
        plan, ok=False, error_code=code, message=message, notes=notes, qualify=qualify, **extra
    )


def render_plan_for_user(plan: Plan, plan_summary: str | None = None) -> str:
    """Pre-rendered approval block. The model only has to echo this string, which is the
    single most reliable operation a weak model can perform."""
    lines = ["PLAN FOR APPROVAL"]
    if plan.goal:
        lines.append(f"Goal: {plan.goal}")
    if plan_summary:
        lines.append("")
        lines.append(plan_summary.strip())
    lines.append("")
    for task in plan.tasks:
        lines.append(f"{task.task_id}. {task.title}")
    lines.append("")
    lines.append("Approve this plan? (yes / no / tell me what to change)")
    return "\n".join(lines)
