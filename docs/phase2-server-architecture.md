# Phase 2 — Local MCP Server Architecture

Target: one Python process on the user's Windows PC, launched by AnythingLLM over **stdio**.
No network, no database, no external service. Everything the corporate LLM needs to behave
like a state machine lives in one JSON file.

---

## 0. Architectural rules

| Rule | Reason |
|---|---|
| **The server owns the state, the model owns nothing** | The corporate LLM's context gets truncated. Any state it must remember is state it will hallucinate. |
| **Single writer, single file** | One AnythingLLM workspace = one process = no lock contention in practice. Complexity here buys nothing. |
| **Every handler returns through one response builder** | `next_action` must be computed in exactly one place, or the model receives contradictory instructions. |
| **No exception ever escapes a tool handler** | A raw MCP error string makes a weak model abandon the protocol and answer from memory. |
| **Persist on every mutation, synchronously** | The user will kill/restart AnythingLLM mid-plan. Losing the plan is worse than a 2ms fsync. |
| **stdout belongs to the protocol** | Under stdio transport, one stray `print()` corrupts the JSON-RPC stream. All logging goes to stderr or a file. |

---

## 1. Process & transport

```
AnythingLLM (Agent Mode)
        │  spawns child process, JSON-RPC over stdin/stdout
        ▼
python server.py                      ← MCP server "planning-mcp"
        │
        ├── state/plan_state.json      ← active + archived plans (source of truth)
        └── state/audit.jsonl          ← append-only event log (debugging / HITL evidence)
```

**Primary transport: stdio.** It requires no port, no firewall exception, and no CORS —
important on a locked-down corporate laptop. AnythingLLM spawns and owns the lifecycle.

**Optional transport: SSE on `127.0.0.1:8931`.** Only needed if the user wants the server to
outlive AnythingLLM restarts or to be shared by several workspaces. Same handlers, different
entry point; selected by `--transport sse`. Bind to loopback only — never `0.0.0.0`.

### Suggested file layout

```
D:/LightweightSequentialThinking/
├── server.py              # entry point: transport wiring + tool registration
├── planning/
│   ├── schemas.py         # the 4 inputSchema dicts from Phase 1 (verbatim, single source)
│   ├── store.py           # load / save / atomic write / archive
│   ├── models.py          # Plan, Task, ThinkingStep dataclasses + enums
│   ├── leniency.py        # input normalization (Phase 1 §6)
│   ├── state_machine.py   # legal transitions + next_action resolver
│   ├── handlers.py        # the 4 tool implementations
│   └── responses.py       # the single response builder
├── state/                 # created at runtime, gitignored
└── docs/
```

---

## 2. Storage design

### 2.1 Why a JSON file (and not SQLite / in-memory)

- **In-memory only**: dies with the process. AnythingLLM restarts are common during bring-up,
  and the model cannot rebuild a plan it can no longer see. Rejected.
- **SQLite**: correct but overweight — migrations, connection handling, and a binary file the
  user cannot inspect. The whole point of this project is that a human can open the state file
  and read what the agent thinks it is doing. Rejected.
- **Single JSON file**: human-readable, trivially resettable (delete the file), and a plan is
  at most a few KB. Chosen.

### 2.2 On-disk shape — `state/plan_state.json`

```json
{
  "schema_version": 1,
  "active_plan_id": "plan_20260724_0001",
  "plans": {
    "plan_20260724_0001": {
      "plan_id": "plan_20260724_0001",
      "goal": "Summarize the Q3 sales report and email it to the team lead.",
      "plan_status": "APPROVED",
      "created_at": "2026-07-24T09:12:03+09:00",
      "updated_at": "2026-07-24T09:18:44+09:00",
      "total_steps": 3,
      "thinking_steps": [
        {
          "step_number": 1,
          "thought": "I need to find the Q3 report before I can summarize it.",
          "superseded": false,
          "revises_step": null,
          "created_at": "2026-07-24T09:12:03+09:00"
        }
      ],
      "tasks": [
        {
          "task_id": 1,
          "title": "Locate the Q3 sales report file",
          "status": "DONE",
          "result_log": "Found /reports/q3_sales.xlsx.",
          "started_at": "2026-07-24T09:17:10+09:00",
          "finished_at": "2026-07-24T09:17:52+09:00"
        }
      ],
      "approval": {
        "requested_at": "2026-07-24T09:15:00+09:00",
        "decided_at": "2026-07-24T09:16:20+09:00",
        "decision": "APPROVED",
        "user_comment": null,
        "revision_count": 1
      }
    }
  }
}
```

Design notes:

- `task_id` is a **1-based index within its plan**, not a global ID. Weak models handle `1,2,3`
  far more reliably than `t_a91f`. Collisions across plans are impossible because a task is
  always resolved relative to `active_plan_id`.
- `plan_id` format `plan_YYYYMMDD_NNNN` is sortable and human-readable in the audit log.
- Superseded thinking steps are **kept, not deleted** — `revises_step` needs the history for
  the recovery tool to explain what changed.
- Completed/cancelled plans stay in `plans` but `active_plan_id` moves on. A retention cap
  (default: keep the last 20 plans) prunes the file so it never grows unbounded.

### 2.3 Write protocol

```
mutate in memory  →  validate invariants  →  write state/plan_state.json.tmp
                  →  flush + fsync        →  os.replace(tmp, real)   [atomic on NTFS]
                  →  append one line to state/audit.jsonl
```

`os.replace` gives an atomic rename on Windows, so a crash mid-write leaves the previous good
file intact rather than a truncated one. The audit log is written **after** the state file: a
duplicated audit line is harmless, a lost state write is not.

On startup: if `plan_state.json` is missing → start empty. If it is present but unparseable →
rename it to `plan_state.corrupt.<timestamp>.json`, start empty, and log to stderr. **Never
crash on startup** — a server that fails to launch gives AnythingLLM no tools at all, and the
model silently reverts to answering from memory, which is the exact failure this project exists
to prevent.

### 2.4 Concurrency

One AnythingLLM workspace spawns one process, and the model is instructed to call exactly one
tool per turn (R5), so real concurrency is near-zero. Still, cheap defenses:

> **Session isolation caveat:** there is ONE active-plan slot per state directory. Approved /
> in-execution plans cannot be hijacked by a second conversation (the redirect leniency in §4
> protects them), but an unfinished *draft* can be replaced by a different-goal
> `plan_and_think` — archived to `superseded_tasks`, audited as `goal_replaced`, and flagged in
> `input_notes`, but replaced nonetheless. For multiple workspaces, register one server entry
> per workspace with distinct `PLANNING_MCP_STATE_DIR` values — separate state dirs are fully
> isolated. Operational guidance: deployment manual §11.

- A single `threading.Lock` around the load-mutate-save cycle.
- An advisory lock file `state/.lock` containing the PID; if it exists and the PID is alive,
  log a warning to stderr but **continue** — refusing to start would be worse than a rare race.

---

## 3. Domain model

```python
class PlanStatus(str, Enum):
    NONE = "NONE"; DRAFTING = "DRAFTING"; AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"; IN_EXECUTION = "IN_EXECUTION"; BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"; CANCELLED = "CANCELLED"

class TaskStatus(str, Enum):
    PENDING = "PENDING"; IN_PROGRESS = "IN_PROGRESS"; DONE = "DONE"; FAILED = "FAILED"

class NextAction(str, Enum):
    CALL_PLAN_AND_THINK = "CALL_PLAN_AND_THINK"
    CALL_REQUEST_USER_APPROVAL = "CALL_REQUEST_USER_APPROVAL"
    CALL_UPDATE_TASK_PROGRESS = "CALL_UPDATE_TASK_PROGRESS"
    CALL_GET_CURRENT_PLAN = "CALL_GET_CURRENT_PLAN"
    STOP_AND_WAIT_FOR_USER = "STOP_AND_WAIT_FOR_USER"
    ANSWER_USER = "ANSWER_USER"
```

These are the same enums published in Phase 1 §5 — `schemas.py` builds the tool `inputSchema`
from these enum members so the advertised schema and the runtime validator can never drift.

---

## 4. Plan state machine

```
                    plan_and_think (need_more_thinking=true)
                              ┌───────┐
                              ▼       │
   NONE ──plan_and_think──► DRAFTING ─┘
                              │
                              │ plan_and_think(need_more_thinking=false, task_list=[...])
                              ▼
                     AWAITING_APPROVAL ──────request_user_approval(REJECTED)────► CANCELLED
                              │  ▲                                                    │
        request_user_approval │  │ request_user_approval(ASK_USER)                    │
              (APPROVED)      │  │                                                    │
                              ▼  │ request_user_approval(REVISE) ──► DRAFTING ────────┘
                          APPROVED                                   (re-plan)     (new request)
                              │
                              │ update_task_progress(IN_PROGRESS) on first task
                              ▼
                        IN_EXECUTION ──update_task_progress(FAILED)──► BLOCKED
                              │                                          │
                              │ all tasks DONE                           │ plan_and_think
                              ▼                                          ▼
                          COMPLETED                                  DRAFTING
```

### Transition table (server-enforced)

| From | Tool call | To | If illegal → |
|---|---|---|---|
| `NONE` / `COMPLETED` / `CANCELLED` | `plan_and_think` step 1 | `DRAFTING` (new plan_id) | — |
| `DRAFTING` | `plan_and_think`, more thinking | `DRAFTING` | — |
| `DRAFTING` | `plan_and_think`, final + task_list | `AWAITING_APPROVAL` | missing task_list → `ok:false`, `MISSING_TASK_LIST` |
| `AWAITING_APPROVAL` | `request_user_approval` ASK_USER | `AWAITING_APPROVAL` | — |
| `AWAITING_APPROVAL` | `request_user_approval` APPROVED | `APPROVED` | — |
| `AWAITING_APPROVAL` | `request_user_approval` REVISE | `DRAFTING` | — |
| `AWAITING_APPROVAL` | `request_user_approval` REJECTED | `CANCELLED` | — |
| `AWAITING_APPROVAL` / `DRAFTING` | `update_task_progress` | *(no change)* | `ok:false`, `PLAN_NOT_APPROVED` ← **the critical guard** |
| `APPROVED` | `update_task_progress` IN_PROGRESS | `IN_EXECUTION` | — |
| `IN_EXECUTION` | `update_task_progress` DONE (some left) | `IN_EXECUTION` | — |
| `IN_EXECUTION` | `update_task_progress` DONE (last one) | `COMPLETED` | — |
| `IN_EXECUTION` | `update_task_progress` FAILED | `BLOCKED` | — |
| `BLOCKED` | `plan_and_think` | `DRAFTING` (same plan_id, revision) | — |
| `BLOCKED` | `update_task_progress` on another task | *(no change)* | `ok:false`, `PLAN_BLOCKED` — must re-plan |
| any | `get_current_plan` | *(no change)* | never fails |

Two deliberate leniencies, because rejecting them would strand a weak model:

- **`plan_and_think` while `APPROVED`/`IN_EXECUTION`** — the model got confused mid-execution.
  Do **not** start a new plan. Return `ok: true` with the *current* plan and
  `next_action: CALL_UPDATE_TASK_PROGRESS` pointing at the in-flight task. Effectively a
  redirect to `get_current_plan`.
- **`update_task_progress` on a task already `DONE`** — idempotent. Return `ok: true` and point
  at the next `PENDING` task rather than erroring.

### Task-level rules

- A task may only go `IN_PROGRESS` if every lower-numbered task is `DONE` or `FAILED`. Out-of-order
  starts are corrected, not rejected: the response redirects to the correct `task_id`.
- `DONE` requires the task to have been `IN_PROGRESS` first. If the model skips straight to
  `DONE`, accept it but record `skipped_in_progress: true` in the audit log — blocking here
  would cost more than it gains.
- `FAILED` immediately halts forward progress. `next_action` becomes `CALL_PLAN_AND_THINK`,
  never `CALL_UPDATE_TASK_PROGRESS` for the next task.

---

## 5. HITL breakpoint flow

The gate is enforced in **two independent places** so that a model ignoring instructions still
cannot execute:

1. **Instructional gate** — `request_user_approval(ASK_USER)` returns
   `next_action: STOP_AND_WAIT_FOR_USER` plus a pre-rendered `display_to_user` block. The model
   only has to echo a string, which is the single most reliable thing a weak model can do.
2. **Enforcement gate** — `update_task_progress` checks `plan_status` on the server. Until it is
   `APPROVED` or `IN_EXECUTION`, every call returns `ok: false` / `PLAN_NOT_APPROVED`. The model
   *cannot* execute early even if it tries, because progress is only real once the server records it.

```
model ── request_user_approval(ASK_USER, plan_summary) ──►  server
                                                              │ status = AWAITING_APPROVAL
                                                              │ render display_to_user
        ◄── next_action: STOP_AND_WAIT_FOR_USER ──────────────┘
model prints display_to_user, ENDS TURN
                    ▼
          ┌──────── human reads plan in AnythingLLM chat ────────┐
          │  "yes"          "no"            "change step 4..."   │
          └─────┬───────────────┬───────────────────┬────────────┘
                ▼               ▼                   ▼
            APPROVED        REJECTED             REVISE
                │               │                   │
        unlock execution   CANCELLED        DRAFTING + user_comment
                │                                   │
                ▼                                   ▼
     next_action:                         next_action:
     CALL_UPDATE_TASK_PROGRESS            CALL_PLAN_AND_THINK
     next_task: {task_id: 1, ...}         (must re-approve afterwards)
```

**Why the human's answer arrives as a tool call, not as a signal to the server:** the server has
no channel to the user — it only sees what AnythingLLM sends. So the model acts as the courier,
and `decision` is its report of what the human said. This is the one place where the protocol
depends on the model being honest, which is why R3 ("never assume approval") is stated three
times across the system prompt and the tool description, and why `revision_count` and the exact
`user_comment` are persisted as evidence in the audit log.

**Re-approval after revision is mandatory.** `REVISE` sets the plan back to `DRAFTING`, which
means the next `plan_and_think` final step lands in `AWAITING_APPROVAL` again. There is no path
from `REVISE` directly to execution.

**Approval binds to the exact plan version the human last saw.** Every mutation of the task
list — finalize, revision, or a different conversation replacing the draft — clears the pending
approval request (`approval.reset_request()`). `APPROVED` arriving when no request is live is
hard-refused with `APPROVAL_NOT_REQUESTED` and audited as `stale_approval_refused`; the model is
redirected to `ASK_USER` so the human is re-shown the *current* plan. This closes the
cross-session misdirection where a user approves plan A in one conversation while another
conversation has silently replaced it with plan B.

---

## 6. Request pipeline

Every tool call goes through the same six stages — no handler does its own parsing or its own
response formatting:

```
1. RECEIVE      raw arguments dict from MCP
2. LENIENCY     leniency.py — Phase 1 §6: case/alias normalization, "3"→3,
                "true"→True, string→list splitting, array-of-objects→titles,
                drop unknown keys.  Never rejects; records what it fixed.
3. VALIDATE     required fields present, enums legal, integers in range.
                Failure → responses.error(...) with a corrective next_action.
4. GUARD        state_machine.py — is this transition legal from the current
                plan_status? Illegal → error response naming the correct tool.
5. MUTATE       handlers.py — apply the change, store.save() atomically,
                append to audit.jsonl.
6. RESPOND      responses.build() — the ONLY place a response is constructed.
                Always emits: ok, plan_status, next_action, next_action_hint.
```

### The response builder

```python
def build(plan, *, ok=True, error_code=None, message=None, **extra) -> dict:
    """Single source of truth for next_action. Every handler returns through here."""
    action, hint = resolve_next_action(plan, error_code)
    payload = {
        "ok": ok,
        "plan_id": plan.plan_id if plan else None,
        "plan_status": plan.plan_status if plan else "NONE",
        "next_action": action,
        "next_action_hint": hint,
    }
    if error_code: payload["error_code"] = error_code
    if message:    payload["message"] = message
    return {**payload, **extra}
```

`resolve_next_action(plan, error_code)` is a pure function of `(plan_status, task states,
error_code)`. Because it is the only producer of `next_action`, the model can never receive two
different instructions for the same state — the failure mode that makes weak models loop.

### Error policy

Handlers are wrapped so that **any** uncaught exception becomes:

```json
{
  "ok": false,
  "error_code": "INTERNAL_ERROR",
  "plan_status": "<last known>",
  "next_action": "CALL_GET_CURRENT_PLAN",
  "next_action_hint": "Something went wrong on the server. Call get_current_plan with plan_id='current' to resync, then continue from the task it reports.",
  "message": "<exception class name only — never a stack trace>"
}
```

Stack traces go to stderr and `audit.jsonl`, never to the model: they consume scarce context and
push a weak model into debugging mode instead of following the protocol.

---

## 7. Context recovery

`get_current_plan` is the antidote to AnythingLLM's context truncation. Three design decisions
make it work on a small model:

- **Always safe, always succeeds.** No state change, no error path. The system prompt can say
  "call it whenever you are unsure" without risk.
- **Returns a ready-to-obey instruction, not just data.** The `next_action_hint` names the exact
  next tool call, including the `task_id` — the model does not have to re-derive its position.
- **Truncates its own payload.** Superseded thinking steps are summarized (`"3 earlier steps
  superseded"`) and `result_log` values are capped at ~200 chars, so recovery never blows the
  remaining context budget of an already-truncated conversation.

---

## 8. Configuration

Environment variables, all optional, all with safe defaults:

| Variable | Default | Purpose |
|---|---|---|
| `PLANNING_MCP_STATE_DIR` | `<server_dir>/state` | Relocate state (e.g. to a synced folder) |
| `PLANNING_MCP_LOG_LEVEL` | `INFO` | stderr verbosity |
| `PLANNING_MCP_MAX_PLANS` | `20` | Retention cap before pruning old plans |
| `PLANNING_MCP_MAX_TASKS` | `12` | Reject oversized task lists (truncate + warn, don't error) |
| `PLANNING_MCP_AUTOAPPROVE` | `false` | **Testing only.** Skips the HITL gate. Logs a loud stderr warning every call so it cannot be left on by accident. |

### Windows / AnythingLLM specifics

- Launch with `python -u server.py` or set `PYTHONUNBUFFERED=1`; buffered stdout makes stdio
  transport look like a hung server.
- Use forward slashes in `anythingllm_mcp_servers.json` (`D:/LightweightSequentialThinking/...`)
  — backslashes must be escaped in JSON and are a common silent failure.
- If `python` is not on PATH inside AnythingLLM's environment, use the absolute interpreter path
  (e.g. `C:/Users/<user>/AppData/Local/Programs/Python/Python312/python.exe`).
- Force UTF-8 (`PYTHONUTF8=1`) so Korean `user_comment` text does not raise `UnicodeEncodeError`
  on the legacy cp949 console encoding.

---

## 9. What is deliberately NOT built

| Omitted | Why |
|---|---|
| Multi-user / multi-workspace isolation | One local PC, one user. Adding tenancy would double the state model for zero benefit. |
| Sub-tasks / dependency graphs | A flat 2–7 item list is the largest structure a weak model can track. Nesting is the fastest route back to hallucinated plans. |
| Automatic task execution | This server plans and tracks; AnythingLLM's other skills execute. Merging the two would make the approval gate unenforceable. |
| Time estimates / priorities | More fields = more malformed calls, no improvement in plan quality. |
| A web UI for approval | Approval happens in the AnythingLLM chat the user is already looking at. A second surface would split attention and break the single-conversation model. |
