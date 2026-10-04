"""Block-pinned risk configuration and its recorded change history."""

from __future__ import annotations

import json
from pathlib import Path

from gmx_crypto_bot_v2.domain.configuration import ConfigKey
from gmx_crypto_bot_v2.domain.events import decode_event_log, event_name_from_data
from gmx_crypto_bot_v2.domain.keys import config_base_key, config_market_data
from gmx_crypto_bot_v2.domain.swap_keys import call_data
from gmx_crypto_bot_v2.evidence.anchors import _recorded_block_hash
from gmx_crypto_bot_v2.evidence.repository import raw_logs


def risk_keys(market: str) -> dict[str, ConfigKey]:
    """DataStore keys used by position limits, PnL caps, and liquidation."""
    market = market.lower()
    trader_type = config_base_key("MAX_PNL_FACTOR_FOR_TRADERS")
    return {
        "min_collateral_usd": ConfigKey("MIN_COLLATERAL_USD"),
        "min_position_size_usd": ConfigKey("MIN_POSITION_SIZE_USD"),
        "min_collateral_factor": ConfigKey("MIN_COLLATERAL_FACTOR", config_market_data(market)),
        "min_collateral_factor_for_liquidation": ConfigKey(
            "MIN_COLLATERAL_FACTOR_FOR_LIQUIDATION", config_market_data(market)
        ),
        "max_position_impact_factor_for_liquidations": ConfigKey(
            "MAX_POSITION_IMPACT_FACTOR_FOR_LIQUIDATIONS", config_market_data(market)
        ),
        "min_collateral_factor_for_open_interest_multiplier_long": ConfigKey(
            "MIN_COLLATERAL_FACTOR_FOR_OPEN_INTEREST_MULTIPLIER",
            config_market_data(market) + f"{1:064x}",
        ),
        "min_collateral_factor_for_open_interest_multiplier_short": ConfigKey(
            "MIN_COLLATERAL_FACTOR_FOR_OPEN_INTEREST_MULTIPLIER",
            config_market_data(market) + f"{0:064x}",
        ),
        "max_pnl_factor_for_traders_long": ConfigKey(
            "MAX_PNL_FACTOR", trader_type + market[2:].zfill(64) + f"{1:064x}"
        ),
        "max_pnl_factor_for_traders_short": ConfigKey(
            "MAX_PNL_FACTOR", trader_type + market[2:].zfill(64) + f"{0:064x}"
        ),
        "liquidation_fee_factor": ConfigKey("LIQUIDATION_FEE_FACTOR", config_market_data(market)),
    }


def _point(recording: Path, rpc, store: str, block: int, keys: dict[str, ConfigKey]) -> dict:
    expected_hash = _recorded_block_hash(recording, block)
    header = rpc.call("eth_getBlockByNumber", [hex(block), False])
    if not isinstance(header, dict) or header.get("hash", "").lower() != expected_hash:
        raise ValueError("archive risk-configuration block hash mismatch")
    jobs = list(keys.items())
    results = rpc.call_many(
        "eth_call",
        [
            [{"to": store, "data": call_data("getUint(bytes32)", key.storage_key)}, hex(block)]
            for _, key in jobs
        ],
    )
    if len(results) != len(jobs):
        raise ValueError("incomplete risk-configuration archive batch")
    values = {}
    for (field, key), raw in zip(jobs, results):
        if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) != 66:
            raise ValueError("invalid risk-configuration archive result for " + field)
        values[field] = {
            "storage_key": key.storage_key,
            "result": raw.lower(),
            "value": int(raw, 16),
        }
    header = rpc.call("eth_getBlockByNumber", [hex(block), False])
    if not isinstance(header, dict) or header.get("hash", "").lower() != expected_hash:
        raise ValueError("archive risk-configuration block changed")
    return {"block_number": block, "block_hash": expected_hash, "values": values}


def collect(recording: Path, rpc) -> dict:
    """Backfill only the missing configuration; preserve the base recording."""
    recording = Path(recording)
    quality = json.loads((recording / "completeness-report.json").read_text())
    if quality.get("complete") is not True or quality.get("gaps") or quality.get("reorgs"):
        raise ValueError("risk configuration requires a complete recording")
    bounds = quality["source_block_range"]
    start, end = bounds["from"], bounds["to"]
    metadata = json.loads((recording / "metadata.json").read_text())
    market = metadata["market"]["market_token_address"].lower()
    store = metadata["contracts"]["data_store"].lower()
    keys = risk_keys(market)
    opening = _point(recording, rpc, store, start - 1, keys)
    closing = _point(recording, rpc, store, end, keys)

    changes = _recorded_changes(recording, keys, start, end, rpc)
    result = {
        "schema": "GmxRiskConfiguration",
        "version": 1,
        "market": market,
        "data_store": store,
        "opening": opening,
        "closing": closing,
        "changes": changes,
    }
    verify(recording, result, recorded_changes=changes)
    return result


def _recorded_changes(
    recording: Path, keys: dict[str, ConfigKey], start: int, end: int, rpc=None
) -> list[dict]:
    wanted = {(key.base_key.lower(), key.data.lower()): field for field, key in keys.items()}
    changes = []
    seen = set()
    verified_hashes = {}
    for log in raw_logs(recording, {"SetUint"}):
        if event_name_from_data(log.get("data", "")) != "SetUint":
            continue
        if log.get("removed"):
            raise ValueError("removed risk-configuration log")
        values = decode_event_log(log["data"]).values
        field = wanted.get(
            (str(values.get("baseKey", "")).lower(), str(values.get("data", "")).lower())
        )
        if field is None:
            continue
        block = int(log["blockNumber"], 16)
        if not start <= block <= end:
            raise ValueError("risk-configuration write outside recording")
        if rpc is not None and block not in verified_hashes:
            header = rpc.call("eth_getBlockByNumber", [hex(block), False])
            if not isinstance(header, dict) or not isinstance(header.get("hash"), str):
                raise ValueError("missing archive risk-configuration change header")
            verified_hashes[block] = header["hash"].lower()
        block_hash = log["blockHash"].lower()
        if rpc is not None and block_hash != verified_hashes[block]:
            raise ValueError("risk-configuration log block hash mismatch")
        coordinate = (block, int(log["transactionIndex"], 16), int(log["logIndex"], 16))
        if coordinate in seen:
            raise ValueError("duplicate risk-configuration coordinate")
        seen.add(coordinate)
        changes.append({
            "field": field,
            "coordinate": coordinate,
            "block_hash": block_hash,
            "transaction_hash": log["transactionHash"].lower(),
            "value": values["value"],
        })
    changes.sort(key=lambda item: item["coordinate"])
    return changes


def verify(recording: Path, snapshot: dict, *, recorded_changes=None) -> None:
    """Reject gaps, wrong keys, changed anchors, and inconsistent final values."""
    recording = Path(recording)
    quality = json.loads((recording / "completeness-report.json").read_text())
    if quality.get("complete") is not True or quality.get("gaps") or quality.get("reorgs"):
        raise ValueError("incomplete recording for risk configuration")
    bounds = quality["source_block_range"]
    metadata = json.loads((recording / "metadata.json").read_text())
    market = metadata["market"]["market_token_address"].lower()
    keys = risk_keys(market)
    if (snapshot.get("schema") != "GmxRiskConfiguration"
            or snapshot.get("version") != 1
            or snapshot.get("market") != market
            or snapshot.get("data_store") != metadata["contracts"]["data_store"].lower()):
        raise ValueError("risk-configuration snapshot identity mismatch")
    for label, block in (("opening", bounds["from"] - 1), ("closing", bounds["to"])):
        point = snapshot.get(label, {})
        if (point.get("block_number") != block
                or point.get("block_hash") != _recorded_block_hash(recording, block)
                or set(point.get("values", {})) != set(keys)):
            raise ValueError("risk-configuration anchor or coverage mismatch")
        for field, key in keys.items():
            item = point["values"][field]
            raw = item.get("result", "")
            if (item.get("storage_key") != key.storage_key
                    or not isinstance(raw, str) or not raw.startswith("0x")
                    or len(raw) != 66 or int(raw, 16) != item.get("value")):
                raise ValueError("invalid risk-configuration value for " + field)
    previous = snapshot["opening"]["values"]
    final = {field: previous[field]["value"] for field in keys}
    last_coordinate = None
    for change in snapshot.get("changes", []):
        field, coordinate = change.get("field"), change.get("coordinate")
        if (field not in keys or not isinstance(coordinate, (list, tuple))
                or len(coordinate) != 3 or any(type(x) is not int for x in coordinate)
                or not bounds["from"] <= coordinate[0] <= bounds["to"]
                or last_coordinate is not None and tuple(coordinate) <= last_coordinate
                or not isinstance(change.get("block_hash"), str)
                or not change["block_hash"].startswith("0x")
                or not isinstance(change.get("transaction_hash"), str)
                or not change["transaction_hash"].startswith("0x")
                or type(change.get("value")) is not int or change["value"] < 0):
            raise ValueError("invalid risk-configuration change")
        last_coordinate = tuple(coordinate)
        final[field] = change["value"]
    if final != {field: snapshot["closing"]["values"][field]["value"] for field in keys}:
        raise ValueError("risk-configuration history disagrees with closing anchor")
    if recorded_changes is None:
        recorded_changes = _recorded_changes(
            recording, keys, bounds["from"], bounds["to"]
        )
    if [dict(change, coordinate=tuple(change["coordinate"])) for change in snapshot.get("changes", [])] != recorded_changes:
        raise ValueError("risk-configuration changes disagree with recorded SetUint logs")
