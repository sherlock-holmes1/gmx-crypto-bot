# V2 code review — 2026-09-28

Scope: `src/gmx_crypto_bot_v2`, 116 Python files and 12,304 lines at review time.
This is a source review with focused local reproductions, not a formal protocol
or security audit. Production code and recording data were not changed for this
review. The companion [file guide](v2-code-map.md) explains every source file.

## Assessment

The separation between collection, evidence storage, reconstruction, models, and
checks is useful. Exact integer arithmetic, explicit missing evidence, preserved
raw responses, immutable block references, a recording writer lock, and resumable
publication are sound foundations. Two real recordings have passed the implemented
checks, and the existing V2 suite passes 141 tests.

However, a passing report is not yet sufficient evidence that all supported
cases are independently verified. Recent fixes contain proof shortcuts, and
storage changes introduced migration and cache-identity gaps. Readability has
improved from the original package, but the rewrite still carries large procedural
modules, weakly typed dictionaries, and many compatibility surfaces.

Priority: **P1** can undermine evidence validity or damage an in-use derived
store; **P2** causes an operational failure, misleading result, or significant
maintenance cost. Findings marked reproduced were demonstrated locally without
network calls. Other findings are source-level failure paths or design risks;
they are not claims of observed failures in the downloaded recording.

## Findings

### 1. P1 — Zero-size cancellation can pass without checking the actual position

Location: [`checks/orders.py:450`](../src/gmx_crypto_bot_v2/checks/orders.py#L450).

The `InvalidPositionSizeValues(0,0)` branch checks only the order type, zero
requested size delta, and two values embedded in the cancellation reason. It
ignores `opening_positions` and `position_events`, unlike the adjacent decrease
size branch. An existing nonzero position therefore does not prevent `matched`.
A zero size delta does not itself establish that the position has zero size.

**Reproduced:** provide a matching active opening position with `sizeInUsd=100`
and `sizeInTokens=10`, an increase request with zero delta, and the encoded `(0,0)`
reason. The function returns `matched`. It also returns `matched` when position
evidence is absent. This can help clear the overall completeness gate without
independently reconstructing the cause.

Recommendation: reconstruct the pre-attempt position and the attempted state
transition; require agreement with both error arguments. If the state cannot be
established, return `unavailable`. Add tests with nonzero and unavailable prior
positions, not only altered request fields.

### 2. P1 — RPC cache entries are not tied to the block hash they describe

Locations: [`sources/request_store.py:12`](../src/gmx_crypto_bot_v2/sources/request_store.py#L12),
[`sources/resumable.py:26`](../src/gmx_crypto_bot_v2/sources/resumable.py#L26),
[`collection/window.py:35`](../src/gmx_crypto_bot_v2/collection/window.py#L35).

The cache key includes method and parameters, with a role namespace. An `eth_call`
at block number N is therefore reusable without storing N's block hash. Headers
are deliberately fetched again, but the cached response is not compared with the
header from the attempt that created it.

One exposure is a failed rolling-window selection: partial calls are cached before
the resolved spec is published. A retry after a changed block at the same height
can combine fresh headers with old number-bound state reads. The before/after
header check then sees a stable *new* hash and does not detect stale cached state.
The same risk applies to other partially collected number-bound historical reads.

Recommendation: bind reusable state reads to chain ID, block number, and hash;
reject or invalidate entries after a hash change. Keep transport IDs outside the
semantic key. Add an interrupted-capture/reorg/resume test. This is a source-level
risk, not a reorg observed in the current recording.

### 3. P1 — Catalog recovery treats operational SQLite errors as corruption

Location: [`evidence/catalog.py:55`](../src/gmx_crypto_bot_v2/evidence/catalog.py#L55).

`_connect()` catches all `sqlite3.DatabaseError` exceptions, unlinks the catalog,
and creates another. `sqlite3.OperationalError`, including lock-related errors,
is a subclass of `DatabaseError`. A failed integrity query caused by contention
can therefore enter the destructive recovery path. Replay and validation do not
share the collector's writer lock around this cache replacement.

Recommendation: distinguish confirmed corruption/incompatible schema from busy,
locked, permission, and I/O failures. Coordinate replacement with a cache lock;
build a replacement separately and atomically publish it. Do not unlink a database
merely because opening/checking it failed. This failure path was identified in
source; an actual busy-database race was not induced against the real catalog.

### 4. P2 — Single requests and one-item batches collide in the RPC cache

Locations: [`sources/request_store.py:12`](../src/gmx_crypto_bot_v2/sources/request_store.py#L12),
[`sources/resumable.py:32`](../src/gmx_crypto_bot_v2/sources/resumable.py#L32).

`request_key(request)` and `request_key([request])` are identical. Replay remaps
IDs but preserves the cached response envelope. A single call can receive a list,
or a batch can receive a dictionary.

**Reproduced:** save a successful one-item `eth_call` batch, then execute the same
single `eth_call` through `ResumableRpc`. The client raises
`AttributeError: 'list' object has no attribute 'get'` without contacting the network.

Recommendation: include envelope shape in the cache key, or store normalized
individual responses and rebuild the requested envelope. Version the cache-key
format and cover both directions in tests.

### 5. P2 — Trace migration rejects legitimate legacy diagnostic files

Location: [`evidence/trace_store.py:77`](../src/gmx_crypto_bot_v2/evidence/trace_store.py#L77).

Migration treats every `*.json` file as a transaction trace or gas probe and
requires a 66-character transaction-hash filename. Older layouts can contain
`verification.json`; existing V2 compatibility tests themselves use that filename.
A resume of such a recording now stops with `invalid legacy trace filename`.

**Reproduced:** a legacy trace directory containing `verification.json` is rejected.
The file is preserved, but automatic migration cannot finish.

Recommendation: explicitly classify trace, gas-probe, and diagnostic/report files.
Migrate evidence; retain or relocate known reports. Report unknown files clearly
without confusing them with corrupt transaction evidence. Test mixed directories.

### 6. P2 — Inferred multichain payouts do not prove the requested route

Location: [`checks/decrease.py:132`](../src/gmx_crypto_bot_v2/checks/decrease.py#L132).

Any matching receipt credit with a falsey `srcChainId` selects the multichain path.
This is not limited to opening orders or unavailable route data. The same flag
also bypasses the credit's source-chain comparison. Amounts and the vault transfer
are still checked, but route correctness is inferred from the observed result.

Recommendation: decode the route from order data/calldata when available. Distinguish
“observed payout reconciles” from “requested route independently verified.” Missing
route information should be explicit rather than implicitly upgrading the result.
This is a proof-strength limitation, not evidence that the observed payout was wrong.

### 7. P2 — Completeness gates require event families even when none apply

Locations: [`application/validation.py:217`](../src/gmx_crypto_bot_v2/application/validation.py#L217),
[`domain/constants.py`](../src/gmx_crypto_bot_v2/domain/constants.py).

Configuration completeness requires a nonempty, exclusively `matched` counter
for every named configuration check. The liquidation gate is removed only if a
nonempty liquidation-settlement counter is exclusively matched. A valid window
with no swaps or no liquidations can stay incomplete because those checks never
ran, rather than because evidence or economics failed.

Recommendation: define applicability from discovered order/event types, and model
`not_applicable` explicitly at aggregate level. Test empty and short windows,
no-swap recordings, and no-liquidation recordings.

### 8. P2 — Staging accumulation remains in the collector code

Locations: [`collection/coordinator.py:311`](../src/gmx_crypto_bot_v2/collection/coordinator.py#L311),
[`collection/coordinator.py:379`](../src/gmx_crypto_bot_v2/collection/coordinator.py#L379).

An interrupted attempt creates a new `base-UUID` directory next time. Successful
publication copies all files and leaves its staging directory behind. Capture
directories are also created on resumed runs. Cached responses prevent repeated
network downloads, but reconstructed raw bundles and staging copies still grow.

The three old base directories were manually removed in this session; that cleanup
did not change this behavior for future runs.

Recommendation: implement explicit staging ownership and retention. After verified
publication and journal commit, garbage-collect unreferenced staging files under
the recording lock. Preserve failed-attempt diagnostics in a bounded form. Test
cleanup after success and restart at each publication boundary.

### 9. P2 — Report output can destroy a previously valid report on interruption

Locations: [`application/validation.py:288`](../src/gmx_crypto_bot_v2/application/validation.py#L288),
[`application/replay.py`](../src/gmx_crypto_bot_v2/application/replay.py).

The CLIs write final reports directly with `Path.write_text`. An interruption or
full disk after truncation can replace a good report with partial JSON. The project
already has durable atomic publication helpers, but the report writers do not use
them.

Recommendation: publish reports through the existing temporary-file, fsync, and
rename mechanism. Keep the previous report until the replacement is complete.

## Architecture and readability weaknesses

- **Large coordinators remain.** `collection/coordinator.py` is 618 lines,
  `checks/trace.py` 529, and `checks/orders.py` 496. Stage discovery, persistence,
  validation, and status reporting are interleaved. Split into stage objects with
  explicit inputs, outputs, and resumability contracts.
- **Dictionary contracts dominate.** `ValidationContext` exposes many bare `dict`,
  `list`, and `Any` fields. `OrderContext` is frozen but contains mutable mappings,
  and callers can provide lists where tuples are annotated. `CheckResult` and
  `EvidenceReference` exist but are not the general reporting contract. Use typed
  records at storage, reconstruction, and checking boundaries.
- **Compatibility surfaces were extensive.** The original review found 27 root
  adapter modules and a separate `execution_fees/` facade. They have since been
  removed; only three small root module CLIs remain. Future imports should target
  the layered implementation modules directly.
- **“Pure” does not mean independent.** For example, `models/decrease.py` consumes
  observed `basePnlUsd`, `totalImpactUsd`, and fee totals, then reconciles settlement.
  Other checks validate several of those inputs, but this dependency chain is
  implicit. Document which values are independent inputs, independently derived
  values, or observed outcomes; avoid describing every model as an independent proof.
- **Hidden repository context.** `evidence/repository.py` uses a `ContextVar` and
  changes helper behavior depending on whether a session is active. This preserves
  compatibility but makes dependencies and repeated reads difficult to see. Prefer
  an explicit repository interface in reconstruction objects.
- **Deployment assumptions are scattered.** Arbitrum addresses, a bytecode-specific
  gas profile, ABI layouts, and selector strings occur across modules. A new market,
  deployment, or protocol upgrade needs a versioned deployment/configuration boundary.
- **No schema lifecycle for durable SQLite stores.** `request_store.py` and
  `trace_store.py` use `CREATE TABLE IF NOT EXISTS` without a store schema version or
  explicit migration registry. Unlike the catalog, these stores hold durable evidence
  and cannot simply be discarded when their layout changes.

## Performance and operational weaknesses

- `application/validation.py` materializes reconstructed histories, orders, per-order
  evidence, and the full output report. SQLite indexing has not made validation
  bounded-memory. Benchmark longer recordings before treating a week as scalable.
- `EvidenceRepository.receipts()` scans every stored receipt response and filters
  hashes in Python. Header access follows a similar pattern. Store/query transaction
  hash and block number explicitly and retrieve only required rows.
- Both new SQLite stores open a connection for each get/put. Collection can decompress
  the same trace through journal verification, store loading, and digest calculation.
  Use scoped connections and reuse verified decoded data within an operation.
- The entire JSON journal is rewritten and fsynced after each trace completion;
  with N trace units, cumulative serialized journal work grows approximately as N².
  A transactional progress table or append-only journal would scale better.
- Responses may exist in raw capture bundles, the request database, and normalized
  trace records containing receipts and traces. Some duplication serves provenance,
  but no explicit retention/compaction policy describes what may be reclaimed.
- Progress messages are stronger for collection than validation. Long silent validation
  phases caused repeated status polling during the recent debugging work. Report
  phase names and processed/total counts with throttled output.
- `SourceError.safe_detail` protects endpoints, but JSON-RPC error responses often
  fall back to a generic message. Preserve method, safe provider code, role, and a
  bounded/redacted category, without dumping credentials or provider response text.

## Validation performed for this review

- Existing V2 suite: **141 tests passed** in approximately 5.5 seconds.
- Local reproductions: false-positive zero-size reason; single/batch cache collision
  and resulting exception; rejection of legacy `verification.json`.
- Source inventory: **116 Python files at review time**, including **27 compatibility modules since removed**.
- No new archive collection or full-recording validator run was needed for this
  read-only review. Earlier successful recording regressions remain useful but do
  not cover the reproduced counterexamples.

## Suggested implementation order

1. Tighten cancellation and payout proof contracts; add negative tests that deliberately
   contradict the reason bytes or requested route.
2. Correct RPC cache shape and block identity; make cache recovery safe under contention.
3. Make trace migration tolerate real legacy directory contents; version durable stores.
4. Add automatic verified staging cleanup and atomic report publication.
5. Define applicability-aware completeness, typed check results, and a small public API.
6. Profile indexed reads and report assembly, then remove repeated scans/decompression
   and full-journal rewrites based on measurements.

These are recommendations for follow-up work, not changes implemented by this review.
