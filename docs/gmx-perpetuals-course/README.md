# GMX perpetuals: a five-lesson course

This course explains GMX V2 on Arbitrum through the recorded ETH/USD [WETH-USDC] market. It teaches the protocol, not the Python package architecture. Each lesson ends with eight questions. Vasilii's answers are recorded under the questions.

| Lesson | Topic | Main skill |
|---|---|---|
| [1. Positions and exposure](01-positions-and-exposure.md) | Longs, shorts, collateral, leverage, PnL | Explain what the trader actually owns and owes |
| [2. Pool and market](02-pool-and-market.md) | WETH/USDC liquidity, open interest, imbalance | Explain who funds profit and why balance matters |
| [3. Order lifecycle](03-order-lifecycle.md) | Requests, keepers, oracles, execution | Trace a request through its terminal event |
| [4. Economics and risk](04-economics-and-risk.md) | Fees, impact, accrual, liquidation | Reconcile a position's changing equity |
| [5. Reading a real recording](05-recorded-orders.md) | One long and one short | Follow evidence from request to validated outcome |

## How to use the questions

Task 3.2 passed with **32.5 of 40 points (81.25%)**, above the 32-point completion gate. The answers are graded correct, partly correct (half a point), or incorrect. The lesson files retain the original questions and Vasilii's answers.

The recorded examples refer to [`recordings/eth-usdc-v2-sep-20-sep-27/order-validation.json`](../../recordings/eth-usdc-v2-sep-20-sep-27/order-validation.json), which contains 2,942 matched terminal orders, zero mismatches, and 104 boundary-unresolved orders. The report is large; use an order key to locate a particular example. All positions and transactions discussed here are historical, read-only observations.

## Sources and version note

Protocol mechanics are grounded in [GMX positions and order types](https://docs.gmx.io/docs/trading/order-types/), [GMX fees](https://docs.gmx.io/docs/trading/fees/), [GMX liquidations](https://docs.gmx.io/docs/trading/liquidations/), and the [GMX Synthetics protocol README](https://github.com/gmx-io/gmx-synthetics/blob/main/README.md). These pages describe current behavior and can change. The September 2026 recording and its block-pinned configuration are authoritative for the two historical examples. Where current docs and the recorded execution differ, the recorded fields take precedence for that example. Figures in simplified examples are illustrations, not a quote of live market parameters.
