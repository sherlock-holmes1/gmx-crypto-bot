"""Compatibility entry point; implementation lives in the layered V2 package."""

from gmx_crypto_bot_v2.collection.swaps import collect as collect


def main():
    from gmx_crypto_bot_v2.application.collection import stage_main

    return stage_main("swaps")


if __name__ == "__main__":
    raise SystemExit(main())
