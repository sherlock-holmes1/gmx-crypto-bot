"""Unified V2 collector entry point."""

from gmx_crypto_bot_v2.application.collection import main as main
from gmx_crypto_bot_v2.collection.base import GmxCollector as GmxCollector
from gmx_crypto_bot_v2.collection.ranges import RangeGap as RangeGap
from gmx_crypto_bot_v2.collection.ranges import adaptive_ranges as adaptive_ranges
from gmx_crypto_bot_v2.domain.filtering import contains_address as contains_address
from gmx_crypto_bot_v2.domain.filtering import contains_value as contains_value
from gmx_crypto_bot_v2.domain.filtering import event_name as event_name
from gmx_crypto_bot_v2.domain.filtering import target_snapshot as target_snapshot
from gmx_crypto_bot_v2.sources.rpc import PublicJsonRpc as PublicJsonRpc
from gmx_crypto_bot_v2.sources.rpc import SourceError as SourceError
from gmx_crypto_bot_v2.sources.rpc import redact_rpc_endpoint as redact_rpc_endpoint

if __name__ == "__main__":
    raise SystemExit(main())
