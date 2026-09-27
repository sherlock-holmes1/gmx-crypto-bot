"""Compatibility entry point; implementation lives in the layered V2 package."""

from gmx_crypto_bot_v2.application.replay import ReplayReport as ReplayReport
from gmx_crypto_bot_v2.application.replay import (
    _replay_with_state as _replay_with_state,
)
from gmx_crypto_bot_v2.application.replay import canonical_events as canonical_events
from gmx_crypto_bot_v2.application.replay import main as main
from gmx_crypto_bot_v2.application.replay import replay as replay

if __name__ == "__main__":
    raise SystemExit(main())
