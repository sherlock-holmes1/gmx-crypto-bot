# GMX Crypto Bot

Read-only GMX perpetuals research tooling: collect public GMX and chain state,
replay it deterministically, then model execution, collateral, fees, funding,
borrowing, price impact, and liquidation.

## Status

The default commands use the V2 pipeline in [`src/gmx_crypto_bot_v2`](src/gmx_crypto_bot_v2):
one resumable collector, a persistent SQLite evidence catalog, bounded-memory
replay, and separate economic models and checks. V1 remains available through
its original `python -m gmx_crypto_bot...` module paths for comparison.

The pinned seven-day recording passes all 3,614 terminal orders, including 103
liquidation settlements and 3,511 execution-fee proofs. V2 preserves the complete
V1 validation report and both replay digests. The refactoring is implemented:
116 original tests and 134 V2 tests pass. Fresh collection and interruption
recovery have been tested with simulated providers; the existing real recording
has been validated and replayed offline. **A complete fresh collection against a
live archive RPC provider has not yet been verified.** A position simulator is
not implemented yet.

See [architecture](docs/architecture.md), [V2 operations](docs/v2-operations.md),
[regression results](docs/v2-validation.md), and the
[V1 reference](docs/v1-collector-and-validation.md).

No wallet, private key, signing, order-submission, or live-capital code belongs
in this project without a separate explicit decision and runbook.

## Why this is not the Polymarket bot

GMX orders are committed on-chain and subsequently executed by keepers using
oracle prices. There is no public CLOB queue to join, and a long/short position
does not merge with a complementary token into a fixed payout.

| Reused framework | GMX-specific replacement |
|---|---|
| Raw event recorder | Chain blocks, GMX events, oracle prices, market configuration |
| Deterministic replay | Canonical block/transaction/log ordering and reorg detection |
| Assumption grid | Request-to-execution latency, oracle movement, gas, and slippage scenarios |
| PnL report | Collateral, long/short PnL, fees, funding, borrowing, price impact, liquidation |

## Run the V2 pipeline

Run these commands from the repository root with Python 3.12+ on Linux. The
runtime has no third-party Python dependencies. Install the project once to run
all commands without the `PYTHONPATH=src` prefix:

```bash
cd /home/vasilii/work/gmx-crypto-bot
python -m pip install -e .
```

If you use a virtual environment, activate it before installing and running the
commands below. The editable installation (`-e`) picks up source-code changes
without reinstalling. Reinstall when package metadata or console entry points
change.

| Module | Installed command | Purpose |
|---|---|---|
| `gmx_crypto_bot_v2.collector` | `gmx-collect` | Collect and resume all evidence stages |
| `gmx_crypto_bot_v2.replay` | `gmx-replay` | Reconstruct state and verify deterministic replay |
| `gmx_crypto_bot_v2.validator` | `gmx-validate` | Check historical order execution and economics |

### 1. Configure the archive provider

```bash
cd /home/vasilii/work/gmx-crypto-bot
read -rsp "Arbitrum archive RPC URL: " GMX_ARCHIVE_RPC_URL
echo
export GMX_ARCHIVE_RPC_URL
```

Use an Arbitrum One endpoint with historical state and transaction-trace support.
The collector reads the endpoint from the environment. `GMX_LOGS_RPC_URL` is an
optional separate endpoint for logs and public headers. Replay and validation
run offline and do not need either endpoint.

### 2. Collect a fresh recording

Choose a new output directory so the existing recording stays available:

```bash
gmx-collect \
  --spec gmx-market-spec-v1.json \
  --last-days 7 \
  --output recordings/eth-usdc-v2-fresh
```

`--last-days 7` selects the latest rolling **168 hours**, ending at the archive
provider's head minus the confirmation depth (64 blocks by default). This is a
rolling interval near the time you start the command, not seven calendar days
ending at midnight. The collector prints the exact UTC dates and block bounds.
It uses the spec's market, contracts, and tokens as a template, replaces the
historical date window, and fetches fresh closing configuration at the selected
end block. The checked-in spec is not modified.

The resolved spec and range are saved in `.collection/window-selection.json`
inside the recording. Resume always uses that saved range, even on another day;
omit `--last-days` to resume it or repeat the same value. To select a newer week,
start a new output directory. Without `--last-days`, a fresh run uses the pinned
**September 15–22, 2026** window from the checked-in spec. Explicit `--from-block`
and `--to-block` options cannot be combined with `--last-days`.

The collector runs base capture, opening state, historical configuration, referral state,
swap state, accrual anchors, and transaction traces in one pipeline. No separate
backfill commands or preliminary validation report are required.

A successful run prints `"ready": true` and exits with code `0`. Inspect
`recordings/eth-usdc-v2-fresh/evidence-readiness.json` for missing evidence if it
returns `2`. Runtime collection errors return `1`. Readiness means the required
evidence is present; economic correctness is checked by the validator.

### 3. Resume an interrupted or incomplete collection

Keep the archive endpoint available and reuse the same directory and spec:

```bash
gmx-collect \
  --spec gmx-market-spec-v1.json \
  --last-days 7 \
  --output recordings/eth-usdc-v2-fresh \
  --resume
```

Resume verifies completed artifacts and reuses saved successful RPC work. If a
provider lacks required historical state or trace support, address that provider
problem before resuming. Completed evidence remains on disk.

### 4. Replay and verify determinism

After collection is ready:

```bash
gmx-replay \
  recordings/eth-usdc-v2-fresh \
  --verify \
  --output recordings/eth-usdc-v2-fresh/replay-report.json
```

`--verify` replays twice and compares digests. The output file contains the
reconstructed-state report. Deterministic replay does not by itself establish
that all economic checks pass.

### 5. Validate orders and economics

```bash
gmx-validate \
  recordings/eth-usdc-v2-fresh \
  --output recordings/eth-usdc-v2-fresh/order-validation.json
```

The terminal shows matched, mismatched, unresolved, and decode-error counts,
plus economic and overall validation status. Exit code `0` means all applicable
validation gates pass; `2` means validation is incomplete or checks failed.
Read the JSON report for individual checks and missing evidence. Validation
does not download missing evidence: rerun the collector with `--resume` to
collect it, then validate again.

When collection and any required retries are finished, clear the endpoint:

```bash
unset GMX_ARCHIVE_RPC_URL
```

To replay or validate an existing recording, substitute its directory in steps
4 and 5. There is no need to collect it again.

### Persistent database and disk space

All evidence and reports in this example remain under
`recordings/eth-usdc-v2-fresh/`. The database is:

```text
recordings/eth-usdc-v2-fresh/.gmx-v2/catalog.sqlite
```

The database persists between collector, replay, and validator runs. Unchanged
sources are reused; it is not rebuilt on every run. A missing, corrupt, or
incompatible catalog is rebuilt from saved evidence. Allow approximately
**17 GiB for a seven-day catalog, plus space for raw evidence and reports**.
`--cache-dir <directory>` overrides the location on all three commands; use the
same cache directory for the same recording.

The already verified recording's database is at
`recordings/eth-usdc-week-2/.gmx-v2/catalog.sqlite`. Its verification logs and
reports are in `recordings/.v2-verification/`.

### Run the tests

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests/v2 -v
```

### Alternative: run without installation

If you prefer not to install the project, replace each installed command with
its module invocation and keep the same arguments:

```text
gmx-collect  → PYTHONPATH=src python -m gmx_crypto_bot_v2.collector
gmx-replay   → PYTHONPATH=src python -m gmx_crypto_bot_v2.replay
gmx-validate → PYTHONPATH=src python -m gmx_crypto_bot_v2.validator
```

For tests without installation, also prefix `python -m unittest` with
`PYTHONPATH=src`.

### Get an Alchemy archive RPC URL

The opening checkpoint reads historical contract state, so the collector needs
an archive-capable Arbitrum Mainnet endpoint. Alchemy provides archive access
for Arbitrum API keys.

1. Sign in to the [Alchemy Dashboard](https://dashboard.alchemy.com/). A new
   account normally has a default app; otherwise open **Apps**, select
   **Create new app**, and enable **Arbitrum Mainnet**.
2. Open the app, then open **Endpoints**.
3. Copy the **Arbitrum Mainnet HTTP** endpoint, not the WebSocket endpoint. It
   has this form:

   ```text
   https://arb-mainnet.g.alchemy.com/v2/YOUR_API_KEY
   ```

4. Load it without putting the credential in shell history:

   ```bash
   read -rsp "Alchemy Arbitrum archive RPC URL: " GMX_ARCHIVE_RPC_URL
   echo
   export GMX_ARCHIVE_RPC_URL
   ```

5. Run the collector normally. It reads `GMX_ARCHIVE_RPC_URL` automatically;
   no archive command-line parameter is needed. Remove it from the current
   shell afterward with `unset GMX_ARCHIVE_RPC_URL`.

Do not add the URL to `gmx-market-spec-v1.json`, `.env`, source control, command
arguments, or recording files. The collector stores only a redacted provider
URL. See Alchemy's [API-key instructions](https://www.alchemy.com/docs/create-an-api-key)
and [Arbitrum quickstart](https://www.alchemy.com/docs/reference/arbitrum-api-quickstart).

The command exits with status `2` before reading the spec or creating the output
directory if neither `GMX_ARCHIVE_RPC_URL` nor `--archive-rpc-url` is supplied.
The command-line option exists for automation, but the environment variable is
preferred because command arguments may be visible in shell history and process
listings.

## Recording schema

A recording directory contains:

```text
metadata.json     # source, chain, market, configuration and capture bounds
events.jsonl      # self-contained append-only ETH/USD replay events
raw/              # rotated compressed JSONL response bundles + manifest
completeness-report.json # source ranges, explicit gaps, and reorg findings
```

Each event envelope contains `seq`, `kind`, `block_number`,
`transaction_index`, `log_index`, local receipt timestamps, and its replay
payload. Every raw source response is stored before a replay event is derived.
`raw/rpc-000001.jsonl.gz` bundles base64-encoded exact response bodies and rotates
at 256 MiB of uncompressed records. The manifest maps each source request to its
bundle record and SHA-256. Replay events do not reference raw files. Chain events
replay in canonical block/transaction/log order; events without canonical coordinates
remain in arrival order after canonical events.

## Stateful replay

`gmx-replay` reduces target-market order, oracle, configuration, open-interest,
funding, borrowing, position, and fee events into a deterministic state digest.
`--output` writes the complete JSON state and calibration report; ordinary output
is a human summary. Older recordings that predate token metadata can use `--spec`.
The collector writes target-order terminal lifecycle events directly to
`events.jsonl`; ordinary replay reads no compressed raw artifacts.

A bounded window cannot reconstruct positions or pending orders that already
existed at its first block from window events alone. The collector therefore
uses `GMX_ARCHIVE_RPC_URL` (or `--archive-rpc-url`) to read `start_block - 1`
and writes a self-contained `opening_state_checkpoint` row into `events.jsonl`.
It enumerates GMX's active order and position sets, keeps the target market,
and captures opening open interest, cumulative borrowing, and funding state.
Older recordings can still supply an external `--opening-checkpoint` file.

## Collector

`gmx-collect --spec <spec> --output <recording>` collects the complete evidence
pipeline: base logs and opening state, historical configuration, referral/pro
state, swap state, accrual anchors, and transaction traces. Collection discovers
its dependencies directly from recorded evidence; it does not need a validation
report. Full trace coverage includes zero-fee liquidation transactions.

Use the same command with `--resume` after interruption, or to enrich a legacy
recording. Completed RPC work units and artifact digests are stored under
`.collection/`, independently of SQLite. Requests are read-only, retried with
bounded backoff, and preserved before reuse. Resume verifies completed artifacts;
conflicting identities or corrupt published evidence stop dependent work.

`GMX_ARCHIVE_RPC_URL` supplies historical state and traces. `GMX_LOGS_RPC_URL`
(or `--rpc-url`) supplies logs and public headers. Credentials are redacted from
metadata and source-error messages. HTTP observations retain their capture time
and do not substitute for historical block-pinned state.

The collector publishes `evidence-readiness.json`. Its `ready` flag means the
collection stages have their required evidence; run the offline validator to
establish whether the economic checks pass. Missing stages are listed explicitly.

The reusable catalog defaults to `<recording>/.gmx-v2/catalog.sqlite`.
`--cache-dir <directory>` places it elsewhere. It indexes new or changed sources
and reuses unchanged sources. Deleting the catalog rebuilds it from saved files
without downloading them again. The seven-day catalog is about 17 GiB.

The [V1 collector and validation reference](docs/v1-collector-and-validation.md)
preserves the old backfill commands. Separate backfills are unnecessary with the
unified V2 collector; V2 backfill module names route into its stages.

### RPC response storage

Successful RPC responses are stored in `.collection/rpc/requests.sqlite`, keyed
by source role and request hash. This persistent resume store is separate from
the rebuildable `.gmx-v2/catalog.sqlite`. Responses retain their original request,
compressed exact body, and SHA-256 checksum. Raw evidence bundles remain available
for replay and auditing. Existing per-request `.json.gz` files are migrated on
resume: each response is committed and read back before its old file is removed.
Empty legacy role directories are removed. Migration can safely resume after an
interruption; invalid files are preserved and reported as errors.
