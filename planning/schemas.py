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

IF THE USER COMMENTED ON PARTICULAR TASKS:
The server will tell you so and name them. Then send task_updates instead of task_list,
rewriting ONLY those tasks. Every other task was already accepted - leave it alone.

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
Handle exactly ONE task per call.

  1. Start the FIRST task: status = "IN_PROGRESS". Then do the work.
  2. Report it: status = "DONE" + a result_log saying what you actually produced.
  3. The server then starts the NEXT task for you and names it in next_task.
     Do that work, then report it "DONE" the same way.
     You do NOT send "IN_PROGRESS" again - just keep reporting DONE, one call per
     task, until the server tells you no tasks remain.
Use status = "FAILED" instead of "DONE" if the task did not work.

DONE IS ENFORCED. The server REFUSES a DONE for a task that:
  - is not the task currently in progress,
  - skips ahead while an earlier task is unfinished,
  - has no result_log describing the real outcome.
Never mark a task DONE before you actually did it. You must work through EVERY task in
order, one at a time. You are not finished until the server tells you so - keep going
while it says tasks remain.
If a task fails, set status = "FAILED" and explain in result_log - then follow the
next_action the server gives you back."""

# Used when PLANNING_MCP_AUTO_ADVANCE=false. The server then starts nothing on its own,
# so the description must ask for both calls or every DONE is refused.
UPDATE_TASK_PROGRESS_DESCRIPTION_MANUAL = """STEP 3 - EXECUTION TRACKING.
Call this tool TWICE for every task:
  1. BEFORE you start the task  -> status = "IN_PROGRESS"
  2. AFTER you finish the task  -> status = "DONE"  (or "FAILED" if it did not work)
Handle exactly ONE task per call. Never mark a task DONE before you actually did it.

DONE IS ENFORCED. The server REFUSES a DONE that:
  - was never marked IN_PROGRESS first,
  - skips ahead while an earlier task is unfinished,
  - has no result_log describing the real outcome.
You must therefore work through EVERY task in order, one at a time. You are not
finished until the server tells you so - keep going while it says tasks remain.
If a task fails, set status = "FAILED" and explain in result_log - then follow the
next_action the server gives you back."""

GET_CURRENT_PLAN_DESCRIPTION = """RECOVERY TOOL.
Call this when you are unsure what the plan is, which task you were on, or after a long
conversation. It returns the full current plan and tells you exactly what to do next.
It changes nothing - it is always safe to call."""


# Shared by the tools that act on an existing plan. Optional on purpose: with one plan
# in flight the server resolves it, so a model that never learns about plan_id behaves
# exactly as before. It is only needed when several sessions are planning at once, and
# then the server puts the value straight into next_action_hint.
_PLAN_ID_PARAM: dict[str, Any] = {
    "type": "string",
    "description": (
        "OPTIONAL. Leave this out unless the server asks for it. If several plans are "
        "active at the same time, set it to the plan_id this conversation has been "
        'receiving in every response. Example: "plan_20260724_0002"'
    ),
}

PLAN_AND_THINK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": (
                "One sentence restating what the user ultimately wants. Repeat the SAME goal "
                "text on every step. Example: 'Summarize the Q3 sales report and email it to "
                "the team lead.' If the user CORRECTS the goal, keep sending the old text here "
                "and put the corrected one in revised_goal."
            ),
        },
        "revised_goal": {
            "type": "string",
            "description": (
                "OPTIONAL. Use ONLY when the user says the goal itself was wrong or has "
                "changed - for example 'no, I meant the Q4 report, not Q3'. Put the corrected "
                "goal here and leave 'goal' as the text you have been sending, so the server "
                "can find the plan and record the change. Do NOT use it to reword or "
                "paraphrase the same goal. Example: 'Summarize the Q4 sales report and email "
                "it to the team lead.'"
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
        "task_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "The task the user commented on. Example: 3",
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "The rewritten task, answering the user's comment. Example: "
                            "'Copy the revenue table into the summary unchanged'"
                        ),
                    },
                },
                "required": ["task_id", "title"],
            },
            "description": (
                "ONLY use this when the server told you the user commented on specific tasks. "
                "Rewrite JUST those tasks; every task you do not list stays exactly as it is. "
                "Do NOT send task_list at the same time, and do NOT use this to add, delete or "
                "reorder tasks - that requires a full task_list. "
                'Example: [{"task_id": 3, "title": "Copy the revenue table in unchanged"}]'
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
        "plan_id": _PLAN_ID_PARAM,
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
        "plan_id": _PLAN_ID_PARAM,
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
                "REQUIRED when status is DONE - the server rejects DONE without it. Write one "
                "or two sentences stating the CONCRETE outcome of the work you just did: what "
                "you found, where you saved it, or what you produced. 'done' / 'ok' / 'completed' "
                "is not acceptable. Example: 'Found the file at /reports/q3_sales.xlsx.'  or  "
                "'FAILED: no file matching q3 was found in /reports.'"
            ),
        },
        "plan_id": _PLAN_ID_PARAM,
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


def build_tool_definitions(auto_advance: bool = True) -> list[dict[str, Any]]:
    """The advertised tool list for a given configuration.

    Only update_task_progress varies: whether the server starts the next task itself
    changes what the model is supposed to send, and a description that contradicts the
    running server is worse for a weak model than a slightly long one.
    """
    return [
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
            "description": UPDATE_TASK_PROGRESS_DESCRIPTION
            if auto_advance
            else UPDATE_TASK_PROGRESS_DESCRIPTION_MANUAL,
            "inputSchema": UPDATE_TASK_PROGRESS_SCHEMA,
        },
        {
            "name": "get_current_plan",
            "description": GET_CURRENT_PLAN_DESCRIPTION,
            "inputSchema": GET_CURRENT_PLAN_SCHEMA,
        },
    ]


TOOL_DEFINITIONS: list[dict[str, Any]] = build_tool_definitions()

TOOL_NAMES = tuple(t["name"] for t in TOOL_DEFINITIONS)

__all__ = [
    "TOOL_DEFINITIONS",
    "TOOL_NAMES",
    "build_tool_definitions",
]
