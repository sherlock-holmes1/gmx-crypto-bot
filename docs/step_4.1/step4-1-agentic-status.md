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
| 3c. Independent decision adapter and CLI | Developer | Tester | Accepted; one clause deferred to 3d | 3 | Final review accepted the stage after the third return. Added `crosscheck/block_prestate.py`: a fail-closed ArbOS system-transaction prestate proof that reads both block `B` and `B-1`, derives `equivalent_pin_block_hash` from the execution header's `parentHash`, and refuses when `timestamp(B-1) != timestamp(B)`. `PROVES` is narrowed to non-ArbOS contract storage; the ArbOS L1-view divergence (26020834 at 507206357 vs 26020836 at 507206358) and the `block.number`/`blockhash` caveat are recorded in `DOES_NOT_PROVE`. The equivalence pin is bound by number and hash. `agreement`/`disagreement` emission exists and is gated; the reject-side asymmetry is disclosed through mandatory `missing_rule_evidence`/`proved_rule_count`/`unproved_rule_count`, and the tester ruled the asymmetry sound. `--pin-at-prestate-boundary` pins the preflight only at the proved boundary; the tester ran 14 abuse cases and a forged proof, all refused, the forgery by the preflight's live on-chain hash re-read. Tamper battery: 15 cases, all rejected except a wholly rewritten transcript, which the live re-read catches. **First verified historical router call:** `passed_preflight` for the selected order at block 507206357, pending and latest, nonce 7727799. Suite: 325 passed, 3 skipped, 100 subtests. **Deferred clause:** "test both outcomes end to end with mock RPC" is NOT met and cannot be met in 3c; the accept-side branch is unreachable with the real inventory and the equivalence branch needs a sidecar at 507206357. Both tester and implementor recorded this as Stage 3d work. |
| 3d. Reproducible contract comparison | Implementor | Critic | Accepted; completion gate NOT met | 0 | A real, reproducible, boundary-pinned same-order run exists. GMX returned `passed_preflight` (raw revert `0x4e48dcda` = `EndOfOracleSimulation()`) for order `0xfd65a4c3...ff90` at block 507206357 / hash `0x6624873b...4af5`, pending and latest, nonce 7727799. All pinned evidence was recaptured at that boundary; creation-block files are preserved unchanged. Our model accepted every evaluable rule and rejected nothing, so neither route to a countable comparison opened: result is `gmx_preflight_only` / `independent_market_increase_rule_coverage_incomplete`, 9 of 15 rules proved, `completion_gate_met: false`. The sidecar trust gap is closed by `crosscheck/sidecar_replay.py`, which re-derives the router gate, 26 order fields, the position key and struct, the 27-cell inventory, OI aggregates and the feature flag from the raw transcript. Critic reproduced the run to a byte-identical digest (`b66df4c4...a147` excluding `run.utc_run_time`), re-issued all 83 `eth_call`s at the boundary (83/83 match) and at the creation block (83/83 identical, so the cross-block equality is a chain fact), defeated four forged deployment files, eight forged prestate proofs and a full sidecar tamper battery, and proved the rules genuinely execute by perturbing six inputs and getting six correctly-attributed `disagreement` results. Suite: 363 passed, 3 skipped, 122 subtests. |
| 4. Complete lifecycle examples | Developer | Tester | Pending | 0 | Depends on 3d for integrated reporting; existing September examples have oracle risk gaps. |
| 5. Integration and user acceptance | Implementor | Critic | Pending | 0 | Depends on Stages 1–4. |

No Step 4.1 contract comparison or complete long/short lifecycle has been accepted yet.

## Blocker for human decision: the completion gate is structurally one-sided

The Stage 3d critic established, by static analysis confirmed with a repo-wide grep,
that six of the fifteen `MARKET_INCREASE_RULES` are never appended to `proved`
anywhere in `src/`. `coverage()["missing_rule_evidence"]` therefore can never be
empty, so the terminal `agreement` branch in `decision_adapter.py` is unreachable
dead code. One of the six, `gas_and_keeper_checks`, needs `startingGas`,
`tx.gasprice`, gas-limit configuration and keeper role -- transaction-execution
context that an `eth_call` at a block boundary structurally never has.

**Consequence: Step 4.1's minimum completion gate can currently be met only by a
`disagreement` -- an order where our model rejects something GMX accepted. An
`agreement` is impossible for any order, at any pin, with any amount of further
evidence capture, while `gas_and_keeper_checks` stays in the inventory and the
preflight is a block-boundary `eth_call`.** More capture work against the other
five rules does not close the gate.

The reject-side route is live and verified: the critic produced six correctly
attributed `disagreement` results against the live chain by perturbing single
inputs. The selected order simply passes every rule the model can evaluate.

This needs a human decision before Stage 4 plans further capture work. The
options are to re-argue the inventory (is a keeper gas check in scope for a
preflight comparison at all?), to redefine the completion gate, or to search for
an order our model genuinely rejects -- which needs the per-order oracle capture
automated, since 99 of 100 candidates currently fail `verified_oracle_input_missing`.

The cross-check package refactor was independently accepted. The router, watcher,
candidate, report, adapter, and archive-reader modules now live in
`src/gmx_crypto_bot_v2/crosscheck/`; the CLI remains in `application/`.
The package move passed 22 runnable focused tests,
CLI smoke, compilation, and whitespace checks.

`pytest` 9.1.1 is now installed. `tests/v2/test_archive_state.py` is the only
module with a top-level `import pytest`, so it was the only module that could
not run earlier; the other focused modules were runnable and did run. On its
first execution it failed. An independent review traced the failure to an
over-strict test fixture, not to production code: `archive_state.py` is
blob-identical to HEAD, and the real pinned transcript plus the captured Reader
and DataStore bytecode confirm that `getOrder`/`getPosition` belong to the
Reader and the cell getters to the DataStore. The fixture now asserts the
correct target per call type and survived eight independent mutations. Stage 3a's
recorded conclusion stands and its evidentiary basis did exist, through
`tests/v2/test_archive_sidecar.py` and the real archive capture. Two known
fixture gaps remain: the second ABI word is never asserted, so swapping the
position key or the two Reader signatures would still pass.

The full `tests/v2` suite is green: 320 passed, 3 skipped, 91 subtests passed.

The three earlier user-authorized additional Stage 3a loops and two later
source-verification loops are complete. The real
capture is [long-increase-archive-sidecar.json](../../evidence/step4-1/long-increase-archive-sidecar.json).
Its [source manifest](../../evidence/step4-1/historical-source-manifest.json) binds
five observed runtime code hashes to Solidity metadata CIDs. The
[Sourcify proof](../../evidence/step4-1/sourcify-source-proof.json) binds saved
exact-match source and ABI records to that manifest and sidecar by digest.
The tester accepted this evidence set for the selected long increase. The raw
sidecar's historical-source and referral flags predate the separate proof and
remain stale; Stage 3c must join the proof by digest before clearing them.
An independent compiler rebuild is still absent. Oracle inputs, final token
delta, and equivalent execution context remain work for Stages 3b–3d.
