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
| Positive and negative impact factors, exponent factors, caps, impact pool | Fixed DataStore keys | Read; key layout must be checked against the historical deployment. |
| Open interest in tokens for both collateral tokens and both sides | Four fixed DataStore keys | Read and aggregated into model long/short values; historical key proof remains open. |
| Position fee factors, UI fee cap and optional UI fee factor | Fixed DataStore keys | Read. |
| Current cumulative borrowing and funding factors | Fixed DataStore keys | Read; accrued factors depend on GMX update timing at execution. |
| Pool amounts | Two fixed DataStore keys | Read; used by wider economics, not the narrow acceptable-price decision. |
| Oracle price ranges and timestamps | Router oracle input | Stage 3b must prove provenance and scale. |
| Virtual position token ID and signed inventory | `Keys.virtualTokenIdKey(indexToken)` and `Keys.virtualInventoryForPositionsInTokensKey(id)` | The reader now resolves and reads both at the pinned hash. A zero ID stays unavailable until the model's no-virtual path is proved. Historical deployment semantics remain unverified. |
| Referral code, affiliate, tier terms, and pro tier | Pinned OrderHandler `referralStorage()` pointer, pinned ReferralStorage contract getters, and fixed DataStore keys | The saved rerun proves a zero trader code and zero pro tier for this account at the pin. The zero code makes affiliate and tier reads inapplicable for this selected order. Historical source/ABI identity is still unproved. |
| Order size delta in tokens | GMX `PositionUtils.getExecutionPriceForIncrease` | The base delta is now derived with long floor and short ceiling rounding; a zero USD delta gives zero tokens for a collateral-only MarketIncrease. The final delta depends on price impact and is still missing. |
| Execute-order feature flag | `Keys.executeOrderFeatureDisabledKey(module, orderType)` | Read as `false` at this pin after checking the OrderHandler address and observed code hash. The historical source/key layout remains unproved. |
| Long and short open interest in tokens | Four pinned collateral buckets | The reader now sums both collateral buckets for each side and rejects a missing bucket. |
| Equivalent pre-execution state and feature checks | Execution-context evidence | **Missing.** A block-boundary read alone cannot reproduce state inside a block. |

The key names and layout come from GMX's [Keys.sol](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/data/Keys.sol), [PositionUtils.sol](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/position/PositionUtils.sol), [MarketStoreUtils.sol](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/market/MarketStoreUtils.sol), and the repository's existing `domain/accrual.py` and `domain/keys.py` mappings. The referral dependency chain follows GMX's [ReferralUtils.sol](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/referral/ReferralUtils.sol). The live `main` source is **not a historical deployment proof**. The deployment preparation file contains observed runtime code hashes, not a verified match to a historical source commit or ABI. The observed DataStore values alone cannot prove that every derived key has the historical meaning claimed by the current source. The stage still needs that source/key proof, final token delta, and equivalent pre-execution state before any comparison certificate is possible.

The referral rerun now has a zero trader referral code and a zero pro tier at the same pin. The sidecar retains `referral_historical_source_and_abi_proof` because the pinned ReferralStorage code has not been matched to verified source. The zero values remove the need to read affiliate and tier branches for this selected account; they do not prove the contract's historical ABI on their own.

### Historical source proof status

The offline [source manifest](../evidence/step4-1/historical-source-manifest.json) binds each saved `eth_getCode` result to the sidecar's runtime code hash and extracts the Solidity IPFS metadata reference. The pinned code footers report these compiler versions:

| Contract | Compiler version in pinned code footer | Metadata CID prefix | Source/ABI status |
|---|---|---|---|
| SimulationRouter | 0.8.29 | `Qmagbt6z` | Metadata reference only |
| DataStore | 0.8.18 | `QmWUuG3i` | Metadata reference only |
| Reader | 0.8.29 | `QmdZfR8A` | Metadata reference only; timestamp slots differ from the current `Order.sol` layout |
| OrderHandler | 0.8.29 | `QmRokDke` | Metadata reference only |
| ReferralStorage | 0.6.12 | `QmVncpnk` | Metadata reference only; current GMX mock ReferralStorage source uses Solidity 0.8 |

The code hashes and metadata references are pinned evidence. They do **not** prove that the current GMX source tree generated this code. The exact remaining work is to retrieve the content-addressed compiler metadata and referenced source files, verify their hashes, rebuild or otherwise verify each runtime code match, then inspect the historical `Order.Props` tuple, Reader ABI, ReferralStorage getters, `Keys.sol`, and market key builders. The public metadata endpoints were unavailable in this environment. The historical source, ABI, and key-layout flags remain unresolved. [Sourcify's API documentation](https://docs.sourcify.dev/docs/api/) describes a public verified-contract lookup that could supply an independent match.
