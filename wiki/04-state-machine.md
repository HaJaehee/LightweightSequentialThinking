# 04 · State Machine

Authoritative logic: `planning/state_machine.py` + the transition checks in
`planning/handlers.py`. Machine-readable form: [data/state-machine.xml](data/state-machine.xml).

## Plan statuses

`NONE → DRAFTING → AWAITING_APPROVAL → APPROVED → IN_EXECUTION → COMPLETED`, with `BLOCKED`
(task failed) and `CANCELLED` (rejected) as branches. `COMPLETED` and `CANCELLED` are terminal.

```
                    plan_and_think (need_more_thinking=true)
                              ┌───────┐
                              ▼       │
   NONE ──plan_and_think──► DRAFTING ─┘
                              │ plan_and_think(final + task_list)
                              ▼
                     AWAITING_APPROVAL ──approval(REJECTED)──► CANCELLED
                       │  ▲                                       │
          approval     │  │ approval(REVISE) → DRAFTING           │ (new goal → new plan)
          (APPROVED)   ▼  │
                     APPROVED
                       │ update_task_progress(IN_PROGRESS) on first task
                       ▼
                  IN_EXECUTION ──update_task_progress(FAILED)──► BLOCKED
                       │                                           │ plan_and_think
                       │ all tasks DONE                            ▼
                       ▼                                        DRAFTING
                   COMPLETED
```

## Transition table (server-enforced)

| From | Call | To | If illegal → |
|---|---|---|---|
| `NONE`/`COMPLETED`/`CANCELLED` | `plan_and_think` step 1 | `DRAFTING` (new plan_id) | — |
| `DRAFTING` | `plan_and_think` (more) | `DRAFTING` | — |
| `DRAFTING` | `plan_and_think` (final+task_list) | `AWAITING_APPROVAL` | no task_list → `MISSING_TASK_LIST` |
| `AWAITING_APPROVAL` | `approval` ASK_USER | `AWAITING_APPROVAL` (+ blocking wait) | — |
| `AWAITING_APPROVAL` | `approval` APPROVED | `APPROVED` | version not shown → `APPROVAL_NOT_REQUESTED` |
| `AWAITING_APPROVAL` | `approval` REVISE | `DRAFTING` | — |
| `AWAITING_APPROVAL` | `approval` REJECTED | `CANCELLED` | — |
| `AWAITING_APPROVAL`/`DRAFTING` | `update_task_progress` | *(no change)* | `PLAN_NOT_APPROVED` ← **critical guard** |
| `APPROVED` | `update_task_progress` IN_PROGRESS | `IN_EXECUTION` | — |
| `IN_EXECUTION` | `update_task_progress` DONE (last) | `COMPLETED` | — |
| `IN_EXECUTION` | `update_task_progress` FAILED | `BLOCKED` | — |
| `BLOCKED` | `plan_and_think` | `DRAFTING` (same plan_id) | — |
| `BLOCKED` | `update_task_progress` on another task | *(no change)* | `PLAN_BLOCKED` |
| any | `get_current_plan` | *(no change)* | never fails |

## Deliberate leniencies (rejecting these would strand a weak model)

- **Out-of-order task start** → redirected to the correct `task_id`, `ok:true`.
- **Duplicate `DONE`** → idempotent, points at the next pending task.
- **`DONE` without prior `IN_PROGRESS`** → accepted, audited `skipped_in_progress`.
- **`plan_and_think` while `APPROVED`/`IN_EXECUTION` with the *same* goal** → not a new plan;
  redirect to the in-flight task.

## Two time-based / version-based guards (added after real bugs)

- **Approval expiry** (1.4.0): an `APPROVED`/`IN_EXECUTION` plan left idle past `approval_ttl`
  (default 1800 s) has its approval revoked on the next touch → back to `AWAITING_APPROVAL`,
  audited `approval_expired`. Stops a morning approval from authorizing afternoon execution.
  An unreadable timestamp counts as expired (fail-safe).
- **Approval binding** (1.4.0): any change to the task list clears the pending approval, so
  `APPROVED` for a version the human never saw is refused with `APPROVAL_NOT_REQUESTED`.

## next_action decoder (what the model does with each)

| next_action | Model action |
|---|---|
| `CALL_PLAN_AND_THINK` | call `plan_and_think` |
| `CALL_REQUEST_USER_APPROVAL` | call `request_user_approval` |
| `CALL_UPDATE_TASK_PROGRESS` | call `update_task_progress` |
| `CALL_GET_CURRENT_PLAN` | call `get_current_plan` |
| `STOP_AND_WAIT_FOR_USER` | print `display_to_user`, end the turn |
| `ANSWER_USER` | write the final answer |

When more than one plan is active, hints are **qualified**: the `next_action_hint` names the
`plan_id` to include, so a model that copies the hint verbatim routes correctly.
