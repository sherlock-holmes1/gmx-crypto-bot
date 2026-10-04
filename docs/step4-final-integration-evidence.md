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

The opt-in simulator test now reconstructs one recorded long and one recorded
short increase through economics and ledger posting and matches the validator's
compared fields and observed position balances. It does not exercise a complete
hypothetical order path through `run_scenario` or a recorded decrease.

## Missing independent inputs and branches

The new `risk-configuration.json` supplies historical minimum collateral,
maximum PnL, and related risk settings. The evidence adapter now reconstructs
virtual inventory from recorded updates, exposes covered referral terms, and
verifies historical accrual timestamps. Full scenario-wide risk coverage still
needs a recorded path test.

The risk-coordinate policy now waits for a complete same-transaction oracle
price set and leaves oracle-only transactions explicitly unavailable. The
recorded path test must verify that this policy covers each required interval.

The simulator now calculates capped PnL, supports some profitable decreases,
and settles a supported insolvent liquidation shape. The final tester found a
binding zero-cap bug in `simulation/pnl_cap.py`: it returns full positive PnL
instead of zero. Decreases with impact or funding and liquidation shapes
outside the tested branch remain unavailable. These are blocking Step 4
acceptance gates; observed output values cannot substitute for hypothetical
order inputs.

## Gap list and next steps

Work locations are relative to `src/gmx_crypto_bot_v2/` unless a repository-root
path is shown.

The collector now supports a `risk-configuration.json` sidecar with opening and
closing archive reads and recorded `SetUint` changes. The September recording
has been backfilled: its 10 risk fields verify against the pinned blocks and
the raw logs contain no matching changes during the window. The simulator
still needs to consume this history at execution and risk coordinates.

| Gap | Work location | Required work |
|---|---|---|
| Historical minimum collateral, maximum PnL, and related risk settings | `src/gmx_crypto_bot_v2/collection/risk.py`; `simulation/evidence.py`, `simulation/risk.py`, `simulation/pnl_cap.py` | Collected and wired into the adapter. Fix the binding zero-cap bug and verify values throughout a complete recorded scenario. Values in `gmx-market-spec-v1.json` are not historical proof. |
| Virtual inventory | `reconstruction/positions.py`, `simulation/evidence.py` | Recorded updates are reconstructed with continuity checks. Verify continuous coverage at every required coordinate in a complete scenario; mark any gap unavailable. |
| Account-specific referral discounts | `collection/referral.py`, `reconstruction/referral.py`, `simulation/evidence.py`, `simulation/risk.py` | Covered recorded accounts can now supply terms. Verify coverage for the simulated account throughout a complete scenario; mark any gap unavailable. |
| Oracle coverage at risk points | `simulation/evidence.py`, `simulation/scenarios.py` | The same-transaction policy now waits for a complete price set. Test full-path risk coverage and retain explicit unavailable intervals where no valid mark exists. |
| Profitable closes, non-flat decreases, and liquidation settlement | `simulation/pnl_cap.py`, `simulation/economics.py`, `simulation/ledger.py`, `simulation/risk.py`, `simulation/scenarios.py`; `tests/v2/` | Fix zero-cap PnL; extend impact/funding decrease and liquidation accounting beyond supported shapes; compare full recorded long and short paths with validator outputs. |

The targeted archive backfill is complete. Remaining work is in simulator
accounting and full-path verification. Stage 6 has reached the orchestrator's
three-return limit, so another developer–tester revision needs a human decision.
