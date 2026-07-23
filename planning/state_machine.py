"""Transition rules and the single next_action resolver.

`resolve_next_action` is a pure function of (plan_status, task states, error_code) and
is the ONLY producer of `next_action`. If two code paths could emit different
instructions for the same state, a weak model receives contradictory orders and loops -
so every response goes through here.
"""

from __future__ import annotations

from .models import ErrorCode, NextAction, Plan, PlanStatus, TaskStatus

# ---------------------------------------------------------------------------
# Error-driven resolution: what the model must do to get unstuck.
# ---------------------------------------------------------------------------


def _error_action(plan: Plan | None, code: ErrorCode) -> tuple[str, str]:
    if code is ErrorCode.PLAN_NOT_APPROVED:
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "You cannot start tasks yet - the user has not approved the plan. "
            "Call request_user_approval with decision='ASK_USER'.",
        )
    if code is ErrorCode.PLAN_NOT_READY:
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            "The plan has no task list yet, so there is nothing to approve. Call "
            "plan_and_think with need_more_thinking=false and a non-empty task_list first.",
        )
    if code is ErrorCode.PLAN_BLOCKED:
        failed = plan.first_failed_task() if plan else None
        which = f"Task {failed.task_id} ('{failed.title}') failed. " if failed else ""
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            f"{which}You may NOT continue to another task. Call plan_and_think to re-plan "
            "around this failure, then request approval again.",
        )
    if code is ErrorCode.PLAN_CANCELLED:
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            "This plan was cancelled by the user. Do not execute any of its tasks. If the user "
            "wants something else, start a new plan with plan_and_think at step_number=1.",
        )
    if code is ErrorCode.NO_ACTIVE_PLAN:
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            "There is no active plan. Start one by calling plan_and_think with step_number=1.",
        )
    if code is ErrorCode.TASK_NOT_FOUND:
        valid = ", ".join(str(t.task_id) for t in plan.tasks) if plan and plan.tasks else "none"
        nxt = plan.current_task() if plan else None
        target = f" The task to work on now is task_id={nxt.task_id}." if nxt else ""
        return (
            NextAction.CALL_UPDATE_TASK_PROGRESS.value,
            f"That task_id does not exist. Valid task_id values are: {valid}.{target}",
        )
    if code is ErrorCode.MISSING_TASK_LIST:
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            "Send the same step again with a non-empty task_list. Example: "
            'task_list=["Locate the file", "Extract the table", "Write the summary"].',
        )
    if code is ErrorCode.MISSING_PLAN_SUMMARY:
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "Call request_user_approval again with decision='ASK_USER' AND a plan_summary "
            "written in plain language for the user.",
        )
    if code is ErrorCode.APPROVAL_NOT_REQUESTED:
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "The CURRENT version of this plan was never shown to the user - it may have "
            "been revised or replaced since you last asked. Call request_user_approval "
            "with decision='ASK_USER' and a plan_summary of the current plan, show it to "
            "the user, and wait for their answer. Do not reuse an old approval.",
        )
    if code is ErrorCode.INVALID_STATUS:
        return (
            NextAction.CALL_UPDATE_TASK_PROGRESS.value,
            "status must be exactly one of: PENDING, IN_PROGRESS, DONE, FAILED. Call the tool "
            "again with a valid status.",
        )
    if code is ErrorCode.INVALID_DECISION:
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "decision must be exactly one of: ASK_USER, APPROVED, REJECTED, REVISE. Use "
            "ASK_USER to ask the human, and the others only to report what the human "
            "actually replied.",
        )
    if code is ErrorCode.INVALID_STEP:
        last = plan.last_step_number() if plan else 0
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            f"revises_step must point at an existing step between 1 and {last}. Call "
            "plan_and_think again with a valid revises_step, or omit it.",
        )
    return (
        NextAction.CALL_GET_CURRENT_PLAN.value,
        "Something went wrong on the server. Call get_current_plan with plan_id='current' to "
        "resync, then continue from the task it reports.",
    )


# ---------------------------------------------------------------------------
# Status-driven resolution: the normal path.
# ---------------------------------------------------------------------------


def _status_action(plan: Plan | None) -> tuple[str, str]:
    if plan is None or plan.status is PlanStatus.NONE:
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            "There is no active plan. Start one by calling plan_and_think with step_number=1.",
        )

    status = plan.status

    if status is PlanStatus.DRAFTING:
        nxt = plan.last_step_number() + 1
        return (
            NextAction.CALL_PLAN_AND_THINK.value,
            f"Call plan_and_think again with step_number={nxt} and the same goal. When your "
            "breakdown is complete, set need_more_thinking=false and send task_list.",
        )

    if status is PlanStatus.AWAITING_APPROVAL:
        if plan.approval.requested_at and not plan.approval.decision:
            return (
                NextAction.STOP_AND_WAIT_FOR_USER.value,
                "STOP NOW. Do not call any other tool. Show the display_to_user text below to "
                "the user and ask: 'Approve this plan? (yes / no / tell me what to change)'. "
                "Wait for their reply in the next message.",
            )
        return (
            NextAction.CALL_REQUEST_USER_APPROVAL.value,
            "Plan is drafted but LOCKED. You must now call request_user_approval with "
            "decision='ASK_USER'. You are NOT allowed to start any task yet.",
        )

    if status in (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION):
        task = plan.current_task()
        if task is None:
            return (
                NextAction.ANSWER_USER.value,
                "All tasks are complete. Now write the final answer to the user, summarizing "
                "the result_log of each task.",
            )
        if task.status == TaskStatus.IN_PROGRESS.value:
            return (
                NextAction.CALL_UPDATE_TASK_PROGRESS.value,
                f"Task {task.task_id} ('{task.title}') is in progress. When it is finished, call "
                f"update_task_progress with task_id={task.task_id}, status='DONE' and a "
                "result_log describing what you actually did.",
            )
        return (
            NextAction.CALL_UPDATE_TASK_PROGRESS.value,
            f"Next task is task_id={task.task_id} '{task.title}'. Call update_task_progress "
            f"with task_id={task.task_id} and status='IN_PROGRESS'.",
        )

    if status is PlanStatus.BLOCKED:
        return _error_action(plan, ErrorCode.PLAN_BLOCKED)

    if status is PlanStatus.COMPLETED:
        return (
            NextAction.ANSWER_USER.value,
            "All tasks are complete. Now write the final answer to the user, summarizing the "
            "result_log of each task.",
        )

    if status is PlanStatus.CANCELLED:
        return (
            NextAction.ANSWER_USER.value,
            "The plan is cancelled. Tell the user it was cancelled and ask what they want to do "
            "instead. Do not execute anything.",
        )

    return (
        NextAction.CALL_GET_CURRENT_PLAN.value,
        "Call get_current_plan with plan_id='current' to find out what to do next.",
    )


def resolve_next_action(plan: Plan | None, error_code: ErrorCode | None = None) -> tuple[str, str]:
    """The single source of truth for (next_action, next_action_hint)."""
    if error_code is not None:
        return _error_action(plan, error_code)
    return _status_action(plan)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def execution_guard(plan: Plan | None, autoapprove: bool = False) -> ErrorCode | None:
    """May the model touch task state right now? Returns an error code if not.

    This is the ENFORCEMENT half of the HITL gate. The instructional half (telling the
    model to stop and wait) can be ignored by a weak model; this one cannot, because
    progress is only real once the server records it.
    """
    if plan is None:
        return ErrorCode.NO_ACTIVE_PLAN
    status = plan.status
    if status in (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION):
        return None
    if status is PlanStatus.BLOCKED:
        return ErrorCode.PLAN_BLOCKED
    if status is PlanStatus.CANCELLED:
        return ErrorCode.PLAN_CANCELLED
    if status is PlanStatus.COMPLETED:
        return ErrorCode.NO_ACTIVE_PLAN
    if autoapprove and status is PlanStatus.AWAITING_APPROVAL:
        return None  # testing escape hatch only; server.py logs a loud warning
    return ErrorCode.PLAN_NOT_APPROVED


def can_start_task(plan: Plan, task_id: int) -> bool:
    """A task may start only once every lower-numbered task has finished."""
    for task in plan.tasks:
        if task.task_id >= task_id:
            break
        if task.status in (TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value):
            return False
    return True
