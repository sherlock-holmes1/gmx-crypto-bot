"""Strict, read-only historical evidence for the position simulator.

Coordinates identify the point *before* a recorded log. Oracle observations are
transaction scoped; a price from another transaction is never carried forward.
"""

from __future__ import annotations

import json
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from gmx_crypto_bot_v2.domain.accrual import slots
from gmx_crypto_bot_v2.domain.entries import (
    _decode_recorded_log, _event_entry, _event_entry_from_log, decoded_log,
)
from gmx_crypto_bot_v2.domain.keys import FACTOR_FIELDS, MARKET_FIELDS, config_base_key
from gmx_crypto_bot_v2.domain.referral import word
from gmx_crypto_bot_v2.domain.swap_keys import key
from gmx_crypto_bot_v2.evidence.repository import event_rows, raw_logs
from gmx_crypto_bot_v2.evidence.timestamps import load_timestamps
from gmx_crypto_bot_v2.reconstruction.fees import build_fee_histories
from gmx_crypto_bot_v2.reconstruction.impact import load_factor_histories
from gmx_crypto_bot_v2.reconstruction.accrual_configuration import load_snapshot
from gmx_crypto_bot_v2.collection.risk import verify as verify_risk_snapshot
from gmx_crypto_bot_v2.reconstruction.referral import ReferralState
from gmx_crypto_bot_v2.simulation.orders import KeeperOpportunity

Coordinate = tuple[int, int, int]


class UnavailableEvidence(ValueError):
    """Required historical evidence is absent or inconsistent."""


def observed_ui_receivers(events: list[dict[str, Any]]) -> set[str]:
    """Use emitted position-fee evidence, matching the V2 validator."""
    return {
        entry["values"]["uiFeeReceiver"].lower()
        for entry in events
        if entry["event_name"] == "PositionFeesCollected"
        and isinstance(entry["values"].get("uiFeeReceiver"), str)
    }


@dataclass(frozen=True)
class EvidenceState:
    coordinate: Coordinate
    transaction_hash: str
    oracle: Mapping[str, tuple[int, int]]
    configuration: Mapping[str, int]
    accrual: Mapping[str, int]
    open_interest_usd: Mapping[str, int]
    open_interest_tokens: Mapping[str, int]
    pool_amount: Mapping[str, int]
    impact_pool_amount: int
    market: str = ""
    index_token: str = ""
    long_token: str = ""
    short_token: str = ""
    counterfactual_applied: bool = False


class EvidenceAdapter:
    """Version recorded state without borrowing values from future events."""

    def __init__(
        self,
        *,
        start: int,
        end: int,
        market: str,
        index_token: str,
        long_token: str,
        short_token: str,
        opening: Mapping[str, int],
        events: list[dict[str, Any]],
        impact_histories: Mapping[str, Any],
        fee_histories: Mapping[str, Any],
        timestamps: Mapping[int, int] | None = None,
        risk_snapshot: Mapping[str, Any] | None = None,
        referral_state: ReferralState | None = None,
        virtual_token_id: str = "",
    ):
        self.start, self.end = start, end
        self.market = market
        self.tokens = (index_token, long_token, short_token)
        self.opening = dict(opening)
        self.events = sorted(events, key=lambda e: self.coordinate(e))
        self.impact_histories = impact_histories
        self.fee_histories = fee_histories
        self.timestamps = dict(timestamps or {})
        self.risk_snapshot = risk_snapshot
        self.referral_state = referral_state
        self.virtual_token_id = virtual_token_id.lower()
        expected_impact = set(FACTOR_FIELDS.values()) | set(MARKET_FIELDS.values())
        expected_fee = {"position_fee_positive", "position_fee_negative",
                        "position_fee_receiver", "borrowing_fee_receiver", "max_ui_fee"}
        if not expected_impact <= impact_histories.keys() or not expected_fee <= fee_histories.keys():
            raise UnavailableEvidence("incomplete historical configuration histories")
        self.slot_index = self._slot_index
        self.initial_impact_pool = None
        for event in self.events:
            if (event["event_name"] == "PositionImpactPoolAmountUpdated"
                    and event["values"].get("market", "").lower() == market):
                values = event["values"]
                self.initial_impact_pool = values["nextValue"] - values["delta"]
                if self.initial_impact_pool < 0:
                    raise UnavailableEvidence("invalid impact-pool anchor")
                break
        coordinates = [self.coordinate(e) for e in self.events]
        if len(set(coordinates)) != len(coordinates):
            raise UnavailableEvidence("duplicate canonical log coordinate")
        self.events_by_coordinate = {self.coordinate(entry): entry for entry in self.events}
        if any(not start <= c[0] <= end for c in coordinates):
            raise UnavailableEvidence("log outside complete recording range")
        self._virtual_coordinates: list[Coordinate] = []
        self._virtual_values: list[int] = []
        virtual_updates = [entry for entry in self.events
                           if entry["event_name"] == "VirtualPositionInventoryUpdated"
                           and entry["values"].get("virtualTokenId", "").lower()
                           == self.virtual_token_id]
        if virtual_updates and self.virtual_token_id:
            first = virtual_updates[0]["values"]
            value = first["nextValue"] - first["delta"]
            self._virtual_values.append(value)
            for entry in virtual_updates:
                values = entry["values"]
                if value + values["delta"] != values["nextValue"]:
                    raise UnavailableEvidence("virtual inventory continuity broken")
                value = values["nextValue"]
                self._virtual_coordinates.append(self.coordinate(entry))
                self._virtual_values.append(value)

    @staticmethod
    def coordinate(entry: Mapping[str, Any]) -> Coordinate:
        return (entry["block_number"], entry["transaction_index"], entry["log_index"])

    @classmethod
    def load(cls, recording: Path, validation: Mapping[str, Any]) -> "EvidenceAdapter":
        """Load a complete, independently validated recording and its anchors."""
        recording = Path(recording)
        report = json.loads((recording / "completeness-report.json").read_text())
        if (report.get("complete") is not True or report.get("gaps")
                or report.get("reorgs") or validation.get("complete") is not True
                or validation.get("decode_errors") or validation.get("mismatched")):
            raise UnavailableEvidence("recording or economic validation incomplete")
        bounds = report["source_block_range"]
        metadata = json.loads((recording / "metadata.json").read_text())
        market = metadata["market"]["market_token_address"].lower()
        tokens = metadata["tokens"]
        snapshot = load_snapshot(recording, metadata)
        if snapshot is None:
            raise UnavailableEvidence("missing accrual opening anchor")
        raw = []
        timestamps = {}
        for row in event_rows(recording):
            if row.get("kind") == "block_header":
                value = row["payload"]["timestamp"]
                timestamps[row["block_number"]] = int(value, 16) if isinstance(value, str) else value
                continue
            if row.get("kind") not in {"gmx_market_log", "gmx_order_lifecycle_log", "configuration_log"}:
                continue
            if "log" not in row.get("payload", {}):
                continue
            decoded = _decode_recorded_log(row)
            if decoded is None:
                raise UnavailableEvidence("undecodable recorded log")
            raw.append(_event_entry(row, decoded))
        virtual_id = metadata.get("pinned_configuration_raw", {}).get(
            "virtual_index_token_id", "").lower()
        if virtual_id:
            for log in raw_logs(recording, {"VirtualPositionInventoryUpdated"}):
                decoded = decoded_log(log)
                if decoded.values.get("virtualTokenId", "").lower() == virtual_id:
                    raw.append(_event_entry_from_log(log, decoded))
        accrual_hashes = {}
        for entry in raw:
            if entry["event_name"] in {"Funding", "CumulativeBorrowingFactorUpdated"}:
                block, block_hash = entry["block_number"], entry["block_hash"]
                if block in accrual_hashes and accrual_hashes[block] != block_hash:
                    raise UnavailableEvidence("conflicting accrual block hashes")
                accrual_hashes[block] = block_hash
        timestamps.update(load_timestamps(recording, accrual_hashes))
        receivers = observed_ui_receivers(raw)
        fees = build_fee_histories(recording, metadata, raw, receivers)
        impacts = load_factor_histories(recording, metadata)
        risk_path = recording / "risk-configuration.json"
        risk_snapshot = None
        if risk_path.exists():
            risk_snapshot = json.loads(risk_path.read_text())
            try:
                verify_risk_snapshot(recording, risk_snapshot)
            except ValueError as error:
                raise UnavailableEvidence(f"invalid risk configuration: {error}") from error
        referral_state = ReferralState(recording, metadata, raw)
        return cls(
            start=bounds["from"], end=bounds["to"], market=market,
            index_token=tokens["index"]["address"].lower(),
            long_token=tokens["long"]["address"].lower(),
            short_token=tokens["short"]["address"].lower(),
            opening=snapshot["opening"], events=raw,
            impact_histories=impacts, fee_histories=fees,
            timestamps=timestamps,
            risk_snapshot=risk_snapshot,
            referral_state=referral_state,
            virtual_token_id=virtual_id,
        )

    def risk_values_at(self, coordinate: Coordinate) -> Mapping[str, int]:
        """Return the independently pinned risk cells before a recorded log."""
        if self.risk_snapshot is None:
            raise UnavailableEvidence("missing historical risk configuration")
        if not self.start <= coordinate[0] <= self.end:
            raise UnavailableEvidence("risk coordinate outside recording")
        values = {name: item["value"] for name, item in
                  self.risk_snapshot["opening"]["values"].items()}
        for change in self.risk_snapshot["changes"]:
            if tuple(change["coordinate"]) >= coordinate:
                break
            values[change["field"]] = change["value"]
        return values

    def risk_configuration_at(self, coordinate: Coordinate):
        """Build a risk monitor input from the verified historical sidecar."""
        from gmx_crypto_bot_v2.simulation.risk import RiskConfiguration

        values = self.risk_values_at(coordinate)
        return RiskConfiguration(
            coordinate, self.end, values["min_collateral_usd"],
            values["min_collateral_factor_for_liquidation"], 0,
            "verified risk-configuration.json",
            values["max_position_impact_factor_for_liquidations"], 0,
        )

    def virtual_inventory_at(self, coordinate: Coordinate) -> int:
        """Reconstruct virtual tokens from the first update's prior value."""
        if not self._virtual_values:
            raise UnavailableEvidence("missing virtual inventory update anchor")
        index = bisect_left(self._virtual_coordinates, coordinate)
        value = self._virtual_values[index]
        target = self.events_by_coordinate.get(coordinate)
        if target is not None and target["event_name"] in {
                "PositionIncrease", "PositionDecrease"} and index:
            values = target["values"]
            size = values.get("sizeDeltaInTokens")
            if type(size) is int:
                signed = size if target["event_name"] == "PositionIncrease" else -size
                expected = -signed if values.get("isLong") else signed
                prior = self._virtual_coordinates[index - 1]
                update = self.events_by_coordinate.get(prior)
                if (update is not None
                        and update["transaction_hash"] == target["transaction_hash"]
                        and update["values"]["delta"] == expected):
                    value -= expected
        return value

    def risk_inputs_for(self, account: str) -> dict:
        """Construct all risk inputs for an account covered by the recording."""
        coordinates = self.required_risk_coordinates()
        return {
            "risk_coordinates": coordinates,
            "risk_configuration": {c: self.risk_configuration_at(c) for c in coordinates},
            "risk_virtual_inventory": {c: self.virtual_inventory_at(c)
                                       for c in coordinates},
            "risk_referral": {c: self.referral_at(account, c) for c in coordinates},
            "timestamps": self.timestamps,
        }

    def referral_at(self, account: str, coordinate: Coordinate):
        """Get account terms only when the referral snapshot covers it."""
        from gmx_crypto_bot_v2.simulation.economics import ReferralTerms
        from gmx_crypto_bot_v2.simulation.risk import RiskReferralEvidence

        if self.referral_state is None:
            raise UnavailableEvidence("missing referral state")
        try:
            values = self.referral_state.at(account.lower(), coordinate)
        except KeyError as error:
            raise UnavailableEvidence("missing account-specific referral coverage") from error
        terms = ReferralTerms(values["code"], values["rebate_bps"],
                              values["share_bps"], values["minimum"],
                              values["pro_tier"], values["pro_factor"])
        return RiskReferralEvidence(coordinate, self.end, terms,
                                    "verified referral configuration")

    def at(self, coordinate: Coordinate) -> EvidenceState:
        """Return state before a log, including only this transaction's oracle."""
        if not self.start <= coordinate[0] <= self.end:
            raise UnavailableEvidence("coordinate outside recording")
        market, (_, long_token, short_token) = self.market, self.tokens
        state = dict(self.opening)
        oracle: dict[str, tuple[int, int]] = {}
        tx: str | None = None
        impact_pool = self.initial_impact_pool
        last_oi: dict[str, dict] = {}
        last_impact: dict | None = None
        market_updates: dict[str, list[dict]] = {}
        for event in self.events:
            c = self.coordinate(event)
            if c >= coordinate:
                break
            name, values = event["event_name"], event["values"]
            current_tx = event["transaction_hash"]
            if current_tx != tx:
                tx, oracle = current_tx, {}
                market_updates = {}
            if name == "OraclePriceUpdate":
                token = values.get("token", "").lower()
                if token in self.tokens:
                    low, high = values.get("minPrice"), values.get("maxPrice")
                    if type(low) is not int or type(high) is not int or low <= 0 or high < low:
                        raise UnavailableEvidence("invalid same-transaction oracle range")
                    oracle[token] = (low, high)
            elif name in {"SetUint", "SetInt", "SetBool"}:
                for slot, desc in self.slot_index.items():
                    if (values.get("baseKey"), values.get("data")) == slot:
                        state[desc] = values["value"]
                        break
            elif name in {"PoolAmountUpdated", "OpenInterestUpdated", "OpenInterestInTokensUpdated"} and values.get("market", "").lower() == market:
                if name == "PoolAmountUpdated":
                    storage = key("POOL_AMOUNT", market, values["token"])
                else:
                    storage = key(
                        "OPEN_INTEREST" if name == "OpenInterestUpdated" else "OPEN_INTEREST_IN_TOKENS",
                        market, values["collateralToken"], values["isLong"],
                    )
                previous = state.get(storage)
                if previous is None or previous + values["delta"] != values["nextValue"]:
                    raise UnavailableEvidence("broken pool or open-interest continuity")
                state[storage] = values["nextValue"]
                market_updates.setdefault(storage, []).append(event)
                if name == "OpenInterestInTokensUpdated":
                    last_oi[storage] = event
            elif name == "PositionImpactPoolAmountUpdated" and values.get("market", "").lower() == market:
                previous = values["nextValue"] - values["delta"]
                if impact_pool is not None and previous != impact_pool:
                    raise UnavailableEvidence("broken impact-pool continuity")
                impact_pool = values["nextValue"]
                last_impact = event
            elif name in {"FundingFeeAmountPerSizeUpdated", "ClaimableFundingAmountPerSizeUpdated"} and values.get("market", "").lower() == market:
                storage = key("FUNDING_FEE_AMOUNT_PER_SIZE" if name.startswith("FundingFee")
                              else "CLAIMABLE_FUNDING_AMOUNT_PER_SIZE", market,
                              values["collateralToken"], values["isLong"])
                previous = state.get(storage)
                if previous is None or previous + values["delta"] != values["value"]:
                    raise UnavailableEvidence("broken funding accumulator continuity")
                state[storage] = values["value"]
            elif name == "CumulativeBorrowingFactorUpdated" and values.get("market", "").lower() == market:
                storage = key("CUMULATIVE_BORROWING_FACTOR", market, values["isLong"])
                previous = state.get(storage)
                if previous is None or previous + values["delta"] != values["nextValue"]:
                    raise UnavailableEvidence("broken borrowing accumulator continuity")
                state[storage] = values["nextValue"]
                timestamp = self.timestamps.get(c[0])
                if timestamp is None:
                    raise UnavailableEvidence("missing borrowing timestamp")
                state[key("CUMULATIVE_BORROWING_FACTOR_UPDATED_AT", market,
                          values["isLong"])] = timestamp
            elif name == "Funding" and values.get("market", "").lower() == market:
                timestamp = self.timestamps.get(c[0])
                if timestamp is None:
                    raise UnavailableEvidence("missing funding timestamp")
                state[key("SAVED_FUNDING_FACTOR_PER_SECOND", market)] = values["fundingFactorPerSecond"]
                state[key("FUNDING_UPDATED_AT", market)] = timestamp
        target = self.events_by_coordinate.get(coordinate)
        if target is None:
            raise UnavailableEvidence("no recorded log at coordinate")
        target_tx = target["transaction_hash"]
        if tx != target_tx:
            oracle = {}
        if any(t not in oracle for t in self.tokens):
            raise UnavailableEvidence("missing same-transaction oracle prices")
        if impact_pool is None:
            raise UnavailableEvidence("impact pool has no prior recorded anchor")
        if target["event_name"] in {"PositionIncrease", "PositionDecrease"}:
            values = target["values"]
            slot = key("OPEN_INTEREST_IN_TOKENS", market,
                       values["collateralToken"], values["isLong"])
            signed = values.get("sizeDeltaInTokens")
            if target["event_name"] == "PositionDecrease" and type(signed) is int:
                signed = -signed
            update = last_oi.get(slot)
            if (type(signed) is not int or (signed != 0 and
                    (update is None or update["transaction_hash"] != target_tx
                     or update["values"]["delta"] != signed))):
                raise UnavailableEvidence("position OI update does not match")
            if signed:
                state[slot] -= signed
            # USD OI and pool logs have no order key. Attribute them only in
            # an unbatched transaction with the same position order key and
            # terminal order. Otherwise the observed pre-log value remains
            # ambiguous and must not be presented as pre-order state.
            other_updates = {k: v for k, v in market_updates.items() if k != slot}
            if other_updates:
                transaction = [e for e in self.events
                               if e["transaction_hash"] == target_tx]
                positions = [e for e in transaction if e["event_name"] in
                             {"PositionIncrease", "PositionDecrease"}]
                terminals = [e for e in transaction if e["event_name"] == "OrderExecuted"]
                order_key = values.get("orderKey")
                if (not isinstance(order_key, str) or len(positions) != 1
                        or positions[0] is not target or len(terminals) != 1
                        or terminals[0]["values"].get("key") != order_key):
                    raise UnavailableEvidence("market updates cannot be attributed to target order")
                for storage, updates in other_updates.items():
                    # A single decrease may touch the same pool several
                    # times (PnL, fees, then impact). Once the transaction is
                    # proven to contain exactly one position and terminal,
                    # their aggregate delta is attributable to that order.
                    delta = sum(update["values"]["delta"] for update in updates)
                    if storage == key("OPEN_INTEREST", market,
                                      values["collateralToken"], values["isLong"]):
                        usd = values.get("sizeDeltaUsd")
                        expected = usd if target["event_name"] == "PositionIncrease" else -usd if type(usd) is int else None
                        if delta != expected:
                            raise UnavailableEvidence("USD OI update does not match target order")
                    elif storage not in {key("POOL_AMOUNT", market, token)
                                        for token in self.tokens[1:]}:
                        raise UnavailableEvidence("unexpected market update in order transaction")
                    state[storage] -= delta
            if last_impact is not None and last_impact["transaction_hash"] == target_tx:
                impact_pool -= last_impact["values"]["delta"]
        configuration = {}
        for prefix, histories in (("impact:", self.impact_histories), ("fee:", self.fee_histories)):
            for field, history in histories.items():
                value = history.at(coordinate)
                if value is None:
                    raise UnavailableEvidence(f"missing historical configuration: {field}")
                configuration[prefix + field] = value
        if self.risk_snapshot is not None:
            configuration.update({"risk:" + name: value for name, value
                                  in self.risk_values_at(coordinate).items()})
        if not (set(FACTOR_FIELDS.values()) | set(MARKET_FIELDS.values())) <= self.impact_histories.keys() or not {
            "position_fee_positive", "position_fee_negative", "position_fee_receiver",
            "borrowing_fee_receiver", "max_ui_fee"
        } <= self.fee_histories.keys():
            raise UnavailableEvidence("missing historical configuration histories")
        oi_usd = self._sides(state, "OPEN_INTEREST")
        oi_tokens = self._sides(state, "OPEN_INTEREST_IN_TOKENS")
        pools = {token: self._required(state, key("POOL_AMOUNT", market, token)) for token in (long_token, short_token)}
        return EvidenceState(
            coordinate, target_tx, oracle, configuration, state,
            oi_usd, oi_tokens, pools, impact_pool,
            market, self.tokens[0], long_token, short_token,
        )

    @property
    def _slot_index(self) -> dict[tuple[str, str], str]:
        return {
            (config_base_key(desc["name"]), "0x" + "".join(word(a) for a in desc["args"])): slot
            for slot, desc in self._slots.items()
        }

    @property
    def _slots(self) -> dict[str, dict]:
        metadata = {"market": {"market_token_address": self.market}, "tokens": {
            "long": {"address": self.tokens[1]}, "short": {"address": self.tokens[2]}}}
        return slots(metadata)

    @staticmethod
    def _required(state: Mapping[str, int], slot: str) -> int:
        value = state.get(slot)
        if type(value) is not int or value < 0:
            raise UnavailableEvidence(f"missing or invalid state cell: {slot}")
        return value

    def _sides(self, state: Mapping[str, int], name: str) -> dict[str, int]:
        return {
            label: sum(self._required(state, key(name, self.market, token, side))
                       for token in self.tokens[1:])
            for label, side in (("long", True), ("short", False))
        }

    def keeper_opportunities(self) -> list[KeeperOpportunity]:
        """Expose only observed executed-order transactions with complete oracle."""
        result = []
        for event in self.events:
            if event["event_name"] != "OrderExecuted":
                continue
            c = self.coordinate(event)
            try:
                state = self.at(c)
            except UnavailableEvidence as error:
                raise UnavailableEvidence(
                    f"keeper opportunity at {c} unavailable: {error}"
                ) from error
            low, high = state.oracle[self.tokens[0]]
            result.append(KeeperOpportunity(*c, low, high))
        return result

    def state_for_opportunity(self, opportunity: KeeperOpportunity) -> EvidenceState:
        """Use the pre-position log in an observed keeper execution transaction."""
        coordinate = (opportunity.block, opportunity.transaction_index,
                      opportunity.log_index)
        terminal = next((entry for entry in self.events
                         if self.coordinate(entry) == coordinate
                         and entry["event_name"] == "OrderExecuted"), None)
        if terminal is None:
            raise UnavailableEvidence("keeper opportunity has no recorded execution")
        transaction = [entry for entry in self.events
                       if entry["transaction_hash"] == terminal["transaction_hash"]]
        positions = [entry for entry in transaction
                     if entry["event_name"] in {"PositionIncrease", "PositionDecrease"}
                     and entry["values"].get("orderKey") == terminal["values"].get("key")
                     and self.coordinate(entry) < coordinate]
        if len(positions) != 1:
            raise UnavailableEvidence("keeper opportunity lacks unique pre-order state")
        return self.at(self.coordinate(positions[0]))

    def required_risk_coordinates(self) -> tuple[Coordinate, ...]:
        """All recorded state changes that can alter a position's risk mark."""
        relevant = {
            "Funding", "CumulativeBorrowingFactorUpdated",
            "PoolAmountUpdated", "OpenInterestUpdated",
            "OpenInterestInTokensUpdated", "PositionImpactPoolAmountUpdated",
            "PositionIncrease", "PositionDecrease", "SetUint", "SetBool", "SetInt",
            "VirtualPositionInventoryUpdated",
        }
        result: list[Coordinate] = []
        transaction = None
        oracle_tokens: set[str] = set()
        pending_oracle: Coordinate | None = None
        for event in self.events:
            tx = event["transaction_hash"]
            if tx != transaction:
                if pending_oracle is not None:
                    # A transaction with no post-oracle log cannot be marked
                    # from complete pre-log prices. Keep the gap explicit.
                    result.append(pending_oracle)
                transaction, oracle_tokens, pending_oracle = tx, set(), None
            coordinate = self.coordinate(event)
            if event["event_name"] == "OraclePriceUpdate":
                token = event["values"].get("token", "").lower()
                if token in self.tokens:
                    oracle_tokens.add(token)
                    pending_oracle = coordinate
                continue
            if pending_oracle is not None and len(oracle_tokens) == len(set(self.tokens)):
                result.append(coordinate)
                pending_oracle = None
            if event["event_name"] in relevant and coordinate not in result[-1:]:
                result.append(coordinate)
        if pending_oracle is not None:
            result.append(pending_oracle)
        return tuple(result)
