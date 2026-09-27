"""Compatibility entry point; implementation lives in the layered V2 package."""

from gmx_crypto_bot_v2.collection.referral import collect as collect
from gmx_crypto_bot_v2.collection.referral import (
    collect_referral_ranges as collect_referral_ranges,
)
from gmx_crypto_bot_v2.collection.referral import executed_traders as executed_traders


def main():
    from gmx_crypto_bot_v2.application.collection import stage_main

    return stage_main("referral")


if __name__ == "__main__":
    raise SystemExit(main())
