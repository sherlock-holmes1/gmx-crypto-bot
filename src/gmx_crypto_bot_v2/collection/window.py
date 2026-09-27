"""Resolve a rolling window and fetch the validator's closing configuration."""

from copy import deepcopy
from datetime import datetime, timezone

from gmx_crypto_bot_v2.domain.configuration import fee_keys
from gmx_crypto_bot_v2.domain.keys import FACTOR_FIELDS, MARKET_FIELDS
from gmx_crypto_bot_v2.domain.swap_keys import call_data, key


def utc(timestamp):
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def resolve_window(template, rpc, days, confirmations):
    """Freeze the last N days at a confirmed header, with exact archive reads."""
    if days <= 0 or confirmations < 0:
        raise ValueError("days must be positive and confirmations nonnegative")
    if int(rpc("eth_chainId", []), 16) != template["deployment"]["chain_id"]:
        raise ValueError("archive provider chain differs from spec")
    end = int(rpc("eth_blockNumber", []), 16) - confirmations
    if end <= 0:
        raise ValueError("no sufficiently confirmed block available")

    def header(block):
        value = rpc("eth_getBlockByNumber", [hex(block), False])
        if not isinstance(value, dict) or int(value["number"], 16) != block:
            raise ValueError("missing or inconsistent window header")
        return value

    closing = header(end)
    end_time = int(closing["timestamp"], 16)
    start_time = end_time - days * 86400
    if start_time <= int(header(0)["timestamp"], 16):
        raise ValueError("requested window predates available chain history")
    low, high = 1, end
    while low < high:
        middle = (low + high) // 2
        if int(header(middle)["timestamp"], 16) < start_time:
            low = middle + 1
        else:
            high = middle
    start = low
    market = template["deployment"]["market_token_address"]
    store = template["contracts"]["data_store"]
    configuration = {}

    def read(signature, storage_key):
        value = rpc(
            "eth_call",
            [{"to": store, "data": call_data(signature, storage_key)}, hex(end)],
        )
        if not isinstance(value, str) or not value.startswith("0x") or len(value) != 66:
            raise ValueError("invalid closing configuration response")
        int(value, 16)
        return value

    # Only freshly read fields become the new pinned configuration. Other
    # historical state is collected by the existing dedicated snapshot stages.
    for (name, positive), field in FACTOR_FIELDS.items():
        configuration[field] = str(
            int(read("getUint(bytes32)", key(name, market, positive)), 16)
        )
    for name, field in MARKET_FIELDS.items():
        configuration[field] = str(int(read("getUint(bytes32)", key(name, market)), 16))
    for description in fee_keys(market, set()).values():
        if description.pinned_field:
            configuration[description.pinned_field] = str(
                int(read("getUint(bytes32)", description.storage_key), 16)
            )
    # GMX Keys.virtualTokenIdKey: keccak256(abi.encode(VIRTUAL_TOKEN_ID, token)).
    # https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/data/Keys.sol
    configuration["virtual_index_token_id"] = read(
        "getBytes32(bytes32)",
        key("VIRTUAL_TOKEN_ID", template["tokens"]["index"]["address"]),
    )
    if header(end)["hash"].lower() != closing["hash"].lower():
        raise ValueError("closing block changed during window configuration capture")
    spec = deepcopy(template)
    # These HTTP-era observations must not be relabelled as current block state.
    for field in ("market_limits", "selection_evidence_raw_1e30"):
        spec.pop(field, None)
    spec["configuration_raw"] = configuration
    spec["observed_at_utc"] = utc(end_time)
    spec["observation_window"] = {
        "start_utc": utc(start_time),
        "end_inclusive_utc": utc(end_time),
        "duration_hours": days * 24,
        "start_block": start,
        "end_block": end,
        "start_block_rule": "first block with timestamp at or after start_utc",
        "end_block_rule": "head minus confirmation depth at initial selection",
    }
    spec["anchor_block"] = {
        "number": end,
        "number_hex": hex(end),
        "hash": closing["hash"],
        "timestamp_utc": utc(end_time),
        "rpc_url": template.get("anchor_block", {}).get(
            "rpc_url", "https://arb1.arbitrum.io/rpc"
        ),
    }
    spec["source_notes"] = [
        "Rolling window fixed once; resume retains the original range.",
        "Closing configuration fields are archive eth_call reads at anchor_block.",
        "Deployment addresses and token definitions are inherited from the input spec.",
    ]
    return spec
