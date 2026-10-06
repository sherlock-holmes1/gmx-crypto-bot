# Step 4 final integration evidence audit

The September 20–27 recording is complete and deterministic, but it does not yet
support a complete position-simulator scenario. This audit separates validation
of observed orders from independent inputs needed for hypothetical orders.

## Recorded checks

- `recordings/eth-usdc-v2-sep-20-sep-27/order-validation.json` reports
  `complete: true`, `mismatched: 0`, and `matched: 2942`. Among executed,
  matched orders, 1,213 are long and 1,256 are short.
- Example recorded long: order
  `0xfd65a4c31638d78c68c9986e0bb21c002790d0a897aaa31d4109b9cd2f0bff90`,
  request block 507206345, execution block 507206358.
- Example recorded short: order
  `0x40974329dc481a5f37104a26262c1578db7e78097b88ef88fbb26ba6915c9c1c`,
  request block 507205341, execution block 507205378.
- `gmx_crypto_bot_v2.replay` with `--verify` reports complete state, zero gaps
  and reorgs, state digest
  `b4d5c913c82c7f1bf4c397ec19d8f19e4c6513259685876d800583e6a10f9982`,
  and event digest
  `5ecd42540032f5afc49ed9c2b59b99652aee4b55730c4f21209de98e7e87b795`.
  It was run without `--output`, leaving the recording unchanged.

The opt-in simulator tests reconstruct selected recorded long and short
increases and full closes through economics, ledger posting, and `run_scenario`.
The paired economics and observed market deltas match the validator and raw
transaction state. The paired scenarios now declare every intervening recorded
risk coordinate. Both return `unavailable` at the first coordinate without a
complete same-transaction oracle price set: long `(507378205, 2, 9)` and short
`(508760311, 6, 22)`. Neither pending close is reported as filled after its
risk coverage gap.

## Missing independent inputs and branches

The new `risk-configuration.json` supplies historical minimum collateral,
maximum PnL, and related risk settings. The evidence adapter now reconstructs
virtual inventory from recorded updates, exposes covered referral terms, and
verifies historical accrual timestamps. The recorded path tests now check all
intervening risk coordinates and surface unavailable oracle coverage.

The risk-coordinate policy waits for a complete same-transaction oracle price
set and leaves oracle-only transactions explicitly unavailable. The recorded
path test verifies this policy across the selected long and short intervals.

The simulator now calculates capped PnL, including a binding zero cap,
supports modeled positive and negative impact and collateral-paid funding
decrease effects, and settles a supported insolvent liquidation shape.
Secondary-token funding, capped negative-impact claimable collateral, and
account claimable-funding balances remain unsupported and return unavailable.
The independent tester accepted the design gate that intervals without
liquidation coverage must return unavailable; observed output values cannot
substitute for hypothetical-order inputs.

## Gap list and next steps

Work locations are relative to `src/gmx_crypto_bot_v2/` unless a repository-root
path is shown.

The collector supports a `risk-configuration.json` sidecar with opening and
closing archive reads and recorded `SetUint` changes. The September recording
has been backfilled: its 10 risk fields verify against the pinned blocks and
the raw logs contain no matching changes during the window. The simulator
consumes this history at supported execution and risk coordinates.

| Gap | Work location | Required work |
|---|---|---|
| Historical minimum collateral, maximum PnL, and related risk settings | `src/gmx_crypto_bot_v2/collection/risk.py`; `simulation/evidence.py`, `simulation/risk.py`, `simulation/pnl_cap.py` | Collected and wired into the adapter, including the zero-cap case. Verify values at every required risk coordinate in a complete recorded scenario. Values in `gmx-market-spec-v1.json` are not historical proof. |
| Virtual inventory | `reconstruction/positions.py`, `simulation/evidence.py` | Recorded updates are reconstructed with continuity checks. Verify continuous coverage at every required coordinate in a complete scenario; mark any gap unavailable. |
| Account-specific referral discounts | `collection/referral.py`, `reconstruction/referral.py`, `simulation/evidence.py`, `simulation/risk.py` | Covered recorded accounts can now supply terms. Verify coverage for the simulated account throughout a complete scenario; mark any gap unavailable. |
| Oracle coverage at risk points | `simulation/evidence.py`, `simulation/scenarios.py`, `tests/v2/test_simulation_recorded_evidence.py` | Every intervening coordinate is checked in the selected recorded lifecycles. The first missing same-transaction oracle mark returns unavailable and prevents a pending close; the September recording cannot yield a complete cash/risk path for those examples. |
| Profitable closes, non-flat decreases, and liquidation settlement | `simulation/pnl_cap.py`, `simulation/economics.py`, `simulation/ledger.py`, `simulation/risk.py`, `simulation/scenarios.py`; `tests/v2/` | Supported capped PnL, impact/funding decrease effects, and insolvent liquidation shapes now have tests. Extend account claimables, secondary-token funding, and other unsupported shapes only with independent evidence and matching accounting. |

The targeted archive backfill is complete. The user authorized two additional
developer–tester cycles beyond the three-return limit. The second independent
review accepted the Step 4 gates with explicit unavailable outcomes for the
recorded intervals that lack oracle coverage. This does not make those
September scenarios fully covered or profitable by default.
