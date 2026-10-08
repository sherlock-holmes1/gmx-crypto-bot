"""Observe historical contract code for a single recorded order's pin."""

from __future__ import annotations

from typing import Any

from gmx_crypto_bot_v2.crosscheck.archive_sidecar import EvidenceRpc
from gmx_crypto_bot_v2.crosscheck.router_preflight import RawRpc, _hex_bytes
from gmx_crypto_bot_v2.domain.keys import keccak256
from gmx_crypto_bot_v2.domain.referral import calldata, words


def prepare_deployment(
    rpc: RawRpc, candidate: dict[str, Any], *, chain_id: int,
    datastore: str, reader: str, order_handler: str, router: str,
    referral_storage: str | None = None,
) -> dict[str, Any]:
    """Return sidecar-compatible deployment JSON with observed, unverified hashes.

    The caller must provide contract addresses from explicit sources. This
    routine proves that code exists at the pin, not that it is the intended
    implementation or that source and historical ABI match.
    """
    if type(chain_id) is not int or chain_id <= 0:
        raise ValueError("invalid chain ID")
    block = candidate.get("proposed_pin_block")
    if type(block) is not int or block < 0:
        raise ValueError("invalid recorded pin block")
    expected_hash = "0x" + _hex_bytes(candidate.get("proposed_pin_hash"), 32).hex()
    order_key = "0x" + _hex_bytes(candidate.get("order_key"), 32).hex()
    if candidate.get("selection_skip_reasons"):
        raise ValueError("candidate has selection skip reasons")
    addresses = {"datastore": datastore, "reader": reader,
                 "order_handler": order_handler, "router": router}
    if referral_storage is not None:
        addresses["referral_storage"] = referral_storage
    for name, value in addresses.items():
        if not any(_hex_bytes(value, 20)):
            raise ValueError(f"zero {name} address")
    traced = EvidenceRpc(rpc)

    def read(method: str, params: list[Any]) -> Any:
        envelope = traced.request(method, params)
        if "error" in envelope or "result" not in envelope:
            raise OSError(f"{method} failed")
        return envelope["result"]

    def header() -> str:
        value = read("eth_getBlockByNumber", [hex(block), False])
        if not isinstance(value, dict) or int(value.get("number", "-1"), 16) != block:
            raise ValueError("archive block number mismatch")
        return "0x" + _hex_bytes(value.get("hash"), 32).hex()

    if int(read("eth_chainId", []), 16) != chain_id:
        raise ValueError("archive chain mismatch")
    before = header()
    if before != expected_hash:
        raise ValueError("recorded archive block hash mismatch")
    hashes: dict[str, str] = {}
    for name, address in addresses.items():
        raw = read("eth_getCode", [address, hex(block)])
        if not isinstance(raw, str) or not raw.startswith("0x") or raw == "0x":
            raise ValueError(f"missing historical {name} code")
        code = bytes.fromhex(raw[2:])
        if not code:
            raise ValueError(f"empty historical {name} code")
        hashes[name] = "0x" + keccak256(code).hex()
    if referral_storage is not None:
        pointer_raw = read("eth_call", [{"to": order_handler,
                                          "data": calldata("referralStorage()")}, hex(block)])
        pointer = words(pointer_raw, 1)[0]
        if pointer >= 2**160 or "0x" + f"{pointer:040x}" != referral_storage.lower():
            raise ValueError("OrderHandler referral pointer differs from supplied address")
    after = header()
    if after != before:
        raise ValueError("archive block changed during deployment read")
    result = {
        "schema": "GmxStep41ObservedDeployment", "version": 1,
        "proof_level": "code_hashes_observed_at_pin_not_source_verified",
        "ready_for_comparison": False,
        "candidate": {"recording": candidate.get("recording"), "order_key": order_key,
                      "creation": candidate.get("creation"),
                      "pin_block": block, "pin_hash": before},
        "deployment": {"chain_id": chain_id, "router": router,
                       "router_code_hash": hashes["router"],
                       "datastore": datastore,
                       "datastore_code_hash": hashes["datastore"],
                       "errors": ["EndOfOracleSimulation()"]},
        "reader": {"address": reader, "code_hash": hashes["reader"]},
        "order_handler": {"address": order_handler,
                          "code_hash": hashes["order_handler"]},
        "rpc_transcript": traced.calls,
    }
    if referral_storage is not None:
        result["referral_storage"] = {"address": referral_storage,
                                      "code_hash": hashes["referral_storage"]}
    return result
