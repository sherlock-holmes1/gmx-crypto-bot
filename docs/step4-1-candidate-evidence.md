# Step 4.1 recorded candidate evidence

Status: **candidate selection only**. No order in this file has passed the GMX pending/latest-request gate or the same-order state gate. These examples are not verified router comparisons.

Source: `recordings/eth-usdc-v2-sep-20-sep-27/events.jsonl`. Stage 1 decoded the recording's raw EventEmitter logs with the existing decoder and selected the first creation-order candidate without a local selection skip in each target category. The proposed pin is the end of the creation block.

| Category | Order key | `OrderCreated` coordinate `(block, transaction, log)` | Proposed pin block hash | Router gate | Same-order state gate |
|---|---|---|---|---|---|
| Long increase | `0xfd65a4c31638d78c68c9986e0bb21c002790d0a897aaa31d4109b9cd2f0bff90` | `(507206345, 2, 18)` | `0x3641e2a0ddbf4e620a1cd6b7323e8cbed3aad6e39602ee8779c2f6604be3856f` | Not checked | Not checked |
| Short increase | `0x40974329dc481a5f37104a26262c1578db7e78097b88ef88fbb26ba6915c9c1c` | `(507205341, 1, 15)` | `0x1e8e290a06d6513973f82b7d6e39247f467581e7dc900ace4f0143c8f4eb74fb` | Not checked | Not checked |
| Long decrease | `0xfaac1e9da68f28666803f1c55969b25a4604a7d7a698139fe10cfe96a9efb6b3` | `(507208861, 5, 73)` | `0xd8b2488c8e1807f5c67ffc3bddcf7658fcf62992cf66c6aefc9ee5b4e14fa9ae` | Not checked | Not checked |
| Short decrease | `0xba2ac20c374605ca5402fef05d9d37139c831cf6f2731800bf16405034e1fc99` | `(507206579, 4, 17)` | `0x5f2098474147ffa7fce02bc5af06b8a20cf537dcdcd0829d8708f3ff19d1d0ad` | Not checked | Not checked |

The selector found 2,975 `OrderCreated` records: 839 long increases, 843 short increases, 686 long decreases, 542 short decreases, and 65 unsupported order types. It flagged 65 same-block terminal cases. These counts describe recorded candidates, not eligible GMX router calls. The four displayed keys are examples for the next gate; they are not final selected comparison orders.

To reproduce this selection from the recording:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path
from collections import Counter
from gmx_crypto_bot_v2.simulation.router_candidates import select_router_candidates

rows = select_router_candidates(Path('recordings/eth-usdc-v2-sep-20-sep-27'))
print(len(rows), Counter(row['category'] for row in rows))
for category in ('long_increase', 'short_increase', 'long_decrease', 'short_decrease'):
    row = next(row for row in rows if row['category'] == category
               and not row['selection_skip_reasons'])
    print(category, row['order_key'], row['creation'], row['proposed_pin_hash'])
PY
```

The next evidence needed is an archive-state proof that each order was pending and the global latest request at its pinned block, followed by a proof that our reconstruction uses equivalent order fields, oracle prices, and pre-execution state. No such proof has been saved yet.
