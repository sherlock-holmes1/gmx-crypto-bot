# V2 validation results

The rewrite lives in `src/gmx_crypto_bot_v2`; V1 remains available. See
[architecture](architecture.md) and [operations](v2-operations.md).

## Automated checks

- Original suite: 116 tests passed.
- V2 suite: 134 tests passed, including collection interruption/resume,
  source integrity, cache corruption/rebuild, offline validation parity,
  and dependency boundaries. Rolling-window tests additionally cover seven-day
  selection, fresh configuration, chain/reorg rejection, and offline frozen resume.
- Built wheel installed successfully; all three installed console commands
  (`gmx-collect`, `gmx-replay`, `gmx-validate`) passed their help smoke checks.
- Static checks cover undefined names, imports, formatting, and diff whitespace.

## Existing recording regression

`recordings/eth-usdc-week-2` was validated offline with network sockets disabled.
The complete V2 JSON report equals the freshly generated V1 baseline. Cold and
warm V2 results also agree.

| Result | Value |
| --- | ---: |
| Orders created | 3,618 |
| Opening terminal orders | 132 |
| Terminal orders matched | 3,614 |
| Mismatches | 0 |
| Boundary unresolved | 136 |
| Decode errors | 0 |
| Check families preserved | 61 |
| Liquidation settlements matched | 103 |
| Execution fee checks matched | 3,511 |
| Execution fee checks not applicable | 103 |
| Remaining economic checks | 0 |

The final report is complete; boundary unresolved entries are retained exactly
as in V1. Warm access reused all 74 sources, indexed none, and decompressed no
bundles. A fixture also verifies identical validation output after deleting and
rebuilding its catalog.

Replay matches V1, and two V2 replay passes agree: 378,744 events, 311,468
canonical events, and 67,276 arrival-only events across blocks 505256001–507621797.

- State digest: `442e6e5d9016bf196fb924e93f036b2096d6c8a8b5d8d1cc87bed79426579c58`
- Event digest: `22386ba64901079ae52fc2badd2533f3394c42d1d9e96afebedd8fac3237d375`

## Local measurements

These are single observations with different workloads, not controlled benchmarks.

| Workload | Seconds | Peak RSS (KiB) |
| --- | ---: | ---: |
| V1 validation CLI | 266.52 | 1,548,620 |
| Initial V2 cold catalog build and validation | 488.87 | 1,834,892 |
| Final V2 warm validation and full baseline comparison | 239.55 | 1,933,504 |
| V1 replay, one pass | 35.64 | 7,493,356 |
| V2 replay with verification, two passes | 64.04 | 309,232 |

The cold measurement predates final query improvements. The warm measurement
includes baseline loading and comparison serialization; it does not establish
a validator memory improvement.

The persistent SQLite catalog is approximately 16.76 GiB and resides at
`recordings/eth-usdc-week-2/.gmx-v2/catalog.sqlite`. Reports, test logs, packaging
artifacts, and the machine-readable regression summary are retained in the
ignored `recordings/.v2-verification/` directory.

## Scope of verification

Fresh collection and interruption recovery were tested with mocked providers.
The real existing recording was validated and replayed offline. A fresh live
archive-provider collection was not run in this session; provider availability
and production throughput remain operational checks for the local environment.

To repeat the offline regression using the default persistent cache:

```bash
PYTHONPATH=src python scripts/verify_v2_regression.py \
  recordings/eth-usdc-week-2 \
  --baseline recordings/.v2-verification/gmx-v1-baseline.json \
  --output recordings/.v2-verification/gmx-v2-regression.json
```
