"""Fail-closed same-order execution-decision evidence gate.

This adapter names every currently identified MarketIncrease rule. It does not
mistake a narrow acceptable-price calculation for the full GMX decision.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.domain.keys import keccak256


MARKET_INCREASE_RULES: tuple[tuple[str, str], ...] = (
    ("pending_latest_request", "router_preflight"),
    ("order_field_identity", "pinned_sidecar"),
    ("execute_order_feature_enabled", "pinned_sidecar"),
    ("oracle_price_source_and_age", "recorded_oracle_evidence"),
    ("order_valid_from_and_expiration", "request_expiration_config"),
    ("market_and_collateral_token_valid", "market_config"),
    ("swap_path_and_min_output", "swap_state"),
    ("execution_price_and_acceptable_price", "independent_economics"),
    ("position_fees_and_collateral_sufficiency", "independent_economics"),
    ("reserve_and_open_interest_reserve", "reserve_config"),
    ("minimum_position_and_collateral", "position_risk_config"),
    ("post_update_position_validity", "position_risk_config"),
    ("market_token_balances", "market_balance_state"),
    ("gas_and_keeper_checks", "execution_context"),
)


def _digest(path: Path) -> str:
    return "0x" + hashlib.sha256(path.read_bytes()).hexdigest()


def verify_pinned_increase_executor(proof_path: Path, source_path: Path) -> dict[str, Any]:
    """Check exact-match executor source against the selected pinned runtime."""
    proof = json.loads(proof_path.read_text())
    source = json.loads(source_path.read_text())
    if proof.get("schema") != "GmxStep41PinnedExecutor" or \
            source.get("runtimeMatch") != "exact_match" or source.get("match") != "exact_match" or \
            source.get("address", "").lower() != proof.get("executor_address") or \
            str(source.get("chainId")) != str(proof.get("chain_id")):
        raise ValueError("pinned increase executor identity mismatch")
    transcript = proof.get("rpc_transcript")
    if not isinstance(transcript, list) or [x.get("method") for x in transcript] != [
            "eth_chainId", "eth_getBlockByNumber", "eth_call", "eth_getCode",
            "eth_getBlockByNumber"] or \
            int(transcript[0]["response"]["result"], 16) != proof["chain_id"] or \
            any(int(transcript[i]["response"]["result"]["number"], 16) != proof["block_number"] or
                transcript[i]["response"]["result"]["hash"].lower() != proof["block_hash"]
                for i in (1, 4)):
        raise ValueError("pinned increase executor transcript mismatch")
    call = transcript[2]
    if call.get("params") != [{"to": proof["order_handler"], "data": "0x7f9011d7"},
                              hex(proof["block_number"])] or \
            int(call["response"]["result"], 16).to_bytes(32, "big")[-20:].hex() != proof["executor_address"][2:]:
        raise ValueError("pinned increase executor pointer mismatch")
    runtime = bytes.fromhex(source["runtimeBytecode"]["onchainBytecode"][2:])
    if transcript[3].get("params") != [proof["executor_address"], hex(proof["block_number"])] or \
            transcript[3]["response"]["result"].lower() != "0x" + runtime.hex() or \
            "0x" + keccak256(runtime).hex() != proof["runtime_code_keccak"] or \
            "0x" + hashlib.sha256(runtime).hexdigest() != proof["runtime_code_sha256"]:
        raise ValueError("pinned increase executor runtime mismatch")
    if source.get("compilation", {}).get("fullyQualifiedName") != \
            "contracts/order/IncreaseOrderExecutor.sol:IncreaseOrderExecutor":
        raise ValueError("unexpected increase executor source target")
    source_files = source.get("sources")
    metadata_files = source.get("metadata", {}).get("sources")
    if not isinstance(source_files, dict) or not isinstance(metadata_files, dict) or \
            set(source_files) != set(metadata_files) or \
            any("0x" + keccak256(item["content"].encode()).hex() !=
                metadata_files[path]["keccak256"] for path, item in source_files.items()) or \
            source.get("abi") != source.get("metadata", {}).get("output", {}).get("abi"):
        raise ValueError("increase executor source or ABI hash mismatch")
    required_fragments = {
        "contracts/swap/SwapUtils.sol": ("params.swapPathMarkets.length == 0",
                                          "params.amountIn < params.minOutputAmount"),
        "contracts/market/MarketUtils.sol": ("token == market.longToken || token == market.shortToken",),
        "contracts/order/IncreaseOrderUtils.sol": ("params.minOracleTimestamp < params.order.updatedAtTime()",
                                                    "Keys.REQUEST_EXPIRATION_TIME"),
    }
    if any(path not in source_files or any(fragment not in source_files[path]["content"]
                                          for fragment in fragments)
           for path, fragments in required_fragments.items()):
        raise ValueError("historical increase branch source unproved")
    return {"proof_sha256": _digest(proof_path), "source_sha256": _digest(source_path),
            "address": proof["executor_address"], "runtime_code_keccak": proof["runtime_code_keccak"]}


def verify_pinned_swap_handler(proof_path: Path, source_path: Path) -> dict[str, Any]:
    proof = json.loads(proof_path.read_text())
    source = json.loads(source_path.read_text())
    transcript = proof.get("rpc_transcript")
    if proof.get("schema") != "GmxStep41PinnedHandler" or \
            source.get("runtimeMatch") != "exact_match" or source.get("match") != "exact_match" or \
            source.get("address", "").lower() != proof.get("handler_address") or \
            str(source.get("chainId")) != str(proof.get("chain_id")) or \
            source.get("compilation", {}).get("fullyQualifiedName") != \
            "contracts/swap/SwapHandler.sol:SwapHandler" or \
            not isinstance(transcript, list) or [x.get("method") for x in transcript] != [
                "eth_chainId", "eth_getBlockByNumber", "eth_call", "eth_getCode",
                "eth_getBlockByNumber"]:
        raise ValueError("pinned swap handler identity mismatch")
    if int(transcript[0]["response"]["result"], 16) != proof["chain_id"] or \
            any(int(transcript[i]["response"]["result"]["number"], 16) != proof["block_number"] or
                transcript[i]["response"]["result"]["hash"].lower() != proof["block_hash"]
                for i in (1, 4)) or \
            transcript[2].get("params") != [{"to": proof["order_handler"], "data": "0x8a53aaac"},
                                             hex(proof["block_number"])] or \
            int(transcript[2]["response"]["result"], 16).to_bytes(32, "big")[-20:].hex() != \
            proof["handler_address"][2:]:
        raise ValueError("pinned swap handler pointer mismatch")
    runtime = bytes.fromhex(source["runtimeBytecode"]["onchainBytecode"][2:])
    if transcript[3].get("params") != [proof["handler_address"], hex(proof["block_number"])] or \
            transcript[3]["response"]["result"].lower() != "0x" + runtime.hex() or \
            "0x" + keccak256(runtime).hex() != proof["runtime_code_keccak"] or \
            "0x" + hashlib.sha256(runtime).hexdigest() != proof["runtime_code_sha256"]:
        raise ValueError("pinned swap handler runtime mismatch")
    files, metadata_files = source.get("sources"), source.get("metadata", {}).get("sources")
    if not isinstance(files, dict) or not isinstance(metadata_files, dict) or \
            set(files) != set(metadata_files) or \
            any("0x" + keccak256(item["content"].encode()).hex() !=
                metadata_files[path]["keccak256"] for path, item in files.items()) or \
            source.get("abi") != source.get("metadata", {}).get("output", {}).get("abi") or \
            "params.swapPathMarkets.length == 0" not in files["contracts/swap/SwapUtils.sol"]["content"]:
        raise ValueError("pinned swap handler source mismatch")
    return {"proof_sha256": _digest(proof_path), "source_sha256": _digest(source_path),
            "address": proof["handler_address"], "runtime_code_keccak": proof["runtime_code_keccak"]}


class SelectedIncreaseDecisionAdapter:
    """Bind accepted artifacts and identify the first unproved execution gate."""

    def __init__(self, sidecar_path: Path, source_proof_path: Path,
                 source_manifest_path: Path, oracle_evidence_path: Path,
                 executor_proof_path: Path | None = None, executor_source_path: Path | None = None,
                 swap_proof_path: Path | None = None, swap_source_path: Path | None = None):
        self.paths = (sidecar_path, source_proof_path, source_manifest_path,
                      oracle_evidence_path)
        self.sidecar, self.proof, self.manifest, self.oracle = (
            json.loads(path.read_text()) for path in self.paths)
        if self.sidecar.get("schema") != "GmxStep41ArchiveSidecar" or \
                self.proof.get("schema") != "GmxStep41SourcifySourceProof" or \
                self.manifest.get("schema") != "GmxStep41HistoricalSourceManifest" or \
                self.oracle.get("schema") != "GmxStep41RecordedOracleEvidence":
            raise ValueError("selected increase evidence schema mismatch")
        if self.proof.get("sidecar_sha256") != _digest(sidecar_path) or \
                self.proof.get("input_manifest_sha256") != _digest(source_manifest_path) or \
                self.manifest.get("sidecar_sha256") != _digest(sidecar_path) or \
                not self.proof.get("sourcify_exact_match_and_local_content_hashes_verified"):
            raise ValueError("selected increase source proof digest mismatch")
        if not self.oracle.get("source_and_scale_verified") or \
                not self.oracle.get("age_within_historical_max"):
            raise ValueError("selected increase oracle source or age unverified")
        self.executor_source = (verify_pinned_increase_executor(executor_proof_path,
                                                                 executor_source_path)
                                if executor_proof_path is not None and executor_source_path is not None
                                else None)
        self.swap_source = (verify_pinned_swap_handler(swap_proof_path, swap_source_path)
                            if swap_proof_path is not None and swap_source_path is not None else None)
        for dependency_path in (executor_proof_path, swap_proof_path):
            if dependency_path is None:
                continue
            dependency = json.loads(dependency_path.read_text())
            pin = self.sidecar["pin"]
            deployment = self.sidecar["deployment"]
            if dependency.get("chain_id") != deployment["chain_id"] or \
                    dependency.get("block_number") != pin["number"] or \
                    dependency.get("block_hash", "").lower() != pin["recorded_hash"].lower() or \
                    dependency.get("order_handler", "").lower() != deployment["order_handler"].lower():
                raise ValueError("delegated contract proof differs from selected sidecar pin")

    def evaluate(self, preflight: dict[str, Any]) -> dict[str, Any]:
        only = lambda reason: {"status": "gmx_preflight_only", "reason": reason,
                               "rule_inventory": [name for name, _ in MARKET_INCREASE_RULES]}
        if preflight.get("outcome") not in {"passed_preflight", "validation_error"}:
            return only("no_decoded_gmx_decision")
        pin = self.sidecar["pin"]
        if preflight.get("order_key", "").lower() != self.oracle.get("order_key") or \
                preflight.get("pin_block") != pin["number"] or \
                preflight.get("pin_block_hash", "").lower() != pin["recorded_hash"] or \
                preflight.get("pin_block_hash_after", "").lower() != pin["recorded_hash"]:
            return only("selected_order_or_pin_mismatch")
        if self.proof.get("pin_block") != pin["number"] or \
                self.proof.get("pin_hash") != pin["recorded_hash"]:
            return only("historical_source_pin_mismatch")
        if self.sidecar["router_gate"] != {
                "derived_latest_key": self.oracle["order_key"], "latest": True,
                "nonce": self.sidecar["router_gate"]["nonce"], "pending": True}:
            return only("pending_latest_proof_missing")
        request = preflight.get("on_chain_request")
        if not isinstance(request, dict) or request != self.sidecar["pinned_order"]:
            return only("pinned_request_not_equal")
        if self.sidecar.get("feature_flag", {}).get("disabled") is not False:
            return only("execute_order_feature_unproved")
        if self.executor_source is None:
            return only("historical_increase_executor_source_unproved")
        router_oracle = preflight.get("oracle") or {}
        if list(router_oracle.get("tokens", ())) != self.oracle["tokens"] or \
                [list(x) for x in router_oracle.get("prices", ())] != self.oracle["prices"] or \
                router_oracle.get("min_timestamp") != self.oracle["min_timestamp"] or \
                router_oracle.get("max_timestamp") != self.oracle["max_timestamp"]:
            return only("router_oracle_not_equal_to_evidence")
        order = self.sidecar["pinned_order"]
        market = self.sidecar["fixed_values"]["market"]
        proved = ["pending_latest_request", "order_field_identity",
                  "execute_order_feature_enabled", "oracle_price_source_and_age"]
        if order.get("orderType") != 2 or order.get("isLong") is not True:
            return only("unsupported_order_type_or_side")
        if order.get("swapPath") != [] or order.get("minOutputAmount") is None or \
                order.get("initialCollateralDeltaAmount") is None:
            return only("swap_branch_unproved")
        if order["initialCollateralDeltaAmount"] < order["minOutputAmount"]:
            return {**only("independent_swap_min_output_failed"),
                    "model_rule": "swap_path_and_min_output", "model_result": "reject"}
        proved.append("zero_swap_path_input_conditions")
        if self.swap_source is not None:
            proved.append("swap_path_and_min_output")
        if order["initialCollateralToken"].lower() not in {
                market["LONG_TOKEN"].lower(), market["SHORT_TOKEN"].lower()} or \
                market["INDEX_TOKEN"].lower() == "0x" + "0" * 40 or \
                market["MARKET_TOKEN"].lower() != order["market"].lower():
            return {**only("independent_market_or_collateral_failed"),
                    "model_rule": "market_and_collateral_token_valid", "model_result": "reject"}
        proved.append("collateral_token_membership")
        if self.oracle["min_timestamp"] < order["updatedAtTime"]:
            return {**only("independent_order_timestamp_failed"),
                    "model_rule": "order_valid_from_and_expiration", "model_result": "reject"}
        if self.sidecar.get("referral", {}).get("code") == "0x" + "0" * 64 and \
                self.sidecar.get("referral", {}).get("pro_trader_tier") == 0:
            proved.append("zero_referral_branch")
        if self.oracle["relationship"] == "observed_execution_transaction_prices_preceding_order_executed":
            return {**only("execution_oracle_and_creation_pin_not_equivalent"),
                    "proved_branches": proved}
        return {**only("independent_market_increase_rule_coverage_incomplete"),
                "proved_branches": proved,
                "missing_rule_evidence": [name for name, _ in MARKET_INCREASE_RULES[4:]
                                          if name not in proved],
                "comparison_scope": "counterfactual_creation_block_prices"}
