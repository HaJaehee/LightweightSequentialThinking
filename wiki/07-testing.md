# 07 · Testing

**147 unit tests + 5 end-to-end smoke tests, all passing** (as of 1.8.1). Standard-library
`unittest` only. This project's defect history proves the rule: *test the seam before you
trust it.* See [09-defects-and-lessons.md](09-defects-and-lessons.md).

## Running

```bash
python -m unittest discover -s tests          # unit suite (~13 s)
python tests/smoke_stdio.py                    # stdio end-to-end
python tests/smoke_blocking_approval.py        # blocking approval, real subprocess
python tests/smoke_shared_approval.py          # one page across 2 processes
python tests/smoke_multi_plan.py               # concurrent plans, 2 processes
python tests/smoke_sse.py                       # SSE transport end-to-end
```

`python tools/verify_install.py` runs the unit suite **and all five smoke tests** as part of
the GO/NO-GO acceptance check on the target machine. Set `PYTHONUTF8=1` on Windows.

## Unit suite layout (`tests/test_server.py`)

One file, `HandlerTestCase` base (fresh temp state dir per test, blocking approval disabled so
tests drive the two-phase path). Test classes, roughly:

| Class | Covers |
|---|---|
| `TestLeniency` | input repair + hostile-input fuzzing (never raises) |
| `TestGuardRails` | schema guards (missing task_list, step normalization, revises_step, …) |
| `TestStateMachine` | transitions, gate enforcement, redirects, idempotency |
| `TestStaleApproval` | approval expiry, binding, new-goal-doesn't-inherit |
| `TestBlockingApproval` | blocking wait, timeout, late decision, fingerprint, UI degradation |
| `TestConcurrencyEdges` | transaction mutual-exclusion, reentrancy, `paused()`, intra-process no-loss |
| `TestMultiPlanEdges` | routing, sibling isolation, ambiguity, max-active, pruning |
| `TestApprovalQueueEdges` | queue semantics, one-decision-wins, concurrent publish, migration |
| `TestStoreFailureEdges` | lost-write reported as failure, corruption quarantine |
| `TestApprovalStoreFailureEdges` | publish/decide failure reporting, stale-entry withdrawal |
| `TestApprovalPageSurface` | the page HTML/JS/endpoints (browser-verified separately) |
| `TestLeniencyDispatchEdges` | dispatch survives every hostile input with the contract |
| `TestPlanStateEdges`, `TestFileLockEdges`, `TestProtocol` | misc edges, locking, JSON-RPC |

## Smoke tests (real subprocesses, real HTTP)

These spawn `server.py` exactly as AnythingLLM does and speak JSON-RPC over the pipe. They
catch things unit tests cannot (threading, real ports, cross-process locks):

- **smoke_stdio** — full lifecycle + sloppy-input recovery + Korean round-trip + restart persistence.
- **smoke_blocking_approval** — 6 checks: tool blocks, heartbeat with token, click→APPROVED,
  safe timeout without token, request survives timeout, blocking doesn't stall other sessions.
- **smoke_shared_approval** — 2 processes, one page, request from the non-owner shows on the
  page, decision reaches the asker, owner death → automatic port takeover.
- **smoke_multi_plan** — 2 processes, separate plans, both approvals on one page, each unblocks
  independently, sibling untouched.
- **smoke_sse** — SSE session, POST returns 202 immediately during a blocking approval,
  heartbeat over the stream, decision arrives over the stream.

## How to add a test that finds a real bug (the method that worked)

1. Pick a seam with no coverage (a failure path, a concurrency boundary, a transport).
2. Write the test that asserts the **guarantee**, not the current output (e.g. "a lost write is
   never reported as success", "the transaction is mutually exclusive across threads").
3. Run it. In this project that step found 13 defects. If it passes first try, you've documented
   a guarantee for free.
4. For concurrency, use `threading.Barrier`/`Event` to force real overlap; for failure paths,
   `mock.patch.object` the *specific* store's `_write` (never `os.replace` globally — `os` is
   shared and you'll break the other store and test the wrong thing).

## Browser verification (manual, for the approval page)

The page's HTML/JS was verified in a real browser: multi-request rendering, XSS escaping (no
execution, no console error), tab-title alert, textarea comment, all three buttons → POST →
recorded decision, `\'` onclick escaping routing to the correct id, queue removal after
decision. `TestApprovalPageSurface` guards the template against regression without a browser.
Re-run the manual browser check if you change `_PAGE` in `approval.py`.
