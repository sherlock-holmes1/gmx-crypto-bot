"""Collect route-market configuration, pools and shared virtual inventories."""

from __future__ import annotations

import json

from gmx_crypto_bot_v2.domain.referral import address, words
from gmx_crypto_bot_v2.domain.swap_keys import (
    MARKET_FIELDS,
    ZERO_ID,
    call_data,
    key,
    market_field_key,
    market_keys,
)
from gmx_crypto_bot_v2.evidence.anchors import _recorded_block_hash
from gmx_crypto_bot_v2.evidence.discovery import discover


def collect(recording, rpc, *, requirements=None):
    m = json.loads((recording / "metadata.json").read_text())
    quality = json.loads((recording / "completeness-report.json").read_text())
    if not quality.get("complete") or quality.get("gaps") or quality.get("reorgs"):
        raise ValueError("incomplete recording")
    block = quality["source_block_range"]["from"] - 1
    expected_hash = _recorded_block_hash(recording, block)
    if rpc.call("eth_getBlockByNumber", [hex(block), False])["hash"] != expected_hash:
        raise ValueError("opening hash mismatch")
    requirements = requirements or discover(recording)
    markets = sorted(requirements.markets)
    receivers = sorted(requirements.ui_receivers)
    store = m["contracts"]["data_store"].lower()
    calls = {}

    def batch(jobs):
        jobs = list(
            dict.fromkeys(
                (to.lower(), data)
                for to, data in jobs
                if to.lower() + ":" + data not in calls
            )
        )
        for i in range(0, len(jobs), 50):
            group = jobs[i : i + 50]
            results = rpc.call_many(
                "eth_call",
                [[{"to": to, "data": data}, hex(block)] for to, data in group],
            )
            if len(results) != len(group):
                raise ValueError("incomplete archive batch")
            for (to, data), result in zip(group, results):
                if (
                    not isinstance(result, str)
                    or not result.startswith("0x")
                    or len(result) < 66
                    or (len(result) - 2) % 64
                ):
                    raise ValueError("invalid archive result")
                calls[to + ":" + data] = result

    batch(
        [
            (store, call_data("getAddress(bytes32)", market_field_key(market, field)))
            for market in markets
            for field in MARKET_FIELDS
        ]
        + [
            (store, call_data("getBytes32(bytes32)", key("VIRTUAL_MARKET_ID", market)))
            for market in markets
        ]
    )
    tokens = {}
    virtual_ids = set()
    jobs = [
        (store, call_data("getUint(bytes32)", key(name)))
        for name in ("SWAP_FEE_RECEIVER_FACTOR", "MAX_UI_FEE_FACTOR")
    ]
    for market in markets:
        props = [
            address(
                words(
                    calls[
                        store
                        + ":"
                        + call_data(
                            "getAddress(bytes32)", market_field_key(market, field)
                        )
                    ],
                    1,
                )[0]
            )
            for field in MARKET_FIELDS
        ]
        if props[0] != market or props[2] == props[3]:
            raise ValueError("invalid or single-token swap market")
        tokens[market] = props[2:]
        virtual = calls[
            store
            + ":"
            + call_data("getBytes32(bytes32)", key("VIRTUAL_MARKET_ID", market))
        ]
        words(virtual, 1)
        if virtual != ZERO_ID:
            virtual_ids.add(virtual)
        jobs.extend(
            (store, call_data("getUint(bytes32)", k))
            for k in market_keys(market).values()
        )
        for token in tokens[market]:
            jobs.extend(
                (store, call_data("getUint(bytes32)", key(name, market, token)))
                for name in ("POOL_AMOUNT", "SWAP_IMPACT_POOL_AMOUNT")
            )
    jobs.extend(
        (store, call_data("getUint(bytes32)", key("UI_FEE_FACTOR", receiver)))
        for receiver in receivers
    )
    jobs.extend(
        (
            store,
            call_data(
                "getUint(bytes32)", key("VIRTUAL_INVENTORY_FOR_SWAPS", vid, side)
            ),
        )
        for vid in sorted(virtual_ids)
        for side in (True, False)
    )
    batch(jobs)
    if rpc.call("eth_getBlockByNumber", [hex(block), False])["hash"] != expected_hash:
        raise ValueError("opening hash changed during backfill")
    return {
        "schema": "GmxSwapOpeningState",
        "version": 1,
        "opening_block": block,
        "opening_hash": expected_hash,
        "market": m["market"]["market_token_address"].lower(),
        "data_store": store,
        "markets": markets,
        "receivers": receivers,
        "calls": calls,
    }
