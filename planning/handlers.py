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

from .approval import (
    PHASE_COMPLETION,
    PHASE_PLAN,
    SCOPE_PLAN,
    SCOPE_TASKS,
    ApprovalServer,
    ApprovalStore,
    Verdict,
)
from .config import NO_PROGRESS_WAIT_CEILING_SEC, Config
from .leniency import UNMATCHED_TITLES_KEY, normalize
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
from .responses import build, error, render_completion_report, render_plan_for_user
from .state_machine import can_start_task, execution_guard
from .store import State, Store, goal_key, title_key

log = logging.getLogger("planning-mcp.handlers")

# Phrases that assert completion without evidencing it. Length alone cannot separate
# these from a genuinely terse but informative log - Korean packs a real sentence into
# ~12 characters - so the filter is content-based and only fires on an exact match.
_EMPTY_CLAIMS = {
    "done", "ok", "okay", "complete", "completed", "finished", "success", "successful",
    "task done", "task complete", "task completed", "work done", "all done", "yes",
    "완료", "성공", "작업완료", "완료함", "완료했습니다", "완료되었습니다", "성공적으로완료",
    "성공적으로완료했습니다", "작업을완료했습니다", "처리완료", "끝", "됐습니다", "했습니다",
}


def _normalize_evidence(text: str) -> str:
    """Lowercase and strip whitespace/punctuation so claims can be compared by content."""
    return "".join(
        ch for ch in (text or "").lower() if not ch.isspace() and ch not in ".,!?;:-_…。、"
    )


def missing_evidence_reason(result_log: str, task_title: str, min_len: int) -> str | None:
    """Why this result_log fails as proof of work, or None if it is acceptable.

    The server cannot see whether work happened; the closest available check is whether
    the model wrote something that *could only* have been written after doing it.
    """
    text = (result_log or "").strip()
    if not text:
        return "result_log was empty"
    normalized = _normalize_evidence(text)
    if normalized in _EMPTY_CLAIMS:
        return "result_log only claims success without saying what was produced"
    if normalized == _normalize_evidence(task_title):
        return "result_log just repeats the task title instead of reporting an outcome"
    if len(normalized) < min_len:
        return f"result_log is too short to be a real outcome (min {min_len} characters)"
    return None


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
        notes: list[str] = []
        try:
            # normalize() lives inside the guard on purpose: were it to raise on some
            # unforeseen input it would otherwise escape as a raw JSON-RPC error, when a
            # weak model needs the graceful ok:false + next_action instead.
            clean, notes = normalize(tool_name, raw_args)
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
        """Identifies the exact plan version shown to the human.

        Includes each task's status and evidence, so a completion report the human
        approved cannot be quietly rewritten afterwards - the decision would no longer
        match and is discarded.
        """
        payload = "\x00".join(
            [plan.goal]
            + [f"{t.task_id}|{t.title}|{t.status}|{(t.result_log or '')}" for t in plan.tasks]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _withdraw_approval_request(self, plan: Plan) -> None:
        """Take a plan's request off the page once it has been settled."""
        if self.approval_ui is not None:
            try:
                self.approval_ui.drop_for_plan(plan.plan_id)
            except Exception:  # noqa: BLE001 - never fail a call over UI housekeeping
                log.debug("Could not withdraw the approval request for %s", plan.plan_id)

    def _mutate_approved(self, plan: Plan, comment: str | None) -> None:
        plan.approval.decision = Decision.APPROVED.value
        plan.approval.decided_at = now_iso()
        if comment:
            plan.approval.user_comment = comment
        # The "you flagged this one" markers exist to guide a re-read of a revised plan.
        # Once it is approved they are answered, and leaving them would decorate the
        # completion report with stale complaints.
        plan.clear_revision_marks()
        plan.pending_revision = None
        plan.rework_from_completion = False
        # Approving a completion report closes the plan; approving a draft unlocks it -
        # unless the draft is already finished work carried through a redraft, in which
        # case unlocking it would let the model answer without the completion check ever
        # being asked. Route that back to the verification gate instead.
        if plan.status is PlanStatus.AWAITING_COMPLETION:
            plan.set_status(PlanStatus.COMPLETED)
        elif plan.all_done() and self.config.completion_approval:
            plan.set_status(PlanStatus.AWAITING_COMPLETION)
        else:
            plan.set_status(PlanStatus.APPROVED)
        self._withdraw_approval_request(plan)

    def _mutate_rejected(self, plan: Plan, comment: str | None) -> None:
        plan.approval.decision = Decision.REJECTED.value
        plan.approval.decided_at = now_iso()
        plan.approval.user_comment = comment or ""
        plan.set_status(PlanStatus.CANCELLED)
        self._withdraw_approval_request(plan)

    @staticmethod
    def _revision_targets(plan: Plan, task_comments: dict[str, str] | None) -> dict[str, str]:
        """Keep only comments that name a task this plan actually has.

        A comment on a task that no longer exists cannot be answered by rewriting it, and
        letting it through would leave a target the model can never satisfy.
        """
        targets: dict[str, str] = {}
        for raw_id, text in (task_comments or {}).items():
            try:
                task_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            body = str(text or "").strip()
            if body and plan.get_task(task_id) is not None:
                targets[str(task_id)] = body
        return targets

    def _mutate_no(
        self,
        plan: Plan,
        comment: str | None,
        task_comments: dict[str, str] | None = None,
        scope: str = SCOPE_PLAN,
    ) -> tuple[str, Any]:
        """Route the human's "no" to the mutation that actually answers it.

        The same button carries two different objections depending on when it is pressed.
        Before execution it means "this plan is wrong". After the completion report it
        means "the plan was fine, what came out of these tasks is not" - and treating
        that as a re-plan is what used to throw away every task's evidence and order the
        model to redo work nobody complained about.

        Returns ("REWORK", [task_id, ...]) or ("REVISE", {task_id: comment}).
        """
        targets = (
            self._revision_targets(plan, task_comments) if scope == SCOPE_TASKS else {}
        )
        if plan.status is PlanStatus.AWAITING_COMPLETION and targets:
            reopened = self._mutate_rework(
                plan, comment, {int(k): v for k, v in targets.items()}
            )
            return "REWORK", reopened
        return "REVISE", self._mutate_revise(plan, comment, task_comments, scope)

    def _mutate_rework(
        self, plan: Plan, comment: str | None, targets: dict[int, str]
    ) -> list[int]:
        """Reopen specific finished tasks without disturbing the plan around them.

        The human is not disputing the task list here - they approved it and it has not
        changed - so it is not re-approved. Only the named tasks go back to PENDING;
        every other task keeps its DONE status and its evidence, which is the whole
        point. Ordering is untouched, so `can_start_task` and `unfinished_before` keep
        working unchanged: a reopened task is simply the next PENDING one.
        """
        plan.approval.revision_count += 1
        plan.approval.user_comment = comment or ""
        # The completion report is withdrawn rather than answered: once the rework lands,
        # a fresh one has to be asked for and verified.
        plan.approval.reset_request()
        reopened: list[int] = []
        for task_id in sorted(targets):
            task = plan.get_task(task_id)
            if task is None:  # _revision_targets already filtered; belt and braces
                continue
            task.revision_note = targets[task_id]
            # What it produced last time, kept so the redo is not done blind. Marks on
            # other tasks are deliberately left alone: a note from an earlier round is
            # still the reason that task reads the way it does on the next report.
            task.previous_result_log = task.result_log or task.previous_result_log
            task.result_log = None
            task.status = TaskStatus.PENDING.value
            task.started_at = None
            task.finished_at = None
            reopened.append(task_id)
        plan.pending_revision = None
        plan.rework_from_completion = False
        plan.set_status(PlanStatus.IN_EXECUTION)
        self._withdraw_approval_request(plan)
        return reopened

    def _mutate_revise(
        self,
        plan: Plan,
        comment: str | None,
        task_comments: dict[str, str] | None = None,
        scope: str = SCOPE_PLAN,
    ) -> dict[str, str]:
        """Send a plan back for changes. Returns the per-task targets, if any.

        Two shapes of "no" share this path. A whole-plan revision is the original
        behaviour: everything is redrafted. A targeted one records exactly which tasks
        the human objected to, which is the only thing that later stops the model from
        quietly rewriting the tasks they already accepted.
        """
        # Read before the status changes. A redraft asked for from the completion report
        # must not delete the work that has already been done - see `_carry_evidence`.
        plan.rework_from_completion = plan.status is PlanStatus.AWAITING_COMPLETION
        plan.approval.revision_count += 1
        plan.approval.user_comment = comment or ""
        plan.approval.reset_request()
        # Markers from a previous round would otherwise read as complaints about this one.
        plan.clear_revision_marks()
        targets = (
            self._revision_targets(plan, task_comments) if scope == SCOPE_TASKS else {}
        )
        plan.pending_revision = {"targets": targets} if targets else None
        plan.set_status(PlanStatus.DRAFTING)
        self._withdraw_approval_request(plan)
        return targets

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
        verdict: Verdict = taken
        if verdict.decision == Decision.APPROVED.value:
            self._mutate_approved(plan, verdict.comment)
        elif verdict.decision == Decision.REJECTED.value:
            self._mutate_rejected(plan, verdict.comment)
        else:
            self._mutate_no(
                plan, verdict.comment, verdict.task_comments, verdict.scope
            )
        self.store.save(state)
        self.store.audit(
            "late_decision_applied",
            plan_id=plan.plan_id,
            decision=verdict.decision,
            comment=verdict.comment,
            scope=verdict.scope,
            task_comments=verdict.task_comments or None,
        )
        log.warning("Applied a late human decision for %s: %s", plan.plan_id, verdict.decision)

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
        revised_goal = (args.get("revised_goal") or "").strip()
        thought = (args.get("thought") or "").strip()
        step_number = args.get("step_number")
        continuing = isinstance(step_number, int) and step_number > 1

        # Routing, in priority order:
        #   1. explicit plan_id wins;
        #   2. exact (normalized) goal match - the model repeats its goal each step;
        #   3. the corrected goal, for a model that put it in both fields;
        #   4. a goal this plan has since revised away from;
        #   5. no match while CONTINUING (step > 1) - do not fork a plan on drifted goal;
        #      hand the model the list of active goals so it picks the exact one;
        #   6. no match while STARTING (step 1) - a genuinely new plan.
        plan = None
        pid_arg = args.get("plan_id")
        if isinstance(pid_arg, str) and pid_arg.strip().lower() not in (
            "", "current", "active", "latest"
        ):
            plan = state.plans.get(pid_arg.strip())
        if plan is None:
            plan = state.plan_for_goal(goal)
        if plan is None and revised_goal:
            plan = state.plan_for_goal(revised_goal)
        if plan is None and goal:
            # One turn of grace after a correction: the model is still echoing the
            # wording it has used all conversation. Continue the plan, do not fork it.
            plan = state.plan_for_former_goal(goal)
            if plan is not None:
                notes.append(
                    f"'{goal}' is this plan's previous goal; it was corrected to "
                    f"'{plan.goal}'. Send the corrected text from now on."
                )
        if plan is None and revised_goal and len(state.active_plans()) == 1:
            # A correction with the corrected text in BOTH fields matches nothing by
            # name. But asking to correct a goal presupposes a plan that has one, and
            # with exactly one in flight there is no other plan it could mean. With
            # several active this stays unresolved and the model is asked to pick.
            plan = state.active_plans()[0]
            notes.append(
                "'goal' matched no plan, so 'revised_goal' was applied to the only "
                "active plan. Send the goal already on record next time."
            )
        if plan is None and not goal:
            resolved = self._resolve_plan(state, pid_arg)
            plan = None if resolved is self._AMBIGUOUS else resolved
        if plan is not None and plan.status in TERMINAL_PLAN_STATUSES:
            plan = None  # a finished plan is never resurrected; this starts a new one

        # The requested feature: no exact goal match, but the model believes it is
        # continuing. Rather than silently create a second plan (goal drift = fork),
        # return the active goals and let the model pick the exact one.
        if plan is None and goal and continuing and state.active_plans():
            self.store.audit("goal_not_matched", goal=goal, step_number=step_number)
            return error(
                None,
                ErrorCode.GOAL_NOT_MATCHED,
                f"No active plan matches the goal '{goal}'.",
                notes=notes,
                active_plans=self._plan_directory(state),
            )

        if self._expire_stale_approval(state, plan):
            notes.append(
                "This plan's approval had expired and was revoked. It no longer "
                "authorizes any execution."
            )

        # --- goal revision --------------------------------------------------
        # The user said the goal itself was wrong. Refusing the edit would leave the
        # server describing the plan by a goal the user has already disowned while it
        # executes tasks written for the corrected one - a state nobody can act on. So
        # the goal moves; `original_goal` and `goal_history` carry the audit trail that
        # freezing it used to provide.
        #
        # Deliberately AFTER the staleness check: revising touches the plan, and a touch
        # before the check would let a wording fix silently un-expire an approval that
        # had already sat idle past its TTL.
        goal_was_revised = False
        if plan is not None and revised_goal and goal_key(revised_goal) != goal_key(plan.goal):
            previous_goal = plan.goal
            goal_was_revised = plan.revise_goal(revised_goal)
            if goal_was_revised:
                self.store.save(state)
                self.store.audit(
                    "goal_revised",
                    plan_id=plan.plan_id,
                    previous_goal=previous_goal,
                    goal=plan.goal,
                    original_goal=plan.original_goal,
                    plan_status=plan.plan_status,
                )
                notes.append(
                    f"The goal was corrected to '{plan.goal}'. The previous wording is "
                    "kept in this plan's history. Send the corrected text as 'goal' from "
                    "now on."
                )

        if plan is not None and plan.status in (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION):
            self.store.audit("plan_and_think_redirected", plan_id=plan.plan_id)
            task = plan.current_task()
            if goal_was_revised:
                # The approval on file was given for the OLD goal. Recording the
                # correction must not quietly widen what the human agreed to.
                notes.append(
                    "The user approved this plan under the previous goal. Check whether "
                    "the remaining tasks still serve the corrected goal - if they do "
                    "not, re-plan and call request_user_approval again."
                )
            return build(
                plan,
                message=(
                    "This plan is already approved and running. Continue it instead of "
                    "planning again. Use get_current_plan if you need the details."
                ),
                notes=notes,
                goal=plan.goal if goal_was_revised else None,
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
        elif revised_goal:
            # A correction with nothing to correct: there is no plan yet, so the
            # corrected text is simply the goal of the one about to be created.
            goal = revised_goal
            notes.append(
                "There was no existing plan to correct, so 'revised_goal' was used as "
                "the goal of this new plan."
            )
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
                goal=plan.goal if goal_was_revised else None,
                message=f"Thinking step {step_number} recorded.",
            )

        # --- finalizing -------------------------------------------------------
        task_list = args.get("task_list") or []
        task_updates = args.get("task_updates") or []
        targets = plan.revision_targets()

        # A model told to rewrite one flagged task frequently sends only the new wording -
        # task_updates=["the rewritten task"] - because that is what it was asked for.
        # With exactly one flagged task there is exactly one task it can mean, so pairing
        # them is a reading, not a guess. With more than one it WOULD be a guess, and the
        # model is sent back to try again rather than have the wrong task rewritten.
        unmatched = args.get(UNMATCHED_TITLES_KEY) or []
        if not task_updates and len(targets) == 1 and len(unmatched) == 1:
            only = next(iter(targets))
            task_updates = [{"task_id": only, "title": unmatched[0]}]
            notes.append(
                f"task_updates arrived with no task_id. The user commented on task {only} "
                f"only, so it was applied there. Next time send "
                f'[{{"task_id": {only}, "title": "..."}}].'
            )
        elif unmatched and targets:
            notes.append(
                f"{len(unmatched)} entry/entries in task_updates had no task_id and were "
                f"ignored: the user commented on {len(targets)} tasks, so there is no way "
                'to tell which one you meant. Send [{"task_id": <number>, "title": "..."}].'
            )

        if task_updates and not targets:
            # Without a pending per-task request this would be the model editing an
            # arbitrary task on its own authority - the plan the human saw would change
            # underneath them. Refuse; a genuine re-plan goes through task_list.
            plan.touch()
            self.store.save(state)
            self.store.audit("unrequested_task_updates", plan_id=plan.plan_id)
            return error(
                plan,
                ErrorCode.REVISION_NOT_REQUESTED,
                "task_updates was sent but the user has not asked for changes to any "
                "specific task.",
                notes=notes,
                recorded_step=step_number,
                tasks=plan.tasks_brief(),
            )

        if targets and task_updates:
            if task_list:
                notes.append(
                    "Both task_updates and task_list were sent. task_list was ignored: "
                    "the user only asked for changes to specific tasks."
                )
            return self._apply_task_updates(
                state, plan, task_updates, targets, notes, step_number
            )

        if not task_list:
            plan.touch()
            self.store.save(state)  # the thought is kept; only the finalization is refused
            if targets:
                return error(
                    plan,
                    ErrorCode.REVISION_INCOMPLETE,
                    "The user asked for changes to specific tasks, but neither "
                    "task_updates nor task_list was sent.",
                    notes=notes,
                    recorded_step=step_number,
                    tasks=plan.tasks_brief(),
                )
            return error(
                plan,
                ErrorCode.MISSING_TASK_LIST,
                "need_more_thinking was false but task_list was empty or missing.",
                notes=notes,
                recorded_step=step_number,
            )

        if targets:
            # The model was asked to change two lines and rewrote the whole plan. That is
            # wasteful rather than unsafe - the human still re-approves every task - so it
            # is accepted, told, and recorded, which is what makes the waste measurable.
            notes.append(
                "The user only asked for changes to task(s) "
                f"{', '.join(str(t) for t in sorted(targets))}, but you replaced the whole "
                "task list. It was accepted; the user must now re-approve every task. "
                "Next time send task_updates."
            )
            self.store.audit(
                "targeted_revision_ignored",
                plan_id=plan.plan_id,
                targets=sorted(targets),
            )
            plan.pending_revision = None

        if len(task_list) > self.config.max_tasks:
            notes.append(
                f"task_list had {len(task_list)} items; kept the first {self.config.max_tasks}. "
                "Aim for 2-7 concrete actions."
            )
            task_list = task_list[: self.config.max_tasks]

        if plan.tasks:  # re-plan: keep the old breakdown as evidence
            plan.superseded_tasks.append([t.to_dict() for t in plan.tasks])

        previous = plan.tasks
        plan.tasks = [Task(task_id=i, title=title) for i, title in enumerate(task_list, start=1)]
        if plan.rework_from_completion:
            plan.rework_from_completion = False
            carried = self._carry_evidence(previous, plan.tasks)
            if carried:
                notes.append(
                    "Task(s) "
                    + ", ".join(str(t) for t in carried)
                    + " survived this rewrite unchanged, so they keep the work you "
                    "already did and their result_log. Do NOT do them again - only the "
                    "tasks that are still PENDING need work."
                )
                self.store.audit(
                    "evidence_carried", plan_id=plan.plan_id, tasks=carried
                )
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
            goal=plan.goal if goal_was_revised else None,
            message=(
                f"Plan created with {len(plan.tasks)} tasks. "
                "Execution is locked until the user approves."
            ),
        )

    @staticmethod
    def _carry_evidence(previous: list[Task], current: list[Task]) -> list[int]:
        """Move finished work onto the tasks that survived a redraft. Returns their ids.

        Only ever used when the redraft answers a revision asked for from the COMPLETION
        report: the work has already happened, and wiping it is what turned "you missed a
        step" into an order to rerun the entire plan. An ordinary re-plan - after a
        failure, say - still drops the old evidence, which is correct there.

        Matched with `title_key`, so a trailing period or a doubled space does not lose a
        task's work, and by key rather than by position, so reordering or inserting a step
        keeps it too. Deliberately no fuzzy matching: a miss only costs a redo, while a
        wrong match would let a task that must be redone keep a stale result_log and be
        skipped. Duplicate titles are paired in order; whatever is left over carries
        nothing. Only DONE work is carried - a FAILED task has nothing worth keeping.
        """
        pool: dict[str, list[Task]] = {}
        for task in previous:
            if task.status == TaskStatus.DONE.value:
                pool.setdefault(title_key(task.title), []).append(task)
        carried: list[int] = []
        for task in current:
            bucket = pool.get(title_key(task.title))
            if not bucket:
                continue
            source = bucket.pop(0)
            task.status = source.status
            task.result_log = source.result_log
            task.started_at = source.started_at
            task.finished_at = source.finished_at
            task.previous_result_log = source.previous_result_log
            carried.append(task.task_id)
        return carried

    def _apply_task_updates(
        self,
        state: State,
        plan: Plan,
        updates: list[dict[str, Any]],
        targets: dict[int, str],
        notes: list[str],
        step_number: int,
    ) -> dict[str, Any]:
        """Rewrite only the tasks the human flagged, leaving the rest untouched.

        This is the whole point of per-task review: the tasks the human already read and
        accepted keep their identity, their position, and - if the plan had already been
        running - their evidence. Only a flagged task is reset, because only a flagged
        task is being asked to change.
        """
        # Validated in full before anything is written. A bad task_id halfway through a
        # single-pass loop would persist half a revision and leave the plan in a state
        # nobody approved.
        planned: list[tuple[int, str]] = []
        ignored: list[int] = []
        for item in updates:
            task_id = item.get("task_id")
            title = str(item.get("title") or "").strip()
            if not isinstance(task_id, int) or plan.get_task(task_id) is None:
                plan.touch()
                self.store.save(state)
                valid = ", ".join(str(t.task_id) for t in plan.tasks) or "none"
                return error(
                    plan,
                    ErrorCode.TASK_NOT_FOUND,
                    f"No task with task_id={task_id} in this plan. Valid task_id "
                    f"values are: {valid}.",
                    notes=notes,
                    recorded_step=step_number,
                    tasks=plan.tasks_brief(),
                )
            if task_id not in targets or not title:
                ignored.append(task_id)
                continue
            planned.append((task_id, title))

        if not planned:
            plan.touch()
            self.store.save(state)
            return error(
                plan,
                ErrorCode.REVISION_INCOMPLETE,
                "None of the tasks the user commented on were rewritten.",
                notes=notes,
                recorded_step=step_number,
                tasks=plan.tasks_brief(),
            )

        plan.superseded_tasks.append([t.to_dict() for t in plan.tasks])
        for task_id, title in planned:
            task = plan.get_task(task_id)
            task.previous_title = task.title
            task.title = title
            task.revision_note = targets[task_id]
            # A rewritten task is a different task, so whatever was done for the old one
            # no longer counts. Untouched tasks keep their status and their result_log.
            task.status = TaskStatus.PENDING.value
            task.result_log = None
            task.started_at = None
            task.finished_at = None

        applied = [task_id for task_id, _ in planned]
        if ignored:
            notes.append(
                f"Ignored the change to task(s) {', '.join(str(t) for t in ignored)}: "
                "the user did not ask for those to change."
            )
        unaddressed = [t for t in sorted(targets) if t not in applied]
        if unaddressed:
            notes.append(
                "The user also commented on task(s) "
                f"{', '.join(str(t) for t in unaddressed)}, which you did not rewrite. "
                "They are unchanged and the user will see that when they review."
            )

        plan.pending_revision = None
        plan.set_status(PlanStatus.AWAITING_APPROVAL)
        plan.approval.reset_request()
        self.store.save(state)
        self.store.audit(
            "tasks_revised",
            plan_id=plan.plan_id,
            applied=applied,
            ignored=ignored or None,
            unaddressed=unaddressed or None,
        )
        return build(
            plan,
            recorded_step=step_number,
            total_steps=plan.total_steps,
            tasks=plan.tasks_brief(),
            notes=notes,
            qualify=len(state.active_plans()) > 1,
            revised_tasks=applied,
            message=(
                f"Rewrote task(s) {', '.join(str(t) for t in applied)} as the user asked. "
                "Every other task is unchanged. Execution stays locked until the user "
                "approves this version."
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

        # Two different questions share this tool: "may I run this plan?" before work,
        # and "is this actually finished?" after it. The plan's status decides which,
        # so the human always sees the right thing.
        completion_phase = plan.status is PlanStatus.AWAITING_COMPLETION
        plan.approval.requested_at = now_iso()
        plan.approval.decision = None
        plan.approval.decided_at = None
        if not completion_phase:
            plan.set_status(PlanStatus.AWAITING_APPROVAL)
        else:
            plan.touch()
        self.store.save(state)
        self.store.audit(
            "completion_verification_requested" if completion_phase else "approval_requested",
            plan_id=plan.plan_id,
            plan_summary=plan_summary,
        )
        display = (
            render_completion_report(plan, plan_summary)
            if completion_phase
            else render_plan_for_user(plan, plan_summary)
        )

        # The URL only reaches the human through stderr otherwise, which nobody reads in
        # a desktop app. Putting it in display_to_user means the model prints it in chat,
        # so a blocked popup or a second monitor no longer hides the approval page.
        approval_url = self.approval_ui.url if self.approval_ui is not None else None
        if approval_url:
            display = f"{display}\n\n현재 페이지: {approval_url.rstrip('/')}"

        if self.approval_ui is not None:
            fingerprint = self._fingerprint(plan)
            waited_plan_id = plan.plan_id
            phase = PHASE_COMPLETION if completion_phase else PHASE_PLAN
            decided = self._wait_for_human(
                plan, display, progress_token, notifier, notes, phase, plan_summary
            )
            if decided is not None:
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
                        decision=decided.decision,
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
                forwarded = {"user_comment": decided.comment} if decided.comment else {}
                if decided.decision == Decision.APPROVED.value:
                    return self._approve(state, plan, forwarded, notes)
                if decided.decision == Decision.REJECTED.value:
                    return self._reject(state, plan, forwarded, notes)
                if decided.decision == Decision.REVISE.value:
                    return self._revise(state, plan, forwarded, notes, verdict=decided)
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
        phase: str = PHASE_PLAN,
        summary: str = "",
    ) -> Verdict | None:
        """Block this tool call until a human decides. Returns None on timeout.

        This is what actually stops the agent: AnythingLLM's loop waits synchronously
        for the tool result, so while we do not return, the model cannot emit another
        tool call - no matter what the system prompt failed to make it do.
        """
        can_heartbeat = progress_token is not None and notifier is not None
        timeout = self.effective_timeout(can_heartbeat)

        request_id = self.approval_ui.open_request(
            plan.plan_id, plan.goal, display, plan.tasks_brief(),
            self._fingerprint(plan), phase, summary,
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
        decided: Verdict | None = None
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
            decision=decided.decision,
            comment=decided.comment,
            scope=decided.scope,
            task_comments=decided.task_comments or None,
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
        completion_phase = plan.status is PlanStatus.AWAITING_COMPLETION
        if plan.status is not PlanStatus.AWAITING_APPROVAL and not completion_phase:
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
        self.store.audit(
            "completion_verified" if completion_phase else "approved",
            plan_id=plan.plan_id,
            comment=args.get("user_comment"),
        )

        if completion_phase:
            return build(
                plan,
                message="The user confirmed the work is finished. The plan is now complete.",
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                tasks=plan.tasks_brief(),
                progress=plan.progress(),
            )

        task = plan.current_task()
        return build(
            plan,
            message=(
                # Everything carried through the redraft, so there is nothing left to
                # run - saying "unlocked" here would read as "go and do it all again".
                "The user approved this plan. Every task in it is already finished, so "
                "report completion now instead of executing anything."
                if plan.status is PlanStatus.AWAITING_COMPLETION
                else "Execution is now unlocked."
            ),
            notes=notes,
            qualify=len(state.active_plans()) > 1,
            tasks=plan.tasks_brief(),
            progress=plan.progress(),
            next_task={"task_id": task.task_id, "title": task.title} if task else None,
        )

    def _revise(
        self,
        state: State,
        plan: Plan,
        args: dict[str, Any],
        notes: list[str],
        verdict: Verdict | None = None,
    ) -> dict[str, Any]:
        """Record a request for changes.

        `verdict` is present only when the decision came from the approval page, which is
        the only place per-task comments can be written. When the model reports the
        decision itself (the two-phase path) there is no such detail, so the plan is
        redrafted whole - exactly as it always was.
        """
        comment = args.get("user_comment") or ""
        kind, detail = self._mutate_no(
            plan,
            comment,
            verdict.task_comments if verdict else None,
            verdict.scope if verdict else SCOPE_PLAN,
        )
        if kind == "REWORK":
            return self._reworked(state, plan, detail, comment, verdict, notes)
        targets = detail
        self.store.save(state)
        self.store.audit(
            "revision_requested",
            plan_id=plan.plan_id,
            revision_count=plan.approval.revision_count,
            user_comment=comment,
            scope=SCOPE_TASKS if targets else SCOPE_PLAN,
            task_comments=(verdict.task_comments or None) if verdict else None,
        )

        if targets:
            flagged = plan.revision_targets()
            return build(
                plan,
                message=(
                    "The user asked for changes to specific tasks. Rewrite ONLY those "
                    "tasks and send them back as task_updates. Every other task was "
                    "accepted and must stay exactly as it is."
                ),
                notes=notes,
                user_comment=comment or None,
                revision_count=plan.approval.revision_count,
                revision_scope=SCOPE_TASKS,
                revision_targets=[
                    {
                        "task_id": task_id,
                        "title": plan.get_task(task_id).title,
                        "user_comment": body,
                    }
                    for task_id, body in sorted(flagged.items())
                ],
                tasks_unchanged=[t.task_id for t in plan.tasks if t.task_id not in flagged],
                tasks=plan.tasks_brief(),
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
            revision_scope=SCOPE_PLAN,
            # Even a whole-plan rewrite benefits from knowing what the human said about
            # each task; it just does not constrain which tasks may change.
            task_comments=(verdict.task_comments or None) if verdict else None,
        )

    def _reworked(
        self,
        state: State,
        plan: Plan,
        reopened: list[int],
        comment: str,
        verdict: Verdict | None,
        notes: list[str],
    ) -> dict[str, Any]:
        """Answer a completion report that sent specific tasks back to be done again."""
        self.store.save(state)
        self.store.audit(
            "rework_requested",
            plan_id=plan.plan_id,
            revision_count=plan.approval.revision_count,
            user_comment=comment,
            reopened=reopened,
            task_comments=(verdict.task_comments or None) if verdict else None,
        )
        which = ", ".join(str(t) for t in reopened)
        kept = [t.task_id for t in plan.tasks if t.status == TaskStatus.DONE.value]
        task = plan.current_task()
        return build(
            plan,
            message=(
                f"The user checked the finished work and sent task(s) {which} back to be "
                "done AGAIN. The plan itself is unchanged and still approved - do not "
                "re-plan and do not ask for approval. Redo only those tasks, then report "
                "completion again."
            ),
            notes=notes,
            qualify=len(state.active_plans()) > 1,
            user_comment=comment or None,
            revision_count=plan.approval.revision_count,
            reopened_tasks=reopened,
            tasks_unchanged=kept or None,
            tasks=plan.tasks_brief(),
            progress=plan.progress(),
            next_task={"task_id": task.task_id, "title": task.title} if task else None,
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
        if task.status == TaskStatus.IN_PROGRESS.value:
            # Already running - usually because the server started it (auto-advance) and
            # the model sent IN_PROGRESS out of habit. Rewriting started_at here would
            # backdate nothing useful and lose the real start time.
            return build(
                plan,
                task_id=task.task_id,
                task_status=task.status,
                progress=plan.progress(),
                tasks=plan.tasks_brief(),
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                message=(
                    f"Task {task.task_id} was already IN_PROGRESS. Do the work now, then "
                    "report it DONE with a result_log."
                ),
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
        # Reporting DONE is a claim that work happened, and a weak model will happily
        # fire DONE at every task in a row without doing any. The server cannot observe
        # the work, but it can refuse claims that are structurally impossible or
        # evidence-free. Starting the wrong task is still forgiven (a redirect); saying
        # the wrong task is finished is not.
        if task.status == TaskStatus.DONE.value:
            nxt = plan.current_task()
            return build(
                plan,
                message=f"Task {task.task_id} is already DONE.",
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                tasks=plan.tasks_brief(),
                progress=plan.progress(),
                next_task={"task_id": nxt.task_id, "title": nxt.title} if nxt else None,
            )

        if task.status != TaskStatus.IN_PROGRESS.value:
            self.store.audit(
                "done_without_start", plan_id=plan.plan_id, task_id=task.task_id,
                previous_status=task.status,
            )
            return error(
                plan,
                ErrorCode.TASK_NOT_STARTED,
                f"Task {task.task_id} was never marked IN_PROGRESS.",
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                tasks=plan.tasks_brief(),
                progress=plan.progress(),
            )

        earlier = plan.unfinished_before(task.task_id)
        if earlier:
            self.store.audit(
                "done_out_of_order", plan_id=plan.plan_id, task_id=task.task_id,
                unfinished=[t.task_id for t in earlier],
            )
            return error(
                plan,
                ErrorCode.TASK_OUT_OF_ORDER,
                f"Tasks {[t.task_id for t in earlier]} are not finished yet.",
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                tasks=plan.tasks_brief(),
                progress=plan.progress(),
            )

        evidence = (args.get("result_log") or "").strip()
        reason = missing_evidence_reason(evidence, task.title, self.config.min_result_log)
        if reason is not None:
            self.store.audit(
                "done_without_evidence", plan_id=plan.plan_id, task_id=task.task_id,
                result_log=evidence, reason=reason,
            )
            return error(
                plan,
                ErrorCode.MISSING_RESULT_LOG,
                f"DONE refused: {reason}.",
                notes=notes,
                qualify=len(state.active_plans()) > 1,
                tasks=plan.tasks_brief(),
                progress=plan.progress(),
            )

        task.status = TaskStatus.DONE.value
        task.finished_at = now_iso()
        task.result_log = evidence

        if plan.all_done():
            if self.config.completion_approval:
                # COMPLETED is no longer something the model can award itself. The human
                # sees the per-task evidence first - the only check that catches invented
                # result_log text.
                plan.set_status(PlanStatus.AWAITING_COMPLETION)
                plan.approval.reset_request()
                self._withdraw_approval_request(plan)
                self.store.audit("completion_pending", plan_id=plan.plan_id)
            else:
                plan.set_status(PlanStatus.COMPLETED)
                self._withdraw_approval_request(plan)
            advanced = None
        else:
            plan.set_status(PlanStatus.IN_EXECUTION)
            advanced = self._auto_advance(plan)
        self.store.save(state)
        self.store.audit(
            "task_done",
            plan_id=plan.plan_id,
            task_id=task.task_id,
            result_log=task.result_log,
        )

        nxt = plan.current_task()
        if advanced is not None:
            message = (
                f"Task {task.task_id} is DONE. Task {advanced.task_id} "
                f"('{advanced.title}') has been started for you - do that work NOW, then "
                "report it DONE with its own result_log. Do not send IN_PROGRESS again."
                + self._rework_suffix(advanced)
            )
        elif nxt is not None:
            # Nothing was auto-started (auto_advance off, or the next task is not
            # startable yet) but work remains - never let this read as "finished".
            message = (
                f"Task {task.task_id} is DONE. The plan is NOT finished: next is "
                f"task_id={nxt.task_id} ('{nxt.title}'). Call update_task_progress with "
                f"task_id={nxt.task_id} and status='IN_PROGRESS'."
                + self._rework_suffix(nxt)
            )
        elif plan.status is PlanStatus.AWAITING_COMPLETION:
            # Every task is finished, so the only thing left is the completion report.
            # A silent message here left the model guessing at the last, most
            # error-prone step.
            message = (
                f"Task {task.task_id} is DONE and every task in this plan is finished. "
                "Report completion NOW: call request_user_approval with "
                "decision='ASK_USER' and a plan_summary that states, task by task, what "
                "you actually produced. Do not declare success yourself."
            )
        else:
            message = (
                f"Task {task.task_id} is DONE and every task in this plan is finished. "
                "Report completion NOW: write the final answer to the user, summarizing "
                "the result_log of each task."
            )
        return build(
            plan,
            task_id=task.task_id,
            task_status=task.status,
            progress=plan.progress(),
            tasks=plan.tasks_brief(),
            notes=notes,
            qualify=len(state.active_plans()) > 1,
            next_task={"task_id": nxt.task_id, "title": nxt.title} if nxt else None,
            message=message,
        )

    @staticmethod
    def _rework_suffix(task: Task | None) -> str:
        """Why this task is open again, restated wherever it is handed to the model.

        Two reworked tasks in one round means the second is reached by finishing the
        first, and the message announcing that is the only thing the model reads at
        that moment. Dropping the request there is how it gets redone as if nothing
        had been asked for.
        """
        if task is None or not task.revision_note:
            return ""
        return (
            f' The user sent task {task.task_id} back to be done again: '
            f'"{task.revision_note}". Answer that, do not repeat what you did before.'
        )

    def _auto_advance(self, plan: Plan) -> Task | None:
        """Start the next task as part of reporting the previous one DONE.

        The separate IN_PROGRESS call exists to force a turn boundary between "I am
        starting" and "I am finished" - but reporting DONE already *is* that boundary
        for the next task: the model receives "task N is in progress, do the work" and
        still cannot claim DONE until a later call, with its own evidence. So the
        boundary survives intact and one round trip per task disappears, which is what a
        small model's context and error rate actually pay for.

        Nothing here relaxes the DONE guard: _finish_task's checks are unchanged, and a
        task is only ever advanced into once every earlier task is finished.
        """
        if not self.config.auto_advance:
            return None
        nxt = plan.current_task()
        if nxt is None or nxt.status != TaskStatus.PENDING.value:
            return None
        if not can_start_task(plan, nxt.task_id):
            return None
        nxt.status = TaskStatus.IN_PROGRESS.value
        nxt.started_at = now_iso()
        self.store.audit("task_auto_started", plan_id=plan.plan_id, task_id=nxt.task_id)
        return nxt

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
            # Only when it actually moved: an unchanged goal repeated twice reads as two
            # different goals to a weak model.
            original_goal=plan.original_goal if plan.goal_history else None,
            goal_revisions=len(plan.goal_history) or None,
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
