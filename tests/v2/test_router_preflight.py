"""Focused ABI, eligibility, and failure classification tests for Stage 2."""

import unittest

from gmx_crypto_bot_v2.domain.keys import keccak256
from gmx_crypto_bot_v2.crosscheck.router_preflight import (
    Deployment, OracleInput, RouterPreflight, _FIELDS, _field_key, _key,
    _selector, _word, decode_revert, encode_simulation, latest_key,
)

DATASTORE = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
ORDER_KEY = latest_key(DATASTORE, 5)
CODE = "0x60016000"
CODE_HASH = "0x" + keccak256(bytes.fromhex(CODE[2:])).hex()
DEPLOYMENT = Deployment(42161, ROUTER, CODE_HASH, DATASTORE, CODE_HASH,
                        ("EndOfOracleSimulation()", "OrderNotFound(bytes32)"))
ORACLE = OracleInput(("0x" + "33" * 20,), ((100, 101),), 10, 11,
                     "recorded tx", "verified GMX integer units")


class MockRpc:
    def __init__(self, request, *, nonce=5, pending=True, router_error=None,
                 block_hash="0x" + "aa" * 32, reorg=False):
        self.request_fields = request
        self.nonce = nonce
        self.pending = pending
        self.router_error = router_error
        self.block_hash = block_hash
        self.reorg = reorg
        self.block_reads = 0
        self.methods = []

    def request(self, method, params):
        self.methods.append(method)
        if method == "eth_chainId":
            return {"result": hex(42161)}
        if method == "eth_getBlockByNumber":
            self.block_reads += 1
            h = "0x" + "bb" * 32 if self.reorg and self.block_reads > 1 else self.block_hash
            return {"result": {"hash": h}}
        if method == "eth_getCode":
            return {"result": CODE}
        assert method == "eth_call"
        to, data = params[0]["to"], bytes.fromhex(params[0]["data"][2:])
        assert params[1] == "0x64"
        if to == ROUTER:
            return {"error": self.router_error} if self.router_error else {"result": "0x"}
        sig, key = data[:4], data[4:]
        if sig == _selector("containsBytes32(bytes32,bytes32)"):
            return {"result": "0x" + _word(int(self.pending)).hex()}
        if sig == _selector("getUint(bytes32)") and key == _key("NONCE"):
            return {"result": "0x" + _word(self.nonce).hex()}
        for name, label in _FIELDS:
            if key != _field_key(bytes.fromhex(ORDER_KEY[2:]), label):
                continue
            value = self.request_fields[name]
            if isinstance(value, list):
                size = 20 if name == "swapPath" else 32
                raw = _word(32) + _word(len(value)) + b"".join(
                    bytes.fromhex(item[2:]).rjust(32, b"\0") for item in value)
            elif isinstance(value, str):
                raw = bytes.fromhex(value[2:]).rjust(32, b"\0")
            else:
                raw = _word(int(value))
            return {"result": "0x" + raw.hex()}
        raise AssertionError("unexpected DataStore call")


def sample_request():
    result = {}
    for name, _ in _FIELDS:
        if name in {"swapPath", "dataList"}:
            result[name] = []
        elif name in {"account", "receiver", "cancellationReceiver", "callbackContract",
                      "uiFeeReceiver", "market", "initialCollateralToken"}:
            result[name] = "0x" + "33" * 20
        elif name in {"isLong", "shouldUnwrapNativeToken", "isFrozen", "autoCancel"}:
            result[name] = name == "isLong"
        else:
            result[name] = 2 if name == "orderType" else 100
    return result


def candidate(request):
    return {"order_key": ORDER_KEY, "proposed_pin_block": 100,
            "proposed_pin_hash": "0x" + "aa" * 32,
            "selection_skip_reasons": [], "request_at_proposed_pin": request}


class RouterPreflightTests(unittest.TestCase):
    def test_abi_layout_vector(self):
        encoded = bytes.fromhex(encode_simulation(ORACLE)[2:])
        self.assertEqual(encoded[:4], _selector("simulateExecuteLatestOrder((address[],(uint256,uint256)[],uint256,uint256))"))
        words = [int.from_bytes(encoded[i:i+32], "big") for i in range(4, len(encoded), 32)]
        self.assertEqual(words[:6], [32, 128, 192, 10, 11, 1])
        self.assertEqual(words[-3:], [1, 100, 101])

    def test_passed_preflight_preserves_raw_revert(self):
        raw = "0x" + _selector("EndOfOracleSimulation()").hex()
        rpc = MockRpc(sample_request(), router_error={"code": 3, "data": raw})
        result = RouterPreflight(rpc, DEPLOYMENT).run(candidate(sample_request()), ORACLE)
        self.assertEqual(result["outcome"], "passed_preflight")
        self.assertEqual(result["raw_revert"], raw)
        self.assertTrue(result["pending"])
        self.assertTrue(result["latest"])
        self.assertEqual(result["pin_block_hash_after"], result["pin_block_hash"])
        self.assertNotIn("eth_sendTransaction", rpc.methods)

    def test_latest_and_pending_gates_prevent_router_call(self):
        for options, reason in (({"nonce": 6}, "global_nonce_key_mismatch"),
                                ({"pending": False}, "order_not_pending")):
            with self.subTest(reason=reason):
                rpc = MockRpc(sample_request(), **options)
                result = RouterPreflight(rpc, DEPLOYMENT).run(candidate(sample_request()), ORACLE)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["outcome"], "ineligible_latest_request")
                self.assertEqual(rpc.methods.count("eth_call"), 1 if reason.startswith("global") else 2)

    def test_request_mismatch_fails_closed(self):
        recorded = sample_request()
        recorded["sizeDeltaUsd"] = 999
        rpc = MockRpc(sample_request())
        result = RouterPreflight(rpc, DEPLOYMENT).run(candidate(recorded), ORACLE)
        self.assertEqual(result["reason"], "request_field_mismatch")
        self.assertIn("sizeDeltaUsd", result["mismatched_fields"])

    def test_recorded_candidate_shape_uses_archive_only_fields(self):
        # Stage 1 OrderCreated omits these Order.Props fields. They are read
        # from pinned storage and labelled, while emitted fields still match.
        recorded = sample_request()
        for name in ("swapPath", "isFrozen", "dataList"):
            del recorded[name]
        rpc = MockRpc(sample_request(), router_error={
            "data": "0x" + _selector("EndOfOracleSimulation()").hex()})
        result = RouterPreflight(rpc, DEPLOYMENT).run(candidate(recorded), ORACLE)
        self.assertEqual(result["outcome"], "passed_preflight")
        self.assertEqual(result["archive_only_request_fields"],
                         ["swapPath", "isFrozen", "dataList"])
        self.assertEqual(result["on_chain_request"]["swapPath"], [])

    def test_transport_timeout_is_provider_failure(self):
        class TimeoutRpc:
            def request(self, method, params):
                raise TimeoutError("provider token must not leak")
        result = RouterPreflight(TimeoutRpc(), DEPLOYMENT).run(candidate(sample_request()), ORACLE)
        self.assertEqual(result["outcome"], "provider_failure")
        self.assertNotIn("provider token", str(result))

    def test_reorg_discards_pass(self):
        raw = "0x" + _selector("EndOfOracleSimulation()").hex()
        rpc = MockRpc(sample_request(), router_error={"data": raw}, reorg=True)
        result = RouterPreflight(rpc, DEPLOYMENT).run(candidate(sample_request()), ORACLE)
        self.assertEqual(result["reason"], "block_hash_changed")

    def test_error_classes(self):
        known = "0x" + _selector("OrderNotFound(bytes32)").hex() + "00" * 32
        self.assertEqual(decode_revert(known, DEPLOYMENT.errors)["outcome"], "validation_error")
        self.assertEqual(decode_revert("0x12345678", DEPLOYMENT.errors)["outcome"], "unknown_contract_error")
        self.assertEqual(decode_revert(None, DEPLOYMENT.errors)["outcome"], "provider_failure")


if __name__ == "__main__":
    unittest.main()
