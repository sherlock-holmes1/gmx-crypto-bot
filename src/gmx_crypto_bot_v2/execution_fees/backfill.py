"""Compatibility entry point; implementation lives in the layered V2 package."""

from gmx_crypto_bot_v2.collection.traces import (
    PAYMENT_GAS_PROGRAM_COUNTERS as PAYMENT_GAS_PROGRAM_COUNTERS,
)
from gmx_crypto_bot_v2.collection.traces import _number as _number
from gmx_crypto_bot_v2.collection.traces import collect_gas_probe as collect_gas_probe
from gmx_crypto_bot_v2.collection.traces import (
    collect_transaction as collect_transaction,
)
from gmx_crypto_bot_v2.collection.traces import recorded_hashes as recorded_hashes
from gmx_crypto_bot_v2.collection.traces import (
    select_transactions as select_transactions,
)


def main():
    from gmx_crypto_bot_v2.application.collection import stage_main

    return stage_main("traces")


if __name__ == "__main__":
    raise SystemExit(main())
