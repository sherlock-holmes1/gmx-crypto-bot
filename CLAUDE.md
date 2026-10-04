# GMX Crypto Bot

## Scope

This repository begins as a read-only GMX perpetuals research system. It may
collect public data, replay recordings, and simulate historical execution.

Do not add wallet access, private-key handling, order signing, order submission,
or live-capital logic without an explicit user decision and a separate runbook.

Model GMX request, keeper, oracle, fee, collateral, and liquidation mechanics.

## Engineering rules

- Preserve raw source events alongside normalized fields.
- Record block number, transaction index, log index, and local receipt time.
- Replay in canonical chain order and make the state digest deterministic.
- Treat missing blocks and reorgs as data gaps, never as harmless omissions.
- Version all market configuration by block number.
- For Step 4 simulator work, load `.agents/skills/gmx-step4-orchestrator/SKILL.md` before starting the implementation pipeline.
