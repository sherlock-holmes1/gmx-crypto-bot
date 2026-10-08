# Step 4.1 targeted archive sidecar

This command reads one recorded `MarketIncrease` order at the end of its creation block. It does not download an event window, change the recording, create an order, sign, or send a transaction. The output is a versioned JSON sidecar. It is **partial evidence**, not a verified Simulator comparison.

The first target is the recorded long increase order `0xfd65a4c31638d78c68c9986e0bb21c002790d0a897aaa31d4109b9cd2f0bff90` in `recordings/eth-usdc-v2-sep-20-sep-27`. The command reads its block number, hash, request, and token addresses from that recording. It checks the recorded block hash against the archive provider. A different order key can be supplied if that candidate is eligible.

First prepare an **observed deployment** file. This reads the DataStore and OrderHandler addresses from the recording, the Reader address from `gmx-market-spec-v1.json`, and a SimulationRouter address that you supply explicitly. It checks the chain and block identity before and after `eth_getCode` reads. The code hashes are **observed at the pin**. This does not verify that the contract source or ABI matches that deployed code.

```bash
GMX_ARCHIVE_RPC_URL="$YOUR_ARCHIVE_RPC_URL" \
  .venv/bin/python -m gmx_crypto_bot_v2.application.deployment_prep \
  --recording recordings/eth-usdc-v2-sep-20-sep-27 \
  --spec gmx-market-spec-v1.json \
  --order-key 0xfd65a4c31638d78c68c9986e0bb21c002790d0a897aaa31d4109b9cd2f0bff90 \
  --simulation-router "$VERIFIED_SIMULATION_ROUTER_ADDRESS" \
  --referral-storage "$VERIFIED_REFERRAL_STORAGE_ADDRESS" \
  --output evidence/step4-1/observed-deployment.json
```

The installed script name is `gmx-prepare-deployment`. It writes the file atomically. A wrong chain, block number, block hash, empty code, changed block, or ReferralStorage address that differs from the pinned OrderHandler pointer stops preparation. The output has the shape required by the sidecar command, plus source paths and SHA-256 digests. It contains no RPC URL or credentials. The referral address must be checked from a historical deployment source; [GMX's current referral documentation](https://docs.gmx.io/docs/referrals/) is a lead, not proof of the September deployment.

The sidecar command also accepts a deployment JSON built from independently verified historical sources. The `reader` address and hash are required. The `order_handler` pair is optional in that format; if absent, the feature flag remains missing. The recording metadata must agree with the DataStore and order handler addresses.

```json
{
  "deployment": {
    "chain_id": 42161,
    "router": "0x...",
    "router_code_hash": "0x...",
    "datastore": "0x...",
    "datastore_code_hash": "0x...",
    "errors": ["EndOfOracleSimulation()"]
  },
  "reader": {"address": "0x...", "code_hash": "0x..."},
  "order_handler": {"address": "0x...", "code_hash": "0x..."}
}
```

Run with a separate archive RPC endpoint:

```bash
GMX_ARCHIVE_RPC_URL="$YOUR_ARCHIVE_RPC_URL" \
  .venv/bin/python -m gmx_crypto_bot_v2.application.archive_sidecar \
  --recording recordings/eth-usdc-v2-sep-20-sep-27 \
  --order-key 0xfd65a4c31638d78c68c9986e0bb21c002790d0a897aaa31d4109b9cd2f0bff90 \
  --deployment evidence/step4-1/observed-deployment.json \
  --output evidence/step4-1/long-increase-archive-sidecar.json
```

The installed script name is `gmx-archive-sidecar`. The output is written atomically. It contains the recording identity, pinned hash, chain and code identities, pending/latest gate, pinned order and position, fixed DataStore cells and values, virtual inventory, and exact read-only RPC calldata/results. Provider error text is redacted; the sidecar retains its response digest. The RPC URL is never included. The sidecar records the deployment file's SHA-256 digest and its observed-only proof level.

When the deployment file includes `referral_storage`, the sidecar verifies the historical code hash and OrderHandler pointer at the pin. It reads the selected account's code and pro tier. For a nonzero code, it also reads the code owner, affiliate tier, rebate and discount shares, minimum affiliate reward factor, and any pro discount factor. These reads follow [GMX's referral calculation](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/referral/ReferralUtils.sol). The sidecar still labels the historical source and ABI proof as missing.

`partial_archive_snapshot` means that the fixed subset was read. `ineligible_latest_request` means the order was not both pending and latest at that block. `unavailable` means a required proof or read failed. All statuses keep `ready_for_comparison: false`. The sidecar lists missing historical key-layout proof, referral terms, oracle ranges and timestamps, final token delta, and equivalent execution context. A block-boundary read does not prove intra-block pre-execution state. If no archive URL or verified deployment is available, the command exits without claiming collection.

## September Reader timestamp layout

The selected September order's pinned DataStore fields and `OrderCreated` event identify `updatedAtTime` as `1789932722` and `validFromTime` as `0`. The archived `Reader.getOrder` return places `0` in the first timestamp slot and `1789932722` in the next slot. [Current GMX `Order.sol`](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/order/Order.sol) lists `updatedAtTime` before `validFromTime`, while [GMX `OrderStoreUtils.sol`](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/order/OrderStoreUtils.sol) reads both DataStore keys by name. The current source is not proof of the September deployment's tuple layout. For the pinned Reader comparison, the archive reader accepts the reversed slots only when they match the independently read DataStore values exactly and no other field differs. It records `reader_timestamp_layout` as an inference. Any other mismatch remains an evidence failure.

## Historical source manifest

Extract the Solidity metadata references directly from the saved runtime code. This command makes no network call:

```bash
PYTHONPATH=src .venv/bin/python -m gmx_crypto_bot_v2.application.source_manifest \
  --sidecar evidence/step4-1/long-increase-archive-sidecar.json \
  --output evidence/step4-1/historical-source-manifest.json
```

The installed script name is `gmx-source-manifest`. It checks every saved runtime code hash against the sidecar deployment and records the IPFS metadata CID and compiler version in the code footer. The manifest does **not** mark historical source, ABI, or key layout as proved. Retrieve and verify the content-addressed metadata and source before making that claim. See [Sourcify's verified-contract API](https://docs.sourcify.dev/docs/api/) for a possible independent source lookup.
