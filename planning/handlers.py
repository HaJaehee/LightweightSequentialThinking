"""The four tool implementations.

Pipeline per call: RECEIVE -> LENIENCY -> VALIDATE -> GUARD -> MUTATE -> RESPOND.
No handler formats its own response; everything goes through `responses.build`.
No handler raises; `dispatch` converts any escaping exception into a resync instruction.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Any

import time

from .approval import ApprovalServer, ApprovalStore
from .config import NO_PROGRESS_WAIT_CEILING_SEC, Config
from .leniency import normalize
from .models import (
    Approval,
    Decision,
    ErrorCode,
    Plan,
    PlanStatus,
    TERMINAL_PLAN_STATUSES,
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
    def __init__(self, store: Store, config: Config, approval_ui: ApprovalServer | None = None):
        self.store = store
        self.config = config
        if approval_ui is not None:
            self.approval_ui = approval_ui
        elif config.blocking_approval:
            self.approval_ui = ApprovalServer(
                ApprovalStore(config.state_dir),
                port=config.approval_port,
                open_browser=config.approval_open_browser,
            )
        else:
            self.approval_ui = None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def dispatch(
        self,
        tool_name: str,
        raw_args: Any,
        progress_token: Any = None,
        notifier: Any = None,
    ) -> dict[str, Any]:
        clean, notes = normalize(tool_name, raw_args)
        try:
            # Serialized against other threads AND other server processes on the same
            # state directory. The blocking approval wait explicitly gives this up
            # (see _wait_for_human) so one pending approval cannot freeze every other
            # session for the length of the wait.
            with self.store.transaction():
                # A human may have clicked approve after the previous call timed out.
                # Collect that first so every handler below sees the true state.
                self._apply_late_decision()
                if tool_name == "plan_and_think":
                    return self._plan_and_think(clean, notes)
                if tool_name == "request_user_approval":
                    return self._request_user_approval(
                        clean, notes, progress_token=progress_token, notifier=notifier
                    )
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

    # ---- decision application (shared by the blocking and late paths) -------
    @staticmethod
    def _fingerprint(plan: Plan) -> str:
        """Identifies the exact plan version shown to the human."""
        payload = plan.goal + "\x00" + "\x00".join(t.title for t in plan.tasks)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _mutate_approved(plan: Plan, comment: str | None) -> None:
        plan.approval.decision = Decision.APPROVED.value
        plan.approval.decided_at = now_iso()
        if comment:
            plan.approval.user_comment = comment
        plan.set_status(PlanStatus.APPROVED)

    @staticmethod
    def _mutate_rejected(plan: Plan, comment: str | None) -> None:
        plan.approval.decision = Decision.REJECTED.value
        plan.approval.decided_at = now_iso()
        plan.approval.user_comment = comment or ""
        plan.set_status(PlanStatus.CANCELLED)

    @staticmethod
    def _mutate_revise(plan: Plan, comment: str | None) -> None:
        plan.approval.revision_count += 1
        plan.approval.user_comment = comment or ""
        plan.approval.reset_request()
        plan.set_status(PlanStatus.DRAFTING)

    def _apply_late_decision(self) -> None:
        """Honour a decision the human made after the tool call had already returned.

        The approval page stays actionable past the request timeout so people can take
        their time. Whatever they clicked is collected here, on the next tool call of
        any kind, and applied to the plan - otherwise the button would appear to work
        while nothing actually happened.
        """
        if self.approval_ui is None:
            return
        state = self.store.load()
        # Any active plan may have a decision waiting - concurrent sessions each have
        # their own, so checking only "the active plan" would strand the others.
        for candidate in state.active_plans():
            taken = self.approval_ui.take_decision(
                candidate.plan_id, self._fingerprint(candidate)
            )
            if taken is not None:
                plan = candidate
                break
        else:
            return
        decision, comment = taken
        if decision == Decision.APPROVED.value:
            self._mutate_approved(plan, comment)
        elif decision == Decision.REJECTED.value:
            self._mutate_rejected(plan, comment)
        else:
            self._mutate_revise(plan, comment)
        self.store.save(state)
        self.store.audit(
            "late_decision_applied", plan_id=plan.plan_id, decision=decision, comment=comment
        )
        log.warning("Applied a late human decision for %s: %s", plan.plan_id, decision)

    # ---- plan routing ---------------------------------------------------
    _AMBIGUOUS = object()

    @staticmethod
    def _resolve_plan(state: State, plan_id: Any) -> Any:
        """Which plan does this call mean?

        Explicit plan_id wins. Otherwise, if exactly one plan is in flight it is
        unambiguous and the model never has to know plan_id exists. Only genuinely
        concurrent sessions hit the ambiguous case, and the error tells the model
        exactly what to send.
        """
        if isinstance(plan_id, str) and plan_id.strip().lower() not in (
            "", "current", "active", "latest"
        ):
            return state.plans.get(plan_id.strip())
        actives = state.active_plans()
        if len(actives) == 1:
            return actives[0]
        if not actives:
            # Nothing live: fall back to the most recent plan so the model is told
            # "that plan was cancelled/completed" instead of "no plan exists", which
            # would invite it to quietly start over.
            if state.plans:
                return max(state.plans.values(), key=lambda p: p.updated_at)
            return None
        return PlanningHandlers._AMBIGUOUS

    @staticmethod
    def _plan_directory(state: State) -> list[dict[str, Any]]:
        return [
            {"plan_id": p.plan_id, "goal": p.goal, "plan_status": p.plan_status,
             "progress": p.progress()}
            for p in state.active_plans()
        ]

    def _ambiguous(self, state: State, notes: list[str]) -> dict[str, Any]:
        return error(
            None,
            ErrorCode.PLAN_AMBIGUOUS,
            f"{len(state.active_plans())} plans are active; say which one with plan_id.",
            notes=notes,
            active_plans=self._plan_directory(state),
        )

    def _expire_stale_approval(self, state: State, plan: Plan | None) -> bool:
        """Revoke an approval that has gone cold, before anything acts on it.

        Without this, a plan approved hours ago in a different conversation keeps
        authorizing execution: plan_and_think redirects to it, request_user_approval
        short-circuits with "already approved" (so nothing ever blocks and no approval
        page appears), and update_task_progress sails through. Observed in the field.
        """
        if plan is None or not plan.approval_is_stale(self.config.approval_ttl):
            return False
        idle = plan.idle_seconds()
        # An unreadable timestamp yields infinity, which int() cannot represent.
        idle_field = None if idle == float("inf") else int(idle)
        plan.set_status(PlanStatus.AWAITING_APPROVAL)
        plan.approval.reset_request()  # forces a fresh ASK_USER, not a replayed decision
        self.store.save(state)
        self.store.audit(
            "approval_expired", plan_id=plan.plan_id, idle_seconds=idle_field,
            ttl=self.config.approval_ttl,
        )
        log.warning(
            "Approval for %s expired (%s idle) - re-approval required",
            plan.plan_id,
            f"{idle_field}s" if idle_field is not None else "unreadable timestamp",
        )
        return True

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
        goal = (args.get("goal") or "").strip()
        thought = (args.get("thought") or "").strip()

        # Route by goal. Within one conversation the model repeats the same goal on
        # every step, so a matching active plan is this session's plan. A different goal
        # belongs to a different session and gets its own plan - it must never evict
        # somebody else's work, which is what a single active-plan slot used to do.
        plan = state.plan_for_goal(goal)
        if plan is None and not goal:
            resolved = self._resolve_plan(state, args.get("plan_id"))
            plan = None if resolved is self._AMBIGUOUS else resolved
        if plan is not None and plan.status in TERMINAL_PLAN_STATUSES:
            plan = None  # a finished plan is never resurrected; this starts a new one

        if self._expire_stale_approval(state, plan):
            notes.append(
                "This plan's approval had expired and was revoked. It no longer "
                "authorizes any execution."
            )

        if plan is not None and plan.status in (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION):
            self.store.audit("plan_and_think_redirected", plan_id=plan.plan_id)
            task = plan.current_task()
            return build(
                plan,
                message=(
                    "This plan is already approved and running. Continue it instead of "
                    "planning again. Use get_current_plan if you need the details."
                ),
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                tasks=plan.tasks_brief(),
                progress=plan.progress(),
                next_task={"task_id": task.task_id, "title": task.title} if task else None,
            )

        if plan is None and len(state.active_plans()) >= self.config.max_active_plans:
            return error(
                None,
                ErrorCode.PLAN_AMBIGUOUS,
                f"{len(state.active_plans())} plans are already active "
                f"(limit {self.config.max_active_plans}).",
                notes=notes,
                active_plans=self._plan_directory(state),
                message_hint=(
                    "Finish or cancel one of the active plans before starting another."
                ),
            )

        # Lenient defaults: erroring on a missing scalar costs a turn and teaches nothing.
        if plan is not None:
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
        if plan is None:
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
            state.active_plan_id = plan.plan_id

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
                qualify=len(state.active_plans()) > 1,
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
            qualify=len(state.active_plans()) > 1,
            message=(
                f"Plan created with {len(plan.tasks)} tasks. "
                "Execution is locked until the user approves."
            ),
        )

    # ------------------------------------------------------------------
    # 2. request_user_approval  (HITL gate)
    # ------------------------------------------------------------------
    def _request_user_approval(
        self,
        args: dict[str, Any],
        notes: list[str],
        progress_token: Any = None,
        notifier: Any = None,
    ) -> dict[str, Any]:
        state = self.store.load()
        plan = self._resolve_plan(state, args.get("plan_id"))
        if plan is self._AMBIGUOUS:
            return self._ambiguous(state, notes)
        if self._expire_stale_approval(state, plan):
            notes.append(
                "This plan's earlier approval had expired and was revoked. Ask the user "
                "to approve the current plan again."
            )

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
            return self._ask_user(
                state, plan, args, notes, progress_token=progress_token, notifier=notifier
            )
        if decision is Decision.APPROVED:
            return self._approve(state, plan, args, notes)
        if decision is Decision.REVISE:
            return self._revise(state, plan, args, notes)
        return self._reject(state, plan, args, notes)

    def _ask_user(
        self,
        state: State,
        plan: Plan,
        args: dict[str, Any],
        notes: list[str],
        progress_token: Any = None,
        notifier: Any = None,
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
        display = render_plan_for_user(plan, plan_summary)

        # The URL only reaches the human through stderr otherwise, which nobody reads in
        # a desktop app. Putting it in display_to_user means the model prints it in chat,
        # so a blocked popup or a second monitor no longer hides the approval page.
        approval_url = self.approval_ui.url if self.approval_ui is not None else None
        if approval_url:
            display = f"{display}\n\n승인/거절: {approval_url}"

        if self.approval_ui is not None:
            fingerprint = self._fingerprint(plan)
            waited_plan_id = plan.plan_id
            decided = self._wait_for_human(plan, display, progress_token, notifier, notes)
            if decided is not None:
                decision, comment = decided
                # The transaction was released while waiting, so another session may
                # have moved things on. Re-read THIS plan by id - resolving "the active
                # plan" would pick up a concurrent session's plan instead - and verify
                # it is still what the human saw.
                state = self.store.load()
                plan = state.plans.get(waited_plan_id)
                if plan is None or self._fingerprint(plan) != fingerprint:
                    self.store.audit(
                        "approval_discarded_plan_changed",
                        plan_id=plan.plan_id if plan else None,
                        decision=decision,
                    )
                    notes.append(
                        "The plan changed while the user was deciding, so that decision "
                        "was discarded. Show the current plan and ask again."
                    )
                    return build(
                        plan,
                        notes=notes,
                        tasks=plan.tasks_brief() if plan else None,
                        approval_url=approval_url,
                    )
                # Reuse the already-tested transitions so the blocking path and the
                # two-phase path can never diverge.
                forwarded = {"user_comment": comment} if comment else {}
                if decision == Decision.APPROVED.value:
                    return self._approve(state, plan, forwarded, notes)
                if decision == Decision.REJECTED.value:
                    return self._reject(state, plan, forwarded, notes)
                if decision == Decision.REVISE.value:
                    return self._revise(state, plan, forwarded, notes)
            notes.append(
                "No human decision arrived before the wait expired. The plan is still "
                "LOCKED. Show the plan to the user and stop; do not execute anything."
            )

        return build(
            plan,
            tasks=plan.tasks_brief(),
            notes=notes,
            display_to_user=display,
            approval_url=approval_url,
        )

    def _wait_for_human(
        self,
        plan: Plan,
        display: str,
        progress_token: Any,
        notifier: Any,
        notes: list[str],
    ) -> tuple[str, str] | None:
        """Block this tool call until a human decides. Returns None on timeout.

        This is what actually stops the agent: AnythingLLM's loop waits synchronously
        for the tool result, so while we do not return, the model cannot emit another
        tool call - no matter what the system prompt failed to make it do.
        """
        can_heartbeat = progress_token is not None and notifier is not None
        timeout = self.effective_timeout(can_heartbeat)

        request_id = self.approval_ui.open_request(
            plan.plan_id, plan.goal, display, plan.tasks_brief(), self._fingerprint(plan)
        )
        if request_id is None:
            # Degrading quietly would remove the hard pause without anyone noticing -
            # the worst possible failure for a safety gate. Make it audible instead.
            log.error(
                "APPROVAL UI UNAVAILABLE - the hard pause is OFF for this call. "
                "The model is only *asked* to stop."
            )
            self.store.audit("approval_ui_unavailable", plan_id=plan.plan_id)
            notes.append(
                "WARNING: the approval UI could not start, so this plan was NOT hard-paused. "
                "Do not execute anything. Show the plan to the user and stop."
            )
            return None

        stop = threading.Event()
        if can_heartbeat:
            threading.Thread(
                target=self._heartbeat,
                args=(notifier, progress_token, stop),
                name="approval-heartbeat",
                daemon=True,
            ).start()

        log.warning(
            "Blocking for human approval of %s at %s (timeout %ss, heartbeat %s)",
            plan.plan_id,
            self.approval_ui.url,
            timeout,
            "on" if can_heartbeat else "off - no progressToken from client",
        )
        decided: tuple[str, str] | None = None
        try:
            # Let every other session through while this one waits on a person.
            with self.store.paused():
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    # Polling, not an Event: the decision may be recorded by a DIFFERENT
                    # server process (whichever one owns the page), so it has to be read
                    # from shared state.
                    decided = self.approval_ui.claim(request_id)
                    if decided is not None:
                        break
                    time.sleep(0.2)
        finally:
            stop.set()

        if decided is None:
            # Deliberately leave the request published. Clearing it here is what made
            # the buttons vanish after 55s, before the human had a chance to answer;
            # the next tool call collects whatever they click later.
            self.store.audit("approval_wait_timeout", plan_id=plan.plan_id, timeout=timeout)
            return None
        self.store.audit(
            "approval_decided_out_of_band",
            plan_id=plan.plan_id,
            decision=decided[0],
            comment=decided[1],
        )
        return decided

    def effective_timeout(self, can_heartbeat: bool) -> int:
        """How long we may hold the tool call open.

        Without a progressToken we cannot legally send progress notifications, so the
        wait has to finish inside the client's 60s request timeout. With one, the
        heartbeat keeps resetting that timer and the configured timeout applies.
        """
        if can_heartbeat:
            return self.config.approval_timeout
        return min(self.config.approval_timeout, NO_PROGRESS_WAIT_CEILING_SEC)

    @staticmethod
    def _heartbeat(notifier: Any, token: Any, stop: threading.Event) -> None:
        """Reset the client's request timer while a human thinks.

        The MCP TS SDK resets its 60s timeout on every progress notification and sets no
        maxTotalTimeout, so a steady heartbeat turns a bounded wait into an open one.
        """
        n = 0
        while not stop.wait(20):
            n += 1
            notifier.progress(token, n, "Waiting for human approval...")

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

        self._mutate_approved(plan, args.get("user_comment"))
        self.store.save(state)
        self.store.audit("approved", plan_id=plan.plan_id, comment=args.get("user_comment"))

        task = plan.current_task()
        return build(
            plan,
            message="Execution is now unlocked.",
            notes=notes,
            qualify=len(state.active_plans()) > 1,
            tasks=plan.tasks_brief(),
            progress=plan.progress(),
            next_task={"task_id": task.task_id, "title": task.title} if task else None,
        )

    def _revise(
        self, state: State, plan: Plan, args: dict[str, Any], notes: list[str]
    ) -> dict[str, Any]:
        comment = args.get("user_comment") or ""
        self._mutate_revise(plan, comment)
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
        self._mutate_rejected(plan, comment)
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
        plan = self._resolve_plan(state, args.get("plan_id"))
        if plan is self._AMBIGUOUS:
            return self._ambiguous(state, notes)
        if self._expire_stale_approval(state, plan):
            self.store.audit("execution_blocked", plan_id=plan.plan_id, reason="APPROVAL_EXPIRED")
            return error(
                plan,
                ErrorCode.APPROVAL_EXPIRED,
                "The approval for this plan expired while it sat idle.",
                notes=notes,
                tasks=plan.tasks_brief(),
            )

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
            qualify=len(state.active_plans()) > 1,
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
            qualify=len(state.active_plans()) > 1,
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

        plan = self._resolve_plan(state, requested)
        if plan is self._AMBIGUOUS:
            # Recovery must never fail; hand back the directory so the model can pick.
            return build(
                None,
                notes=notes,
                message=(
                    f"{len(state.active_plans())} plans are active. Call again with the "
                    "plan_id of the one this conversation is working on."
                ),
                active_plans=self._plan_directory(state),
            )
        if plan is None and requested.lower() not in ("current", "active", "latest", ""):
            notes.append(f"No plan with id '{requested}'.")

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
