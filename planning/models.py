"""Domain model: enums and dataclasses for the planning MCP server.

These enums are the single source of truth. `schemas.py` builds the advertised
tool schemas from them, and `state_machine.py` validates against them, so the
schema the LLM sees can never drift from what the server actually accepts.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def now_iso() -> str:
    """Local-time ISO8601 with offset, second precision (human-readable in the state file)."""
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


class PlanStatus(str, Enum):
    NONE = "NONE"
    DRAFTING = "DRAFTING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    IN_EXECUTION = "IN_EXECUTION"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    FAILED = "FAILED"


class Decision(str, Enum):
    ASK_USER = "ASK_USER"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVISE = "REVISE"


class NextAction(str, Enum):
    CALL_PLAN_AND_THINK = "CALL_PLAN_AND_THINK"
    CALL_REQUEST_USER_APPROVAL = "CALL_REQUEST_USER_APPROVAL"
    CALL_UPDATE_TASK_PROGRESS = "CALL_UPDATE_TASK_PROGRESS"
    CALL_GET_CURRENT_PLAN = "CALL_GET_CURRENT_PLAN"
    STOP_AND_WAIT_FOR_USER = "STOP_AND_WAIT_FOR_USER"
    ANSWER_USER = "ANSWER_USER"


class ErrorCode(str, Enum):
    PLAN_NOT_APPROVED = "PLAN_NOT_APPROVED"
    PLAN_NOT_READY = "PLAN_NOT_READY"
    PLAN_BLOCKED = "PLAN_BLOCKED"
    PLAN_CANCELLED = "PLAN_CANCELLED"
    NO_ACTIVE_PLAN = "NO_ACTIVE_PLAN"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    MISSING_TASK_LIST = "MISSING_TASK_LIST"
    MISSING_PLAN_SUMMARY = "MISSING_PLAN_SUMMARY"
    APPROVAL_NOT_REQUESTED = "APPROVAL_NOT_REQUESTED"
    INVALID_STATUS = "INVALID_STATUS"
    INVALID_DECISION = "INVALID_DECISION"
    INVALID_STEP = "INVALID_STEP"
    INTERNAL_ERROR = "INTERNAL_ERROR"


TERMINAL_PLAN_STATUSES = (PlanStatus.COMPLETED, PlanStatus.CANCELLED)
EXECUTABLE_PLAN_STATUSES = (PlanStatus.APPROVED, PlanStatus.IN_EXECUTION)


@dataclass
class Task:
    task_id: int
    title: str
    status: str = TaskStatus.PENDING.value
    result_log: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "status": self.status,
            "result_log": self.result_log,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Task":
        return cls(
            task_id=int(raw.get("task_id", 0)),
            title=str(raw.get("title", "")),
            status=str(raw.get("status", TaskStatus.PENDING.value)),
            result_log=raw.get("result_log"),
            started_at=raw.get("started_at"),
            finished_at=raw.get("finished_at"),
        )

    def brief(self, log_limit: int = 200) -> dict[str, Any]:
        """Compact form sent to the LLM. result_log is capped to protect context."""
        log = self.result_log
        if log and len(log) > log_limit:
            log = log[:log_limit] + "..."
        out: dict[str, Any] = {"task_id": self.task_id, "title": self.title, "status": self.status}
        if log:
            out["result_log"] = log
        return out


@dataclass
class ThinkingStep:
    step_number: int
    thought: str
    superseded: bool = False
    revises_step: int | None = None
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_number": self.step_number,
            "thought": self.thought,
            "superseded": self.superseded,
            "revises_step": self.revises_step,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ThinkingStep":
        return cls(
            step_number=int(raw.get("step_number", 1)),
            thought=str(raw.get("thought", "")),
            superseded=bool(raw.get("superseded", False)),
            revises_step=raw.get("revises_step"),
            created_at=raw.get("created_at") or now_iso(),
        )


@dataclass
class Approval:
    requested_at: str | None = None
    decided_at: str | None = None
    decision: str | None = None
    user_comment: str | None = None
    revision_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_at": self.requested_at,
            "decided_at": self.decided_at,
            "decision": self.decision,
            "user_comment": self.user_comment,
            "revision_count": self.revision_count,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Approval":
        raw = raw or {}
        return cls(
            requested_at=raw.get("requested_at"),
            decided_at=raw.get("decided_at"),
            decision=raw.get("decision"),
            user_comment=raw.get("user_comment"),
            revision_count=int(raw.get("revision_count", 0)),
        )

    def reset_request(self) -> None:
        """Called whenever the plan returns to DRAFTING: a changed plan needs a fresh approval."""
        self.requested_at = None
        self.decided_at = None
        self.decision = None


@dataclass
class Plan:
    plan_id: str
    goal: str
    plan_status: str = PlanStatus.DRAFTING.value
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    total_steps: int = 1
    thinking_steps: list[ThinkingStep] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    approval: Approval = field(default_factory=Approval)
    superseded_tasks: list[list[dict[str, Any]]] = field(default_factory=list)

    # ---- serialization -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "goal": self.goal,
            "plan_status": self.plan_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total_steps": self.total_steps,
            "thinking_steps": [s.to_dict() for s in self.thinking_steps],
            "tasks": [t.to_dict() for t in self.tasks],
            "approval": self.approval.to_dict(),
            "superseded_tasks": self.superseded_tasks,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Plan":
        return cls(
            plan_id=str(raw.get("plan_id", "")),
            goal=str(raw.get("goal", "")),
            plan_status=str(raw.get("plan_status", PlanStatus.DRAFTING.value)),
            created_at=raw.get("created_at") or now_iso(),
            updated_at=raw.get("updated_at") or now_iso(),
            total_steps=int(raw.get("total_steps", 1)),
            thinking_steps=[ThinkingStep.from_dict(s) for s in raw.get("thinking_steps", [])],
            tasks=[Task.from_dict(t) for t in raw.get("tasks", [])],
            approval=Approval.from_dict(raw.get("approval")),
            superseded_tasks=raw.get("superseded_tasks", []),
        )

    # ---- queries -------------------------------------------------------
    @property
    def status(self) -> PlanStatus:
        try:
            return PlanStatus(self.plan_status)
        except ValueError:
            return PlanStatus.DRAFTING

    def touch(self) -> None:
        self.updated_at = now_iso()

    def set_status(self, status: PlanStatus) -> None:
        self.plan_status = status.value
        self.touch()

    def get_task(self, task_id: int) -> Task | None:
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        return None

    def last_step_number(self) -> int:
        return max((s.step_number for s in self.thinking_steps), default=0)

    def current_task(self) -> Task | None:
        """The task the model should be working on: an in-flight one, else the first pending one."""
        for t in self.tasks:
            if t.status == TaskStatus.IN_PROGRESS.value:
                return t
        for t in self.tasks:
            if t.status == TaskStatus.PENDING.value:
                return t
        return None

    def first_failed_task(self) -> Task | None:
        for t in self.tasks:
            if t.status == TaskStatus.FAILED.value:
                return t
        return None

    def done_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == TaskStatus.DONE.value)

    def progress(self) -> str:
        return f"{self.done_count()}/{len(self.tasks)} done"

    def all_done(self) -> bool:
        return bool(self.tasks) and all(t.status == TaskStatus.DONE.value for t in self.tasks)

    def tasks_brief(self) -> list[dict[str, Any]]:
        return [t.brief() for t in self.tasks]
