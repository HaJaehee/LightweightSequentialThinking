# 09 · Defects and Lessons

The 13 real defects found while hardening the server, each with root cause, symptom, and fix.
**This is the highest-value page for predicting where the next bug is.** Every one lived in a
place that had no test, and several were *silent* — the server reported success while losing
data or disarming the safety gate.

The meta-lesson, stated once: **a green test suite over untested seams means nothing.** The way
these were found was to write a test asserting a *guarantee* (not the current output) for each
untested seam — threading, transports, locking, failure paths, hostile input — and watch it
fail.

---

<a id="d1"></a>
## D1 — Stale approval authorized later execution (1.4.0)
**Symptom:** approval popup never appeared, yet the agent executed. A `plan_state.json` from the
morning held a plan still marked `APPROVED`.
**Root cause:** `plan_and_think` redirected a *new, different* goal onto the old approved plan;
`request_user_approval(ASK_USER)` short-circuited with "already approved" so nothing blocked;
`update_task_progress` sailed through. A 5-hour-old approval licensed today's execution.
**Fix:** approval **expiry** (TTL, default 1800 s, unreadable timestamp = expired) checked at all
three entry points; a different goal no longer inherits an approval.

<a id="d2"></a>
## D2 — Approval window closed too early (1.5.0)
**Symptom:** approve/reject buttons vanished ~55 s after appearing, before the human answered.
**Root cause:** on tool-call timeout the server *cleared* the approval request.
**Fix:** leave the request published on timeout; collect a late click on the next tool call and
apply it (fingerprint-checked). The wait has a ceiling; the human does not.

<a id="d3"></a>
## D3 — Concurrent plan loss across processes (1.6.0)
**Symptom:** two near-simultaneous `plan_and_think` calls from different processes both got
`plan_20260724_0001`; one plan vanished from disk entirely (atomic write, so no corruption —
just a silent lost update).
**Root cause:** `Store.lock` is a `threading.Lock` — no effect across processes, and ghost
processes on one state dir are normal.
**Fix:** OS-level file lock (`state/.txnlock`) around the load-mutate-save cycle.

<a id="d4"></a>
## D4 — Per-process approval pages (1.7.0)
**Symptom:** approval requests raised by one process appeared on a port the human wasn't
watching, then timed out — the gate silently degraded to "asked, nothing stopped it".
**Root cause:** approval state lived in process memory; each process bound its own port.
**Fix:** shared `state/approval.json`; a singleton page elected by port binding with `/api/health`
peer detection and automatic takeover.

<a id="d5"></a>
## D5 — Transaction not mutually exclusive across threads (1.8.1)
**Symptom:** a test asserting mutual exclusion caught two threads inside the critical section at
once. Combined with D6's threaded transport, the D3 fix was *silently off* for same-process
concurrency.
**Root cause:** the nesting-depth counter was a shared instance attribute; a second thread saw
`depth != 0`, skipped the lock, and walked in.
**Fix:** per-thread depth and lock handle via `threading.local()`.
**Lesson:** re-entrant lock state must be thread-local, always.

<a id="d6"></a>
## D6 — Blocking approval stalled other sessions 52 s (1.6.0)
**Symptom:** an unrelated `get_current_plan` waited 52 s while another session's approval was
pending.
**Root cause (two):** (a) the store lock was held across the human wait; (b) the stdio read loop
handled requests inline, so nothing else was even *read*.
**Fix:** `store.paused()` releases the transaction during the wait (caller re-reads + re-verifies
after); the stdio loop handles each message on its own daemon thread (responses matched by id).

<a id="d7"></a>
## D7 — Single plan slot evicted concurrent sessions (1.8.0)
**Symptom:** two conversations working at once; the second's plan replaced the first's.
**Root cause:** a single `active_plan_id`.
**Fix:** plans coexist; route by explicit id / goal / only-active; `PLAN_AMBIGUOUS` + directory
when unclear; hints qualified with the plan_id. **Follow-on bug caught by the smoke test:** after
a blocking wait the handler resolved "the active plan" and picked up a *sibling*; fixed to re-read
by its own id.

<a id="d8"></a>
## D8 — File-lock timeout did nothing (1.8.1)
**Symptom:** a 0.3 s lock holder stalled the next caller ~10 s; the `timeout` argument was inert.
**Root cause:** Windows `msvcrt.locking(LK_LOCK)` blocks internally ~10 s before failing.
**Fix:** non-blocking attempts (`LK_NBLCK` / `LOCK_NB`) in a 50 ms retry loop honouring our own
timeout.

<a id="d9"></a>
## D9 — Permanent lock failure burned the whole timeout (1.8.1)
**Symptom:** the test suite took 33 s; a lock on a missing directory retried for 20 s.
**Root cause:** transient failure (peer holds file) and permanent failure (no such dir) were
treated the same.
**Fix:** a missing parent directory fails immediately; only genuine contention retries. Suite
33 s → 13 s.

<a id="d10"></a>
## D10 — SSE POST hung; no SSE heartbeat (1.8.1)
**Symptom:** under SSE, a blocking approval left the POST hanging until the client gave up;
approvals silently fell back to the 55 s no-token ceiling.
**Root cause:** `do_POST` ran `handle_message` *before* sending 202; and no notifier was wired
for SSE, so progress heartbeats were impossible.
**Fix:** send 202 first, run the work on a thread, deliver the result over the SSE stream; add
`_SseNotifier` that fans heartbeats to open streams.
**Lesson:** stdio was tested; SSE was not — so SSE was where the bug was.

<a id="d11"></a>
## D11 — Four "reported success on a failed write" defects (1.8.1)
Writing failure-path tests for `store`/`approval` failed **7 times immediately**; the common
shape was *"failed but answered success"*:
- **D11a** `Store.save` logged an `OSError` and returned → `ok:true` for a mutation never
  written; the model executes a plan the server has no record of. **Fix:** raise
  `StoreWriteError` → `INTERNAL_ERROR` + resync hint.
- **D11b** valid JSON of the wrong shape (e.g. `plans` a list) raised in `State.from_dict` on
  *every* call, wedging the server permanently on `INTERNAL_ERROR`. **Fix:** quarantine like any
  corrupt file, start empty.
- **D11c** `ApprovalStore.publish` / `record_decision` returned success when the write failed →
  a request nobody can answer; the gate silently disarmed. **Fix:** `_write` returns bool;
  `publish` returns `None` on failure → handler emits the loud `NOT hard-paused` warning.
- **D11d** a cancelled/completed/revised plan left a live-looking approval request on the page
  whose buttons did nothing (discarded on fingerprint). **Fix:** withdraw the plan's entry on
  every settling transition.
**Test lesson:** patch the *specific* store's `_write`, never `os.replace` globally — `os` is
shared, so a global patch breaks the other store and tests the wrong thing.

<a id="d12"></a>
## D12 — `normalize()` outside the guard; crashed on non-string keys (1.8.1)
**Symptom:** a crafted non-string key made `normalize` call `.lower()` on a non-str and raise —
and because `normalize` ran *before* dispatch's try block, it escaped as a raw JSON-RPC error
instead of the graceful `ok:false` a weak model needs.
**Fix:** move `normalize()` inside the exception guard (any future leniency bug now degrades
gracefully); drop non-string keys; reject NaN/inf for integer fields. Fuzzed against nested
objects, mixed types, nulls, 100k-char strings across all four tools — it must never raise.

---

<a id="d13"></a>
## D13 — Goal drift forked plans silently (1.8.2)
**Symptom:** during multi-step drafting, the model rephrasing its `goal` between steps created a
second (third, …) plan; at approval time the model hit `PLAN_AMBIGUOUS` with plans it didn't
know it had. A period-only difference ("find file" vs "find file.") also forked.
**Root cause:** routing was by exact goal string, and the model repeating the goal verbatim
every step is exactly the kind of discipline a weak model lacks.
**Fix:** use `step_number` as the start-vs-continue signal. `step==1` starts a new plan;
`step>1` with no goal match returns `GOAL_NOT_MATCHED` + the active-goal list so the model picks
the exact one (or sends `plan_id`), never forking. Goal matching ignores trailing punctuation.
**Lesson:** any routing key the *model* must reproduce is a key the model can forget — give it a
way to recover (a list to pick from) instead of guessing on its behalf.

<a id="d14"></a>
## D14 — Tasks planned but never executed; plan reported finished (1.9.0)
**Symptom (field report):** the small model drove one full plan-approve-update-recover cycle
correctly, then stopped doing the actual work: it marked tasks `DONE` in a row without
performing them and reported the plan complete.
**Root cause:** `DONE` was accepted unconditionally. Missing `IN_PROGRESS`, missing
`result_log`, and out-of-order completion were all *notes*, not refusals — and `all_done()`
awarded `COMPLETED` on the model's word alone. The server treated a **claim** as a **fact**.
**Fix, three layers:**
1. `DONE` is refused unless the task was started, all earlier tasks are finished, and
   `result_log` is real evidence (`TASK_NOT_STARTED` / `TASK_OUT_OF_ORDER` /
   `MISSING_RESULT_LOG`). Since `IN_PROGRESS` already enforces order, batch-marking becomes
   structurally impossible.
2. Evidence is judged by **content, not length** — an exact-match filter for bare success
   phrases ("완료", "done", "ok", repeating the task title) plus a low floor of 8 normalized
   characters. A first attempt used a 15-character minimum and wrongly rejected
   `"매출 표 12행을 추출함"`, a perfectly concrete Korean outcome; length is a bad proxy in a
   dense script.
3. `COMPLETED` is no longer reachable by the model: the last `DONE` moves the plan to
   `AWAITING_COMPLETION`, and a human must approve a per-task **completion report**. The
   fingerprint covers each task's status and `result_log`, so the evidence cannot be edited
   after the human saw it.
**Lesson:** the server cannot observe work, so anything the model *asserts* about work must
either carry evidence or be verified by a human. Accepting an unverifiable claim with a
warning note is the same as accepting it.

## Where the next bug probably is

Judging by the pattern (bugs cluster in untested seams and failure paths), the thinner-covered
areas still are:
- `models.py` serialization round-trips at the boundaries (unusual `from_dict` inputs, huge task
  counts, retention pruning interacting with multi-plan).
- `config.py` env-var parsing (already partly covered, but not every coercion path).
- The **SSE session lifecycle** under abrupt client disconnects (queues left in `_SseSessions`).
- Interaction of **retention pruning** (`max_plans`) with **many active plans** — pruning must
  never drop an active plan (there is a test; extend it for the multi-plan case).
