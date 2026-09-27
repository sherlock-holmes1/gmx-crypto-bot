"""Version referral identity and discount settings in canonical event order."""

from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.domain.referral import (
    ZERO,
    ZERO_CODE,
    address,
    calldata,
    config_change,
    datastore_key,
    decode_referral_log,
    word,
    words,
)
from gmx_crypto_bot_v2.evidence.anchors import _recorded_block_hash
from gmx_crypto_bot_v2.evidence.snapshots import snapshot_path


@dataclass
class History:
    opening: Any
    changes: list[tuple[tuple[int, int, int], Any]]

    def at(self, coordinate):
        index = bisect_right([c for c, _ in self.changes], coordinate) - 1
        return self.changes[index][1] if index >= 0 else self.opening


class ReferralState:
    def __init__(self, recording: Path, metadata: dict, config_events: list[dict]):
        self.histories: dict[tuple[str, Any], History] = {}
        self.available = False
        path = snapshot_path(recording, "liquidation-referral-configuration.json")
        if not path.exists():
            path = recording / "referral-configuration.json"
        if not path.exists():
            return
        s = json.loads(path.read_text())
        complete = json.loads((recording / "completeness-report.json").read_text())
        start, end = (
            complete["source_block_range"]["from"],
            complete["source_block_range"]["to"],
        )
        if (
            not complete.get("complete")
            or complete.get("gaps")
            or complete.get("reorgs")
        ):
            raise ValueError("referral history requires complete recording")
        self.start, self.end = start, end
        if (
            s.get("schema") != "GmxReferralConfiguration"
            or s.get("version") != 1
            or s.get("market") != metadata["market"]["market_token_address"].lower()
            or s.get("data_store") != metadata["contracts"]["data_store"].lower()
            or s.get("order_handler") != metadata["contracts"]["order_handler"].lower()
            or s.get("opening_block") != start - 1
            or s.get("end_block") != end
            or s.get("opening_hash") != _recorded_block_hash(recording, start - 1)
        ):
            raise ValueError("referral snapshot identity mismatch")
        raw_calls = s["calls"]

        def read(to, sig, arg=None, count=1):
            key = to.lower() + ":" + calldata(sig, arg)
            raw = raw_calls[key]
            return words(raw, count)

        ref = address(read(s["order_handler"], "referralStorage()")[0])
        if ref != s.get("referral_storage") or ref == ZERO:
            raise ValueError("referral storage pointer mismatch")
        self.referral_storage = ref
        # Decode only call-proven anchors, never trust duplicated derived values.
        for account in s["traders"]:
            self.histories["trader", account] = History(
                "0x" + word(read(ref, "traderReferralCodes(address)", account)[0]), []
            )
            tier = read(
                s["data_store"],
                "getUint(bytes32)",
                datastore_key("PRO_TRADER_TIER", account),
            )[0]
            self.histories["pro_tier", account] = History(tier, [])
        for code in s["codes"]:
            self.histories["code", code] = History(
                address(read(ref, "codeOwners(bytes32)", code)[0]), []
            )
        for affiliate in s["affiliates"]:
            self.histories["affiliate_tier", affiliate] = History(
                read(ref, "referrerTiers(address)", affiliate)[0], []
            )
            self.histories["share", affiliate] = History(
                read(ref, "referrerDiscountShares(address)", affiliate)[0], []
            )
        for tier in s["referral_tiers"]:
            self.histories["tier", tier] = History(
                tuple(read(ref, "tiers(uint256)", tier, 2)), []
            )
            self.histories["minimum", tier] = History(
                read(
                    s["data_store"],
                    "getUint(bytes32)",
                    datastore_key("MIN_AFFILIATE_REWARD_FACTOR", tier),
                )[0],
                [],
            )
        for tier in s["pro_tiers"]:
            self.histories["pro_factor", tier] = History(
                read(
                    s["data_store"],
                    "getUint(bytes32)",
                    datastore_key("PRO_DISCOUNT_FACTOR", tier),
                )[0],
                [],
            )
        seen = {}
        next_block = start
        for chunk in s["log_ranges"]:
            if (
                chunk["from"] != next_block
                or chunk["to"] < chunk["from"]
                or chunk["to"] > end
            ):
                raise ValueError("referral log coverage gap or overlap")
            next_block = chunk["to"] + 1
            for log in chunk["logs"]:
                block = int(log["blockNumber"], 16)
                if (
                    log["address"].lower() != ref
                    or not chunk["from"] <= block <= chunk["to"]
                ):
                    raise ValueError("referral log outside requested range or contract")
                if s["log_block_hashes"].get(str(block)) != log["blockHash"]:
                    raise ValueError("referral log block hash mismatch")
                coordinate = (
                    block,
                    int(log["transactionIndex"], 16),
                    int(log["logIndex"], 16),
                )
                if coordinate in seen and seen[coordinate] != log:
                    raise ValueError("conflicting referral logs")
                if coordinate in seen:
                    continue
                seen[coordinate] = log
                change = decode_referral_log(log)
                if change and (change[0], change[1]) in self.histories:
                    self.histories[change[0], change[1]].changes.append(
                        (coordinate, change[2])
                    )
        if next_block != end + 1:
            raise ValueError("incomplete referral log coverage")
        for e in config_events:
            change = config_change(e)
            if change and (change[0], change[1]) in self.histories:
                coordinate = (e["block_number"], e["transaction_index"], e["log_index"])
                if start <= coordinate[0] <= end:
                    self.histories[change[0], change[1]].changes.append(
                        (coordinate, change[2])
                    )
        for h in self.histories.values():
            h.changes.sort(key=lambda item: item[0])
        self.available = True

    def at(self, account: str, coordinate: tuple) -> dict:
        if not self.available or not self.start <= coordinate[0] <= self.end:
            raise KeyError("referral evidence unavailable")

        def value(field, arg):
            return self.histories[field, arg].at(coordinate)

        code = value("trader", account)
        affiliate = ZERO
        rebate = share = minimum = 0
        if code != ZERO_CODE:
            affiliate = value("code", code)
            tier = value("affiliate_tier", affiliate)
            rebate, share = value("tier", tier)
            minimum = value("minimum", tier)
            custom = value("share", affiliate)
            if custom:
                share = custom
        pro_tier = value("pro_tier", account)
        return {
            "code": code,
            "affiliate": affiliate,
            "rebate_bps": rebate,
            "share_bps": share,
            "minimum": minimum,
            "pro_tier": pro_tier,
            "pro_factor": value("pro_factor", pro_tier) if pro_tier else 0,
        }
