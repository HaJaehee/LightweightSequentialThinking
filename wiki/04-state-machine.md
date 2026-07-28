# 04 · State Machine

Authoritative logic: `planning/state_machine.py` + the transition checks in
`planning/handlers.py`. Machine-readable form: [data/state-machine.xml](data/state-machine.xml).

## Plan statuses

`NONE → DRAFTING → AWAITING_APPROVAL → APPROVED → IN_EXECUTION → AWAITING_COMPLETION →
COMPLETED`, with `BLOCKED` (task failed) and `CANCELLED` (rejected) as branches. `COMPLETED`
and `CANCELLED` are terminal. `AWAITING_COMPLETION` (1.9.0) is the human's verification of the
finished work — see below.

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
| `AWAITING_APPROVAL` | `approval` REVISE (scope `PLAN`) | `DRAFTING` | — |
| `AWAITING_APPROVAL` | `approval` REVISE (scope `TASKS`) | `DRAFTING` + `pending_revision` | — |
| `DRAFTING` + `pending_revision` | `plan_and_think` (final+`task_updates`) | `AWAITING_APPROVAL` (only flagged tasks rewritten) | no target addressed → `REVISION_INCOMPLETE` |
| `DRAFTING` *without* `pending_revision` | `plan_and_think` (final+`task_updates`) | *(no change)* | `REVISION_NOT_REQUESTED` |
| `AWAITING_APPROVAL` | `approval` REJECTED | `CANCELLED` | — |
| `AWAITING_APPROVAL`/`DRAFTING` | `update_task_progress` | *(no change)* | `PLAN_NOT_APPROVED` ← **critical guard** |
| `APPROVED` | `update_task_progress` IN_PROGRESS | `IN_EXECUTION` | — |
| `IN_EXECUTION` | `update_task_progress` DONE (not last) | `IN_EXECUTION`, next task auto-started `IN_PROGRESS` | — |
| `IN_EXECUTION` | `update_task_progress` DONE (last) | `AWAITING_COMPLETION` | — |
| `IN_EXECUTION` | `update_task_progress` FAILED | `BLOCKED` | — |
| `BLOCKED` | `plan_and_think` | `DRAFTING` (same plan_id) | — |
| `BLOCKED` | `update_task_progress` on another task | *(no change)* | `PLAN_BLOCKED` |
| any | `get_current_plan` | *(no change)* | never fails |

## Deliberate leniencies (rejecting these would strand a weak model)

- **Out-of-order task start** → redirected to the correct `task_id`, `ok:true`.
- **Duplicate `DONE`** → idempotent, points at the next pending task.
- **Redundant `IN_PROGRESS` on a task already running** → accepted, `started_at` preserved. This
  is the normal shape once auto-advance is on: the server started the task and the model sends
  the call anyway, out of habit or because its prompt predates the change.
- **`plan_and_think` while `APPROVED`/`IN_EXECUTION` with the *same* goal** → not a new plan;
  redirect to the in-flight task.

## Task completion is enforced, not asserted (1.9.0)

The field failure: a small model marked every task `DONE` in a row without doing the work,
and reported the plan finished. `DONE` is a *claim*, so it is now checked:

| Refusal | error_code |
|---|---|
| the task was never `IN_PROGRESS` | `TASK_NOT_STARTED` |
| an earlier task is unfinished | `TASK_OUT_OF_ORDER` |
| `result_log` is empty, a bare success claim ("완료", "done", "ok"), or just the task title | `MISSING_RESULT_LOG` |

Length alone cannot judge evidence — Korean fits a real outcome into ~8 characters — so the
check is content-based (an exact-match phrase filter plus a low length floor,
`PLANNING_MCP_MIN_RESULT_LOG`, default 8 normalized characters). Starting the wrong task is
still forgiven with a redirect; *claiming* the wrong task is finished is not.

Because each `DONE` now requires the task to be the one in progress, and starting a task already
enforces order, batch-marking is structurally impossible.

## Auto-advance (1.11.0)

Accepting a `DONE` also puts the next task into `IN_PROGRESS` (`PLANNING_MCP_AUTO_ADVANCE`,
default on). This is a round-trip cut, not a relaxation. The `IN_PROGRESS` call was never the
thing stopping a model from claiming work it had not done — the evidence check is — and the turn
boundary it provided survives: the model still receives one "task N is in progress, do the work"
instruction per task and still cannot claim `DONE` for it until a later call, with its own
`result_log`. What disappears is one empty round trip per task: 5 tasks cost 6 execution calls
instead of 10, which is context and error surface a small model does not have to spend.

Rejected alternative: a `batch_update` that marks several tasks `DONE` in one call. It would
delete the enforcement outright — the failure mode in the section above is exactly a model
firing `DONE` at every task at once — and the models it was proposed to help are the ones most
likely to do it.

**And `COMPLETED` is no longer self-awarded.** When the last task is `DONE` the plan enters
`AWAITING_COMPLETION`; the model must call `request_user_approval(ASK_USER)`, which shows the
human a **completion report** of every task with its `result_log`. Only the human's `APPROVED`
closes the plan (`REJECTED` → `CANCELLED`, `REVISE` → `DRAFTING` with their comment). This is
the only check that catches invented `result_log` text, since the server cannot observe work.
The approval fingerprint covers each task's status and evidence, so a report cannot be
rewritten after the human saw it. Set `PLANNING_MCP_COMPLETION_APPROVAL=false` to go straight
to `COMPLETED` as before.

Anti-abandonment: while any task remains, every `next_action_hint` names how many are left and
says not to tell the user the work is finished.

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
