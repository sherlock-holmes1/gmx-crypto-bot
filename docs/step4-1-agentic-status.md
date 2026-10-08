# Step 4.1 agentic status

Flow: [Step 4.1 agentic flow](step4-1-agentic-flow.md). The primary agent owns this state. Return counts do not reset within a stage.

| Stage | Implementor type | Critic type | Status | Returns used | Evidence or blocker |
|---|---|---|---|---:|---|
| 0. Design and evidence contract | Implementor | Critic | Accepted | 1 | Critic verified raw-log decoding on a September order and accepted the corrected frozen-state gate; archive/deployment and price scale remain explicit runtime evidence gates. |
| 1. Candidate and evidence selection | Developer | Tester | Accepted | 3 | Final tester accepted real-schema decoding, ordered updates, coordinate-scoped gaps, and 9 focused tests. September scan: 2,975 creations; 2,910 target-category candidates remain unverified by RPC. |
| 2. GMX contract preflight | Developer | Tester | Accepted | 1 | Tester accepted pinned DataStore request proof, archive-only field labels, timeout classification, and 8 focused tests. No verified historical call: deployment, price units, and archive access remain gaps. |
| Former 3. Same-order comparison and report | Developer | Tester | Unaccepted; replaced by proposed 3a–3d | 3 + 2 extra cycles | Review history retained. Rule-diagnostic safety correction accepted; 22 focused router tests pass. No concrete pinned reader, CLI adapter wiring, or verified full same-order comparison. |
| 3a. Pinned archive-state reader | Developer | Tester | Accepted for selected long increase | 3 + 2 earlier extra cycles; 5 new developer–tester loops, with 3 corrections | Tester accepted the regenerated pinned sidecar plus digest-linked five-contract Sourcify exact-match source proof. The 13-slot Reader decoder and per-order UI fee factor are corrected. Pending/latest and pinned state gates pass at block 507206345. Sidecar remains `ready_for_comparison: false` pending Stages 3b–3d. |
| 3b. Oracle evidence capture | Developer | Tester | Accepted | 1 | Tester reproduced creation and execution transaction WETH/USDC integer price sets from recorded logs and pinned historical Oracle code/source. Execution prices precede `OrderExecuted`; their age is 1 second against a pinned 300 second maximum. Bounded watch validates chain, block and log scope. Equivalence of pre-execution state remains Stage 3c–3d. |
| 3c. Independent decision adapter and CLI | Developer | Tester | Bounded gate accepted; stage open | 1 | CLI joins pinned sidecar, source, oracle, increase-executor and SwapHandler proofs; tester accepted exact order identity and pin binding after one return. The selected order still reports `gmx_preflight_only` because multiple MarketIncrease rules lack pinned inputs and observed execution has no equivalent intra-block prestate proof. |
| 3d. Reproducible contract comparison | Implementor | Critic | Pending | 0 | Depends on 3a–3c and real archive/oracle access; no verified same-order result. |
| 4. Complete lifecycle examples | Developer | Tester | Pending | 0 | Depends on 3d for integrated reporting; existing September examples have oracle risk gaps. |
| 5. Integration and user acceptance | Implementor | Critic | Pending | 0 | Depends on Stages 1–4. |

No Step 4.1 contract comparison or complete long/short lifecycle has been accepted yet.

The cross-check package refactor was independently accepted. The router, watcher,
candidate, report, adapter, and archive-reader modules now live in
`src/gmx_crypto_bot_v2/crosscheck/`; the CLI remains in `application/`.
The package move passed 22 runnable focused tests,
CLI smoke, compilation, and whitespace checks. The pytest-only archive-state
test could not run because pytest is not installed.

The three earlier user-authorized additional Stage 3a loops and two later
source-verification loops are complete. The real
capture is [long-increase-archive-sidecar.json](../evidence/step4-1/long-increase-archive-sidecar.json).
Its [source manifest](../evidence/step4-1/historical-source-manifest.json) binds
five observed runtime code hashes to Solidity metadata CIDs. The
[Sourcify proof](../evidence/step4-1/sourcify-source-proof.json) binds saved
exact-match source and ABI records to that manifest and sidecar by digest.
The tester accepted this evidence set for the selected long increase. The raw
sidecar's historical-source and referral flags predate the separate proof and
remain stale; Stage 3c must join the proof by digest before clearing them.
An independent compiler rebuild is still absent. Oracle inputs, final token
delta, and equivalent execution context remain work for Stages 3b–3d.
