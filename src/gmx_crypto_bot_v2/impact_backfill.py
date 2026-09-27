"""Compatibility entry point; implementation lives in the layered V2 package."""

from gmx_crypto_bot_v2.collection.impact import (
    fetch_opening_impact_factors as fetch_opening_impact_factors,
)


def main():
    from gmx_crypto_bot_v2.application.collection import stage_main

    return stage_main("impact")


if __name__ == "__main__":
    raise SystemExit(main())
