"""Compatibility entry point; implementation lives in the layered V2 package."""

from gmx_crypto_bot_v2.collection.fees import (
    fetch_opening_fee_configuration as fetch_opening_fee_configuration,
)


def main():
    from gmx_crypto_bot_v2.application.collection import stage_main

    return stage_main("fees")


if __name__ == "__main__":
    raise SystemExit(main())
