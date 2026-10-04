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

These observed-order validator results are not simulator-versus-validator
parity. No recorded long or short has been reconstructed as a full hypothetical
position path through `run_scenario`.

## Missing independent inputs and branches

The opening configuration artifacts (`impact-opening-configuration.json`,
`fee-opening-configuration.json`, `accrual-configuration.json`, and
`liquidation-referral-configuration.json`) have no named historical series for
minimum collateral, maximum PnL, or virtual inventory. The referral artifact
has account data, but the scenario runner requires a separately pinned
`RiskReferralEvidence` at every risk coordinate. No adapter currently derives
these inputs and proves coverage through the recording window.

`EvidenceAdapter.required_risk_coordinates()` includes individual
`OraclePriceUpdate` logs. `EvidenceAdapter.at()` exposes only prices observed
before that log in the same transaction; therefore the first oracle update of
a transaction cannot furnish a complete price set at its own pre-log risk
coordinate. That interval must remain unavailable until a defensible risk
coordinate and coverage policy is implemented.

The simulator explicitly returns unavailable for profitable decrease PnL
without an independent maximum-PnL cap, for liquidation settlement without
an insolvency model, and for non-flat decrease pool effects without complete
pool accounting. `run_scenario` halts on liquidation without settling it.
These are blocking Step 4 acceptance gates; zero or observed output values
cannot substitute for missing hypothetical-order inputs.

## Gap list and next steps

Work locations are relative to `src/gmx_crypto_bot_v2/` unless a repository-root
path is shown.

| Gap | Work location | Required work |
|---|---|---|
| Historical minimum collateral, maximum PnL, and related risk settings | `src/gmx_crypto_bot_v2/collection/` opening-state backfill; `simulation/evidence.py`, `simulation/risk.py`, `simulation/economics.py` | Capture the values at the recording's opening block and track changes through the window. Values in `gmx-market-spec-v1.json` are not proof of historical state. |
| Virtual inventory | `reconstruction/positions.py`, `simulation/evidence.py`; `collection/` if an opening backfill is needed | Reuse recorded `VirtualPositionInventoryUpdated` events where they establish continuous state; the observed-order validator already reconstructs pre-trade values. Backfill an opening value if needed, then prove coverage at hypothetical risk coordinates. |
| Account-specific referral discounts | `collection/referral.py`, `reconstruction/referral.py`, `simulation/evidence.py`, `simulation/risk.py` | Use the existing referral artifact to derive `RiskReferralEvidence` for the simulated account at every required coordinate. Mark uncovered intervals unavailable. |
| Oracle coverage at risk points | `simulation/evidence.py`, `simulation/scenarios.py` | Define risk coordinates and a transaction-scoped oracle policy that never treats an incomplete pre-log price set as a complete mark. Test the first `OraclePriceUpdate` in a transaction. |
| Profitable closes, non-flat decreases, and liquidation settlement | `simulation/economics.py`, `simulation/ledger.py`, `simulation/risk.py`, `simulation/scenarios.py`; `tests/v2/` | Implement the remaining maximum-PnL, pool-effect, and insolvency accounting paths using independent historical inputs; compare recorded long and short examples with validator outputs. |

Start with a read-only audit and targeted archive backfill of missing opening
configuration, pinned to the recorded block hash. This may avoid collecting the
whole window again. Extend the collector for fields or change events that the
existing recording cannot reconstruct, then wire the resulting evidence into
the adapter and finish the simulator accounting and parity checks.
