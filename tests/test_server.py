"""Phase 4 Part D - server unit tests. No LLM required.

    python -m unittest discover -s tests -v

Run these before spending corporate-LLM turns on the behavioral matrix.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The server logs expected warnings (lock files, quarantined state, autoapprove) that would
# otherwise drown the test output. The tests assert on responses and the audit log instead.
logging.disable(logging.CRITICAL)

from planning.approval import ApprovalServer, PendingApproval  # noqa: E402
from planning.config import SDK_REQUEST_TIMEOUT_SEC, Config  # noqa: E402
from planning.handlers import PlanningHandlers  # noqa: E402
from planning.leniency import normalize  # noqa: E402
from planning.protocol import McpProtocol  # noqa: E402
from planning.schemas import TOOL_DEFINITIONS  # noqa: E402
from planning.store import Store  # noqa: E402

REQUIRED_FIELDS = ("ok", "plan_status", "next_action", "next_action_hint")


class HandlerTestCase(unittest.TestCase):
    """Base: a fresh handler over a throwaway state dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        # Blocking approval is the production default; the unit suite drives the
        # two-phase path, so it is disabled here and exercised in TestBlockingApproval.
        self.config = Config(state_dir=self.state_dir, blocking_approval=False)
        self.store = Store(self.state_dir, max_plans=self.config.max_plans)
        self.h = PlanningHandlers(self.store, self.config)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- helpers -------------------------------------------------------
    def think(self, **kwargs):
        args = {
            "goal": "Summarize the Q3 sales report.",
            "thought": "thinking",
            "step_number": 1,
            "total_steps": 2,
            "need_more_thinking": True,
        }
        args.update(kwargs)
        return self.h.dispatch("plan_and_think", args)

    def approve_flow(self, tasks=None):
        """Drive a plan all the way to APPROVED."""
        tasks = tasks or ["Find the file", "Read it", "Write the summary"]
        self.think(step_number=1, need_more_thinking=True)
        self.think(step_number=2, need_more_thinking=False, task_list=tasks)
        self.h.dispatch(
            "request_user_approval", {"decision": "ASK_USER", "plan_summary": "I will do X."}
        )
        return self.h.dispatch("request_user_approval", {"decision": "APPROVED"})

    def assertContract(self, res):  # noqa: N802
        for field in REQUIRED_FIELDS:
            self.assertIn(field, res, f"missing {field} in {res}")


# ===========================================================================
# D1 - leniency layer
# ===========================================================================


class TestLeniency(unittest.TestCase):
    def test_status_aliases(self):
        for raw in ("done", "Done", "completed", "finished", "완료"):
            clean, _ = normalize("update_task_progress", {"task_id": 1, "status": raw})
            self.assertEqual(clean["status"], "DONE", raw)
        for raw in ("in progress", "started", "doing", "running", "IN_PROGRESS"):
            clean, _ = normalize("update_task_progress", {"task_id": 1, "status": raw})
            self.assertEqual(clean["status"], "IN_PROGRESS", raw)
        for raw in ("fail", "failed", "error", "실패"):
            clean, _ = normalize("update_task_progress", {"task_id": 1, "status": raw})
            self.assertEqual(clean["status"], "FAILED", raw)

    def test_decision_aliases(self):
        for raw in ("yes", "y", "ok", "approve", "승인", "네", "진행"):
            clean, _ = normalize("request_user_approval", {"decision": raw})
            self.assertEqual(clean["decision"], "APPROVED", raw)
        for raw in ("no", "cancel", "reject", "취소", "아니오", "하지마"):
            clean, _ = normalize("request_user_approval", {"decision": raw})
            self.assertEqual(clean["decision"], "REJECTED", raw)
        for raw in ("revise", "change", "수정"):
            clean, _ = normalize("request_user_approval", {"decision": raw})
            self.assertEqual(clean["decision"], "REVISE", raw)

    def test_bool_coercion(self):
        for raw in ("false", "False", 0, "0", "no"):
            clean, _ = normalize("plan_and_think", {"need_more_thinking": raw})
            self.assertIs(clean["need_more_thinking"], False, raw)
        for raw in ("true", "True", 1, "yes"):
            clean, _ = normalize("plan_and_think", {"need_more_thinking": raw})
            self.assertIs(clean["need_more_thinking"], True, raw)

    def test_int_coercion(self):
        clean, _ = normalize("plan_and_think", {"step_number": "3", "total_steps": 4.0})
        self.assertEqual(clean["step_number"], 3)
        self.assertEqual(clean["total_steps"], 4)
        clean, _ = normalize("update_task_progress", {"task_id": "task 2"})
        self.assertEqual(clean["task_id"], 2)

    def test_task_list_from_string(self):
        clean, _ = normalize("plan_and_think", {"task_list": "a\nb\nc"})
        self.assertEqual(clean["task_list"], ["a", "b", "c"])
        clean, _ = normalize("plan_and_think", {"task_list": "a, b, c"})
        self.assertEqual(clean["task_list"], ["a", "b", "c"])

    def test_task_list_from_objects(self):
        clean, _ = normalize(
            "plan_and_think", {"task_list": [{"title": "a"}, {"task": "b"}, {"name": "c"}]}
        )
        self.assertEqual(clean["task_list"], ["a", "b", "c"])

    def test_task_list_numbering_stripped(self):
        clean, _ = normalize("plan_and_think", {"task_list": ["1. a", "2) b", "- c", "• d"]})
        self.assertEqual(clean["task_list"], ["a", "b", "c", "d"])

    def test_unknown_keys_dropped_and_remapped(self):
        clean, notes = normalize(
            "update_task_progress", {"taskId": 1, "status": "DONE", "bogus": 9}
        )
        self.assertEqual(clean["task_id"], 1)
        self.assertNotIn("bogus", clean)
        self.assertTrue(any("bogus" in n for n in notes))

    def test_non_dict_arguments(self):
        clean, notes = normalize("plan_and_think", "not a dict")
        self.assertEqual(clean, {})
        self.assertTrue(notes)


# ===========================================================================
# D2 - schema guard rails
# ===========================================================================


class TestGuardRails(HandlerTestCase):
    def test_missing_task_list_on_final_step(self):
        self.think(step_number=1)
        res = self.think(step_number=2, need_more_thinking=False)
        self.assertContract(res)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "MISSING_TASK_LIST")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")
        self.assertIn("task_list", res["next_action_hint"])

    def test_empty_task_list_on_final_step(self):
        res = self.think(need_more_thinking=False, task_list=[])
        self.assertEqual(res["error_code"], "MISSING_TASK_LIST")

    def test_step_number_jump_is_normalized(self):
        self.think(step_number=1)
        res = self.think(step_number=5)
        self.assertTrue(res["ok"])
        self.assertEqual(res["recorded_step"], 2)
        self.assertTrue(any("corrected" in n for n in res.get("input_notes", [])))

    def test_step_number_repeat_is_normalized(self):
        self.think(step_number=1)
        self.think(step_number=2)
        res = self.think(step_number=2)
        self.assertEqual(res["recorded_step"], 3)

    def test_revises_step_supersedes_and_reverts_to_drafting(self):
        self.think(step_number=1)
        self.think(step_number=2, need_more_thinking=False, task_list=["a", "b"])
        res = self.think(step_number=3, revises_step=2)
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "DRAFTING")
        current = self.h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertIn("superseded_steps", current)

    def test_revises_step_out_of_range(self):
        self.think(step_number=1)
        res = self.think(step_number=2, revises_step=99)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "INVALID_STEP")

    def test_oversized_task_list_is_truncated(self):
        res = self.think(
            need_more_thinking=False, task_list=[f"task {i}" for i in range(40)]
        )
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["tasks"]), self.config.max_tasks)
        self.assertTrue(any("kept the first" in n for n in res["input_notes"]))

    def test_task_not_found(self):
        self.approve_flow()
        res = self.h.dispatch("update_task_progress", {"task_id": 99, "status": "IN_PROGRESS"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "TASK_NOT_FOUND")
        self.assertIn("1", res["next_action_hint"])

    def test_get_current_plan_with_no_plan(self):
        res = self.h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "NONE")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")

    def test_missing_plan_summary(self):
        self.think(need_more_thinking=False, task_list=["a", "b"])
        res = self.h.dispatch("request_user_approval", {"decision": "ASK_USER"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "MISSING_PLAN_SUMMARY")

    def test_invalid_decision(self):
        self.think()
        res = self.h.dispatch("request_user_approval", {"decision": "maybe later"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "INVALID_DECISION")

    def test_missing_scalars_are_tolerated(self):
        res = self.h.dispatch("plan_and_think", {"thought": "just thinking"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["recorded_step"], 1)


# ===========================================================================
# D3 - state machine invariants
# ===========================================================================


class TestStateMachine(HandlerTestCase):
    def test_execution_blocked_while_drafting(self):
        self.think()
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "PLAN_NOT_APPROVED")
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")

    def test_execution_blocked_while_awaiting_approval(self):
        self.think(need_more_thinking=False, task_list=["a", "b"])
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "x"})
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "PLAN_NOT_APPROVED")

    def test_ask_user_returns_stop_and_display(self):
        self.think(need_more_thinking=False, task_list=["Find the file", "Read it"])
        res = self.h.dispatch(
            "request_user_approval", {"decision": "ASK_USER", "plan_summary": "I will do X."}
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")
        self.assertIn("display_to_user", res)
        self.assertIn("Find the file", res["display_to_user"])

    def test_full_happy_path(self):
        res = self.approve_flow(["a", "b"])
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(res["next_task"]["task_id"], 1)

        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(res["plan_status"], "IN_EXECUTION")
        res = self.h.dispatch(
            "update_task_progress", {"task_id": 1, "status": "DONE", "result_log": "did a"}
        )
        self.assertEqual(res["progress"], "1/2 done")
        self.assertEqual(res["next_task"]["task_id"], 2)

        self.h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        res = self.h.dispatch(
            "update_task_progress", {"task_id": 2, "status": "DONE", "result_log": "did b"}
        )
        self.assertEqual(res["plan_status"], "COMPLETED")
        self.assertEqual(res["next_action"], "ANSWER_USER")

    def test_rejection_cancels_plan(self):
        self.think(need_more_thinking=False, task_list=["a"])
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "x"})
        res = self.h.dispatch(
            "request_user_approval", {"decision": "REJECTED", "user_comment": "아니, 하지마"}
        )
        self.assertEqual(res["plan_status"], "CANCELLED")
        self.assertEqual(res["next_action"], "ANSWER_USER")
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(res["error_code"], "PLAN_CANCELLED")

    def test_revise_requires_second_approval(self):
        self.think(need_more_thinking=False, task_list=["a", "email it"])
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "x"})
        res = self.h.dispatch(
            "request_user_approval",
            {"decision": "REVISE", "user_comment": "이메일은 보내지 마"},
        )
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertEqual(res["revision_count"], 1)
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")

        # Execution must still be locked after the revision.
        blocked = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(blocked["error_code"], "PLAN_NOT_APPROVED")

        # Re-planning lands back at the gate, not at execution.
        res = self.think(need_more_thinking=False, task_list=["a"])
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")

    def test_approve_while_drafting_is_refused(self):
        self.think()
        res = self.h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "PLAN_NOT_READY")

    def test_failed_task_blocks_forward_progress(self):
        self.approve_flow(["a", "b", "c"])
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "DONE"})
        self.h.dispatch("update_task_progress", {"task_id": 2, "status": "IN_PROGRESS"})
        res = self.h.dispatch(
            "update_task_progress",
            {"task_id": 2, "status": "FAILED", "result_log": "file not found"},
        )
        self.assertEqual(res["plan_status"], "BLOCKED")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")
        self.assertEqual(res["failed_task"]["task_id"], 2)

        res = self.h.dispatch("update_task_progress", {"task_id": 3, "status": "IN_PROGRESS"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "PLAN_BLOCKED")

    def test_replan_after_block_requires_new_approval(self):
        self.approve_flow(["a", "b"])
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "FAILED"})
        res = self.think(need_more_thinking=False, task_list=["a2", "b2"])
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(res["error_code"], "PLAN_NOT_APPROVED")

    def test_out_of_order_start_is_redirected_not_rejected(self):
        self.approve_flow(["a", "b", "c"])
        res = self.h.dispatch("update_task_progress", {"task_id": 3, "status": "IN_PROGRESS"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["next_task"]["task_id"], 1)

    def test_done_is_idempotent(self):
        self.approve_flow(["a", "b"])
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "DONE"})
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "DONE"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["progress"], "1/2 done")

    def test_done_without_in_progress_is_accepted_and_audited(self):
        self.approve_flow(["a", "b"])
        res = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "DONE"})
        self.assertTrue(res["ok"])
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("skipped_in_progress", audit)

    def test_approved_without_ask_is_refused(self):
        """APPROVED must be preceded by ASK_USER on the SAME plan version."""
        self.think(need_more_thinking=False, task_list=["a", "b"])
        res = self.h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "APPROVAL_NOT_REQUESTED")
        self.assertEqual(res["next_action"], "CALL_REQUEST_USER_APPROVAL")
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("stale_approval_refused", audit)

    def test_stale_approval_after_replacement_is_refused(self):
        """Session 1 asks about plan A; session 2 replaces it; session 1's belated
        approval must NOT unlock plan B - the human never saw B."""
        self.think(goal="A: 보고서 요약", need_more_thinking=False, task_list=["찾기", "요약"])
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        self.think(goal="B: 전 직원 메일 발송", step_number=1,
                   need_more_thinking=False, task_list=["초안", "발송"])
        res = self.h.dispatch(
            "request_user_approval", {"decision": "APPROVED", "user_comment": "승인"}
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "APPROVAL_NOT_REQUESTED")
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")  # still locked
        blocked = self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(blocked["error_code"], "PLAN_NOT_APPROVED")
        # Re-asking (showing the CURRENT plan) and then approving works.
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s2"})
        res = self.h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertEqual(res["plan_status"], "APPROVED")

    def test_revise_then_approve_without_reask_is_refused(self):
        """The B2 shortcut (revise -> approve without re-showing) is now server-blocked."""
        self.think(need_more_thinking=False, task_list=["a", "email"])
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
        self.h.dispatch("request_user_approval", {"decision": "REVISE", "user_comment": "no email"})
        self.think(need_more_thinking=False, task_list=["a"])
        res = self.h.dispatch("request_user_approval", {"decision": "APPROVED"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "APPROVAL_NOT_REQUESTED")

    def test_draft_replacement_by_different_goal_is_flagged(self):
        """A second 'session' overwriting an unfinished draft must leave evidence."""
        self.think(goal="A세션: 회의실 예약", need_more_thinking=False, task_list=["a1", "a2"])
        res = self.think(goal="B세션: 보고서 요약", need_more_thinking=False, task_list=["b1"])
        self.assertTrue(res["ok"])
        self.assertTrue(
            any("replaced" in n for n in res.get("input_notes", [])),
            f"no replacement warning in {res.get('input_notes')}",
        )
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("goal_replaced", audit)
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        plan = next(iter(raw["plans"].values()))
        self.assertEqual(
            [t["title"] for t in plan["superseded_tasks"][0]], ["a1", "a2"]
        )  # A's tasks archived, not destroyed

    def test_approved_plan_cannot_be_replaced_by_new_goal(self):
        """Once approved, a different-goal plan_and_think must NOT hijack the plan."""
        self.approve_flow(["a", "b"])
        res = self.think(goal="다른 세션의 새 목표", step_number=1,
                         need_more_thinking=False, task_list=["hijack"])
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(res["tasks"][0]["title"], "a")
        current = self.h.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(current["goal"], "Summarize the Q3 sales report.")

    def test_separate_state_dirs_are_fully_isolated(self):
        other_dir = self.state_dir / "other"
        other = PlanningHandlers(
            Store(other_dir), Config(state_dir=other_dir, blocking_approval=False)
        )
        self.think(goal="세션1 목표", need_more_thinking=False, task_list=["x"])
        res = other.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["plan_status"], "NONE")

    def test_plan_and_think_during_execution_redirects(self):
        self.approve_flow(["a", "b"])
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = self.think(step_number=1, need_more_thinking=False, task_list=["totally new"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "IN_EXECUTION")
        self.assertEqual(res["next_action"], "CALL_UPDATE_TASK_PROGRESS")
        self.assertEqual(res["tasks"][0]["title"], "a")  # original plan untouched

    def test_every_response_has_the_contract_fields(self):
        calls = [
            ("plan_and_think", {"goal": "g", "thought": "t", "step_number": 1,
                                "total_steps": 1, "need_more_thinking": False,
                                "task_list": ["a"]}),
            ("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"}),
            ("request_user_approval", {"decision": "APPROVED"}),
            ("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"}),
            ("update_task_progress", {"task_id": 1, "status": "DONE"}),
            ("get_current_plan", {"plan_id": "current"}),
            ("update_task_progress", {"task_id": 42, "status": "DONE"}),
            ("request_user_approval", {"decision": "nonsense"}),
            ("bogus_tool", {}),
        ]
        for name, args in calls:
            self.assertContract(self.h.dispatch(name, args))

    def test_unknown_tool_does_not_raise(self):
        res = self.h.dispatch("definitely_not_a_tool", {"x": 1})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "INTERNAL_ERROR")


# ===========================================================================
# D4 - persistence & robustness
# ===========================================================================


class TestPersistence(HandlerTestCase):
    def test_state_survives_a_new_handler_instance(self):
        self.approve_flow(["a", "b"])
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "DONE"})

        fresh = PlanningHandlers(Store(self.state_dir), self.config)
        res = fresh.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertEqual(res["progress"], "1/2 done")
        self.assertEqual(res["next_task"] if "next_task" in res else res["tasks"][1]["task_id"], 2)

    def test_corrupt_state_file_is_quarantined(self):
        self.think()
        (self.state_dir / "plan_state.json").write_text("{{{garbage", encoding="utf-8")
        fresh = PlanningHandlers(Store(self.state_dir), self.config)
        res = fresh.dispatch("get_current_plan", {"plan_id": "current"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "NONE")
        self.assertTrue(list(self.state_dir.glob("plan_state.corrupt.*.json")))

    def test_korean_text_round_trips(self):
        self.think(goal="한글 목표", thought="한글 생각", need_more_thinking=False,
                   task_list=["첫 번째 작업", "두 번째 작업"])
        self.h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "요약"})
        self.h.dispatch(
            "request_user_approval", {"decision": "REVISE", "user_comment": "이메일은 빼줘"}
        )
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        plan = next(iter(raw["plans"].values()))
        self.assertEqual(plan["approval"]["user_comment"], "이메일은 빼줘")
        self.assertEqual(plan["tasks"][0]["title"], "첫 번째 작업")

    def test_result_log_is_capped_in_responses(self):
        self.approve_flow(["a"])
        self.h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        res = self.h.dispatch(
            "update_task_progress", {"task_id": 1, "status": "DONE", "result_log": "x" * 500}
        )
        self.assertLessEqual(len(res["tasks"][0]["result_log"]), 205)

    def test_plan_pruning_keeps_the_active_plan(self):
        config = Config(state_dir=self.state_dir, max_plans=3, blocking_approval=False)
        h = PlanningHandlers(Store(self.state_dir, max_plans=3), config)
        for i in range(6):
            h.dispatch(
                "plan_and_think",
                {"goal": f"goal {i}", "thought": "t", "step_number": 1, "total_steps": 1,
                 "need_more_thinking": False, "task_list": ["a"]},
            )
            h.dispatch("request_user_approval", {"decision": "ASK_USER", "plan_summary": "s"})
            h.dispatch("request_user_approval", {"decision": "REJECTED"})
        raw = json.loads((self.state_dir / "plan_state.json").read_text(encoding="utf-8"))
        self.assertLessEqual(len(raw["plans"]), 3)
        self.assertIn(raw["active_plan_id"], raw["plans"])

    def test_autoapprove_bypasses_the_gate(self):
        config = Config(state_dir=self.state_dir, autoapprove=True, blocking_approval=False)
        h = PlanningHandlers(Store(self.state_dir), config)
        h.dispatch(
            "plan_and_think",
            {"goal": "g", "thought": "t", "step_number": 1, "total_steps": 1,
             "need_more_thinking": False, "task_list": ["a"]},
        )
        res = h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertTrue(res["ok"])


# ===========================================================================
# Blocking approval (the real HITL pause)
# ===========================================================================


class FakeApprovalUI:
    """Stands in for the localhost page. Resolves immediately, or never."""

    url = "http://127.0.0.1:0/"

    def __init__(self, decision=None, comment="", available=True):
        self.decision = decision
        self.comment = comment
        self.available = available
        self.opened: list[PendingApproval] = []
        self.closed = 0

    def open_request(self, plan_id, goal, display, tasks):
        if not self.available:
            return None
        pending = PendingApproval(plan_id, goal, display, tasks)
        self.opened.append(pending)
        if self.decision:
            pending.resolve(self.decision, self.comment)
        return pending

    def close_request(self, pending):
        self.closed += 1


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def progress(self, token, progress, message=None):
        self.sent.append((token, progress, message))


class TestBlockingApproval(HandlerTestCase):
    def blocking(self, ui, timeout=1):
        cfg = Config(
            state_dir=self.state_dir, blocking_approval=True, approval_timeout=timeout
        )
        return PlanningHandlers(Store(self.state_dir), cfg, approval_ui=ui)

    def draft(self, h):
        h.dispatch("plan_and_think", {
            "goal": "블로킹 승인 검증", "thought": "t", "step_number": 1, "total_steps": 1,
            "need_more_thinking": False, "task_list": ["작업 1", "작업 2"]})

    def ask(self, h, **kw):
        return h.dispatch(
            "request_user_approval",
            {"decision": "ASK_USER", "plan_summary": "요약"},
            **kw,
        )

    def test_human_approves_unlocks_in_one_call(self):
        """ASK_USER blocks, the human clicks approve, and the SAME call returns APPROVED."""
        ui = FakeApprovalUI("APPROVED")
        h = self.blocking(ui)
        self.draft(h)
        res = self.ask(h)
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "APPROVED")
        self.assertEqual(res["next_action"], "CALL_UPDATE_TASK_PROGRESS")
        self.assertEqual(res["next_task"]["task_id"], 1)
        self.assertEqual(len(ui.opened), 1)
        self.assertEqual(ui.closed, 1)

    def test_human_rejects(self):
        h = self.blocking(FakeApprovalUI("REJECTED", "하지마"))
        self.draft(h)
        res = self.ask(h)
        self.assertEqual(res["plan_status"], "CANCELLED")
        blocked = h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(blocked["error_code"], "PLAN_CANCELLED")

    def test_human_requests_revision(self):
        h = self.blocking(FakeApprovalUI("REVISE", "2번 빼줘"))
        self.draft(h)
        res = self.ask(h)
        self.assertEqual(res["plan_status"], "DRAFTING")
        self.assertEqual(res["user_comment"], "2번 빼줘")
        self.assertEqual(res["next_action"], "CALL_PLAN_AND_THINK")

    def test_timeout_leaves_plan_locked(self):
        h = self.blocking(FakeApprovalUI(decision=None), timeout=1)
        self.draft(h)
        res = self.ask(h)
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")
        self.assertIn("display_to_user", res)
        blocked = h.dispatch("update_task_progress", {"task_id": 1, "status": "IN_PROGRESS"})
        self.assertEqual(blocked["error_code"], "PLAN_NOT_APPROVED")
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("approval_wait_timeout", audit)

    def test_ui_unavailable_degrades_loudly(self):
        """If the UI cannot start we must degrade - but never silently."""
        h = self.blocking(FakeApprovalUI(available=False))
        self.draft(h)
        res = self.ask(h)
        self.assertTrue(res["ok"])
        self.assertEqual(res["next_action"], "STOP_AND_WAIT_FOR_USER")
        self.assertTrue(
            any("NOT hard-paused" in n for n in res.get("input_notes", [])),
            f"degradation must be visible, got {res.get('input_notes')}",
        )
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("approval_ui_unavailable", audit)

    def test_port_fallback_when_base_port_is_taken(self):
        """A stale instance holding 8765 must not disable the gate for a new one."""
        first = ApprovalServer(port=8795, open_browser=False)
        second = ApprovalServer(port=8795, open_browser=False)
        try:
            self.assertIsNotNone(first.open_request("p1", "g", "d", []))
            self.assertEqual(first.port, 8795)
            self.assertIsNotNone(second.open_request("p2", "g", "d", []))
            self.assertNotEqual(second.port, first.port)
            self.assertEqual(second.port, 8796)
        finally:
            first.shutdown()
            second.shutdown()

    def test_all_ports_busy_reports_failure(self):
        blockers = [ApprovalServer(port=8797 + i, open_browser=False) for i in range(2)]
        for b in blockers:
            b.open_request("x", "g", "d", [])
        crowded = ApprovalServer(port=8797, open_browser=False, port_attempts=2)
        try:
            self.assertIsNone(crowded.open_request("p", "g", "d", []))
        finally:
            crowded.shutdown()
            for b in blockers:
                b.shutdown()

    def test_heartbeat_only_with_progress_token(self):
        notifier = RecordingNotifier()
        h = self.blocking(FakeApprovalUI(decision=None), timeout=1)
        self.draft(h)
        self.ask(h, progress_token=None, notifier=notifier)
        self.assertEqual(notifier.sent, [])  # no token -> must not send progress

    def test_wait_is_capped_without_progress_token(self):
        """No token means the wait must stay under the client's 60s request timeout."""
        h = self.blocking(FakeApprovalUI(decision=None), timeout=3600)
        self.assertLessEqual(
            h.effective_timeout(can_heartbeat=False),
            SDK_REQUEST_TIMEOUT_SEC - 1,
            "wait must be capped below the SDK request timeout",
        )
        self.assertEqual(h.effective_timeout(can_heartbeat=True), 3600)

    def test_short_configured_timeout_is_respected_either_way(self):
        h = self.blocking(FakeApprovalUI(decision=None), timeout=5)
        self.assertEqual(h.effective_timeout(can_heartbeat=False), 5)
        self.assertEqual(h.effective_timeout(can_heartbeat=True), 5)

    def test_approval_url_reaches_the_user_in_chat(self):
        """A blocked popup must not hide the page: the URL rides in display_to_user."""
        ui = FakeApprovalUI(decision=None)
        ui.url = "http://127.0.0.1:8899/"
        h = self.blocking(ui, timeout=1)
        self.draft(h)
        res = self.ask(h)
        self.assertEqual(res["approval_url"], "http://127.0.0.1:8899/")
        self.assertIn("http://127.0.0.1:8899/", res["display_to_user"])

    def test_out_of_band_decision_is_audited(self):
        h = self.blocking(FakeApprovalUI("APPROVED", "승인"))
        self.draft(h)
        self.ask(h)
        audit = (self.state_dir / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("approval_decided_out_of_band", audit)

    def test_approval_server_binds_and_serves(self):
        """The real server (not the fake) must start and answer /api/pending."""
        import urllib.request

        srv = ApprovalServer(port=8791, open_browser=False)
        try:
            pending = srv.open_request("plan_x", "목표", "PLAN...", [{"task_id": 1}])
            self.assertIsNotNone(pending)
            with urllib.request.urlopen(srv.url + "api/pending", timeout=5) as r:
                payload = json.loads(r.read().decode("utf-8"))
            self.assertEqual(payload["plan_id"], "plan_x")
            body = json.dumps(
                {"id": payload["id"], "decision": "APPROVED", "comment": "ok"}
            ).encode("utf-8")
            req = urllib.request.Request(
                srv.url + "api/decide", data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertTrue(json.loads(r.read().decode("utf-8"))["ok"])
            self.assertTrue(pending.wait(2))
            self.assertEqual(pending.decision, "APPROVED")
        finally:
            srv.shutdown()


# ===========================================================================
# Protocol layer
# ===========================================================================


class TestProtocol(HandlerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.p = McpProtocol(self.h, TOOL_DEFINITIONS, "planning-mcp", "1.0.0")

    def test_initialize(self):
        res = self.p.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertIn("serverInfo", res["result"])
        self.assertIn("tools", res["result"]["capabilities"])

    def test_tools_list_exposes_exactly_four_tools(self):
        res = self.p.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = res["result"]["tools"]
        self.assertEqual(
            sorted(t["name"] for t in tools),
            ["get_current_plan", "plan_and_think", "request_user_approval", "update_task_progress"],
        )
        for tool in tools:
            self.assertTrue(tool["description"].strip())
            self.assertTrue(tool["inputSchema"]["required"])  # every tool needs >=1 required param

    def test_tools_call_returns_text_content(self):
        res = self.p.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_current_plan",
                    "arguments": {"plan_id": "current"},
                },
            }
        )
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(payload["next_action"], "CALL_PLAN_AND_THINK")
        self.assertFalse(res["result"]["isError"])

    def test_string_arguments_are_parsed(self):
        res = self.p.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "get_current_plan", "arguments": '{"plan_id": "current"}'},
            }
        )
        self.assertIn("content", res["result"])

    def test_failed_tool_call_is_not_a_protocol_error(self):
        res = self.p.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "update_task_progress",
                           "arguments": {"task_id": 1, "status": "DONE"}},
            }
        )
        self.assertNotIn("error", res)
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertFalse(payload["ok"])

    def test_notification_gets_no_response(self):
        self.assertIsNone(
            self.p.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_unknown_method(self):
        res = self.p.handle_message({"jsonrpc": "2.0", "id": 6, "method": "nope/nope"})
        self.assertEqual(res["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main(verbosity=2)
