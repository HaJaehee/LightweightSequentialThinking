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
