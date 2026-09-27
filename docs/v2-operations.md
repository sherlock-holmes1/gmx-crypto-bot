# V2 collection, replay, and validation

The runtime package is `src/gmx_crypto_bot_v2`. The original package remains an
independent regression reference. Public entry points in `pyproject.toml` use V2;
reinstall an existing editable installation to refresh its console scripts.

```bash
python -m pip install -e .
gmx-collect --spec gmx-market-spec-v1.json --last-days 7 --output recordings/new-window
gmx-collect --spec gmx-market-spec-v1.json --output recordings/new-window --resume
gmx-replay recordings/new-window --verify
gmx-validate recordings/new-window --output recordings/new-window/order-validation.json
```

`--last-days 7` selects a rolling 168-hour interval ending at the archive head
minus the confirmation depth. It resolves timestamps to block numbers and reads
fresh closing configuration at the end block. The resolved spec, original
template digest, and raw-evidence digests are saved in
`.collection/window-selection.json`. Resume reuses this fixed selection, with or
without repeating `--last-days 7`; it never shifts an existing recording to a
newer week. Use a new output directory to select a new window. Without this
option, a fresh run uses the dates pinned in the supplied spec. It cannot be
combined with explicit block bounds.

Fresh collection needs `GMX_ARCHIVE_RPC_URL` in the environment. An optional
`GMX_LOGS_RPC_URL` selects the log provider. Keep endpoints out of command history
and committed files. The existing README explains local credential setup.

Without installing, use `PYTHONPATH=src python -m gmx_crypto_bot_v2.collector`,
`.replay`, or `.validator` with the same arguments.

## Recording files

| Path | Purpose | Disposable? |
|---|---|---|
| `metadata.json`, `events.jsonl`, `raw/` | Legacy-compatible identity, normalized observations, exact responses | No |
| Historical snapshots and `execution-fee-traces/` | Block-bound configuration and transaction evidence | No |
| `v2-<snapshot-name>.json` | Expanded dependencies collected without overwriting older snapshots | No |
| `.collection/journal.json` | Identity and verified completed artifact digests | No |
| `.collection/rpc/requests.sqlite` | Durable compressed RPC responses for resume, separate from the derived catalog | No |
| `.collection/base-*/`, `.collection/capture-*/` | Unpublished views and preserved capture diagnostics | Keep while collecting |
| `.gmx-v2/catalog.sqlite` and its integrity stamp | Rebuildable evidence index | Yes |
| `evidence-readiness.json` | Collection coverage, missing inputs, and catalog counters | Derived |
| `order-validation.json` | Economic check results | Derived |

The database persists. Normal runs reuse it; they do not rebuild it. Changed
sources invalidate their affected rows. If the database is missing, corrupt, or
incompatible, the next run recreates it from saved evidence. The seven-day
recording produces approximately 17 GiB of indexes and decoded evidence, so
choose a filesystem with adequate free space. An unchanged database reuses its
previous integrity check; a changed database is checked again.

Use `--cache-dir /path/to/cache` on collection, replay, and validation to share an
alternate catalog location for a recording. The index is bound to the recording
path, metadata digest, and schema/decoder versions. A cache directory should be
assigned to one recording.

## Resume and readiness

Use `--resume` for an existing directory. The coordinator verifies published
artifacts, discovers current dependencies, and collects missing work. In-flight
writes are not complete journal entries. Log requests, checkpoint pages, archive
batches, and transaction traces reuse successful saved RPC responses. Headers are
rechecked where collection establishes block identity.

Only one collector can own a recording. Changing the requested spec or fixed
block range is rejected. Corrupt evidence and conflicting block identities stop
collection; they are not converted into a success or silently overwritten.

A valid older referral or swap snapshot with insufficient coverage can receive
an additive `v2-` supplement. Reconstruction prefers that supplement and checks
its identity. Deleting SQLite does not delete these snapshots or the journal.

Full trace collection is the default. Native-payout transactions remain required
even when their execution fee is zero. Known gas bytecode uses the unchanged
calibrated profile; other shapes require opcode evidence and remain unavailable
when that evidence cannot be obtained. `--gas-probes` requests opcode capture
explicitly. No trace sample is presented as full coverage.

Collection exit codes: `0` means ready, `2` means missing evidence or a deliberately
selected partial stage, and `1` means collection stopped on an error. Validation
returns `0` only when all applicable economic gates pass; otherwise it returns
`2`. Replay and validation are offline and never obtain missing evidence themselves.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m unittest discover -s tests/v2
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/verify_v2_regression.py \
  recordings/eth-usdc-week-2 \
  --baseline recordings/eth-usdc-week-2/order-validation.json \
  --cache-dir /path/to/cache \
  --output /path/to/regression-summary.json
```

The regression command compares every report field, blocks socket creation, and
records elapsed time, peak memory, and cache counters. Use a frozen V1 report as
the baseline. A cold index build and a warm validation are different workloads;
measure both separately.

### RPC response storage

Successful RPC responses are stored in `.collection/rpc/requests.sqlite`, keyed
by source role and request hash. This persistent resume store is separate from
the rebuildable `.gmx-v2/catalog.sqlite`. Responses retain their original request,
compressed exact body, and SHA-256 checksum. Raw evidence bundles remain available
for replay and auditing. Existing per-request `.json.gz` files are migrated on
resume: each response is committed and read back before its old file is removed.
Empty legacy role directories are removed. Migration can safely resume after an
interruption; invalid files are preserved and reported as errors.
