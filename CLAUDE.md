# GMX Crypto Bot

## Scope

This repository begins as a read-only GMX perpetuals research system. It may
collect public data, replay recordings, and simulate historical execution.

Do not add wallet access, private-key handling, order signing, order submission,
or live-capital logic without an explicit user decision and a separate runbook.

GMX is not a CLOB. Do not import Polymarket assumptions about bid/ask queues,
maker rebates, or complementary pair merging. Model GMX request, keeper, oracle,
fee, collateral, and liquidation mechanics instead.

## Engineering rules

- Preserve raw source events alongside normalized fields.
- Record block number, transaction index, log index, and local receipt time.
- Replay in canonical chain order and make the state digest deterministic.
- Treat missing blocks and reorgs as data gaps, never as harmless omissions.
- Version all market configuration by block number.

