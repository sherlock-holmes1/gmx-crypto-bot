import json
from pathlib import Path
from tempfile import TemporaryDirectory

from gmx_crypto_bot_v2.crosscheck.source_manifest import (
    build_source_manifest, extract_solidity_metadata,
)
from gmx_crypto_bot_v2.domain.keys import keccak256


def code():
    trailer = (bytes.fromhex("a2646970667358221220") + b"\x11" * 32 +
               bytes.fromhex("64736f6c6343") + bytes([0, 8, 29]))
    assert len(trailer) == 51
    return "0x" + (b"\x60\x00" + trailer + len(trailer).to_bytes(2, "big")).hex()


def test_metadata_extraction_binds_runtime_and_compiler():
    result = extract_solidity_metadata(code())
    assert result["ipfs_cid_v0"].startswith("Qm")
    assert result["compiler_version"] == "0.8.29"
    assert result["metadata_length"] == 51
    assert result["runtime_code_hash"] == "0x" + keccak256(bytes.fromhex(code()[2:])).hex()
    for bad in ("0x", code()[:-4] + "0000"):
        try:
            extract_solidity_metadata(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("malformed metadata accepted")


def test_manifest_requires_every_pinned_address_and_code_hash():
    address = "0x" + "11" * 20
    h = "0x" + keccak256(bytes.fromhex(code()[2:])).hex()
    deployment = {"chain_id": 1}
    for role in ("router", "datastore", "reader", "order_handler", "referral_storage"):
        deployment[role] = address
        deployment[role + "_code_hash"] = h
    sidecar = {"schema": "GmxStep41ArchiveSidecar", "version": 1,
               "deployment": deployment,
               "pin": {"number": 100, "archive_hash": "0x" + "aa" * 32,
                       "hash_after": "0x" + "aa" * 32},
               "rpc_transcript": [{"method": "eth_getCode",
                                   "params": [address, "0x64"],
                                   "response": {"result": code()}}]}
    with TemporaryDirectory() as directory:
        path = Path(directory) / "sidecar.json"
        path.write_text(json.dumps(sidecar))
        result = build_source_manifest(path)
        assert len(result["contracts"]) == 5
        assert result["historical_source_abi_key_layout_proved"] is False
        sidecar["deployment"]["reader_code_hash"] = "0x" + "00" * 32
        path.write_text(json.dumps(sidecar))
        try:
            build_source_manifest(path)
        except ValueError as error:
            assert "mismatched reader" in str(error)
        else:
            raise AssertionError("mismatched Reader code accepted")
        for missing in ("reader_code_hash", "referral_storage"):
            damaged = json.loads(json.dumps(sidecar))
            damaged["deployment"]["reader_code_hash"] = h
            del damaged["deployment"][missing]
            path.write_text(json.dumps(damaged))
            try:
                build_source_manifest(path)
            except ValueError as error:
                assert "missing" in str(error) and "deployment" in str(error)
            else:
                raise AssertionError(f"missing {missing} was silently omitted")
