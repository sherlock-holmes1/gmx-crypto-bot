"""Reconstruct swap configuration and pool state for each route hop."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from gmx_crypto_bot_v2.domain.keys import config_base_key, keccak256
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
from gmx_crypto_bot_v2.evidence.snapshots import snapshot_path
from gmx_crypto_bot_v2.models.swaps import TOKEN_TOLERANCE, USD_TOLERANCE, price_swap

STATE_EVENTS = {
    "PoolAmountUpdated",
    "SwapImpactPoolAmountUpdated",
    "VirtualSwapInventoryUpdated",
    "OraclePriceUpdate",
    "SwapInfo",
    "SwapFeesCollected",
    "SetBytes32",
}


CHECKS = (
    "independent_swap_state",
    "historical_swap_fees",
    "independent_swap_price_impact",
    "independent_swap_output",
)


CONFIG_BASES = {
    config_base_key(n)
    for n in (
        "SWAP_FEE_FACTOR",
        "SWAP_IMPACT_FACTOR",
        "SWAP_IMPACT_EXPONENT_FACTOR",
        "SWAP_FEE_RECEIVER_FACTOR",
        "MAX_UI_FEE_FACTOR",
        "UI_FEE_FACTOR",
    )
}


def coordinate(e):
    return (e["block_number"], e["transaction_index"], e["log_index"])


class SwapReplay:
    def __init__(self, recording: Path, metadata: dict):
        self.available = False
        self.markets = {}
        self.virtual_ids = set()
        self.tokens = set()
        self.results = defaultdict(list)
        path = snapshot_path(recording, "swap-opening-state.json")
        if not path.exists():
            return
        s = json.loads(path.read_text())
        q = json.loads((recording / "completeness-report.json").read_text())
        self.start, self.end = (
            q["source_block_range"]["from"],
            q["source_block_range"]["to"],
        )
        if not q.get("complete") or q.get("gaps") or q.get("reorgs"):
            raise ValueError("incomplete swap evidence")
        store = metadata["contracts"]["data_store"].lower()
        if (
            s.get("schema") != "GmxSwapOpeningState"
            or s.get("version") != 1
            or s.get("data_store") != store
            or s.get("market") != metadata["market"]["market_token_address"].lower()
            or s.get("opening_block") != self.start - 1
            or s.get("opening_hash") != _recorded_block_hash(recording, self.start - 1)
        ):
            raise ValueError("swap snapshot identity mismatch")
        calls = s["calls"]
        self.config = {}
        self.state = {}
        self.bad = set()
        self.unsupported = set()
        self.oracles = {}

        def uint(k):
            value = words(calls[store + ":" + call_data("getUint(bytes32)", k)], 1)[0]
            self.config[k] = value
            return value

        for n in ("SWAP_FEE_RECEIVER_FACTOR", "MAX_UI_FEE_FACTOR"):
            uint(key(n))
        self._ui_keys = {key("UI_FEE_FACTOR", receiver) for receiver in s["receivers"]}
        for k in self._ui_keys:
            uint(k)
        for market in s["markets"]:
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
                raise ValueError("invalid swap market")
            vid = calls[
                store
                + ":"
                + call_data("getBytes32(bytes32)", key("VIRTUAL_MARKET_ID", market))
            ]
            words(vid, 1)
            self.markets[market] = {"long": props[2], "short": props[3], "virtual": vid}
            self.tokens.update(props[2:])
            for k in market_keys(market).values():
                uint(k)
            for token in props[2:]:
                for kind, name in [
                    ("pool", "POOL_AMOUNT"),
                    ("impact", "SWAP_IMPACT_POOL_AMOUNT"),
                ]:
                    self.state[kind, market, token] = uint(key(name, market, token))
            if vid != ZERO_ID:
                self.virtual_ids.add(vid)
        for vid in self.virtual_ids:
            for side in (True, False):
                self.state["virtual", vid, side] = uint(
                    key("VIRTUAL_INVENTORY_FOR_SWAPS", vid, side)
                )
        self.available = True

    def wants(self, name, v):
        if not self.available:
            return False
        if name == "OraclePriceUpdate":
            return v.get("token") in self.tokens
        if name == "VirtualSwapInventoryUpdated":
            return v.get("virtualMarketId") in self.virtual_ids
        if name == "SetBytes32":
            return v.get("baseKey") == config_base_key("VIRTUAL_MARKET_ID")
        return v.get("market") in self.markets

    def replay(self, events, config_events, target_keys):
        if not self.available:
            return
        timeline = sorted(
            events
            + [
                e
                for e in config_events
                if e["event_name"] == "UiFeeFactorUpdated"
                or e["values"].get("baseKey") in CONFIG_BASES
            ],
            key=coordinate,
        )
        tails = defaultdict(list)
        pending = {}
        tx = None
        seen = {}
        for e in timeline:
            c = coordinate(e)
            if c in seen:
                if seen[c] != e:
                    raise ValueError("conflicting swap event coordinate")
                continue
            seen[c] = e
            if not self.start <= c[0] <= self.end:
                continue
            if tx != e["transaction_hash"]:
                tails.clear()
                pending.clear()
                tx = e["transaction_hash"]
            name = e["event_name"]
            v = e["values"]
            market = v.get("market")
            if name == "SetUint":
                base = v["baseKey"]
                data = v["data"]
                k = (
                    "0x" + keccak256(bytes.fromhex(base[2:] + data[2:])).hex()
                    if data != "0x"
                    else base
                )
                if k in self.config:
                    self.config[k] = v["value"]
                continue
            if name == "UiFeeFactorUpdated":
                k = key("UI_FEE_FACTOR", v["account"])
                if k in self.config:
                    self.config[k] = v["uiFeeFactor"]
                continue
            if name == "SetBytes32":
                market = address(words(v["data"], 1)[0])
                if (
                    market in self.markets
                    and v["value"] != self.markets[market]["virtual"]
                ):
                    self.unsupported.add(market)
                continue
            if name == "OraclePriceUpdate":
                self.oracles[v["token"]] = (tx, v["minPrice"], v["maxPrice"])
                continue
            if name in {
                "PoolAmountUpdated",
                "SwapImpactPoolAmountUpdated",
                "VirtualSwapInventoryUpdated",
            }:
                if name == "VirtualSwapInventoryUpdated":
                    k = ("virtual", v["virtualMarketId"], v["isLongToken"])
                else:
                    k = (
                        "pool" if name == "PoolAmountUpdated" else "impact",
                        market,
                        v["token"],
                    )
                if k not in self.state:
                    continue
                before = self.state[k]
                if before + v["delta"] != v["nextValue"] or v["nextValue"] < 0:
                    self.bad.add(k)
                self.state[k] = v["nextValue"]
                tails[market].append((e, k, before))
                continue
            if name == "SwapInfo":
                order = v.get("orderKey")
                if order in target_keys:
                    pending[order, market] = self.model(e, tails[market])
                tails[market].clear()
                continue
            if name == "SwapFeesCollected":
                order = v.get("tradeKey")
                result = pending.pop((order, market), None)
                if result is not None:
                    self.finish(result, e)
                    self.results[order].append(result)
        return self.results

    def model(self, e, tail):
        v = e["values"]
        market = v["market"]
        a = v["tokenIn"]
        b = v["tokenOut"]
        props = self.markets[market]
        result = {
            "coordinate": list(coordinate(e)),
            "market": market,
            "token_in": a,
            "token_out": b,
            "checks": {name: "unavailable" for name in CHECKS},
            "observed_swap": v,
        }
        try:
            if market in self.unsupported or {a, b} != {props["long"], props["short"]}:
                raise KeyError("unsupported swap market state")
            pool_updates = [x for x in tail if x[1][0] == "pool"][-2:]
            if len(pool_updates) != 2 or [x[1][2] for x in pool_updates] != [a, b]:
                raise KeyError("ambiguous swap pool updates")
            used = [
                ("pool", market, a),
                ("pool", market, b),
                ("impact", market, a),
                ("impact", market, b),
            ]
            pool = [x[2] for x in pool_updates]
            impact_pool = [self.state["impact", market, t] for t in (a, b)]
            impact_deltas = [0, 0]
            for i, t in enumerate((a, b)):
                updates = [x for x in tail if x[1] == ("impact", market, t)]
                if updates:
                    impact_pool[i] = updates[-1][2]
                    impact_deltas[i] = updates[-1][0]["values"]["delta"]
            virtual = None
            if props["virtual"] != ZERO_ID:
                sides = [a == props["long"], b == props["long"]]
                virtual = []
                for i, side in enumerate(sides):
                    k = ("virtual", props["virtual"], side)
                    used.append(k)
                    updates = [x for x in tail if x[1] == k]
                    if (
                        not updates
                        or updates[-1][0]["values"]["delta"]
                        != pool_updates[i][0]["values"]["delta"]
                    ):
                        raise KeyError("swap virtual delta not reconciled")
                    virtual.append(updates[-1][2])
            if any(k in self.bad for k in used):
                raise KeyError("swap state continuity failed")
            pa, pb = self.oracles[a], self.oracles[b]
            if pa[0] != e["transaction_hash"] or pb[0] != e["transaction_hash"]:
                raise KeyError("missing same-transaction swap oracle")
            factors = {name: self.config[k] for name, k in market_keys(market).items()}
            result.update(
                pre_pool=pool,
                pre_virtual=virtual,
                pre_impact_pool=impact_pool,
                prices=[pa[1], pa[2], pb[1], pb[2]],
                factors=factors,
                receiver_factor=self.config[key("SWAP_FEE_RECEIVER_FACTOR")],
                ui_cap=self.config[key("MAX_UI_FEE_FACTOR")],
                ui_settings={
                    k: self.config[k] for k in self.config if k in self.ui_keys
                },
                impact_deltas=impact_deltas,
                pool_deltas=[x[0]["values"]["delta"] for x in pool_updates],
            )
        except (KeyError, ValueError) as error:
            result["unavailable_reason"] = str(error)
        return result

    @property
    def ui_keys(self):
        return getattr(self, "_ui_keys", set())

    def finish(self, result, fees):
        if "unavailable_reason" in result:
            return
        v = result["observed_swap"]
        f = fees["values"]
        checks = result["checks"]
        try:
            ui = min(
                result["ui_cap"],
                result["ui_settings"][key("UI_FEE_FACTOR", f["uiFeeReceiver"])],
            )
            model = price_swap(
                v["amountIn"],
                result["prices"],
                result["pre_pool"],
                result["pre_virtual"],
                result["pre_impact_pool"],
                result["factors"],
                result["receiver_factor"],
                ui,
            )
        except (KeyError, ValueError) as error:
            result["unavailable_reason"] = str(error)
            return
        result["ui_fee_receiver"] = f.get("uiFeeReceiver")
        result["modeled"] = model
        result["usd_tolerance"] = USD_TOLERANCE
        result["token_tolerance"] = TOKEN_TOLERANCE
        check = lambda name, passed: checks.update(
            {name: "matched" if passed else "mismatch"}
        )
        check(
            "historical_swap_fees",
            f.get("swapFeeType") == config_base_key("SWAP_FEE_TYPE")
            and f.get("token") == v["tokenIn"]
            and f.get("tradeKey") == v["orderKey"]
            and f.get("tokenPrice") == result["prices"][0]
            and all(
                f.get(k) == model[k]
                for k in (
                    "feeReceiverAmount",
                    "feeAmountForPool",
                    "uiFeeReceiverFactor",
                    "uiFeeAmount",
                )
            )
            and f.get("amountAfterFees") == model["amountInAfterFees"]
            and v.get("amountInAfterFees") == model["amountInAfterFees"],
        )
        check(
            "independent_swap_price_impact",
            abs(v["priceImpactUsd"] - model["priceImpactUsd"]) <= USD_TOLERANCE
            and abs(v["priceImpactAmount"] - model["priceImpactAmount"])
            <= TOKEN_TOLERANCE
            and abs(v["tokenInPriceImpactAmount"] - model["tokenInPriceImpactAmount"])
            <= TOKEN_TOLERANCE,
        )
        # No output allowance is needed with exact PRBMath integer rounding.
        output_tolerance = (
            TOKEN_TOLERANCE
            + (TOKEN_TOLERANCE * result["prices"][0] + result["prices"][3] - 1)
            // result["prices"][3]
        )
        result["output_token_tolerance"] = output_tolerance
        check(
            "independent_swap_output",
            v["tokenInPrice"] == result["prices"][0]
            and v["tokenOutPrice"] == result["prices"][3]
            and abs(v["amountOut"] - model["amountOut"]) <= output_tolerance,
        )
        expected_impact_deltas = (
            [-model["tokenInPriceImpactAmount"], -model["priceImpactAmount"]]
            if model["priceImpactUsd"] > 0
            else [-model["priceImpactAmount"], 0]
        )
        check(
            "independent_swap_state",
            all(
                abs(a - b) <= TOKEN_TOLERANCE
                for a, b in zip(result["impact_deltas"], expected_impact_deltas)
            )
            and abs(result["pool_deltas"][0] - model["pool_delta_in"])
            <= TOKEN_TOLERANCE
            and abs(result["pool_deltas"][1] - model["pool_delta_out"])
            <= output_tolerance,
        )
