import json
from pathlib import Path
from tempfile import TemporaryDirectory

from gmx_crypto_bot_v2.crosscheck.source_manifest import (
    _cid_v0, build_source_manifest, extract_solidity_metadata,
    verify_local_source_bundle,
)
from gmx_crypto_bot_v2.domain.keys import keccak256
from hashlib import sha256
import shutil


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


def test_local_compiler_bundle_requires_cid_and_all_source_hashes():
    source = b"pragma solidity ^0.8.29; contract X {}\n"
    metadata = json.dumps({"compiler": {"version": "0.8.29+commit.example"},
                           "sources": {"contracts/X.sol": {
                               "keccak256": "0x" + keccak256(source).hex()}},
                           "output": {"abi": []}}, separators=(",", ":")).encode()
    cid = _cid_v0(b"\x12\x20" + sha256(metadata).digest())
    roles = ("router", "datastore", "reader", "order_handler", "referral_storage")
    manifest = {"schema": "GmxStep41HistoricalSourceManifest", "version": 1,
                "sidecar_sha256": "0x" + "aa" * 32,
                "chain_id": 42161, "pin_block": 100,
                "pin_hash": "0x" + "bb" * 32,
                "contracts": {role: {"ipfs_cid_v0": cid,
                                     "compiler_version": "0.8.29",
                                     "address": "0x" + "11" * 20,
                                     "runtime_code_hash": "0x" + "cc" * 32,
                                     "runtime_code_sha256": "0x" + "dd" * 32}
                              for role in roles}}
    with TemporaryDirectory() as directory:
        root = Path(directory)
        for role in roles:
            destination = root / role / "sources" / "contracts"
            destination.mkdir(parents=True)
            (destination / "X.sol").write_bytes(source)
            (root / role / "metadata.json").write_bytes(metadata)
        report = verify_local_source_bundle(manifest, root)
        assert report["content_hashes_verified"] is True
        assert report["runtime_rebuild_verified"] is False
        assert report["sidecar_sha256"] == manifest["sidecar_sha256"]
        assert report["pin_block"] == 100 and report["pin_hash"] == manifest["pin_hash"]
        assert report["contracts"]["reader"]["runtime_code_hash"] == "0x" + "cc" * 32
        assert report["input_manifest_sha256"] == "0x" + sha256(json.dumps(
            manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        (root / "reader" / "sources" / "contracts" / "X.sol").write_bytes(source + b"// tampered")
        try:
            verify_local_source_bundle(manifest, root)
        except ValueError as error:
            assert "reader source hash mismatch" in str(error)
        else:
            raise AssertionError("tampered source accepted")
        (root / "reader" / "sources" / "contracts" / "X.sol").write_bytes(source)
        (root / "reader" / "metadata.json").write_bytes(metadata + b" ")
        try:
            verify_local_source_bundle(manifest, root)
        except ValueError as error:
            assert "reader metadata CID hash mismatch" in str(error)
        else:
            raise AssertionError("tampered metadata accepted")


def test_bundle_role_symlink_cannot_escape_bundle_root():
    source = b"pragma solidity ^0.8.29; contract X {}\n"
    metadata = json.dumps({"compiler": {"version": "0.8.29"},
                           "sources": {"X.sol": {"keccak256": "0x" + keccak256(source).hex()}},
                           "output": {"abi": []}}).encode()
    cid = _cid_v0(b"\x12\x20" + sha256(metadata).digest())
    roles = ("router", "datastore", "reader", "order_handler", "referral_storage")
    manifest = {"schema": "GmxStep41HistoricalSourceManifest", "version": 1,
                "sidecar_sha256": "0x" + "aa" * 32, "chain_id": 1,
                "pin_block": 100, "pin_hash": "0x" + "bb" * 32,
                "contracts": {role: {"ipfs_cid_v0": cid,
                                     "compiler_version": "0.8.29",
                                     "address": "0x" + "11" * 20,
                                     "runtime_code_hash": "0x" + "cc" * 32,
                                     "runtime_code_sha256": "0x" + "dd" * 32}
                              for role in roles}}
    with TemporaryDirectory() as bundle, TemporaryDirectory() as outside:
        root = Path(bundle)
        for role in roles:
            target = root / role
            (target / "sources").mkdir(parents=True)
            (target / "metadata.json").write_bytes(metadata)
            (target / "sources" / "X.sol").write_bytes(source)
        shutil.rmtree(root / "reader")
        (root / "reader").symlink_to(outside, target_is_directory=True)
        try:
            verify_local_source_bundle(manifest, root)
        except ValueError as error:
            assert "reader role path escapes bundle" in str(error)
        else:
            raise AssertionError("role symlink escape accepted")
        (root / "reader").unlink()
        (root / "reader").mkdir()
        (root / "reader" / "metadata.json").write_bytes(metadata)
        (Path(outside) / "X.sol").write_bytes(source)
        (root / "reader" / "sources").symlink_to(outside, target_is_directory=True)
        try:
            verify_local_source_bundle(manifest, root)
        except ValueError as error:
            assert "reader sources directory escapes role" in str(error)
        else:
            raise AssertionError("sources directory symlink escape accepted")
