# GMX Crypto Bot

Read-only GMX perpetuals research tooling: collect public GMX and chain state,
replay it deterministically, then model execution, collateral, fees, funding,
borrowing, price impact, and liquidation.

## Status

The initial scaffold provides an append-only recording format and deterministic
replay verifier. A GMX network collector and position simulator are intentionally
not implemented yet.

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
PYTHONPATH=src python -m gmx_crypto_bot.replay recordings/<recording> --verify
```

## Recording schema

A recording directory contains:

```text
metadata.json     # source, chain, market, configuration and capture bounds
events.jsonl      # append-only raw event envelopes
```

Each event envelope contains `seq`, `kind`, `block_number`,
`transaction_index`, `log_index`, local receipt timestamps, and its untouched
`payload`. Chain events replay in canonical block/transaction/log order; events
without canonical coordinates remain in arrival order after canonical events.

## Roadmap

1. Define a versioned GMX market specification for one Arbitrum market.
2. Build a public collector for finalized blocks, GMX events, oracle prices, and
   configuration changes.
3. Extend replay with source-range completeness and reorg checks.
4. Build a GMX position simulator calibrated against observed order executions.
5. Evaluate one pre-registered strategy on development and holdout periods.

Technical references: [GMX architecture](https://docs.gmx.io/docs/api/contracts/architecture/), [fees](https://docs.gmx.io/docs/trading/fees/), and [liquidations](https://docs.gmx.io/docs/trading/liquidations/).
