# Stage 3b oracle evidence for the selected increase

The [saved creation-transaction price set](../evidence/step4-1/selected-order-creation-oracle.json) contains the WETH and USDC `OraclePriceUpdate` events in transaction `0xac5153f889493d4da056a3f569eb3d0d26f75bb80e757aa6dd05284675b067c0`. The selected `OrderCreated` event follows those price events in the same transaction at block 507206345. The file keeps exact integer ranges, the common timestamp 1789932722, log coordinates, a recording digest, and the router calldata that those integers would produce.

This is **creation-transaction evidence**, not the oracle input of an observed execution. A simulation with these values is counterfactual. The file therefore has `ready_for_observed_execution_comparison: false`. The extractor rejects missing or duplicate expected tokens and invalid ranges. It does not authorize a comparison merely because the events and creation share a transaction.

The [pinned Oracle code transcript](../evidence/step4-1/pinned-oracle-code.json) records stable block hashes around `eth_getCode`. Its bytes match the [exact-match Sourcify Oracle source](../evidence/step4-1/sourcify-v2/oracle.json). The historical `Oracle.sol` emits the same validated integer min/max prices that it installs as primary prices, with each token's timestamp. The extractor verifies that source mapping, runtime identity, and the event timestamp against the pinned block timestamp. These checks prove that the raw event integers have the units expected by the historical Oracle primary-price path; they do not convert a displayed USD value into an integer oracle price.

The [selected execution-transaction price set](../evidence/step4-1/selected-order-execution-oracle.json) is a separate artifact. Its WETH and USDC updates are logs 2 and 3 of the transaction that emits the selected order's `OrderExecuted` at log 24 in block 507206358. The [execution-block Oracle transcript](../evidence/step4-1/execution-oracle-code.json) binds the historical source bytes to this later block and reads `MAX_ORACLE_PRICE_AGE` as 300 seconds. Historical `Oracle.sol` rejects a price only when its token timestamp plus that configured age is earlier than block time. Both event timestamps are 1789932724 and the block timestamp is 1789932725, so the observed age is one second. This proves the recorded execution price set and age condition. It does not prove that a block-boundary router call sees the same pre-execution state within the block; the artifact therefore still disallows an observed-execution comparison.

The bounded watcher captures same-creation-transaction price updates under the strict policy that token timestamps equal the creation block timestamp. Watch prices remain creation-price counterfactuals. A later observed execution transaction is needed for any watch candidate used in an observed-execution comparison.

To run the selected counterfactual preflight with a configured archive provider and verified deployment file, use:

```bash
gmx-router-check --recording recordings/eth-usdc-v2-sep-20-sep-27 \
  --deployment path/to/verified-router-deployment.json \
  --oracle-evidence evidence/step4-1/selected-order-creation-oracle.json \
  --output evidence/step4-1/creation-price-preflight-report.json
```

The command re-derives the price set from `events.jsonl`, checks the pinned Oracle runtime transcript and exact-match source, and compares the reconstructed calldata with the saved evidence before using the prices. The report labels the price relationship and disallows an observed-execution comparison. The command requires `GMX_ARCHIVE_RPC_URL`; the deployment argument must name the verified file used by this recording.

For a bounded watch, add `--watch-start-block`, `--watch-end-block`, `--watch-oracle-source evidence/step4-1/sourcify-v2/oracle.json`, `--watch-oracle-address 0x26c02f221e8db5a821e12347c7ea8a6b6e10842f`, and two ordered `--watch-oracle-token` arguments (WETH first, USDC second for this market). The watcher reads OraclePriceUpdate logs in the same creation transaction, checks that each update precedes OrderCreated, rechecks the block hash, and checks the Oracle runtime against the historical exact-match source at that block. A missing or changed source, duplicate token, incomplete pair, or timestamp outside the strict same-block policy leaves that candidate without oracle input and records `oracle_evidence_failure`. Watch prices remain creation-price counterfactuals.
