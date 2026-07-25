# 03 · Tool Contract

Four tools. Machine-readable schemas: [data/tool-schemas.json](data/tool-schemas.json). Shared
enums: [data/enums.json](data/enums.json). The authoritative source is `planning/schemas.py`
(built from `planning/models.py`); this page is the human-readable summary.

Design rules behind the schemas (all because the model is weak):
`snake_case`, all-lowercase; **no nested input objects** (`task_list` is `array<string>`, the
server assigns ids); UPPERCASE enums; every tool has ≥1 required param; every response carries
`next_action` + `next_action_hint`.

---

## 1. `plan_and_think` — the mandatory entry point

One thinking step per call. `need_more_thinking=true` to continue; on the final step set it
`false` **and** provide `task_list`.

Required: `goal`, `thought`, `step_number`, `total_steps`, `need_more_thinking`.
Optional: `task_list` (required when finalizing), `revises_step`, `plan_id`.

- **Routing by goal.** The model repeats the same `goal` on every step (the system prompt tells
  it to), so a matching active plan *is* this session's plan. A different goal starts its own
  new plan and never touches another session's. See [05](05-concurrency-and-sessions.md).
- On finalize → `plan_status: AWAITING_APPROVAL`, `next_action: CALL_REQUEST_USER_APPROVAL`.
- Guard rails: missing/empty `task_list` on finalize → `MISSING_TASK_LIST`; `step_number`
  jumps/repeats are auto-normalized (never an error); `revises_step` marks the old step
  superseded and reverts to `DRAFTING`; oversized `task_list` truncated to `max_tasks`.

## 2. `request_user_approval` — the HITL gate

Required: `decision` ∈ {`ASK_USER`,`APPROVED`,`REJECTED`,`REVISE`}.
Optional: `plan_summary` (required for `ASK_USER`), `user_comment`, `plan_id`.

- **`ASK_USER`**: publishes the plan to the approval page and, in blocking mode, **holds the
  tool call open until a human decides** (see [06](06-human-in-the-loop.md)). Returns
  `next_action: STOP_AND_WAIT_FOR_USER` with a pre-rendered `display_to_user` and `approval_url`
  if the wait times out.
- **`APPROVED`/`REJECTED`/`REVISE`**: report what the human actually said.
- **Approval binds to the exact version shown** (goal + task-title fingerprint). Approving a
  plan version the human never saw → `APPROVAL_NOT_REQUESTED`. An approval left idle past
  `approval_ttl` → `APPROVAL_EXPIRED`.

## 3. `update_task_progress` — execution tracking + the enforced gate

Required: `task_id`, `status` ∈ {`PENDING`,`IN_PROGRESS`,`DONE`,`FAILED`}.
Optional: `result_log`, `plan_id`.

- **The enforcement half of the HITL gate.** Until `plan_status` is `APPROVED`/`IN_EXECUTION`,
  every call returns `ok:false` / `PLAN_NOT_APPROVED`. The model cannot execute early even if it
  ignores the instruction.
- Call twice per task: `IN_PROGRESS` before, `DONE`/`FAILED` after.
- Out-of-order starts are **redirected**, not rejected. Duplicate `DONE` is idempotent.
- `FAILED` → `plan_status: BLOCKED`; `next_action: CALL_PLAN_AND_THINK` (re-plan, do not
  continue). Attempting another task while `BLOCKED` → `PLAN_BLOCKED`.
- All tasks `DONE` → `COMPLETED`, `next_action: ANSWER_USER`.

## 4. `get_current_plan` — always-safe recovery

Required: `plan_id` (use the constant `"current"` for the active plan; a real id for a specific
one). Never mutates, never errors. Returns goal, recent thinking steps (superseded ones
summarized), tasks with capped `result_log`, progress, approval record, `next_action_hint`
naming the exact next call. If several plans are active and `plan_id="current"`, returns an
`active_plans` directory instead of guessing.

---

## Error codes → corrective next_action

Every error maps to a `next_action` that tells the model how to recover. Full list in
[data/enums.json](data/enums.json). The important ones:

| error_code | Meaning | Recovery next_action |
|---|---|---|
| `PLAN_NOT_APPROVED` | executing before approval | `CALL_REQUEST_USER_APPROVAL` |
| `PLAN_NOT_READY` | approving a plan with no task list yet | `CALL_PLAN_AND_THINK` |
| `PLAN_BLOCKED` | a task failed; must re-plan | `CALL_PLAN_AND_THINK` |
| `MISSING_TASK_LIST` | finalized without a task list | `CALL_PLAN_AND_THINK` |
| `APPROVAL_NOT_REQUESTED` | approving a version never shown | `CALL_REQUEST_USER_APPROVAL` |
| `APPROVAL_EXPIRED` | approval idle past TTL | `CALL_REQUEST_USER_APPROVAL` |
| `PLAN_AMBIGUOUS` | several plans active, no `plan_id` given | `CALL_GET_CURRENT_PLAN` |
| `TASK_NOT_FOUND` | bad `task_id` | `CALL_UPDATE_TASK_PROGRESS` (with valid ids listed) |
| `INTERNAL_ERROR` | something unexpected | `CALL_GET_CURRENT_PLAN` (resync) |

## Input leniency (invisible to the model)

Applied by `leniency.normalize()` before validation, so near-miss calls succeed:
case/alias normalization (`done|완료`→`DONE`, `네|yes`→`APPROVED`, …); `"true"/1/"3"` coercion;
`task_list` as a newline/comma string → array; array-of-objects → titles; numbering prefixes
stripped; unknown keys dropped; non-string keys dropped; NaN/inf rejected for integer fields.

> **Client caveat:** some MCP clients validate enums *before* the server sees them, so enum
> aliases like `done`/`진행중` may be rejected with `-32602` at the client. Non-enum leniency
> (task_list shapes, number coercion) works everywhere. Reinforce exact UPPERCASE enum values
> in the system prompt.
