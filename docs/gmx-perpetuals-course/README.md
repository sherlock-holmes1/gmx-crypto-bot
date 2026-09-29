# GMX perpetuals: a five-lesson course

This course explains GMX V2 on Arbitrum through the recorded ETH/USD [WETH-USDC] market. It teaches the protocol, not the Python package architecture. Read one lesson at a time and answer its eight questions in a separate message. Each lesson ends with questions and contains no answer key.

| Lesson | Topic | Main skill |
|---|---|---|
| [1. Positions and exposure](01-positions-and-exposure.md) | Longs, shorts, collateral, leverage, PnL | Explain what the trader actually owns and owes |
| [2. Pool and market](02-pool-and-market.md) | WETH/USDC liquidity, open interest, imbalance | Explain who funds profit and why balance matters |
| [3. Order lifecycle](03-order-lifecycle.md) | Requests, keepers, oracles, execution | Trace a request through its terminal event |
| [4. Economics and risk](04-economics-and-risk.md) | Fees, impact, accrual, liquidation | Reconcile a position's changing equity |
| [5. Reading a real recording](05-recorded-orders.md) | One long and one short | Follow evidence from request to validated outcome |

## How to use the questions

Answer with lesson number and question numbers, for example `Lesson 2: 1) ... 2) ...`. You can answer one lesson at a time and ask for clarification at any point. I will grade each answer as correct, partly correct, or incorrect, explain corrections, and track the count. A partly correct answer counts as half a point. Task 3.2 stays open until you score at least **32 of 40 points (80%)** across all five lessons. You can revise any answer after feedback; the most recent answer replaces the earlier score. The questions are the assessment, so the lesson files have no answer key or worked solutions to the questions.

The recorded examples refer to [`recordings/eth-usdc-v2-sep-20-sep-27/order-validation.json`](../../recordings/eth-usdc-v2-sep-20-sep-27/order-validation.json), which contains 2,942 matched terminal orders, zero mismatches, and 104 boundary-unresolved orders. The report is large; use an order key to locate a particular example. All positions and transactions discussed here are historical, read-only observations.

## Sources and version note

Protocol mechanics are grounded in [GMX positions and order types](https://docs.gmx.io/docs/trading/order-types/), [GMX fees](https://docs.gmx.io/docs/trading/fees/), [GMX liquidations](https://docs.gmx.io/docs/trading/liquidations/), and the [GMX Synthetics protocol README](https://github.com/gmx-io/gmx-synthetics/blob/main/README.md). These pages describe current behavior and can change. The September 2026 recording and its block-pinned configuration are authoritative for the two historical examples. Where current docs and the recorded execution differ, the recorded fields take precedence for that example. Figures in simplified examples are illustrations, not a quote of live market parameters.
