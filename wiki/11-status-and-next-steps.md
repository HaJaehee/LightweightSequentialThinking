# 11 · Status and Next Steps

## Current state (as of version 1.8.1)

- **Code:** feature-complete for the design. All four tools, blocking approval, multi-plan,
  shared approval surface, cross-process locking, failure-path hardening, leniency fuzzing.
- **Tests:** 147 unit + 5 smoke, all passing. `verify_install.py` runs everything.
- **Git:** committed on `main` up to `6d3748b`. Remote is
  `github.com/HaJaehee/LightweightSequentialThinking`. **NOT pushed.** Pushing is a user
  decision — the repo may be public and `docs/` contains deployment/security material; confirm
  before pushing.
- **Build artifacts:** `dist/planning-mcp-1.8.1-20260725*.zip` (source-only ~110 KB, and a
  `-with-python` bundle ~13 MB). Hashes change on every rebuild — always re-record after
  building. Current hashes are in the last build output, not hard-coded here.

## Known open items

1. **Live install is behind.** `D:\planning-mcp` (the AnythingLLM-registered install) lagged
   several versions during development. Before relying on it, redeploy the current build:
   re-run `setup_runtime.py` then `verify_install.py` (expect GO). The `_pth` patch reverts on
   re-extraction — `verify_install` detects it, `setup_runtime` repairs it.
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
