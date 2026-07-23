"""Persistence: one JSON file, atomic writes, an append-only audit log.

Failure philosophy: never crash. A server that fails to start gives AnythingLLM no
tools at all, and the model silently reverts to answering from memory - the exact
failure this project exists to prevent. A corrupt state file is therefore quarantined
and replaced with an empty one, not raised.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from .models import Plan, PlanStatus, TERMINAL_PLAN_STATUSES, now_iso

log = logging.getLogger("planning-mcp.store")

SCHEMA_VERSION = 1
STATE_FILENAME = "plan_state.json"
AUDIT_FILENAME = "audit.jsonl"
LOCK_FILENAME = ".lock"


class State:
    """In-memory view of the whole state file."""

    def __init__(self, active_plan_id: str | None = None, plans: dict[str, Plan] | None = None):
        self.active_plan_id = active_plan_id
        self.plans: dict[str, Plan] = plans or {}

    @property
    def active_plan(self) -> Plan | None:
        if not self.active_plan_id:
            return None
        return self.plans.get(self.active_plan_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "active_plan_id": self.active_plan_id,
            "plans": {pid: p.to_dict() for pid, p in self.plans.items()},
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "State":
        plans = {}
        for pid, praw in (raw.get("plans") or {}).items():
            try:
                plans[pid] = Plan.from_dict(praw)
            except Exception:  # one bad plan must not take down the whole file
                log.warning("Dropping unreadable plan %s", pid)
        return cls(active_plan_id=raw.get("active_plan_id"), plans=plans)


class Store:
    def __init__(self, state_dir: Path, max_plans: int = 20):
        self.state_dir = Path(state_dir)
        self.max_plans = max_plans
        self.lock = threading.Lock()
        self._ensure_dir()
        self._write_lock_file()

    # ---- paths ---------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.state_dir / STATE_FILENAME

    @property
    def audit_path(self) -> Path:
        return self.state_dir / AUDIT_FILENAME

    def _ensure_dir(self) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("Cannot create state dir %s: %s", self.state_dir, exc)

    def _write_lock_file(self) -> None:
        """Advisory only. A stale or conflicting lock warns but never blocks startup."""
        path = self.state_dir / LOCK_FILENAME
        try:
            if path.exists():
                existing = path.read_text(encoding="utf-8").strip()
                log.warning(
                    "Lock file already present (pid %s). Continuing anyway - "
                    "make sure only one AnythingLLM workspace uses this state dir.",
                    existing,
                )
            path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass  # a read-only state dir must not prevent serving tools

    # ---- load / save ---------------------------------------------------
    def load(self) -> State:
        path = self.state_path
        if not path.exists():
            return State()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            self._quarantine(path, exc)
            return State()
        if not isinstance(raw, dict):
            self._quarantine(path, ValueError("state file root is not an object"))
            return State()
        return State.from_dict(raw)

    def _quarantine(self, path: Path, exc: Exception) -> None:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        target = path.with_name(f"plan_state.corrupt.{stamp}.json")
        log.error("State file unreadable (%s). Quarantining to %s and starting empty.", exc, target)
        try:
            path.replace(target)
        except OSError:
            pass

    def save(self, state: State) -> None:
        self._prune(state)
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        tmp = self.state_path.with_suffix(".json.tmp")
        try:
            self._ensure_dir()
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)  # atomic on NTFS
        except OSError as exc:
            # Losing a write is bad, but dying is worse - report and keep serving.
            log.error("Failed to persist state: %s", exc)

    def _prune(self, state: State) -> None:
        """Keep the newest `max_plans`. The active plan is never pruned."""
        if len(state.plans) <= self.max_plans:
            return
        finished = [
            p
            for p in state.plans.values()
            if p.plan_id != state.active_plan_id
            and PlanStatus(p.plan_status) in TERMINAL_PLAN_STATUSES
        ]
        finished.sort(key=lambda p: p.updated_at)
        for plan in finished[: len(state.plans) - self.max_plans]:
            state.plans.pop(plan.plan_id, None)
            log.info("Pruned old plan %s", plan.plan_id)

    # ---- audit ---------------------------------------------------------
    def audit(self, event: str, **fields: Any) -> None:
        """Append-only evidence log. Written after the state file: a duplicate line is
        harmless, a lost state write is not."""
        record = {"ts": now_iso(), "event": event, **fields}
        try:
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("Could not append to audit log: %s", exc)

    # ---- ids -----------------------------------------------------------
    def next_plan_id(self, state: State) -> str:
        day = datetime.datetime.now().strftime("%Y%m%d")
        prefix = f"plan_{day}_"
        used = [pid for pid in state.plans if pid.startswith(prefix)]
        seq = len(used) + 1
        while f"{prefix}{seq:04d}" in state.plans:
            seq += 1
        return f"{prefix}{seq:04d}"
