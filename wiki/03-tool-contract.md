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
Optional: `task_list` (required when finalizing), `task_updates`, `revised_goal`, `revises_step`,
`plan_id`.

- **Routing by goal.** The model repeats the same `goal` on every step (the system prompt tells
  it to), so a matching active plan *is* this session's plan. A different goal starts its own
  new plan and never touches another conversation's. Optional `plan_id` overrides goal routing.
- **Goal-drift protection (1.8.2).** If the model is *continuing* (`step_number > 1`) but its
  goal matches no active plan — i.e. the goal drifted — the server does **not** fork a new plan.
  It returns `GOAL_NOT_MATCHED` with an `active_plans` directory (id + goal), telling the model
  to call again with the exact goal shown, or the `plan_id`, or `step_number=1` for a genuinely
  new plan. `step_number == 1` with a new goal always starts a new plan (so new conversations
  work). Goal matching ignores trailing punctuation/whitespace. See
  [05](05-concurrency-and-sessions.md#goal-drift).
- **Goal revision (1.12.0).** Drift protection must not become an inability to be corrected.
  When the *user* says the goal itself was wrong ("I meant Q4, not Q3"), the model sends
  `revised_goal` alongside the `goal` it has been using: the plan is found by the old text and
  its `goal` is **updated in place** — same `plan_id`, same tasks, no fork. The anchor survives
  as `original_goal` plus a `goal_history` of `{at, from, to, source}` hops (audit event
  `goal_revised`), which is what immutability was really protecting. Routing then accepts the
  new text; the *old* text keeps resolving to the plan for as long as it stays active, so a
  model one turn behind the correction is not sent back to re-select. A `revised_goal` equal to
  the current goal after normalization is not a correction and is not recorded. If the model
  puts the corrected text in both fields it matches nothing by name, so with exactly one active
  plan the server applies it there; with several it returns `GOAL_NOT_MATCHED` to pick from.
  Revising the goal of an `APPROVED`/`IN_EXECUTION` plan is accepted and recorded but does not
  silently widen the approval: the response says the human approved the *previous* goal and
  tells the model to re-plan and re-approve if the tasks no longer serve the corrected one.
- On finalize → `plan_status: AWAITING_APPROVAL`, `next_action: CALL_REQUEST_USER_APPROVAL`.
- **Targeted revision (1.9.x).** When the human commented on individual tasks on the approval
  page, the plan carries `pending_revision` and the model finalizes with `task_updates`
  (`[{"task_id": 3, "title": "..."}]`) instead of `task_list`. Only the flagged tasks are
  rewritten — everything else keeps its id, position, status and `result_log`. An edit to an
  unflagged task is dropped with a note; an unknown `task_id` is `TASK_NOT_FOUND` and **nothing
  is written** (validation completes before any mutation). `task_updates` with no pending request
  → `REVISION_NOT_REQUESTED`. See [06](06-human-in-the-loop.md#per-task-review-19x).
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
- **`APPROVED`/`REJECTED`/`REVISE`**: report what the human actually said. A `REVISE` the model
  reports itself is always a whole-plan revision; only the approval page can express a per-task
  one, because only there can the human point at a specific task.
- **What a per-task `REVISE` means depends on when it arrives (1.13.0).** Before execution it
  rewrites those tasks' wording (`pending_revision` + `task_updates`). On the **completion
  report** it means *do them again*: the named tasks reopen as `PENDING` carrying the human's
  sentence, every other task keeps its `DONE` and evidence, and the plan goes back to
  `IN_EXECUTION` **without re-approval** — the task list never changed. `_mutate_no` is the
  single place that reading is chosen. See [06](06-human-in-the-loop.md#rework-1130) and
  [D17](09-defects-and-lessons.md#d17).
- **Approval binds to the exact version shown** (goal + task-title fingerprint). Approving a
  plan version the human never saw → `APPROVAL_NOT_REQUESTED`. An approval left idle past
  `approval_ttl` → `APPROVAL_EXPIRED`. A decision sent while the request is still open on the
  page did not come from the user at all → `APPROVAL_PENDING` (see
  [06](06-human-in-the-loop.md)); the same code is returned when a chunked wait slice ends
  undecided, carrying `waited_seconds` and `remaining_seconds`.

## 3. `update_task_progress` — execution tracking + the enforced gate

Required: `task_id`, `status` ∈ {`PENDING`,`IN_PROGRESS`,`DONE`,`FAILED`}.
Optional: `result_log`, `plan_id`.

- **The enforcement half of the HITL gate.** Until `plan_status` is `APPROVED`/`IN_EXECUTION`,
  every call returns `ok:false` / `PLAN_NOT_APPROVED`. The model cannot execute early even if it
  ignores the instruction.
- `IN_PROGRESS` before the work, `DONE`/`FAILED` after. With `auto_advance` on (default) the
  server puts the next task into `IN_PROGRESS` as part of accepting a `DONE`, so only the first
  task needs an explicit start: a 5-task plan costs 1 + 5 calls instead of 5 + 5. The `DONE`
  guard is untouched — a task still has to be the one in progress, in order, with evidence —
  because the boundary that guard relies on is the `DONE` call itself, not the `IN_PROGRESS` one.
  Set `PLANNING_MCP_AUTO_ADVANCE=false` to require both calls; the tool description follows.
- Out-of-order starts are **redirected**, not rejected. Duplicate `DONE` is idempotent, and a
  redundant `IN_PROGRESS` on a running task is accepted without resetting `started_at`.
- `FAILED` → `plan_status: BLOCKED`; `next_action: CALL_PLAN_AND_THINK` (re-plan, do not
  continue). Attempting another task while `BLOCKED` → `PLAN_BLOCKED`.
- All tasks `DONE` → `COMPLETED`, `next_action: ANSWER_USER`.

## 4. `get_current_plan` — always-safe recovery

Required: `plan_id` (use the constant `"current"` for the active plan; a real id for a specific
one). Never mutates, never errors. Returns goal, recent thinking steps (superseded ones
summarized), tasks with their **full** `result_log`, progress, approval record,
`next_action_hint` naming the exact next call. If several plans are active and
`plan_id="current"`, returns an `active_plans` directory instead of guessing.

**Evidence is never truncated (1.13.1).** `Task.brief` used to cut `result_log` at 200
characters "to protect context". The approval page is built from that same dict — handlers pass
`tasks_brief()` straight to `open_request` — so a completion report asked a human to certify
work whose evidence ended in `...`, while the whole text sat in `plan_state.json`. If evidence
ever does threaten context, cap it where it is **written** (a maximum beside `min_result_log`),
never where it is read: a limit at the read point silently disagrees with what the store holds.

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
| `APPROVAL_PENDING` | the user has not answered yet — either this wait slice ended, or the model tried to decide on their behalf | `CALL_REQUEST_USER_APPROVAL` (with `ASK_USER`, immediately) |
| `PLAN_AMBIGUOUS` | several plans active, no `plan_id` given | `CALL_GET_CURRENT_PLAN` |
| `GOAL_NOT_MATCHED` | continuing (step>1) but goal matches no plan (drift) | `CALL_PLAN_AND_THINK` (with the exact goal from `active_plans`) |
| `TASK_NOT_FOUND` | bad `task_id` | `CALL_UPDATE_TASK_PROGRESS` (with valid ids listed) |
| `MISSING_RESULT_LOG` | `DONE` with evidence that is empty, a bare claim, or the task title | `CALL_UPDATE_TASK_PROGRESS` |
| `REWORK_NOT_DONE` | a reopened task reported `DONE` with the very outcome the user rejected | `CALL_UPDATE_TASK_PROGRESS` (quoting what they asked for) |
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
