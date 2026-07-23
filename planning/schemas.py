"""Tool schemas exactly as advertised to the LLM (Phase 1 blueprint).

Enum values are pulled from `models.py` so the advertised schema and the runtime
validator cannot drift apart. Descriptions are written for a weak model: every
parameter carries a concrete Example, and every tool description states the
protocol position ("STEP 1", "STEP 2", ...) explicitly.
"""

from __future__ import annotations

from typing import Any

from .models import Decision, TaskStatus

PLAN_AND_THINK_DESCRIPTION = """STEP 1 - MANDATORY FIRST TOOL.
You MUST call this tool before answering ANY user request, even simple-looking ones.
Use it to think one step at a time and to write down the task breakdown.

HOW TO USE:
- Call it once per thinking step. Start at step_number = 1.
- Keep calling with need_more_thinking = true until your plan is complete.
- On your FINAL thinking step, set need_more_thinking = false AND provide task_list.
- To correct an earlier step, set revises_step to that step number.

DO NOT execute anything, DO NOT answer the user while using this tool."""

REQUEST_USER_APPROVAL_DESCRIPTION = """STEP 2 - MANDATORY HUMAN APPROVAL GATE.
The plan is LOCKED until the user approves it. You cannot skip this tool.

USE IN TWO PHASES:
Phase A - ASK: call with decision = "ASK_USER" and a plan_summary.
          Then STOP. Print the plan to the user and wait. Say nothing else.
Phase B - REPORT: after the user replies in chat, call this tool AGAIN with
          decision = "APPROVED"  (user said yes / ok / proceed / 승인)
          decision = "REJECTED"  (user said no / cancel / stop / 취소)
          decision = "REVISE"    (user asked for changes) + put the requested
                                 change into user_comment.

NEVER guess the user's answer. NEVER call APPROVED unless the user actually said so."""

UPDATE_TASK_PROGRESS_DESCRIPTION = """STEP 3 - EXECUTION TRACKING.
Call this tool TWICE for every task:
  1. BEFORE you start the task  -> status = "IN_PROGRESS"
  2. AFTER you finish the task  -> status = "DONE"  (or "FAILED" if it did not work)
Handle exactly ONE task per call. Never mark a task DONE before you actually did it.
If a task fails, set status = "FAILED" and explain in result_log - then follow the
next_action the server gives you back."""

GET_CURRENT_PLAN_DESCRIPTION = """RECOVERY TOOL.
Call this when you are unsure what the plan is, which task you were on, or after a long
conversation. It returns the full current plan and tells you exactly what to do next.
It changes nothing - it is always safe to call."""


PLAN_AND_THINK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": (
                "One sentence restating what the user ultimately wants. Repeat the SAME goal "
                "text on every step. Example: 'Summarize the Q3 sales report and email it to "
                "the team lead.'"
            ),
        },
        "thought": {
            "type": "string",
            "description": (
                "Your reasoning for THIS step only. One idea per call. Example: 'Step 2: I must "
                "first locate the Q3 report file before I can summarize it.'"
            ),
        },
        "step_number": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Which thinking step this is. Starts at 1 and increases by exactly 1 each call. "
                "Example: 2"
            ),
        },
        "total_steps": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Your current estimate of how many thinking steps are needed. You may raise this "
                "number later. Example: 4"
            ),
        },
        "need_more_thinking": {
            "type": "boolean",
            "description": (
                "true = you will call plan_and_think again. false = this is your LAST thinking "
                "step and task_list is now final. Example: true"
            ),
        },
        "task_list": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "REQUIRED when need_more_thinking is false. A flat list of plain-text action "
                "items, in execution order. Plain strings only - do NOT send objects, do NOT add "
                "numbering, do NOT add status. The server assigns task_id automatically. "
                'Example: ["Locate the Q3 sales report file", "Extract the revenue table", '
                '"Write a 5-line summary", "Send the summary by email"]'
            ),
        },
        "revises_step": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "OPTIONAL. Only set this when you are CORRECTING an earlier thinking step. Set it "
                "to the step_number you are replacing. Example: 2"
            ),
        },
    },
    "required": ["goal", "thought", "step_number", "total_steps", "need_more_thinking"],
}

REQUEST_USER_APPROVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": [d.value for d in Decision],
            "description": (
                "ASK_USER = first call, ask the human. APPROVED / REJECTED / REVISE = report what "
                'the human actually replied. Example: "ASK_USER"'
            ),
        },
        "plan_summary": {
            "type": "string",
            "description": (
                "REQUIRED when decision is ASK_USER. A short human-readable summary of the plan "
                "you want approval for, written for a non-technical reader. Example: 'I will (1) "
                "find the Q3 report, (2) extract the revenue table, (3) write a 5-line summary, "
                "(4) email it to the team lead.'"
            ),
        },
        "user_comment": {
            "type": "string",
            "description": (
                "OPTIONAL. Copy the user's exact words here when decision is REVISE or REJECTED. "
                "Example: 'Do not send the email, just show me the summary.'"
            ),
        },
    },
    "required": ["decision"],
}

UPDATE_TASK_PROGRESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "The number of the task you are working on, taken from the tasks list the server "
                "gave you. One task per call. Example: 1"
            ),
        },
        "status": {
            "type": "string",
            "enum": [s.value for s in TaskStatus],
            "description": (
                "IN_PROGRESS = starting now. DONE = finished successfully. FAILED = could not "
                'finish. PENDING = reset back to not-started. Example: "IN_PROGRESS"'
            ),
        },
        "result_log": {
            "type": "string",
            "description": (
                "OPTIONAL but STRONGLY recommended. What you actually did or what actually went "
                "wrong, in one or two sentences. Example: 'Found the file at "
                "/reports/q3_sales.xlsx.'  or  'FAILED: no file matching q3 was found in "
                "/reports.'"
            ),
        },
    },
    "required": ["task_id", "status"],
}

GET_CURRENT_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "plan_id": {
            "type": "string",
            "default": "current",
            "description": (
                'Use the exact text "current" to get the active plan. Only use a real plan_id if '
                'you want an older plan. Example: "current"'
            ),
        },
    },
    # Required-with-a-constant on purpose: weak models reliably emit {"plan_id":"current"}
    # but frequently emit malformed/empty arguments for zero-parameter tools.
    "required": ["plan_id"],
}


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "plan_and_think",
        "description": PLAN_AND_THINK_DESCRIPTION,
        "inputSchema": PLAN_AND_THINK_SCHEMA,
    },
    {
        "name": "request_user_approval",
        "description": REQUEST_USER_APPROVAL_DESCRIPTION,
        "inputSchema": REQUEST_USER_APPROVAL_SCHEMA,
    },
    {
        "name": "update_task_progress",
        "description": UPDATE_TASK_PROGRESS_DESCRIPTION,
        "inputSchema": UPDATE_TASK_PROGRESS_SCHEMA,
    },
    {
        "name": "get_current_plan",
        "description": GET_CURRENT_PLAN_DESCRIPTION,
        "inputSchema": GET_CURRENT_PLAN_SCHEMA,
    },
]

TOOL_NAMES = tuple(t["name"] for t in TOOL_DEFINITIONS)
