# 11 · Status and Next Steps

## Current state (as of version 1.12.0)

- **Code:** feature-complete for the design. All four tools, blocking approval, multi-plan,
  shared approval surface, cross-process locking, failure-path hardening, leniency fuzzing,
  per-task plan review (1.10.0), auto-advance (1.11.0), goal revision (1.12.0).
- **Tests:** 237 unit + 5 smoke, all passing. `verify_install.py` runs everything.
- **Git:** on `develop`. Remote is `github.com/HaJaehee/LightweightSequentialThinking`.
  **NOT pushed.** Pushing is a user decision — the repo may be public and `docs/` contains
  deployment/security material; confirm before pushing.
- **Build artifacts (1.12.0, built 2026-07-28):**

  | archive | size | sha256 |
  |---|---|---|
  | `dist/planning-mcp-1.12.0-20260728.zip` | 184,383 B | `8f976bc0618327bde7fa24e27ace231a62fcbd2bf92bbf617bb2213e4692f120` |
  | `dist/planning-mcp-1.12.0-20260728-with-python.zip` | 13,160,512 B | `db3f87fe3469673cfab1636ef2d0166178b23a44ccd8e62a9c6533df58942845` |

  Both were built from commit `74572a3`. The source-only archive was extracted to a scratch
  directory and `verify_install.py` returned **GO** there: 31 manifest files match, all modules
  import, 237 unit + 5 smoke tests pass. The `--with-python` variant is current again (1.11.0
  never had one).

  Every archive embeds its own `MANIFEST.txt`, which carries a build timestamp — so **the zip
  hash changes on every rebuild even when no source changed.** Re-record after building, and
  hand the hash to the corporate side out-of-band.
- **Live install:** `D:\planning-mcp` is **still on 1.11.0** — 1.12.0 has not been synced there.
  The 1.11.0 sync (2026-07-28) verified **GO** with its bundled runtime: 31 manifest files match,
  228 unit + 5 smoke tests pass. `state/` and `runtime/` were not touched by that sync; the
  previous code is at `D:\planning-mcp.backup-1.10.0-20260728-110855`.

  **The running processes stay on the old code until AnythingLLM restarts them** — Python reads a
  module once at import, so overwriting the files changes nothing already in memory. After
  syncing to 1.12.0, restart AnythingLLM and **repaste the agent prompt**: an agent still running
  the 1.11.0 prompt never sends `revised_goal`, so a user correcting the goal still leaves the
  old goal in the metadata — the server-side fix alone does not produce the behaviour.

## Known open items

1. **`make_package --with-python` clobbers the repo's `MANIFEST.txt`.** It appends a
   `runtime/python-3.x-embed-amd64.zip` line, which is correct *inside that archive* but names a
   path the repo working tree does not have — so a later `verify_install.py` in the repo reports
   `[FAIL] missing: runtime/...`. Workaround: build the `--with-python` variant **first** and the
   source-only variant **last**, so the repo is left with a manifest that matches it. A real fix
   would write the archive manifest without touching the repo copy.
2. **Push decision** — see above.
3. **AnythingLLM progressToken behaviour is unconfirmed.** Claude Code sends none (55 s
   ceiling). If AnythingLLM also sends none, blocking approvals have a 55 s window per call;
   check the stderr `heartbeat on/off` line in the field. If it's a problem, the mitigation is a
   client that supplies a progressToken, not a server change.

## Where to look for the next bug

From [09](09-defects-and-lessons.md#where-the-next-bug-probably-is): thinner-covered seams are
`models.py` serialization boundaries, `config.py` env parsing, SSE session cleanup on abrupt
disconnect, and retention pruning under many active plans. The reliable method: write the test
that asserts the guarantee, watch it fail.

## Possible future work (not started, design notes only)

- **Structural edits under per-task review.** 1.10.0 deliberately excludes add/delete/reorder
  because they renumber `task_id` and break the ordering invariants in `can_start_task` /
  `unfinished_before`. Doing it properly means a stable task identity separate from the ordering
  key, which is a real change to `models.py` and every id the model holds. Only worth it if the
  field shows people reaching for 계획 전체 재작성 mainly to add one step.
- **Measure whether models actually use `task_updates`.** The audit log records
  `targeted_revision_ignored` every time a model answers a targeted request with a whole
  `task_list`. That count against `tasks_revised` is the metric; if it stays high for the
  corporate model, the fix is prompt/hint wording, not more server logic.

- **Gate execution itself.** The gate governs our own tools; the model executes with other
  AnythingLLM skills we can't intercept. To close that, execution would have to become one of
  *our* tools (e.g. `execute_step` checking `plan_status == APPROVED`). Significant redesign;
  only worth it if disabling other skills during bring-up proves insufficient.
- **Retention/pruning polish** for the multi-plan era (ensure pruning never touches an active
  plan under `max_active_plans` pressure; there is a base test to extend).

## Hard rules for whoever continues (repeat of README, because they matter)

1. Zero third-party dependencies. Standard library only.
2. Never crash a call — always the `ok/plan_status/next_action/next_action_hint` contract.
3. Never report a lost write as success.
4. The approval gate is enforced, not requested; blocking approval physically pauses the loop.
5. Approval binds to the exact plan version the human saw (fingerprint).
6. Tool schemas are generated from the enums in `models.py` — keep them in sync there.
7. **Before trusting a seam, write the test that fuzzes it.** This is how every real bug here
   was found.

## Fast orientation for a fresh AI session

1. Read [README.md](README.md) and this page.
2. Skim [09-defects-and-lessons.md](09-defects-and-lessons.md) — it's the fastest way to
   understand what's fragile and why the code looks the way it does.
3. `python -m unittest discover -s tests` to confirm a green baseline (~13 s).
4. For any change touching concurrency/approval, run the relevant smoke test too.
