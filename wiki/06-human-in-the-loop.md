# 06 · Human-in-the-Loop (Blocking Approval)

The core insight of this project. Logic: `handlers._wait_for_human` + `approval.py`.

## Why blocking is necessary

AnythingLLM's agent loop feeds a tool result back to the model and lets *the model* decide
whether to keep calling tools. Returning `STOP_AND_WAIT_FOR_USER` as text does **not** stop a
weak model — it reads the instruction as one more observation and calls the next tool. As of
AnythingLLM 1.15.0 there is no host-side lever for this: `directOutput` is Agent-Flow-only, MCP
config is server-level only, and the custom-skill `"exit"` return is unofficial and prompt-
dependent (i.e. the thing that already fails).

## The mechanism: don't break the loop, make the loop wait on us

The agent loop waits **synchronously** for a tool result before the model can generate anything
else. So if `request_user_approval(ASK_USER)` does not return, the loop physically cannot
advance. The pause needs nothing from AnythingLLM — no loop patch, no directOutput.

```
model ── request_user_approval(ASK_USER) ──►  server
                                               │ persist AWAITING_APPROVAL
                                               │ publish plan to http://127.0.0.1:8765/
                                               │ store.paused()  ← release lock, let others run
                                               │ ┌ heartbeat: notifications/progress q20s
   agent loop BLOCKED here, cannot execute ◄────┤ └ (resets the client's 60s request timer)
                                               │
        human clicks 승인 / 거절 / 수정요청 ──────┤
        ◄── APPROVED + next_task ───────────────┘  (same call returns)
```

## Timeout arithmetic (this determines everything)

The MCP TypeScript SDK that AnythingLLM uses:
- default per-request timeout **60 s** (`DEFAULT_REQUEST_TIMEOUT_MSEC`),
- resets it on every progress notification (`resetTimeoutOnProgress` defaults true),
- no `maxTotalTimeout`.

So:
- **With a `progressToken`** (client supplies one): a 20 s heartbeat resets the timer forever →
  the wait can last up to `approval_timeout` (default 900 s), effectively unbounded.
- **Without a token:** the server caps the wait at `NO_PROGRESS_WAIT_CEILING_SEC` (55 s) and
  returns the ordinary locked response, avoiding a `-32001` client error.

**Measured fact:** Claude Code does **not** send a progressToken → it hits the 55 s ceiling.
Whether AnythingLLM sends one must be checked in the field — the server logs which mode it is in
(`heartbeat on` / `heartbeat off - no progressToken from client`). Both transports (stdio and
SSE) can send heartbeats; SSE's notifier fans them to open streams.

## The request outlives the tool call (1.5.0)

The wait has a ceiling but the human does not. On timeout the request is **deliberately left on
the page** (clearing it is what made buttons vanish after 55 s before anyone could answer).
Whatever the human clicks afterwards is collected on the **next tool call of any kind**
(`_apply_late_decision`, audited `late_decision_applied`) and applied. A late decision is only
honoured for the exact plan version that was on screen (fingerprint), else discarded.

## Two enforcement layers

1. **Instructional gate:** `STOP_AND_WAIT_FOR_USER` + pre-rendered `display_to_user`. The model
   only has to echo a string — the single most reliable thing a weak model does.
2. **Enforcement gate:** `update_task_progress` checks `plan_status` server-side. Until
   `APPROVED`/`IN_EXECUTION`, every call is `PLAN_NOT_APPROVED`. Even a model ignoring the
   instruction cannot make execution *real*.

Blocking approval adds a third, physical layer on top of these.

## The approval web page (`approval.py`)

- Loopback-only HTTP on `127.0.0.1:8765` (configurable). Serves one self-contained HTML page
  (CSS/JS inlined as string constants — **zero static files, zero dependencies**) plus three
  JSON endpoints: `GET /api/health`, `GET /api/pending`, `POST /api/decide`.
- Renders the **whole queue** (multiple concurrent sessions), each with 승인/수정요청/거절
  buttons and a comment box. Polls every 1.5 s. Tab title flashes `⚠ 승인 대기 N건` and a short
  tone plays on a new request.
- A **PLAN** request renders a header — `계획 승인 요청 · <plan_id>`, the labelled 목표, and the
  model's 개요 (`plan_summary`) — then the task list as rows, each with its own comment box (see
  [Per-task review](#per-task-review-19x)). A **COMPLETION** request keeps the original single
  `<pre>` + one comment box.
- `plan_summary` travels as **its own field** on the request, not only inside the pre-rendered
  `display`. It briefly did not: when the page stopped rendering `display` for PLAN requests, the
  overview went with it and the human was left judging a bare task list. A field the page can
  compose with is the fix; `display` remains what the model echoes into chat.
- **HTML/XSS escaping** is client-side (`esc()`), verified in a real browser: a `<script>` in
  plan text renders as inert text, no console error. The `onclick` handlers pass the request's
  hex id (safe), so escaped plan content cannot break routing.
- **Surfacing is best-effort.** The browser is auto-opened once at startup and on each request,
  but a blocked popup / second monitor / missing default browser can defeat it — so the
  approval URL is also printed to stderr (`APPROVE PLANS AT -> ...`) and included in the chat
  `display_to_user`. **Operational advice: open the tab once and leave it; it polls.**
- If the page cannot start at all, the server **degrades loudly**: the response carries a
  `NOT hard-paused` warning in `input_notes` and audits `approval_ui_unavailable`. It never
  silently disarms.

## Per-task review (1.9.x)

REVISE used to mean one thing: throw the breakdown away and redraft it. For a mid-sized model
that is a waste — the human objected to one line, and the model rewrites five, drifting on the
four nobody questioned. Per-task review makes the narrow case narrow.

**The human's side.** On a PLAN request each task is a row with its own comment box, plus the
global box. The REVISE button **states its own consequence before it is clicked**:

| what is typed | button label | `scope` sent |
|---|---|---|
| nothing per-task | `수정 요청 · 계획 전체 재작성` | `PLAN` |
| task 3 only | `수정 요청 · 3번만` | `TASKS` |
| tasks 2 and 3 | `수정 요청 · 2개 태스크만` | `TASKS` |
| any, with `☐ 계획 전체를 다시 세우기` ticked | `수정 요청 · 계획 전체 재작성` | `PLAN` |

That is why the server **never infers the scope**: the page already showed the human what would
happen, and re-deriving it from "are there comments?" would overrule what they were shown.

**The model's side.** `TASKS` scope records `plan.pending_revision = {"targets": {...}}`. While
that is set, `next_action_hint` hands the model the literal argument to send —
`task_updates=[{"task_id": 3, "title": "<the rewritten task>"}]` — and names the tasks it must
not touch. `plan_and_think` then rewrites only the flagged tasks: **task ids, positions, and the
`result_log` of untouched tasks all survive.** Unflagged edits are dropped with a note.

**Boundaries, and why.**
- **The completion phase is excluded.** A COMPLETION request asks whether work already done is
  real; rewriting one line of the plan does not answer that. `record_decision` forces `scope =
  PLAN` for any non-PLAN entry, so an older or hand-crafted client cannot get around it.
- **Add / delete / reorder are excluded.** They renumber `task_id`, which breaks the ordering
  invariants in `can_start_task` / `unfinished_before` and any id the model is holding. Those go
  through 계획 전체 재작성; the checkbox label says so.
- **A full `task_list` in answer to a targeted request is accepted**, with a note and an audited
  `targeted_revision_ignored`. It is wasteful, not unsafe — the human re-approves every task
  either way — and refusing would strand a model that cannot follow the hint. Auditing it is
  what makes the waste measurable in the field.
- **`task_updates` with no pending request is refused** (`REVISION_NOT_REQUESTED`). Otherwise the
  model could edit an approved plan on its own authority.

**Mixed versions.** An entry published by an older process has no `phase`; `/api/pending` returns
it as `null` rather than guessing `PLAN`, and the page falls back to the original single-comment
form. A decided entry with no `scope` reads as `PLAN`. Both directions degrade to 1.8 behaviour.

## Config knobs

`PLANNING_MCP_BLOCKING_APPROVAL` (default true), `_APPROVAL_PORT` (8765), `_APPROVAL_TIMEOUT`
(900), `_APPROVAL_OPEN_BROWSER` (true), `_APPROVAL_TTL` (1800). Full list:
[data/config.json](data/config.json).
