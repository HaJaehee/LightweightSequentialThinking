"""The four tool implementations.

Pipeline per call: RECEIVE -> LENIENCY -> VALIDATE -> GUARD -> MUTATE -> RESPOND.
No handler formats its own response; everything goes through `responses.build`.
No handler raises; `dispatch` converts any escaping exception into a resync instruction.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Config
from .leniency import normalize
from .models import (
    Approval,
    Decision,
    ErrorCode,
    Plan,
    PlanStatus,
    Task,
    TaskStatus,
    ThinkingStep,
    now_iso,
)
from .responses import build, error, render_plan_for_user
from .state_machine import can_start_task, execution_guard
from .store import State, Store

log = logging.getLogger("planning-mcp.handlers")


class PlanningHandlers:
    def __init__(self, store: Store, config: Config):
        self.store = store
        self.config = config

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def dispatch(self, tool_name: str, raw_args: Any) -> dict[str, Any]:
        clean, notes = normalize(tool_name, raw_args)
        try:
            with self.store.lock:
                if tool_name == "plan_and_think":
                    return self._plan_and_think(clean, notes)
                if tool_name == "request_user_approval":
                    return self._request_user_approval(clean, notes)
                if tool_name == "update_task_progress":
                    return self._update_task_progress(clean, notes)
                if tool_name == "get_current_plan":
                    return self._get_current_plan(clean, notes)
        except Exception as exc:  # noqa: BLE001 - nothing may escape to the model
            log.exception("Handler %s failed", tool_name)
            self.store.audit("internal_error", tool=tool_name, error=type(exc).__name__)
            plan = self._safe_active_plan()
            return error(
                plan,
                ErrorCode.INTERNAL_ERROR,
                type(exc).__name__,  # class name only - never a stack trace
                notes=notes,
            )
        return error(
            None,
            ErrorCode.INTERNAL_ERROR,
            f"Unknown tool '{tool_name}'.",
            notes=notes,
        )

    def _safe_active_plan(self) -> Plan | None:
        try:
            return self.store.load().active_plan
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # 1. plan_and_think
    # ------------------------------------------------------------------
    def _plan_and_think(self, args: dict[str, Any], notes: list[str]) -> dict[str, Any]:
        state = self.store.load()
        plan = state.active_plan

        # A plan already under way: do NOT start a second one. Redirect to the in-flight task.
        if plan is not None and plan.status in (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION):
            self.store.audit("plan_and_think_redirected", plan_id=plan.plan_id)
            task = plan.current_task()
            return build(
                plan,
                message=(
                    "A plan is already approved and running. Continue it instead of planning "
                    "again. Use get_current_plan if you need the details."
                ),
                notes=notes,
                tasks=plan.tasks_brief(),
                progress=plan.progress(),
                next_task={"task_id": task.task_id, "title": task.title} if task else None,
            )

        goal = (args.get("goal") or "").strip()
        thought = (args.get("thought") or "").strip()

        # Lenient defaults: erroring on a missing scalar costs a turn and teaches nothing.
        if plan is not None and plan.status not in (PlanStatus.COMPLETED, PlanStatus.CANCELLED):
            if not goal:
                goal = plan.goal
                notes.append("No goal was sent; reused the goal already on record.")
        if not goal:
            goal = thought[:120] or "(goal not stated)"
            notes.append("No goal was sent; derived one from your thought. Send 'goal' next time.")
        if not thought:
            thought = "(no thought text provided)"
            notes.append("No thought text was sent. Send one short reasoning sentence per step.")

        # Start or reuse the plan.
        if plan is None or plan.status in (PlanStatus.COMPLETED, PlanStatus.CANCELLED):
            plan_id = self.store.next_plan_id(state)
            plan = Plan(plan_id=plan_id, goal=goal, plan_status=PlanStatus.DRAFTING.value)
            state.plans[plan_id] = plan
            state.active_plan_id = plan_id
            self.store.audit("plan_created", plan_id=plan_id, goal=goal)
        else:
            if plan.status is PlanStatus.BLOCKED:
                self.store.audit("replan_after_failure", plan_id=plan.plan_id)
            plan.set_status(PlanStatus.DRAFTING)
            plan.approval.reset_request()
            if goal and goal != plan.goal:
                # A different goal arriving mid-draft usually means a NEW conversation is
                # steamrolling an unfinished plan - there is only one active-plan slot.
                # Allow it (blocking would strand legitimate goal rewording in the same
                # session), but leave loud evidence for both the model and the audit trail.
                notes.append(
                    f"An unfinished plan with a different goal ('{plan.goal}') was active "
                    "and is being replaced by this one. Its task list is archived. If you "
                    "did not intend to abandon that plan, call get_current_plan."
                )
                self.store.audit(
                    "goal_replaced", plan_id=plan.plan_id, old_goal=plan.goal, new_goal=goal
                )
                plan.goal = goal

        # --- revises_step -------------------------------------------------
        revises_step = args.get("revises_step")
        if revises_step is not None:
            target = next(
                (s for s in plan.thinking_steps if s.step_number == revises_step and not s.superseded),
                None,
            )
            if target is None:
                return error(
                    plan,
                    ErrorCode.INVALID_STEP,
                    f"There is no active thinking step numbered {revises_step}.",
                    notes=notes,
                )
            target.superseded = True
            notes.append(f"Step {revises_step} was marked superseded by this revision.")

        # --- step numbering ------------------------------------------------
        step_number = args.get("step_number")
        expected = plan.last_step_number() + 1
        if step_number is None or step_number != expected:
            if step_number is not None:
                notes.append(f"step_number {step_number} was corrected to {expected}.")
            step_number = expected

        total_steps = args.get("total_steps") or 0
        total_steps = max(int(total_steps), step_number)
        plan.total_steps = total_steps

        plan.thinking_steps.append(
            ThinkingStep(step_number=step_number, thought=thought, revises_step=revises_step)
        )
        self.store.audit(
            "thinking_step",
            plan_id=plan.plan_id,
            step_number=step_number,
            revises_step=revises_step,
            thought=thought,
        )

        need_more = args.get("need_more_thinking")
        if need_more is None:
            need_more = True
            notes.append("need_more_thinking was missing; assumed true (still planning).")

        # --- still thinking --------------------------------------------------
        if need_more:
            plan.touch()
            self.store.save(state)
            return build(
                plan,
                recorded_step=step_number,
                total_steps=plan.total_steps,
                tasks=plan.tasks_brief(),
                notes=notes,
                message=f"Thinking step {step_number} recorded.",
            )

        # --- finalizing: a task_list is mandatory -----------------------------
        task_list = args.get("task_list") or []
        if not task_list:
            plan.touch()
            self.store.save(state)  # the thought is kept; only the finalization is refused
            return error(
                plan,
                ErrorCode.MISSING_TASK_LIST,
                "need_more_thinking was false but task_list was empty or missing.",
                notes=notes,
                recorded_step=step_number,
            )

        if len(task_list) > self.config.max_tasks:
            notes.append(
                f"task_list had {len(task_list)} items; kept the first {self.config.max_tasks}. "
                "Aim for 2-7 concrete actions."
            )
            task_list = task_list[: self.config.max_tasks]

        if plan.tasks:  # re-plan: keep the old breakdown as evidence
            plan.superseded_tasks.append([t.to_dict() for t in plan.tasks])

        plan.tasks = [Task(task_id=i, title=title) for i, title in enumerate(task_list, start=1)]
        plan.set_status(PlanStatus.AWAITING_APPROVAL)
        plan.approval.reset_request()
        self.store.save(state)
        self.store.audit(
            "plan_finalized", plan_id=plan.plan_id, tasks=[t.title for t in plan.tasks]
        )

        return build(
            plan,
            recorded_step=step_number,
            total_steps=plan.total_steps,
            tasks=plan.tasks_brief(),
            notes=notes,
            message=(
                f"Plan created with {len(plan.tasks)} tasks. "
                "Execution is locked until the user approves."
            ),
        )

    # ------------------------------------------------------------------
    # 2. request_user_approval  (HITL gate)
    # ------------------------------------------------------------------
    def _request_user_approval(self, args: dict[str, Any], notes: list[str]) -> dict[str, Any]:
        state = self.store.load()
        plan = state.active_plan

        raw_decision = args.get("decision")
        try:
            decision = Decision(raw_decision)
        except ValueError:
            return error(
                plan,
                ErrorCode.INVALID_DECISION,
                f"'{raw_decision}' is not a valid decision.",
                notes=notes,
            )

        if plan is None:
            return error(plan, ErrorCode.NO_ACTIVE_PLAN, "No plan exists yet.", notes=notes)

        if decision is Decision.ASK_USER:
            return self._ask_user(state, plan, args, notes)
        if decision is Decision.APPROVED:
            return self._approve(state, plan, args, notes)
        if decision is Decision.REVISE:
            return self._revise(state, plan, args, notes)
        return self._reject(state, plan, args, notes)

    def _ask_user(
        self, state: State, plan: Plan, args: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        if plan.status in (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION):
            task = plan.current_task()
            return build(
                plan,
                message="This plan was already approved by the user. Continue executing it.",
                notes=notes,
                tasks=plan.tasks_brief(),
                next_task={"task_id": task.task_id, "title": task.title} if task else None,
            )
        if plan.status is PlanStatus.CANCELLED:
            return error(plan, ErrorCode.PLAN_CANCELLED, "This plan was cancelled.", notes=notes)
        if plan.status is PlanStatus.BLOCKED:
            return error(plan, ErrorCode.PLAN_BLOCKED, "A task failed.", notes=notes)
        if not plan.tasks:
            return error(
                plan,
                ErrorCode.PLAN_NOT_READY,
                "There is no task list to approve yet.",
                notes=notes,
            )

        plan_summary = (args.get("plan_summary") or "").strip()
        if not plan_summary:
            return error(
                plan,
                ErrorCode.MISSING_PLAN_SUMMARY,
                "plan_summary is required when decision is ASK_USER.",
                notes=notes,
            )

        plan.approval.requested_at = now_iso()
        plan.approval.decision = None
        plan.approval.decided_at = None
        plan.set_status(PlanStatus.AWAITING_APPROVAL)
        self.store.save(state)
        self.store.audit("approval_requested", plan_id=plan.plan_id, plan_summary=plan_summary)

        return build(
            plan,
            tasks=plan.tasks_brief(),
            notes=notes,
            display_to_user=render_plan_for_user(plan, plan_summary),
        )

    def _approve(
        self, state: State, plan: Plan, args: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        if plan.status in (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION):
            task = plan.current_task()
            return build(
                plan,
                message="Already approved. Continue executing.",
                notes=notes,
                tasks=plan.tasks_brief(),
                next_task={"task_id": task.task_id, "title": task.title} if task else None,
            )
        if plan.status is not PlanStatus.AWAITING_APPROVAL:
            return error(
                plan,
                ErrorCode.PLAN_NOT_READY,
                f"A plan in status {plan.plan_status} cannot be approved.",
                notes=notes,
            )
        if not plan.approval.requested_at:
            # Approval reported for a plan version the user was never shown. Every change
            # to the task list (finalize, revise, cross-session replacement) clears
            # requested_at, so this branch means either the model skipped ASK_USER or the
            # plan CHANGED after it was shown. Accepting would let an approval land on
            # tasks the human never saw - the exact cross-session misdirection this gate
            # exists to prevent. Hard refusal: force a re-display of the current plan.
            self.store.audit("stale_approval_refused", plan_id=plan.plan_id)
            return error(
                plan,
                ErrorCode.APPROVAL_NOT_REQUESTED,
                "The current version of this plan was never shown to the user.",
                notes=notes,
                tasks=plan.tasks_brief(),
            )

        plan.approval.decision = Decision.APPROVED.value
        plan.approval.decided_at = now_iso()
        if args.get("user_comment"):
            plan.approval.user_comment = args["user_comment"]
        plan.set_status(PlanStatus.APPROVED)
        self.store.save(state)
        self.store.audit("approved", plan_id=plan.plan_id, comment=args.get("user_comment"))

        task = plan.current_task()
        return build(
            plan,
            message="Execution is now unlocked.",
            notes=notes,
            tasks=plan.tasks_brief(),
            progress=plan.progress(),
            next_task={"task_id": task.task_id, "title": task.title} if task else None,
        )

    def _revise(
        self, state: State, plan: Plan, args: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        comment = args.get("user_comment") or ""
        plan.approval.revision_count += 1
        plan.approval.user_comment = comment
        plan.approval.reset_request()
        plan.set_status(PlanStatus.DRAFTING)
        self.store.save(state)
        self.store.audit(
            "revision_requested",
            plan_id=plan.plan_id,
            revision_count=plan.approval.revision_count,
            user_comment=comment,
        )
        return build(
            plan,
            message=(
                "The user requested changes. Re-plan with plan_and_think, then ask for approval "
                "again. The plan stays locked until they approve the new version."
            ),
            notes=notes,
            user_comment=comment or None,
            revision_count=plan.approval.revision_count,
        )

    def _reject(
        self, state: State, plan: Plan, args: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        comment = args.get("user_comment") or ""
        plan.approval.decision = Decision.REJECTED.value
        plan.approval.decided_at = now_iso()
        plan.approval.user_comment = comment
        plan.set_status(PlanStatus.CANCELLED)
        self.store.save(state)
        self.store.audit("rejected", plan_id=plan.plan_id, user_comment=comment)
        return build(
            plan,
            message="The plan was cancelled by the user. Nothing was executed.",
            notes=notes,
            user_comment=comment or None,
        )

    # ------------------------------------------------------------------
    # 3. update_task_progress
    # ------------------------------------------------------------------
    def _update_task_progress(self, args: dict[str, Any], notes: list[str]) -> dict[str, Any]:
        state = self.store.load()
        plan = state.active_plan

        guard = execution_guard(plan, autoapprove=self.config.autoapprove)
        if guard is not None:
            self.store.audit(
                "execution_blocked",
                plan_id=plan.plan_id if plan else None,
                reason=guard.value,
                attempted_task_id=args.get("task_id"),
            )
            return error(plan, guard, "Execution is not allowed in the current state.", notes=notes)

        assert plan is not None  # execution_guard returns NO_ACTIVE_PLAN otherwise
        if self.config.autoapprove and plan.status is PlanStatus.AWAITING_APPROVAL:
            log.warning("PLANNING_MCP_AUTOAPPROVE is ON - the HITL gate is bypassed (test mode).")
            plan.set_status(PlanStatus.APPROVED)

        raw_status = args.get("status")
        try:
            status = TaskStatus(raw_status)
        except ValueError:
            return error(
                plan,
                ErrorCode.INVALID_STATUS,
                f"'{raw_status}' is not a valid task status.",
                notes=notes,
                tasks=plan.tasks_brief(),
            )

        task_id = args.get("task_id")
        task = plan.get_task(task_id) if task_id is not None else None
        if task is None:
            return error(
                plan,
                ErrorCode.TASK_NOT_FOUND,
                f"No task with task_id={task_id} in this plan.",
                notes=notes,
                tasks=plan.tasks_brief(),
            )

        if status is TaskStatus.IN_PROGRESS:
            return self._start_task(state, plan, task, notes)
        if status is TaskStatus.DONE:
            return self._finish_task(state, plan, task, args, notes)
        if status is TaskStatus.FAILED:
            return self._fail_task(state, plan, task, args, notes)
        return self._reset_task(state, plan, task, notes)

    def _start_task(
        self, state: State, plan: Plan, task: Task, notes: list[str]
    ) -> dict[str, Any]:
        if task.status == TaskStatus.DONE.value:
            nxt = plan.current_task()
            return build(
                plan,
                message=f"Task {task.task_id} is already DONE.",
                notes=notes,
                tasks=plan.tasks_brief(),
                progress=plan.progress(),
                next_task={"task_id": nxt.task_id, "title": nxt.title} if nxt else None,
            )
        if not can_start_task(plan, task.task_id):
            # Redirect rather than reject: rejecting strands the model.
            correct = plan.current_task()
            self.store.audit(
                "out_of_order_start", plan_id=plan.plan_id, attempted=task.task_id
            )
            return build(
                plan,
                message=(
                    f"Task {task.task_id} cannot start yet because earlier tasks are not "
                    "finished."
                ),
                notes=notes,
                tasks=plan.tasks_brief(),
                progress=plan.progress(),
                next_task={"task_id": correct.task_id, "title": correct.title}
                if correct
                else None,
            )

        task.status = TaskStatus.IN_PROGRESS.value
        task.started_at = now_iso()
        plan.set_status(PlanStatus.IN_EXECUTION)
        self.store.save(state)
        self.store.audit("task_started", plan_id=plan.plan_id, task_id=task.task_id)
        return build(
            plan,
            task_id=task.task_id,
            task_status=task.status,
            progress=plan.progress(),
            tasks=plan.tasks_brief(),
            notes=notes,
            message=f"Task {task.task_id} marked IN_PROGRESS. Do the work now.",
        )

    def _finish_task(
        self, state: State, plan: Plan, task: Task, args: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        if task.status != TaskStatus.IN_PROGRESS.value and task.status != TaskStatus.DONE.value:
            self.store.audit(
                "skipped_in_progress",
                plan_id=plan.plan_id,
                task_id=task.task_id,
                previous_status=task.status,
            )
            notes.append(
                f"Task {task.task_id} was marked DONE without ever being IN_PROGRESS. Accepted, "
                "but call IN_PROGRESS before starting work next time."
            )

        task.status = TaskStatus.DONE.value
        task.finished_at = now_iso()
        if args.get("result_log"):
            task.result_log = args["result_log"]
        elif not task.result_log:
            notes.append(
                "No result_log was sent. Always record what you actually did - the final "
                "answer is built from these logs."
            )

        if plan.all_done():
            plan.set_status(PlanStatus.COMPLETED)
        else:
            plan.set_status(PlanStatus.IN_EXECUTION)
        self.store.save(state)
        self.store.audit(
            "task_done",
            plan_id=plan.plan_id,
            task_id=task.task_id,
            result_log=task.result_log,
        )

        nxt = plan.current_task()
        return build(
            plan,
            task_id=task.task_id,
            task_status=task.status,
            progress=plan.progress(),
            tasks=plan.tasks_brief(),
            notes=notes,
            next_task={"task_id": nxt.task_id, "title": nxt.title} if nxt else None,
        )

    def _fail_task(
        self, state: State, plan: Plan, task: Task, args: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        task.status = TaskStatus.FAILED.value
        task.finished_at = now_iso()
        task.result_log = args.get("result_log") or task.result_log
        plan.set_status(PlanStatus.BLOCKED)
        self.store.save(state)
        self.store.audit(
            "task_failed", plan_id=plan.plan_id, task_id=task.task_id, result_log=task.result_log
        )
        return build(
            plan,
            task_id=task.task_id,
            task_status=task.status,
            progress=plan.progress(),
            tasks=plan.tasks_brief(),
            notes=notes,
            failed_task={
                "task_id": task.task_id,
                "title": task.title,
                "result_log": task.result_log,
            },
            message=f"Task {task.task_id} failed. Forward progress is halted.",
        )

    def _reset_task(
        self, state: State, plan: Plan, task: Task, notes: list[str]
    ) -> dict[str, Any]:
        task.status = TaskStatus.PENDING.value
        task.started_at = None
        task.finished_at = None
        if plan.status is PlanStatus.BLOCKED and not plan.first_failed_task():
            plan.set_status(PlanStatus.IN_EXECUTION)
        self.store.save(state)
        self.store.audit("task_reset", plan_id=plan.plan_id, task_id=task.task_id)
        return build(
            plan,
            task_id=task.task_id,
            task_status=task.status,
            progress=plan.progress(),
            tasks=plan.tasks_brief(),
            notes=notes,
            message=f"Task {task.task_id} reset to PENDING.",
        )

    # ------------------------------------------------------------------
    # 4. get_current_plan
    # ------------------------------------------------------------------
    def _get_current_plan(self, args: dict[str, Any], notes: list[str]) -> dict[str, Any]:
        state = self.store.load()
        requested = (args.get("plan_id") or "current").strip()

        if requested.lower() in ("current", "active", "latest", ""):
            plan = state.active_plan
        else:
            plan = state.plans.get(requested)
            if plan is None:
                plan = state.active_plan
                notes.append(
                    f"No plan with id '{requested}'. Returned the active plan instead. "
                    "Use plan_id='current'."
                )

        if plan is None:
            return build(None, notes=notes, message="No plan has been created yet.")

        # Compact the history so recovery never blows an already-truncated context.
        active_steps = [s for s in plan.thinking_steps if not s.superseded]
        superseded_count = len(plan.thinking_steps) - len(active_steps)
        thinking = [
            {"step_number": s.step_number, "thought": s.thought, "superseded": False}
            for s in active_steps[-6:]
        ]

        return build(
            plan,
            goal=plan.goal,
            thinking_steps=thinking,
            superseded_steps=f"{superseded_count} earlier steps superseded"
            if superseded_count
            else None,
            tasks=plan.tasks_brief(),
            progress=plan.progress(),
            approval={
                "decision": plan.approval.decision,
                "revision_count": plan.approval.revision_count,
                "user_comment": plan.approval.user_comment,
            },
            notes=notes,
        )


__all__ = ["PlanningHandlers", "Approval"]
