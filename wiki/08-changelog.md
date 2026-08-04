# 08 · Changelog

Version-by-version evolution with the *reason* for each. Machine-readable:
[data/versions.json](data/versions.json). Root-cause detail for the bug fixes:
[09-defects-and-lessons.md](09-defects-and-lessons.md).

The arc: v1.0–1.2 built the design; v1.3 added the physical pause; v1.4–1.5 hardened the
approval semantics; v1.6–1.8.1 made it correct under concurrency and hostile input. The later
versions are almost entirely *bug fixes found by writing tests for untested seams*.

---

### 1.0–1.2 — foundation
Four-tool MCP server, stdlib-only, atomic JSON persistence, audit log, leniency layer, the
single response builder, the enforced approval gate (`PLAN_NOT_APPROVED`). 1.2.0 added
`APPROVAL_NOT_REQUESTED` (approval binds to the shown version) and the air-gapped packaging
tooling (`make_package` / `setup_runtime` / `verify_install`, optional bundled Python).

### 1.3.0 — blocking approval (the physical pause)
`request_user_approval(ASK_USER)` holds the tool call open until a human decides on a localhost
page, so the agent loop cannot advance. Progress-heartbeat timeout arithmetic. This is the
project's central idea — see [06](06-human-in-the-loop.md).

### 1.4.0 — stale approval fix
A plan left `APPROVED` in `plan_state.json` from a previous session authorized execution hours
later in an unrelated conversation. Added approval **expiry** (TTL) and stopped a new goal from
inheriting an old approval. Defect [D1](09-defects-and-lessons.md#d1).

### 1.5.0 — approval window persists
The approval request was cleared when the tool call timed out (55 s), so buttons vanished before
the human answered. Now the request outlives the tool call and a late click is applied on the
next call. Defect [D2](09-defects-and-lessons.md#d2).

### 1.6.0 — concurrency, round 1
Two processes on one state dir lost a whole plan (shared `threading.Lock` is useless across
processes) → cross-process file lock. A blocking approval froze other sessions for 52 s → release
the lock during the wait (`store.paused()`) **and** thread the stdio read loop. Defects
[D3](09-defects-and-lessons.md#d3), [D6](09-defects-and-lessons.md#d6).

### 1.7.0 — one approval surface per state dir
Approval state was per-process, so ghost processes served their own unwatched pages. Moved it to
a shared `state/approval.json` with a singleton page elected by port binding + automatic
takeover. Defect [D4](09-defects-and-lessons.md#d4).

### 1.8.0 — multi-plan
Single `active_plan_id` meant concurrent sessions evicted each other. Plans now coexist, routed
by explicit id / by goal / the-only-active-one; approval became a queue. Defect
[D7](09-defects-and-lessons.md#d7).

### 1.8.1 — the edge-case hunt (three commits)
Writing tests for untested seams surfaced **8 more defects**:
- Thread-unsafe transaction depth counter (shared, not thread-local) → serialization silently
  off. [D5](09-defects-and-lessons.md#d5)
- File-lock timeout ignored (Windows `LK_LOCK` blocks 10 s internally). [D8](09-defects-and-lessons.md#d8)
- File lock burned full timeout on permanent failures (missing dir). [D9](09-defects-and-lessons.md#d9)
- SSE POST hung during blocking approval; SSE had no heartbeat notifier. [D10](09-defects-and-lessons.md#d10)
- `Store.save` reported lost writes as success. [D11a](09-defects-and-lessons.md#d11)
- Structurally-wrong (but valid-JSON) state file wedged every call on `INTERNAL_ERROR`. [D11b](09-defects-and-lessons.md#d11)
- Approval publish/decide reported unpersisted writes as success. [D11c](09-defects-and-lessons.md#d11)
- Settled plans left ghost approval requests on the page. [D11d](09-defects-and-lessons.md#d11)
- `normalize()` ran outside the exception guard; crashed on non-string keys. [D12](09-defects-and-lessons.md#d12)

Plus real-browser verification of the approval page and a fuzz suite for leniency.

### 1.10.0 — per-task plan review
`REVISE` used to mean one thing: throw the breakdown away and redraft it. A mid-sized model then
rewrites five tasks because the human objected to one, drifting on the four nobody questioned.

The approval page now renders a **PLAN** request as task rows, each with its own comment box, and
the REVISE button **states its consequence before it is clicked** (`수정 요청 · 3번만` vs
`수정 요청 · 계획 전체 재작성`, with a `☐ 계획 전체를 다시 세우기` override). The page sends that
`scope`; the server never infers it. `scope=TASKS` records `plan.pending_revision`, and
`plan_and_think` finalizes with the new `task_updates` parameter, rewriting **only** the flagged
tasks — ids, positions, and the `result_log` of untouched tasks all survive.

Deliberately out of scope, both for the same reason (each would answer a question the human did
not ask): the **completion phase** stays whole-plan — a completion report disputes whether work
happened, which rewriting a plan line cannot address — and **add/delete/reorder** stay whole-plan
because they renumber `task_id` and break the ordering invariants. A full `task_list` sent in
answer to a targeted request is accepted with a note and audited `targeted_revision_ignored`:
wasteful, not unsafe, and now measurable. See
[06](06-human-in-the-loop.md#per-task-review-19x).

### 1.11.0 — fewer calls, same enforcement
An external review of the 1.10.0 tool surface raised two objections on behalf of small models:
14+ tool calls for a 5-task plan, and the `task_list` (strings) vs `task_updates` (objects) type
split. Its proposed fixes — a `batch_update` that marks several tasks `DONE` at once, and merging
the two parameters into one shape — were both rejected, and both for the same reason: the thing
being called overhead is the enforcement.

`DONE` in a batch is precisely the 1.9.0 field failure ([09](09-defects-and-lessons.md)), and
the two task parameters differ in *authority*, not format — `task_list` costs a full
re-approval, `task_updates` may only answer a request the human made. What did survive review:

- **Auto-advance** (`PLANNING_MCP_AUTO_ADVANCE`, default on). Accepting a `DONE` also puts the
  next task into `IN_PROGRESS`. The separate start call was a round trip, not a safeguard: the
  model still gets one "do the work" instruction per task and still cannot claim `DONE` without
  its own evidence, so 5 tasks cost 6 execution calls instead of 10 with nothing given up. A
  redundant `IN_PROGRESS` is accepted without resetting `started_at`, and
  `build_tool_definitions(auto_advance=...)` swaps the tool description so the advertised
  contract cannot contradict the running server.
- **Bare `task_updates` are read, not guessed.** A model asked to rewrite one task often sends
  just the new wording (`["the rewritten task"]`, or the bare sentence). Leniency now sets
  id-less titles aside instead of dropping them, and `handlers` pairs one with the single flagged
  task — unambiguous by construction. With several flagged tasks it stays a guess, so it is
  refused with a note naming the shape to send.

The review's own call count was also out of date: blocking approval has folded the second
`request_user_approval` into the first since 1.3.0. Real cost for 5 tasks: 14 → 10 calls.

Also in 1.11.0: the **per-task comment boxes are collapsed** behind an [의견] button. Rendered
open, a twelve-task plan was twelve textareas and the plan itself became unreadable — the page
exists to be read before it is answered. The boxes stay in the DOM, so a comment survives being
collapsed; the row keeps an amber marker so it cannot be submitted invisibly.

---

### 1.12.0 — the goal became mutable

Every other part of the plan could be corrected by the human; the one thing that could not was
the sentence saying what the plan was *for*. `goal` was frozen at creation and doubled as the
routing key, so a user saying "that is not what I meant — Q4, not Q3" left the server in the one
state nobody can act on: correct tasks under a goal the user had already disowned, shown back to
them on every response and on the approval page. Drift protection (1.8.2) had quietly hardened
into an inability to be corrected.

The fix separates the two things immutability was conflating. **Identity** stays fixed — the
plan keeps its `plan_id`, tasks, evidence and history. **Wording** follows the user: the new
optional `revised_goal` parameter updates `goal` in place, and the audit anchor moves into
`original_goal` + a `goal_history` of `{at, from, to, source}` hops with a `goal_revised` audit
event. Auditability was never a reason to freeze the field; it is a reason to record the change.

Routing absorbs the correction rather than punishing it: a plan answers to its previous goal
text for as long as it is active, so a model still echoing the old wording continues its plan
instead of being told it does not exist. A "revision" that only adds punctuation is not
recorded. And a goal revised on an already-approved plan is taken, but the response says
outright that the human approved the *previous* goal — the correction updates the metadata, it
does not extend the mandate.

---

### 1.12.1 — the last DONE stopped being silent ([D16](09-defects-and-lessons.md#d16))

Observed in use: the model completed every task, marked them all `DONE` with real evidence — and
then wrote a final answer to the user instead of requesting the completion report. The server
had moved the plan to `AWAITING_COMPLETION` and answered `next_action =
CALL_REQUEST_USER_APPROVAL`; the model went past it. Nothing was corrupted (the plan simply sits
in `AWAITING_COMPLETION` and the guard refuses further task writes), but the HITL gate that puts
a human in front of the evidence quietly never ran.

The cause was not that hints are weak. Auto-advance (1.11.0) made `message` the field that
carries the next order on every DONE — "task N has been started for you — do that work NOW" —
so over a plan the model learns that `message` is where its instruction lives, while
`next_action_hint` repeats similar wording every turn and becomes background. On the **last**
DONE there is nothing to advance into, so `message` was `None`: the one response in the whole
loop that said nothing, arriving at the one moment when declaring victory is the cheapest
continuation available. The model was not overriding an instruction — it was filling a silence.

`_finish_task` now always says what happens next. With every task DONE it asks for the
completion report explicitly — `request_user_approval` with `decision='ASK_USER'` and a per-task
`plan_summary` under `completion_approval`, or the final answer to the user when that gate is
off — and adds "do not declare success yourself".

The branch is on `advanced is None`, but that condition is not the same as "the plan is
finished": with `auto_advance` off, nothing is auto-started even when tasks remain. Collapsing
the two would have made a manual-mode server tell the model to report completion halfway through
the plan — the exact failure 1.9.0 was built to prevent. So the empty-`advanced` case splits
three ways: work left (name the next task), all done + `AWAITING_COMPLETION` (ask for the
report), all done + `COMPLETED` (write the final answer). `next_action` is untouched;
`resolve_next_action` remains its single producer.

The agent prompt was the other half of the gap: R4 and R7 named `next_action`,
`next_action_hint` and `display_to_user`, and never mentioned `message` — so the field the model
had actually been steering by was one no rule acknowledged. Both variants now name it, and
Phase 3b spells out that the last DONE is not an ending. This half only takes effect when the
prompt is repasted into AnythingLLM; the server-side message alone does not deliver it.

---

## Git commit ↔ version map

| Commit | Version / theme |
|---|---|
| `4ac3562` | v1.2.0 foundation + packaging |
| `8e277da` | v1.3.0 blocking approval |
| `98abb86` | approval UI surfacing hardening |
| `920dd7d` | v1.4.0 stale approval |
| `225c0fb` | v1.5.0 persistent approval window |
| `dab9c97` | v1.6.0 concurrency round 1 |
| `a3456bd` | v1.7.0 shared approval surface |
| `4980396` | v1.8.0 multi-plan |
| `046a75e` | v1.8.1 thread-safe txn + 27 edge tests |
| `49129d3` | protocol/transport/filelock fixes |
| `7ded398` | store/approval failure paths |
| `6d3748b` | approval page browser check + leniency safety |

`main` branch, remote `github.com/HaJaehee/LightweightSequentialThinking`. **Not pushed** as of
this writing — see [11](11-status-and-next-steps.md).
