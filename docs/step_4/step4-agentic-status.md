# Step 4 agentic implementation status

The primary agent updates this file after each developer–tester handoff. A return means the tester requested changes and the same stage was handed back to the developer. The limit is three returns per stage.

| Stage | Status | Returns used | Acceptance evidence or blocker |
|---|---|---:|---|
| 1. Evidence adapter | Accepted | 1 | Independent tester accepted corrected fee receiver history, attributed pre-event reversal, and explicit keeper evidence gaps. Focused 7/7, V2 154/154, `git diff --check` clean. September 20–27 recording was later found in ignored `recordings/`; real-order parity remains a final integration gate. |
| 2. Position economics | Accepted | 1 | Tester verified decrease token rounding/full close and explicit unavailable profitable settlement or liquidation without independent cap/insolvency inputs. Focused 15/15, V2 162/162, compile and diff checks clean. Complete Step 4 still needs supported profitable/liquidation cases and September 20–27 recorded parity. |
| 3. Counterfactual market and position ledger | Accepted | 1 | Tester verified digest-bound overlay economics and two-fill impact regression; focused 21/21, V2 168/168, compile and diff checks clean. General decrease and liquidation pool effects remain unavailable without independent derivation at final integration. |
| 4. Risk monitor | Accepted | 3 | Final tester review accepted evidenced account-specific close fee discounts and all prior numerical fixes. Focused 11/11, V2 179/179, compile and diff checks clean. Real September risk factors/virtual inventory and liquidation settlement remain unavailable for final integration. |
| 5. Scenario runner and report | Accepted | 1 | Tester verified ETH fee valuation in returns, explicit unavailable on missing prices, and full ledger snapshots at risk points. Focused 12/12, V2 184/184. Raw recording risk coordinate coverage remains a final integration gate. |
| 6. Final integration | Accepted | 3 | Second user-authorized extra review accepted the Step 4 design gates: selected recorded long/short lifecycles include every intervening risk coordinate and return unavailable at missing same-transaction oracle marks `(507378205, 2, 9)` and `(508760311, 6, 22)`, withholding pending closes. Focused evidence 12/12, default V2 202/202 (3 opt-in skipped), September opt-in 3/3. Final replay remained deterministic and complete with 0 gaps/reorgs and unchanged state/event digests; saved validation remains 2,942 matched, 0 mismatched. Return count preserved at 3. |

The request scheduler predates this pipeline and is complete. Stages 1–6 have been accepted through the developer–tester loop, including two user-authorized extra final-integration cycles without resetting the return count. Step 4 implementation is complete against the design gates. The selected September lifecycles lack complete same-transaction oracle risk coverage and therefore produce explicit unavailable reports rather than complete cash/risk paths.

## Follow-on work: complete lifecycle examples

Status: **Pending**. This is separate from the accepted Step 4 stages. No complete recorded open-to-close example has been accepted yet.

The developer work is to search recorded long and short lifecycles for complete, independent evidence at every required risk coordinate. Before selecting examples by outcome, define risk limits for leverage, liquidation buffer, acceptable price, and missing data. Run each candidate from opening request through close or supported liquidation. Record the selection method, rejected candidates, report files, and all `unavailable` reasons. If the recording has no qualifying lifecycle, identify the exact missing oracle or other evidence and the collector or adapter change needed; do not substitute observed trade results for hypothetical inputs.

The tester work is to check the search method and risk limits independently, rerun the chosen lifecycle reports, compare supported economics with validation, and verify that every required risk coordinate is covered. Acceptance requires at least one complete long and one complete short lifecycle with no unresolved evidence gap, a posted close or supported liquidation, and reproducible reports and example plans. A scenario that only has a favorable historical return is not sufficient evidence of acceptable risk.
