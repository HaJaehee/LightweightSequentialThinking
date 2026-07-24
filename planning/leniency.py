"""Input leniency layer - invisible to the LLM, applied before validation.

Weak models produce near-miss arguments: "done" instead of "DONE", "3" instead of 3,
a newline-joined string instead of an array. Rejecting those wastes a turn and often
makes the model abandon the protocol. This module repairs what it can and records
what it repaired; it never raises and never rejects.
"""

from __future__ import annotations

import re
from typing import Any

from .models import Decision, TaskStatus

_ALLOWED_KEYS: dict[str, set[str]] = {
    "plan_and_think": {
        "goal",
        "thought",
        "step_number",
        "total_steps",
        "need_more_thinking",
        "task_list",
        "revises_step",
    },
    "request_user_approval": {"decision", "plan_summary", "user_comment", "plan_id"},
    "update_task_progress": {"task_id", "status", "result_log", "plan_id"},
    "get_current_plan": {"plan_id"},
}

_STATUS_ALIASES = {
    "done": TaskStatus.DONE,
    "complete": TaskStatus.DONE,
    "completed": TaskStatus.DONE,
    "finished": TaskStatus.DONE,
    "finish": TaskStatus.DONE,
    "success": TaskStatus.DONE,
    "완료": TaskStatus.DONE,
    "in progress": TaskStatus.IN_PROGRESS,
    "inprogress": TaskStatus.IN_PROGRESS,
    "in_progress": TaskStatus.IN_PROGRESS,
    "started": TaskStatus.IN_PROGRESS,
    "start": TaskStatus.IN_PROGRESS,
    "starting": TaskStatus.IN_PROGRESS,
    "doing": TaskStatus.IN_PROGRESS,
    "running": TaskStatus.IN_PROGRESS,
    "진행중": TaskStatus.IN_PROGRESS,
    "fail": TaskStatus.FAILED,
    "failed": TaskStatus.FAILED,
    "failure": TaskStatus.FAILED,
    "error": TaskStatus.FAILED,
    "실패": TaskStatus.FAILED,
    "pending": TaskStatus.PENDING,
    "todo": TaskStatus.PENDING,
    "not started": TaskStatus.PENDING,
    "대기": TaskStatus.PENDING,
}

_DECISION_ALIASES = {
    "ask": Decision.ASK_USER,
    "ask user": Decision.ASK_USER,
    "ask_user": Decision.ASK_USER,
    "request": Decision.ASK_USER,
    "확인": Decision.ASK_USER,
    "yes": Decision.APPROVED,
    "y": Decision.APPROVED,
    "ok": Decision.APPROVED,
    "okay": Decision.APPROVED,
    "approve": Decision.APPROVED,
    "approved": Decision.APPROVED,
    "accept": Decision.APPROVED,
    "proceed": Decision.APPROVED,
    "go": Decision.APPROVED,
    "승인": Decision.APPROVED,
    "네": Decision.APPROVED,
    "예": Decision.APPROVED,
    "진행": Decision.APPROVED,
    "좋아요": Decision.APPROVED,
    "no": Decision.REJECTED,
    "n": Decision.REJECTED,
    "cancel": Decision.REJECTED,
    "cancelled": Decision.REJECTED,
    "reject": Decision.REJECTED,
    "rejected": Decision.REJECTED,
    "stop": Decision.REJECTED,
    "deny": Decision.REJECTED,
    "취소": Decision.REJECTED,
    "아니오": Decision.REJECTED,
    "아니요": Decision.REJECTED,
    "하지마": Decision.REJECTED,
    "거부": Decision.REJECTED,
    "revise": Decision.REVISE,
    "revised": Decision.REVISE,
    "change": Decision.REVISE,
    "modify": Decision.REVISE,
    "edit": Decision.REVISE,
    "update": Decision.REVISE,
    "수정": Decision.REVISE,
    "변경": Decision.REVISE,
}

_TRUE_WORDS = {"true", "1", "yes", "y", "on", "continue", "네", "예"}
_FALSE_WORDS = {"false", "0", "no", "n", "off", "done", "아니오", "아니요"}

# Strips "1. ", "2) ", "- ", "* ", "• " but leaves ordinary prose untouched.
_NUMBERING = re.compile(r"^\s*(?:[-*•·]|\(?\d{1,2}[.)\]])\s+")
_TITLE_KEYS = ("title", "name", "task", "text", "description", "step", "action")


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE_WORDS:
            return True
        if v in _FALSE_WORDS:
            return False
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        m = re.search(r"-?\d+", value)
        if m:
            try:
                return int(m.group())
            except ValueError:
                return None
    return None


def _clean_title(raw: Any) -> str | None:
    if isinstance(raw, dict):
        for key in _TITLE_KEYS:
            if isinstance(raw.get(key), str) and raw[key].strip():
                raw = raw[key]
                break
        else:
            return None
    if not isinstance(raw, str):
        return None
    text = _NUMBERING.sub("", raw).strip()
    return text or None


def _coerce_task_list(value: Any) -> list[str] | None:
    items: list[Any]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if "\n" in text:
            items = text.split("\n")
        elif ";" in text:
            items = text.split(";")
        elif "," in text:
            items = text.split(",")
        else:
            items = [text]
    elif isinstance(value, list):
        items = value
    elif isinstance(value, dict):
        # {"1": "do a", "2": "do b"} - seen from models that mimic JSON objects
        items = [value[k] for k in sorted(value, key=lambda k: str(k))]
    else:
        return None
    cleaned = [t for t in (_clean_title(i) for i in items) if t]
    return cleaned


def _normalize_enum(value: Any, aliases: dict[str, Any]) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    if not key:
        return None
    direct = key.upper().replace(" ", "_").replace("-", "_")
    known = {m.value for m in TaskStatus} | {m.value for m in Decision}
    if direct in known:
        return direct
    hit = aliases.get(key) or aliases.get(key.replace("_", " "))
    return hit.value if hit else None


def normalize(tool_name: str, args: Any) -> tuple[dict[str, Any], list[str]]:
    """Repair a raw arguments dict. Returns (clean_args, notes)."""
    notes: list[str] = []
    if not isinstance(args, dict):
        return {}, ["Arguments were not a JSON object; treated as empty."]

    allowed = _ALLOWED_KEYS.get(tool_name, set())
    clean: dict[str, Any] = {}
    for key, value in args.items():
        if key in allowed:
            clean[key] = value
            continue
        # Common misspellings weak models produce for the id fields.
        lowered = key.lower().replace("-", "_")
        remap = {
            "taskid": "task_id",
            "task_number": "task_id",
            "id": "task_id",
            "stepnumber": "step_number",
            "step": "step_number",
            "totalsteps": "total_steps",
            "needmorethinking": "need_more_thinking",
            "tasklist": "task_list",
            "tasks": "task_list",
            "planid": "plan_id",
            "summary": "plan_summary",
            "comment": "user_comment",
            "log": "result_log",
            "result": "result_log",
        }.get(lowered.replace("_", ""), remap_direct(lowered, allowed))
        if remap and remap in allowed and remap not in clean:
            clean[remap] = value
            notes.append(f"Renamed unknown parameter '{key}' to '{remap}'.")
        else:
            notes.append(f"Ignored unknown parameter '{key}'.")

    for int_key in ("step_number", "total_steps", "task_id", "revises_step"):
        if int_key in clean:
            coerced = _coerce_int(clean[int_key])
            if coerced is None:
                clean.pop(int_key)
                notes.append(f"Could not read '{int_key}' as a number; ignored it.")
            elif coerced != clean[int_key]:
                notes.append(f"Read '{int_key}' as the number {coerced}.")
                clean[int_key] = coerced

    if "need_more_thinking" in clean:
        coerced_bool = _coerce_bool(clean["need_more_thinking"])
        if coerced_bool is None:
            clean.pop("need_more_thinking")
            notes.append("Could not read 'need_more_thinking' as true/false; ignored it.")
        elif coerced_bool is not clean["need_more_thinking"]:
            notes.append(f"Read 'need_more_thinking' as {str(coerced_bool).lower()}.")
            clean["need_more_thinking"] = coerced_bool

    if "task_list" in clean:
        coerced_list = _coerce_task_list(clean["task_list"])
        if coerced_list is None:
            clean.pop("task_list")
            notes.append("Could not read 'task_list' as a list of strings; ignored it.")
        else:
            if coerced_list != clean["task_list"]:
                notes.append(f"Normalized 'task_list' into {len(coerced_list)} plain strings.")
            clean["task_list"] = coerced_list

    if "status" in clean:
        normalized = _normalize_enum(clean["status"], _STATUS_ALIASES)
        if normalized and normalized in {s.value for s in TaskStatus}:
            if normalized != clean["status"]:
                notes.append(f"Read status '{clean['status']}' as '{normalized}'.")
            clean["status"] = normalized

    if "decision" in clean:
        normalized = _normalize_enum(clean["decision"], _DECISION_ALIASES)
        if normalized and normalized in {d.value for d in Decision}:
            if normalized != clean["decision"]:
                notes.append(f"Read decision '{clean['decision']}' as '{normalized}'.")
            clean["decision"] = normalized

    for str_key in ("goal", "thought", "plan_summary", "user_comment", "result_log", "plan_id"):
        if str_key in clean and clean[str_key] is not None and not isinstance(clean[str_key], str):
            clean[str_key] = str(clean[str_key])
            notes.append(f"Converted '{str_key}' to text.")

    return clean, notes


def remap_direct(lowered: str, allowed: set[str]) -> str | None:
    """Last-chance match: a key that already equals an allowed name apart from case."""
    return lowered if lowered in allowed else None
