# Step 4.1 agentic status

Flow: [Step 4.1 agentic flow](step4-1-agentic-flow.md). The primary agent owns this state. Return counts do not reset within a stage.

| Stage | Implementor type | Critic type | Status | Returns used | Evidence or blocker |
|---|---|---|---|---:|---|
| 0. Design and evidence contract | Implementor | Critic | Accepted | 1 | Critic verified raw-log decoding on a September order and accepted the corrected frozen-state gate; archive/deployment and price scale remain explicit runtime evidence gates. |
| 1. Candidate and evidence selection | Developer | Tester | Accepted | 3 | Final tester accepted real-schema decoding, ordered updates, coordinate-scoped gaps, and 9 focused tests. September scan: 2,975 creations; 2,910 target-category candidates remain unverified by RPC. |
| 2. GMX contract preflight | Developer | Tester | Accepted | 1 | Tester accepted pinned DataStore request proof, archive-only field labels, timeout classification, and 8 focused tests. No verified historical call: deployment, price units, and archive access remain gaps. |
| Former 3. Same-order comparison and report | Developer | Tester | Unaccepted; replaced by proposed 3a–3d | 3 + 2 extra cycles | Review history retained. Rule-diagnostic safety correction accepted; 22 focused router tests pass. No concrete pinned reader, CLI adapter wiring, or verified full same-order comparison. |
| 3a. Pinned archive-state reader | Developer | Tester | Human decision required | 3 | Final tester review rejected Stage 3a. Position key, side, opening-position handling, and pinned Reader checks improved; snapshot remains incomplete. Caller-selected cell inventory, missing config/OI/liquidity/impact/accrual/referral/virtual/feature mapping, absent adapter point, and weak independent Reader tests block acceptance. Return limit exhausted. |
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
