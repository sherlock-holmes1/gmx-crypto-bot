# Stage 3a: fixed archive inventory for one increase case

Status: **partial archive snapshot for one selected long `MarketIncrease`; no pinned economic point or comparison certificate**. The saved sidecar at `evidence/step4-1/long-increase-archive-sidecar.json` proves that this order was pending and the latest global request at block `507206345`. Its Reader order, position, 27 fixed cells, virtual inventory, and execute-order feature flag were read at the recorded block hash. The sidecar still says `ready_for_comparison: false`.

The supported subset is an increase order in a two-token ETH/USD market with
the index and long token equal to WETH and the collateral and short token equal
to USDC. The caller supplies the three token addresses. The archive reader
checks these addresses against the market fields stored at the pinned block.
It also checks the Reader order against the preflight request. The fixed cell
inventory is then read from the DataStore at that block and checked against the
same block hash. A caller cannot add cells and thereby mark the result complete.

| Input to `economics.calculate` | Current source | State |
|---|---|---|
| Order fields and prior position | Pinned `Reader.getOrder` and `Reader.getPosition` | Read and identity checked; token delta still needs derivation. |
| Market token addresses | Pinned DataStore market fields | Read and checked against supplied token identities. |
| Positive and negative impact factors, exponent factors, caps, impact pool | Fixed DataStore keys | Read; historical `Keys.sol` confirms the key formulas. |
| Open interest in tokens for both collateral tokens and both sides | Four fixed DataStore keys | Read and aggregated into model long/short values; historical `Keys.sol` confirms the key formula. |
| Position fee factors, UI fee cap and optional UI fee factor | Fixed DataStore keys | Read. |
| Current cumulative borrowing and funding factors | Fixed DataStore keys | Read; accrued factors depend on GMX update timing at execution. |
| Pool amounts | Two fixed DataStore keys | Read; used by wider economics, not the narrow acceptable-price decision. |
| Oracle price ranges and timestamps | Router oracle input | Stage 3b must prove provenance and scale. |
| Virtual position token ID and signed inventory | `Keys.virtualTokenIdKey(indexToken)` and `Keys.virtualInventoryForPositionsInTokensKey(id)` | The reader now resolves and reads both at the pinned hash. A zero ID stays unavailable until the model's no-virtual path is proved. Historical `Keys.sol` confirms the key formulas. |
| Referral code, affiliate, tier terms, and pro tier | Pinned OrderHandler `referralStorage()` pointer, pinned ReferralStorage contract getters, and fixed DataStore keys | The saved rerun proves a zero trader code and zero pro tier for this account at the pin. The zero code makes affiliate and tier reads inapplicable for this selected order. Exact-match source and ABI are now saved. |
| Order size delta in tokens | GMX `PositionUtils.getExecutionPriceForIncrease` | The base delta is now derived with long floor and short ceiling rounding; a zero USD delta gives zero tokens for a collateral-only MarketIncrease. The final delta depends on price impact and is still missing. |
| Execute-order feature flag | `Keys.executeOrderFeatureDisabledKey(module, orderType)` | Read as `false` at this pin after checking the OrderHandler address and observed code hash. Historical `Keys.sol` confirms the key formula. |
| Long and short open interest in tokens | Four pinned collateral buckets | The reader now sums both collateral buckets for each side and rejects a missing bucket. |
| Equivalent pre-execution state and feature checks | Execution-context evidence | **Missing.** A block-boundary read alone cannot reproduce state inside a block. |

The [Sourcify source proof](../evidence/step4-1/sourcify-source-proof.json) binds exact-match source and ABI records to the pinned runtime hashes for all five contracts. The reviewed historical key formulas agree with the repository's fixed key builders. An independent compiler rebuild remains open. The stage still needs a refreshed sidecar with the corrected order layout, final token delta, and equivalent pre-execution state before any comparison certificate is possible.

The referral rerun has a zero trader referral code and a zero pro tier at the same pin. Its sidecar still lists `referral_historical_source_and_abi_proof` because that file predates the public source check below. The zero values remove the need to read affiliate and tier branches for this selected account.

### Historical source proof status

The offline [source manifest](../evidence/step4-1/historical-source-manifest.json) binds each saved `eth_getCode` result to the sidecar's runtime code hash and extracts the Solidity IPFS metadata reference. The pinned code footers report these compiler versions:

| Contract | Compiler version in pinned code footer | Metadata CID prefix | Source/ABI status |
|---|---|---|---|
| SimulationRouter | 0.8.29 | `Qmagbt6z` | Exact-match source and ABI saved |
| DataStore | 0.8.18 | `QmWUuG3i` | Exact-match source and ABI saved |
| Reader | 0.8.29 | `QmdZfR8A` | Exact-match source and ABI saved; 13 number slots confirmed |
| OrderHandler | 0.8.29 | `QmRokDke` | Exact-match source and ABI saved |
| ReferralStorage | 0.6.12 | `QmVncpnk` | Exact-match source and ABI saved |

The [saved public Sourcify responses](../evidence/step4-1/sourcify-v2/) report exact runtime matches for all five addresses. The [local proof](../evidence/step4-1/sourcify-source-proof.json) checks their reported on-chain runtime code hashes and SHA-256 values against this manifest, verifies all reported source content hashes, and checks ABI consistency. This binds a historical source set to the pinned runtime through Sourcify's exact-match result and local hashes. Raw compiler metadata CID preimages and an independent compiler rebuild remain unverified. The current GMX `main` source is not used as historical proof.

The exact-match ReferralStorage record contains the GMX V1 contract source and its 0.6.12 compiler identity. Its getter names match those used by the bounded referral reader. The [GMX V1 contract list](https://docs.gmx.io/docs/archived/contracts-v1/) also identifies the pinned address. The earlier unverified V2 mock source lead is no longer used.

A bounded direct fetch of the Reader metadata from the public `ipfs.io` gateway returned HTTP 403. The [fetch result](../evidence/step4-1/public-source-fetch-attempt.json) records that earlier attempt. Sourcify V2 subsequently supplied an exact-match source record, but its API response does not expose the raw metadata CID preimage. The offline CID verifier remains available if those raw files are obtained.

### Historical Reader and key layout correction

The exact-match historical `Order.sol` and Reader ABI show `uiFeeFactor` at numeric slot 9, followed by `updatedAtTime`, `validFromTime`, and `srcChainId`. Our original decoder omitted slot 9 and falsely inferred a timestamp swap. The decoder now reads all 13 fields in order. The historical `OrderStoreUtils.sol` reads a per-order `UI_FEE_FACTOR` DataStore key; the pinned order reader now reads that key separately from the UI receiver's fee configuration. The saved sidecar predates this correction and needs a read-only rerun to update its stale `reader_timestamp_layout` and pinned request fields.

The exact-match `Keys.sol` uses `keccak256(abi.encode(baseKey, arguments...))` for execute-order flags, virtual inventory, impact factors, position fees, and open interest in tokens. The exact-match `MarketStoreUtils.sol` uses `keccak256(abi.encode(market, fieldBaseKey))` for market token fields. These formulas agree with the repository's key builders for the fixed subset. The local proof report preserves the hashes of these historical sources. A full independent compiler rebuild is still open; this source review alone does not make the sidecar ready for a Simulator comparison.
