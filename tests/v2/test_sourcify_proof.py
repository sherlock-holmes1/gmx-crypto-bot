import json
from pathlib import Path
from tempfile import TemporaryDirectory

from gmx_crypto_bot_v2.crosscheck.sourcify_proof import verify_sourcify_records
from gmx_crypto_bot_v2.crosscheck.source_manifest import extract_solidity_metadata
from gmx_crypto_bot_v2.domain.keys import keccak256


ROLES = ("router", "datastore", "reader", "order_handler", "referral_storage")
ADDRESS = "0x" + "11" * 20


def _fixture(root: Path):
    trailer = (bytes.fromhex("a2646970667358221220") + b"\x22" * 32 +
               bytes.fromhex("64736f6c6343") + bytes([0, 8, 29]))
    code = "0x" + (b"\x60\x00" + trailer + len(trailer).to_bytes(2, "big")).hex()
    identity = extract_solidity_metadata(code)
    reader_numbers = ["field" + str(i) for i in range(13)]
    reader_numbers[9:13] = ["uiFeeFactor", "updatedAtTime", "validFromTime", "srcChainId"]
    reader_abi = [{"name": "getOrder", "outputs": [{"components": [
        {"components": []}, {"components": [{"name": name} for name in reader_numbers]}
    ]}]}]
    manifest = {"schema": "GmxStep41HistoricalSourceManifest", "version": 1,
                "chain_id": 42161, "pin_block": 100,
                "pin_hash": "0x" + "aa" * 32,
                "sidecar_sha256": "0x" + "bb" * 32,
                "contracts": {role: {"address": ADDRESS, **identity} for role in ROLES}}
    source_text = {
        "X.sol": "contract X {}",
        "contracts/order/Order.sol": "uint256 uiFeeFactor;",
        "contracts/order/OrderStoreUtils.sol": "keccak256(abi.encode(key, UI_FEE_FACTOR))",
    }
    metadata_sources = {path: {"keccak256": "0x" + keccak256(content.encode()).hex()}
                        for path, content in source_text.items()}
    for role in ROLES:
        abi = reader_abi if role == "reader" else []
        record = {
            "runtimeMatch": "exact_match", "match": "exact_match", "matchId": 1,
            "chainId": 42161, "address": ADDRESS,
            "runtimeBytecode": {"onchainBytecode": code},
            "compilation": {"compilerVersion": "0.8.29+commit.test",
                            "fullyQualifiedName": "X.sol:X"},
            "metadata": {"sources": metadata_sources, "output": {"abi": abi}},
            "sources": {path: {"content": content} for path, content in source_text.items()},
            "abi": abi,
            "stdJsonOutput": {"contracts": {"X.sol": {"X": {}}}},
        }
        (root / f"{role}.json").write_text(json.dumps(record))
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return manifest_path


def test_exact_match_records_bind_code_sources_and_reader_layout():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        manifest = _fixture(root)
        proof = verify_sourcify_records(manifest, root)
        assert len(proof["contracts"]) == 5
        assert proof["reader_ui_fee_slot"] == 9
        assert proof["raw_metadata_cid_preimage_verified"] is False
        assert proof["independent_compiler_rebuild_verified"] is False
        record_path = root / "reader.json"
        record = json.loads(record_path.read_text())
        record["sources"]["contracts/order/Order.sol"]["content"] += " changed"
        record_path.write_text(json.dumps(record))
        try:
            verify_sourcify_records(manifest, root)
        except ValueError as error:
            assert "source hash mismatch" in str(error)
        else:
            raise AssertionError("tampered historical source accepted")


def test_runtime_and_exact_match_must_both_hold():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        manifest = _fixture(root)
        path = root / "router.json"
        record = json.loads(path.read_text())
        record["runtimeMatch"] = "match"
        path.write_text(json.dumps(record))
        try:
            verify_sourcify_records(manifest, root)
        except ValueError as error:
            assert "not an exact" in str(error)
        else:
            raise AssertionError("non-exact source accepted")
        record["runtimeMatch"] = "exact_match"
        record["runtimeBytecode"]["onchainBytecode"] = "0x6001" + record[
            "runtimeBytecode"]["onchainBytecode"][6:]
        path.write_text(json.dumps(record))
        try:
            verify_sourcify_records(manifest, root)
        except ValueError as error:
            assert "pinned runtime" in str(error)
        else:
            raise AssertionError("wrong runtime accepted")
