"""Prove that an observed execution's pre-state equals a block boundary.

A router ``eth_call`` can only observe state at a whole block boundary. The
order selected for Step 4.1 executed at transaction index 1 of its block, so
the state immediately before that execution is an *intra-block* coordinate that
no block-boundary call reproduces -- unless every transaction ordered before it
in that block provably cannot have modified GMX storage.

This module captures exactly that proof, and nothing wider. For every
transaction at an index lower than the observed execution it requires, from
explicit raw RPC evidence:

* transaction ``type`` ``0x6a``, the Arbitrum internal transaction type;
* ``from`` and ``to`` both equal to the ArbOS internal account
  ``0x00000000000000000000000000000000000a4b05``;
* calldata whose four-byte selector is ``0x6bf6a42d``, the ArbOS start-block
  call;
* ``gas``, ``gasPrice`` and ``value`` equal to zero;
* a receipt with ``status`` 1, ``gasUsed`` 0, an empty ``logs`` array, a null
  ``contractAddress``, the ArbOS internal account as ``to``, and the pinned
  execution block hash.

The capture also reads the boundary block ``B - 1`` itself. It derives that
block's hash from the verified execution header's ``parentHash``, requires the
``B - 1`` header to carry exactly that hash, and requires ``timestamp(B - 1)``
to equal ``timestamp(B)``. GMX's MarketIncrease path reads ``block.timestamp``
for the oracle max-age gate and for borrowing and funding elapsed seconds, so a
boundary whose timestamp differs is refused with
``block_timestamp_advances_across_pin_boundary``.

The execution block hash is read before and after the capture and must equal
the recorded execution block hash both times; otherwise the capture is
classified ``reorg_detected``.

What a passing capture proves
-----------------------------
No **non-ArbOS contract storage** can have changed between the end of block
``B - 1`` and the observed execution: the only transactions ordered before it
are ArbOS internal start-block transactions that consumed zero gas, emitted no
logs, created no contract, and targeted the ArbOS internal account rather than
any GMX contract. The boundary block is identified by hash, and it carries the
same ``block.timestamp`` as the execution block.

What it does NOT prove
----------------------
* That **ArbOS internal state** is equal across the boundary. The proved
  start-block transaction exists precisely to advance ArbOS's L1 view, and
  ``l1BlockNumber`` differs between the two blocks. Both values are recorded so
  the divergence stays visible.
* That the rest of the EVM **block context** is equal. A call pinned at
  ``B - 1`` observes that block's ``block.number`` and ``blockhash``; only
  ``block.timestamp`` equality is proved.
* That an archive provider will serve state at ``B - 1``, or that the chain has
  not since reorganised past it. That is a separate gate.
* That any GMX state cell was actually read at that pin. Every pinned DataStore
  transcript remains its own separate gate.
* Anything about transactions ordered *after* the observed execution.
* That the observed execution itself succeeded, or why.
* That the observed execution is the order under study. That binding comes from
  the oracle evidence's recorded coordinates, which the decision adapter checks
  against this proof's block, hash and transaction index.

Absence of a log in a filtered recording is never evidence here. Every
conclusion is derived from a recorded RPC response, and any gap, provider
error, unexpected field, or missing receipt fails the capture closed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.sources.rpc import redact_rpc_endpoint


SCHEMA = "GmxStep41SystemTransactionPrestate"
VERSION = 1

ARBOS_INTERNAL_ADDRESS = "0x00000000000000000000000000000000000a4b05"
ARBOS_INTERNAL_TX_TYPE = 0x6A
ARBOS_START_BLOCK_SELECTOR = "0x6bf6a42d"

PROVES = (
    "every transaction ordered before the observed execution in this block is an ArbOS "
    "internal start-block transaction that consumed zero gas, emitted no logs, created no "
    "contract, and targeted the ArbOS internal account rather than any GMX contract; no "
    "non-ArbOS contract storage can therefore have changed between the end of the preceding "
    "block, identified here by hash, and the observed execution, and that preceding block "
    "carries the same block timestamp"
)

DOES_NOT_PROVE = (
    "that ArbOS internal state is equal across the boundary: the proved start-block "
    "transaction exists to advance ArbOS's L1 view, and l1BlockNumber differs between the two "
    "blocks",
    "that the rest of the EVM block context is equal: a call pinned at the preceding block "
    "observes that block's block.number and blockhash, and only block.timestamp equality is "
    "proved",
    "that an archive provider will serve state at the preceding block, or that the chain has "
    "not since reorganised past it",
    "that any GMX state cell was read at that pin",
    "anything about transactions ordered after the observed execution",
    "that the observed execution itself succeeded",
    "that the observed execution belongs to any particular order",
)

READ_ONLY_METHODS = frozenset({
    "eth_chainId", "eth_getBlockByNumber",
    "eth_getTransactionByBlockNumberAndIndex", "eth_getTransactionReceipt",
})


class _Unproved(Exception):
    """One exact, machine-readable reason why equivalence stayed unproved."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PrestateRpc:
    """Record every read-only request and response without keeping an endpoint URL."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, params: list[Any]) -> dict[str, Any]:
        if method not in READ_ONLY_METHODS:
            raise ValueError("block prestate capture permits read-only methods only")
        envelope = self.inner.request(method, params)
        if not isinstance(envelope, dict):
            envelope = {"error": {"message": "malformed_rpc_envelope"}}
        # Provider errors can echo a credential-bearing endpoint.
        recorded = {"error": "redacted_provider_error"} if "error" in envelope else envelope
        self.calls.append({"method": method, "params": params, "response": recorded})
        return envelope


def _uint(value: Any) -> int | None:
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 66 and value.startswith("0x")


def _address(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return value.lower()


def _transcript_digest(transcript: list[dict[str, Any]]) -> str:
    body = json.dumps(transcript, sort_keys=True, separators=(",", ":")).encode()
    return "0x" + hashlib.sha256(body).hexdigest()


def _result(rpc: PrestateRpc, method: str, params: list[Any], reason: str) -> Any:
    envelope = rpc.request(method, params)
    if not isinstance(envelope, dict) or "error" in envelope or "result" not in envelope:
        raise _Unproved(reason)
    return envelope["result"]


def _check_header(header: Any, *, block_number: int, block_hash: str, phase: str,
                  mismatch_reason: str = "reorg_detected") -> dict[str, Any]:
    if not isinstance(header, dict) or _uint(header.get("number")) != block_number:
        raise _Unproved("block_header_number_mismatch_" + phase)
    if not _is_hash(header.get("hash")) or header["hash"].lower() != block_hash:
        raise _Unproved(mismatch_reason)
    return header


def _boundary_facts(record: dict[str, Any], execution_header: dict[str, Any],
                    boundary_header: Any, *, block_number: int) -> None:
    """Derive and check the preceding block's identity and timestamp.

    The boundary hash is derived from the verified execution header's
    ``parentHash``; the boundary header must then carry exactly that hash. GMX
    reads ``block.timestamp`` on this path, so an advancing timestamp refuses
    the boundary outright.
    """
    timestamp = _uint(execution_header.get("timestamp"))
    if timestamp is None:
        raise _Unproved("execution_block_timestamp_unavailable")
    parent = execution_header.get("parentHash")
    if not _is_hash(parent):
        raise _Unproved("execution_block_parent_hash_malformed")
    record["execution_block_timestamp"] = timestamp
    record["equivalent_pin_block_hash"] = parent.lower()
    record["arbos_l1_block_number"]["execution_block"] = _uint(
        execution_header.get("l1BlockNumber"))
    boundary = _check_header(boundary_header, block_number=block_number - 1,
                             block_hash=parent.lower(), phase="equivalent_pin",
                             mismatch_reason="equivalent_pin_block_hash_mismatch")
    record["equivalent_pin_block_timestamp"] = _uint(boundary.get("timestamp"))
    record["arbos_l1_block_number"]["equivalent_pin_block"] = _uint(
        boundary.get("l1BlockNumber"))
    if record["equivalent_pin_block_timestamp"] != timestamp:
        raise _Unproved("block_timestamp_advances_across_pin_boundary")


def _transaction_record(transaction: Any, *, index: int, block_number: int,
                        block_hash: str) -> dict[str, Any]:
    """Classify one preceding transaction, or fail closed with an exact reason."""
    if not isinstance(transaction, dict):
        raise _Unproved("preceding_transaction_unavailable")
    if not _is_hash(transaction.get("hash")):
        raise _Unproved("preceding_transaction_hash_malformed")
    if not _is_hash(transaction.get("blockHash")) or \
            transaction["blockHash"].lower() != block_hash:
        raise _Unproved("preceding_transaction_block_hash_mismatch")
    if _uint(transaction.get("blockNumber")) != block_number:
        raise _Unproved("preceding_transaction_block_number_mismatch")
    if _uint(transaction.get("transactionIndex")) != index:
        raise _Unproved("preceding_transaction_index_mismatch")
    if _uint(transaction.get("type")) != ARBOS_INTERNAL_TX_TYPE:
        raise _Unproved("preceding_transaction_is_not_arbos_internal_type")
    sender, target = _address(transaction.get("from")), _address(transaction.get("to"))
    if sender != ARBOS_INTERNAL_ADDRESS or target != ARBOS_INTERNAL_ADDRESS:
        raise _Unproved("preceding_transaction_is_not_arbos_internal_account")
    if _uint(transaction.get("gas")) != 0 or _uint(transaction.get("gasPrice")) != 0:
        raise _Unproved("preceding_transaction_reserves_gas")
    if _uint(transaction.get("value")) != 0:
        raise _Unproved("preceding_transaction_transfers_value")
    data = transaction.get("input")
    if not isinstance(data, str) or not data.startswith("0x") or len(data) < 10 or \
            data[:10].lower() != ARBOS_START_BLOCK_SELECTOR:
        raise _Unproved("preceding_transaction_is_not_arbos_start_block_call")
    return {"transaction_index": index, "transaction_hash": transaction["hash"].lower(),
            "type": ARBOS_INTERNAL_TX_TYPE, "from": sender, "to": target,
            "gas": 0, "gas_price": 0, "value": 0, "input": data.lower(),
            "selector": ARBOS_START_BLOCK_SELECTOR,
            "receipt_status": None, "receipt_gas_used": None,
            "receipt_log_count": None, "receipt_contract_address": None,
            "classification": "arbos_internal_start_block_transaction"}


def _check_receipt(receipt: Any, record: dict[str, Any], *, index: int,
                   block_number: int, block_hash: str) -> dict[str, Any]:
    """Require a zero-gas, zero-log, non-creating successful receipt at the pin."""
    if not isinstance(receipt, dict):
        raise _Unproved("preceding_receipt_unavailable")
    if not _is_hash(receipt.get("transactionHash")) or \
            receipt["transactionHash"].lower() != record["transaction_hash"]:
        raise _Unproved("preceding_receipt_transaction_hash_mismatch")
    if not _is_hash(receipt.get("blockHash")) or receipt["blockHash"].lower() != block_hash:
        raise _Unproved("preceding_receipt_block_hash_mismatch")
    if _uint(receipt.get("blockNumber")) != block_number or \
            _uint(receipt.get("transactionIndex")) != index:
        raise _Unproved("preceding_receipt_coordinate_mismatch")
    if _uint(receipt.get("status")) != 1:
        raise _Unproved("preceding_receipt_status_not_successful")
    if _uint(receipt.get("gasUsed")) != 0:
        raise _Unproved("preceding_receipt_consumed_gas")
    logs = receipt.get("logs")
    if not isinstance(logs, list) or logs:
        raise _Unproved("preceding_receipt_emitted_logs")
    if receipt.get("contractAddress") is not None:
        raise _Unproved("preceding_receipt_created_contract")
    if _address(receipt.get("to")) != ARBOS_INTERNAL_ADDRESS:
        raise _Unproved("preceding_receipt_target_is_not_arbos_internal_account")
    if _uint(receipt.get("type")) != ARBOS_INTERNAL_TX_TYPE:
        raise _Unproved("preceding_receipt_is_not_arbos_internal_type")
    record.update(receipt_status=1, receipt_gas_used=0, receipt_log_count=0,
                  receipt_contract_address=None)
    return record


def capture_system_transaction_prestate(
        rpc: Any, *, chain_id: int, block_number: int, block_hash: str,
        transaction_index: int, endpoint_url: str | None = None,
        expected_transaction_hash: str | None = None) -> dict[str, Any]:
    """Capture one deterministic pre-execution equivalence transcript.

    The capture never claims equivalence from the absence of recorded logs. It
    reads every transaction ordered before ``transaction_index`` and proves each
    is an ArbOS internal start-block transaction, or records the exact reason it
    could not.
    """
    if not isinstance(chain_id, int) or chain_id <= 0 or \
            not isinstance(block_number, int) or block_number <= 0 or \
            not isinstance(transaction_index, int) or transaction_index < 0 or \
            isinstance(transaction_index, bool) or not _is_hash(block_hash):
        raise ValueError("invalid observed execution coordinate")
    traced = PrestateRpc(rpc)
    pinned_hash = block_hash.lower()
    record: dict[str, Any] = {
        "schema": SCHEMA, "version": VERSION, "chain_id": chain_id,
        "endpoint": redact_rpc_endpoint(endpoint_url) if endpoint_url else "<unrecorded>",
        "arbos_internal_address": ARBOS_INTERNAL_ADDRESS,
        "arbos_internal_transaction_type": ARBOS_INTERNAL_TX_TYPE,
        "arbos_start_block_selector": ARBOS_START_BLOCK_SELECTOR,
        "execution_block_number": block_number,
        "execution_block_hash": pinned_hash,
        "execution_block_timestamp": None,
        "observed_transaction_index": transaction_index,
        "observed_transaction_hash": None,
        "equivalent_pin_block": block_number - 1,
        "equivalent_pin_block_hash": None,
        "equivalent_pin_block_timestamp": None,
        "arbos_l1_block_number": {"execution_block": None, "equivalent_pin_block": None},
        "preceding_transactions": [],
        "prestate_equivalent_to_block_boundary": False,
        "reason": "capture_incomplete",
        "proves": PROVES, "does_not_prove": list(DOES_NOT_PROVE),
    }
    try:
        chain = _result(traced, "eth_chainId", [], "chain_id_unavailable")
        if _uint(chain) != chain_id:
            raise _Unproved("chain_id_mismatch")
        header = _check_header(
            _result(traced, "eth_getBlockByNumber", [hex(block_number), False],
                    "block_header_unavailable_before"),
            block_number=block_number, block_hash=pinned_hash, phase="before")
        _boundary_facts(
            record, header,
            _result(traced, "eth_getBlockByNumber", [hex(block_number - 1), False],
                    "equivalent_pin_block_header_unavailable"),
            block_number=block_number)
        transactions = header.get("transactions")
        if not isinstance(transactions, list) or len(transactions) <= transaction_index:
            raise _Unproved("observed_transaction_index_out_of_range")
        observed = transactions[transaction_index]
        if not _is_hash(observed):
            raise _Unproved("observed_transaction_hash_malformed")
        record["observed_transaction_hash"] = observed.lower()
        if expected_transaction_hash is not None and \
                observed.lower() != expected_transaction_hash.lower():
            raise _Unproved("observed_transaction_hash_mismatch")
        for index in range(transaction_index):
            entry = _transaction_record(
                _result(traced, "eth_getTransactionByBlockNumberAndIndex",
                        [hex(block_number), hex(index)], "preceding_transaction_unavailable"),
                index=index, block_number=block_number, block_hash=pinned_hash)
            _check_receipt(
                _result(traced, "eth_getTransactionReceipt", [entry["transaction_hash"]],
                        "preceding_receipt_unavailable"),
                entry, index=index, block_number=block_number, block_hash=pinned_hash)
            record["preceding_transactions"].append(entry)
        _check_header(
            _result(traced, "eth_getBlockByNumber", [hex(block_number), False],
                    "block_header_unavailable_after"),
            block_number=block_number, block_hash=pinned_hash, phase="after")
        record["prestate_equivalent_to_block_boundary"] = True
        record["reason"] = ("no_preceding_transactions_in_block" if transaction_index == 0 else
                            "all_preceding_transactions_are_arbos_internal_start_block_transactions")
    except _Unproved as failure:
        record["reason"] = failure.reason
    record["rpc_transcript"] = traced.calls
    record["rpc_transcript_sha256"] = _transcript_digest(traced.calls)
    return record


def _replay(record: dict[str, Any]) -> None:
    """Rebuild the positive verdict from the raw transcript alone."""
    index_count = record["observed_transaction_index"]
    transcript = record.get("rpc_transcript")
    expected_methods = (
        ["eth_chainId", "eth_getBlockByNumber", "eth_getBlockByNumber"] +
        ["eth_getTransactionByBlockNumberAndIndex", "eth_getTransactionReceipt"] * index_count +
        ["eth_getBlockByNumber"])
    if not isinstance(transcript, list) or any(not isinstance(row, dict) for row in transcript) or \
            [row.get("method") for row in transcript] != expected_methods:
        raise ValueError("system transaction prestate transcript shape mismatch")
    if record.get("rpc_transcript_sha256") != _transcript_digest(transcript):
        raise ValueError("system transaction prestate transcript digest mismatch")
    block_number = record["execution_block_number"]
    block_hash = record["execution_block_hash"]
    block_tag = hex(block_number)
    try:
        if transcript[0].get("params") != [] or \
                _uint(transcript[0]["response"]["result"]) != record["chain_id"]:
            raise _Unproved("chain_id_mismatch")
        for position, phase in ((1, "before"), (len(transcript) - 1, "after")):
            row = transcript[position]
            if row.get("params") != [block_tag, False]:
                raise _Unproved("block_header_request_mismatch_" + phase)
            _check_header(row["response"]["result"], block_number=block_number,
                          block_hash=block_hash, phase=phase)
        if transcript[2].get("params") != [hex(block_number - 1), False]:
            raise _Unproved("equivalent_pin_block_request_mismatch")
        boundary = {"execution_block_timestamp": None, "equivalent_pin_block_hash": None,
                    "equivalent_pin_block_timestamp": None,
                    "arbos_l1_block_number": {"execution_block": None,
                                              "equivalent_pin_block": None}}
        _boundary_facts(boundary, transcript[1]["response"]["result"],
                        transcript[2]["response"]["result"], block_number=block_number)
        if any(record.get(name) != value for name, value in boundary.items()) or \
                record.get("equivalent_pin_block") != block_number - 1:
            raise _Unproved("equivalent_pin_block_record_mismatch")
        listed = transcript[1]["response"]["result"].get("transactions")
        if not isinstance(listed, list) or len(listed) <= index_count or \
                not _is_hash(listed[index_count]) or \
                listed[index_count].lower() != record.get("observed_transaction_hash"):
            raise _Unproved("observed_transaction_hash_mismatch")
        rebuilt = []
        for index in range(index_count):
            transaction_row, receipt_row = transcript[3 + index * 2], transcript[4 + index * 2]
            if transaction_row.get("params") != [block_tag, hex(index)]:
                raise _Unproved("preceding_transaction_request_mismatch")
            entry = _transaction_record(transaction_row["response"]["result"], index=index,
                                        block_number=block_number, block_hash=block_hash)
            if receipt_row.get("params") != [entry["transaction_hash"]]:
                raise _Unproved("preceding_receipt_request_mismatch")
            _check_receipt(receipt_row["response"]["result"], entry, index=index,
                           block_number=block_number, block_hash=block_hash)
            rebuilt.append(entry)
        if record.get("preceding_transactions") != rebuilt:
            raise _Unproved("preceding_transaction_record_mismatch")
        if record.get("reason") != ("no_preceding_transactions_in_block" if index_count == 0 else
                                    "all_preceding_transactions_are_arbos_internal_start_block"
                                    "_transactions"):
            raise _Unproved("verdict_reason_mismatch")
    except (KeyError, TypeError, IndexError, AttributeError) as error:
        raise ValueError("system transaction prestate malformed transcript") from error
    except _Unproved as failure:
        raise ValueError(
            "system transaction prestate rebuild failed: " + failure.reason) from failure


def verify_system_transaction_prestate(
        path: Path, *, chain_id: int, execution_block_number: int,
        execution_block_hash: str, observed_transaction_index: int,
        observed_transaction_hash: str | None = None) -> dict[str, Any]:
    """Bind a saved proof to one observed execution coordinate and re-derive it.

    A ``False`` verdict is returned unchanged so the caller can refuse with the
    proof's own exact reason. A ``True`` verdict is only returned after the raw
    transcript reproduces it; anything else raises.
    """
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("schema") != SCHEMA or \
            record.get("version") != VERSION or record.get("chain_id") != chain_id or \
            record.get("execution_block_number") != execution_block_number or \
            not _is_hash(record.get("execution_block_hash")) or \
            record["execution_block_hash"].lower() != execution_block_hash.lower() or \
            record.get("observed_transaction_index") != observed_transaction_index or \
            record.get("equivalent_pin_block") != execution_block_number - 1 or \
            record.get("arbos_internal_address") != ARBOS_INTERNAL_ADDRESS or \
            record.get("arbos_internal_transaction_type") != ARBOS_INTERNAL_TX_TYPE or \
            record.get("arbos_start_block_selector") != ARBOS_START_BLOCK_SELECTOR or \
            record.get("proves") != PROVES or \
            record.get("does_not_prove") != list(DOES_NOT_PROVE):
        raise ValueError("system transaction prestate identity mismatch")
    if observed_transaction_hash is not None and (
            not _is_hash(record.get("observed_transaction_hash")) or
            record["observed_transaction_hash"].lower() != observed_transaction_hash.lower()):
        raise ValueError("system transaction prestate observed transaction mismatch")
    verdict = record.get("prestate_equivalent_to_block_boundary")
    if not isinstance(record.get("reason"), str) or not record["reason"] or \
            not isinstance(verdict, bool):
        raise ValueError("system transaction prestate verdict malformed")
    if verdict is False:
        return record
    _replay(record)
    return record
