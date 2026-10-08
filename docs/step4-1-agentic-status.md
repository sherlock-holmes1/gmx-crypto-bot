# Step 4.1 agentic status

Flow: [Step 4.1 agentic flow](step4-1-agentic-flow.md). The primary agent owns this state. Return counts do not reset within a stage.

| Stage | Implementor type | Critic type | Status | Returns used | Evidence or blocker |
|---|---|---|---|---:|---|
| 0. Design and evidence contract | Implementor | Critic | Accepted | 1 | Critic verified raw-log decoding on a September order and accepted the corrected frozen-state gate; archive/deployment and price scale remain explicit runtime evidence gates. |
| 1. Candidate and evidence selection | Developer | Tester | Accepted | 3 | Final tester accepted real-schema decoding, ordered updates, coordinate-scoped gaps, and 9 focused tests. September scan: 2,975 creations; 2,910 target-category candidates remain unverified by RPC. |
| 2. GMX contract preflight | Developer | Tester | Accepted | 1 | Tester accepted pinned DataStore request proof, archive-only field labels, timeout classification, and 8 focused tests. No verified historical call: deployment, price units, and archive access remain gaps. |
| Former 3. Same-order comparison and report | Developer | Tester | Unaccepted; replaced by proposed 3a–3d | 3 + 2 extra cycles | Review history retained. Rule-diagnostic safety correction accepted; 22 focused router tests pass. No concrete pinned reader, CLI adapter wiring, or verified full same-order comparison. |
| 3a. Pinned archive-state reader | Developer | Tester | Partial capture and source verifier accepted; stage open | 3 + 2 earlier extra cycles; 4 new developer–tester loops, with 3 corrections | Tester accepted the bounded Reader timestamp correction, pinned referral capture, five-contract source manifest, and offline source-bundle verifier. Real archive capture proves the selected long increase was pending and globally latest at block 507206345. Public Reader metadata fetch returned HTTP 403; historical source/ABI/key-layout proof is still missing and `ready_for_comparison: false`. |
| 3b. Oracle evidence capture | Developer | Tester | Pending | 0 | Depends on 3a; no verified historical or watched oracle input source yet. |
| 3c. Independent decision adapter and CLI | Developer | Tester | Pending | 0 | Depends on 3a–3b; existing narrow rule adapter is not a full execution decision and is not wired to CLI. |
| 3d. Reproducible contract comparison | Implementor | Critic | Pending | 0 | Depends on 3a–3c and real archive/oracle access; no verified same-order result. |
| 4. Complete lifecycle examples | Developer | Tester | Pending | 0 | Depends on 3d for integrated reporting; existing September examples have oracle risk gaps. |
| 5. Integration and user acceptance | Implementor | Critic | Pending | 0 | Depends on Stages 1–4. |

No Step 4.1 contract comparison or complete long/short lifecycle has been accepted yet.

The cross-check package refactor was independently accepted. The router, watcher,
candidate, report, adapter, and archive-reader modules now live in
`src/gmx_crypto_bot_v2/crosscheck/`; the CLI remains in `application/`.
Stage 3a remains unaccepted. The package move passed 22 runnable focused tests,
CLI smoke, compilation, and whitespace checks. The pytest-only archive-state
test could not run because pytest is not installed.

The three earlier user-authorized additional Stage 3a loops and one later
source-verification loop are complete. The real
capture is [long-increase-archive-sidecar.json](../evidence/step4-1/long-increase-archive-sidecar.json).
Its [source manifest](../evidence/step4-1/historical-source-manifest.json) binds
five observed runtime code hashes to Solidity metadata CIDs but does not verify
the historical source or ABI. Stage 3a cannot be marked accepted until that
proof and the remaining required state mapping are independently reviewed.
The later loop added an offline verifier for a future source bundle; its tester
accepted the identity binding and path checks after two corrections. The
[public fetch attempt](../evidence/step4-1/public-source-fetch-attempt.json)
returned HTTP 403 and supplied no metadata or source files.
