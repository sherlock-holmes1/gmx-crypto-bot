# GMX Crypto Bot

Read-only GMX perpetuals research tooling: collect public GMX and chain state,
replay it deterministically, then model execution, collateral, fees, funding,
borrowing, price impact, and liquidation.

## Status

The repository provides an append-only recording format, deterministic replay
verifier, and bounded public GMX collector. The 1,000-block test recording and
replay passed with no gaps or reorgs. Target-market normalization is complete.
Next: the pinned seven-day recording, then validation of reconstructed execution
against observed terminal orders. A position simulator is not implemented yet.

No wallet, private key, signing, order-submission, or live-capital code belongs
in this project without a separate explicit decision and runbook.

## Why this is not the Polymarket bot

GMX orders are committed on-chain and subsequently executed by keepers using
oracle prices. There is no public CLOB queue to join, and a long/short position
does not merge with a complementary token into a fixed payout.

| Reused framework | GMX-specific replacement |
|---|---|
| Raw event recorder | Chain blocks, GMX events, oracle prices, market configuration |
| Deterministic replay | Canonical block/transaction/log ordering and reorg detection |
| Assumption grid | Request-to-execution latency, oracle movement, gas, and slippage scenarios |
| PnL report | Collateral, long/short PnL, fees, funding, borrowing, price impact, liquidation |

## Quick start

The scaffold has no dependencies beyond Python 3.12+.

```bash
python -m unittest discover -s tests -v
PYTHONPATH=src python -m gmx_crypto_bot.collector --spec gmx-market-spec-v1.json --output recordings/eth-usdc-week-1
PYTHONPATH=src python -m gmx_crypto_bot.replay recordings/<recording> --verify

 PYTHONPATH=src python -m gmx_crypto_bot.collector \
    --spec gmx-market-spec-v1.json \
    --output recordings/test-1000-blocks \
    --from-block 507156678 \
    --to-block 507157678 \
    --chunk-size 1000
```

## Recording schema

A recording directory contains:

```text
metadata.json     # source, chain, market, configuration and capture bounds
events.jsonl      # self-contained append-only ETH/USD replay events
raw/              # rotated compressed JSONL response bundles + manifest
completeness-report.json # source ranges, explicit gaps, and reorg findings
```

Each event envelope contains `seq`, `kind`, `block_number`,
`transaction_index`, `log_index`, local receipt timestamps, and its replay
payload. Every raw source response is stored before a replay event is derived.
`raw/rpc-000001.jsonl.gz` bundles base64-encoded exact response bodies and rotates
at 256 MiB of uncompressed records. The manifest maps each source request to its
bundle record and SHA-256. Replay events do not reference raw files. Chain events
replay in canonical block/transaction/log order; events without canonical coordinates
remain in arrival order after canonical events.

## Collector

`gmx-collect` accepts a pinned `GmxMarketSpec` and a new output directory. It
queries only public JSON-RPC and GMX public HTTP endpoints. It has no wallet,
account, private-key, signing, transaction, or order-submission code.

It stores full JSON-RPC and HTTP responses in rotated `raw/rpc-*.jsonl.gz` bundles
and records their request, bundle location, and SHA-256 in `raw/manifest.jsonl`
before it derives replay events. It watches the pinned GMX EventEmitter, DataStore, Oracle,
OrderHandler, and LiquidationHandler contracts. `events.jsonl` retains only the
pinned ETH/USD market events, target-market configuration changes, and WETH/USDC
oracle updates. Unrelated GMX markets remain only in the raw source artifacts.
Receipts and headers are fetched only for retained events.

Large `eth_getLogs` ranges are split recursively. A source failure at a single
block becomes a `data_gap` record and makes `complete` false. A log whose block
hash differs from a freshly read canonical header becomes a `reorg_detected`
record and also makes the recording incomplete. The collector refuses ranges
newer than the selected confirmation depth.

## Roadmap

1. Completed — define a versioned GMX market specification for one Arbitrum market.
2. Completed — build a public collector for finalized blocks, GMX events, oracle
   prices, and configuration changes, with raw artifacts and target-market replay filtering.
3. Completed — verify canonical replay ordering, source-range completeness, and reorg checks.
4. Collect the pinned seven-day recording.
5. Validate reconstructed execution against observed terminal orders: join each
   request to its execution, cancellation, or freeze; compare terminal outcome,
   oracle prices, execution price, position and cash movements, fees, and receipt
   result within fixed tolerances; emit per-order and aggregate mismatch reports.
6. Build a GMX position simulator only after execution validation passes.
7. Evaluate one pre-registered strategy on development and holdout periods.

Technical references: [GMX architecture](https://docs.gmx.io/docs/api/contracts/architecture/), [fees](https://docs.gmx.io/docs/trading/fees/), and [liquidations](https://docs.gmx.io/docs/trading/liquidations/).
