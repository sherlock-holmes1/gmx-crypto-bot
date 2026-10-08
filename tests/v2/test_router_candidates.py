"""Recorded GMX order selection for the router preflight."""

import json
from pathlib import Path

from gmx_crypto_bot_v2.crosscheck.router_candidates import select_router_candidates


RECORDING = Path(__file__).resolve().parents[2] / "recordings/eth-usdc-v2-sep-20-sep-27"


def _real_creation():
    with (RECORDING / "events.jsonl").open() as stream:
        for line in stream:
            row = json.loads(line)
            if row["kind"] == "gmx_market_log" and row["payload"].get("event_name") == "OrderCreated":
                return row
    raise AssertionError("recording has no OrderCreated event")


def _write(tmp_path, *rows):
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    return tmp_path


def test_real_raw_schema_decodes_request_and_keeps_coordinates(tmp_path):
    row = _real_creation()
    candidates = select_router_candidates(_write(tmp_path, row))
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["order_key"] == "0x40974329dc481a5f37104a26262c1578db7e78097b88ef88fbb26ba6915c9c1c"
    assert candidate["category"] == "short_increase"
    assert candidate["creation"]["transaction_hash"] == row["payload"]["log"]["transactionHash"].lower()
    assert candidate["creation"]["block_hash"] == row["payload"]["log"]["blockHash"]
    assert candidate["creation_request"]["sizeDeltaUsd"] > 0
    assert candidate["router_eligibility_gate"] == "not_checked"
    assert candidate["same_order_state_gate"] == "not_checked"


def test_same_block_terminal_skips_without_archive_claim(tmp_path):
    created = _real_creation()
    terminal = json.loads(json.dumps(created))
    terminal["payload"]["event_name"] = "OrderExecuted"
    terminal["payload"]["log"]["_decoded"] = {
        "event_name": "OrderExecuted",
        "values": {"key": "0x40974329dc481a5f37104a26262c1578db7e78097b88ef88fbb26ba6915c9c1c"},
    }
    terminal["log_index"] += 1
    candidates = select_router_candidates(_write(tmp_path, terminal, created))
    assert candidates[0]["selection_skip_reasons"] == ["same_block_terminal"]
    assert candidates[0]["router_eligibility_gate"] == "selection_ineligible"


def test_bad_creation_log_is_visible_and_skipped(tmp_path):
    created = _real_creation()
    created["payload"]["log"]["data"] = "0x1234"
    candidate = select_router_candidates(_write(tmp_path, created))[0]
    assert "creation_log_decode_failed" in candidate["selection_skip_reasons"]
    assert candidate["order_key"] is None
    assert candidate["creation"]["block_hash"] == created["payload"]["log"]["blockHash"]


def test_four_categories_and_unsupported_type(tmp_path):
    seed = _real_creation()
    rows = []
    expected = ["long_increase", "short_increase", "long_decrease", "short_decrease", None]
    for index, (order_type, is_long) in enumerate(((2, True), (3, False), (4, True), (6, False), (7, True))):
        row = json.loads(json.dumps(seed))
        row["block_number"] += index
        row["payload"]["log"]["_decoded"] = {
            "event_name": "OrderCreated",
            "values": {
                "key": f"0x{index + 1:064x}", "market": "0xmarket", "orderType": order_type,
                "isLong": is_long, "account": "0xaccount", "initialCollateralToken": "0xtoken",
                "sizeDeltaUsd": 100, "acceptablePrice": 10, "triggerPrice": 0,
            },
        }
        rows.append(row)
    candidates = select_router_candidates(_write(tmp_path, *rows))
    assert [candidate["category"] for candidate in candidates] == expected
    assert candidates[-1]["selection_skip_reasons"] == ["unsupported_order_type"]


def _lifecycle(seed, name, values, log_index):
    row = json.loads(json.dumps(seed))
    row["payload"]["event_name"] = name
    row["payload"]["log"]["_decoded"] = {"event_name": name, "values": values}
    row["log_index"] = log_index
    return row


def test_auto_updates_apply_in_coordinate_order(tmp_path):
    created = _real_creation()
    key = "0x40974329dc481a5f37104a26262c1578db7e78097b88ef88fbb26ba6915c9c1c"
    size = _lifecycle(created, "OrderSizeDeltaAutoUpdated", {"key": key, "nextSizeDeltaUsd": 40}, created["log_index"] + 1)
    collateral = _lifecycle(created, "OrderCollateralDeltaAmountAutoUpdated", {"key": key, "nextCollateralDeltaAmount": 30}, created["log_index"] + 2)
    ordinary = _lifecycle(created, "OrderUpdated", {"key": key, "sizeDeltaUsd": 20}, created["log_index"] + 3)
    candidate = select_router_candidates(_write(tmp_path, ordinary, collateral, created, size))[0]
    assert candidate["request_at_proposed_pin"]["sizeDeltaUsd"] == 20
    assert candidate["request_at_proposed_pin"]["initialCollateralDeltaAmount"] == 30
    assert [x["event_name"] for x in candidate["same_block_updates"]] == [
        "OrderSizeDeltaAutoUpdated", "OrderCollateralDeltaAmountAutoUpdated", "OrderUpdated"]


def test_failed_or_mismatched_lifecycle_decode_is_an_evidence_gap(tmp_path):
    created = _real_creation()
    broken = _lifecycle(created, "OrderUpdated", {"key": "0xunknown"}, created["log_index"] + 1)
    broken["payload"]["log"]["data"] = "0x1234"
    del broken["payload"]["log"]["_decoded"]
    candidate = select_router_candidates(_write(tmp_path, created, broken))[0]
    assert "lifecycle_log_decode_failed" in candidate["selection_skip_reasons"]
    assert candidate["router_eligibility_gate"] == "selection_ineligible"

    mismatched = _lifecycle(created, "OrderUpdated", {"key": candidate["order_key"]}, created["log_index"] + 1)
    mismatched["payload"]["log"]["_decoded"]["event_name"] = "OrderCancelled"
    candidate = select_router_candidates(_write(tmp_path, created, mismatched))[0]
    assert "lifecycle_event_name_mismatch" in candidate["selection_skip_reasons"]


def test_later_malformed_lifecycle_does_not_disqualify_earlier_pin(tmp_path):
    created = _real_creation()
    later = _lifecycle(created, "OrderUpdated", {"key": "0xunknown"}, created["log_index"] + 1)
    later["block_number"] = created["block_number"] + 1
    later["payload"]["log"]["data"] = "0x1234"
    del later["payload"]["log"]["_decoded"]
    candidate = select_router_candidates(_write(tmp_path, created, later))[0]
    assert candidate["selection_skip_reasons"] == []

    later["block_number"] = created["block_number"]
    candidate = select_router_candidates(_write(tmp_path, created, later))[0]
    assert candidate["selection_skip_reasons"] == ["lifecycle_log_decode_failed"]


def test_known_key_lifecycle_gap_only_affects_that_order(tmp_path):
    seed = _real_creation()
    first = json.loads(json.dumps(seed))
    second = json.loads(json.dumps(seed))
    second["block_number"] += 1
    second["payload"]["log"]["_decoded"] = {
        "event_name": "OrderCreated",
        "values": {
            "key": "0x" + "2" * 64, "market": "0xmarket", "orderType": 2,
            "isLong": True, "account": "0xaccount", "initialCollateralToken": "0xtoken",
            "sizeDeltaUsd": 100, "acceptablePrice": 10, "triggerPrice": 0,
        },
    }
    mismatch = _lifecycle(first, "OrderUpdated", {"key": "0x40974329dc481a5f37104a26262c1578db7e78097b88ef88fbb26ba6915c9c1c"}, first["log_index"] + 1)
    mismatch["payload"]["log"]["_decoded"]["event_name"] = "OrderCancelled"
    candidates = select_router_candidates(_write(tmp_path, first, second, mismatch))
    assert candidates[0]["selection_skip_reasons"] == ["lifecycle_event_name_mismatch"]
    assert candidates[1]["selection_skip_reasons"] == []


def test_malformed_lifecycle_before_creation_does_not_affect_candidate(tmp_path):
    created = _real_creation()
    broken = _lifecycle(created, "OrderUpdated", {"key": "0xunknown"}, created["log_index"] - 1)
    broken["payload"]["log"]["data"] = "0x1234"
    del broken["payload"]["log"]["_decoded"]

    same_block = select_router_candidates(_write(tmp_path, created, broken))[0]
    assert same_block["selection_skip_reasons"] == []

    broken["block_number"] = created["block_number"] - 1
    earlier_block = select_router_candidates(_write(tmp_path, created, broken))[0]
    assert earlier_block["selection_skip_reasons"] == []
