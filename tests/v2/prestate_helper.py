"""Shared read-only mock RPC fixtures for the observed-execution prestate proof."""

from gmx_crypto_bot_v2.crosscheck.block_prestate import (
    ARBOS_INTERNAL_ADDRESS, capture_system_transaction_prestate)


BLOCK = 507206358
BLOCK_HASH = "0xddd659c204cc34e968cb82fd78338dbc8a9716e0cba8100d416bc4fd6b4016bf"
# hash(507206357) == parentHash(507206358), both read from the chain.
BOUNDARY_HASH = "0x6624873b47717258e06e038aa89bf86c2bd9b34542b5b3f72b5b04badc8b4af5"
TIMESTAMP = "0x6ab034b5"
EXECUTION_TX = "0xd7c47b718151b96cc6a25eec97f5f913208d86aa8774c17be4271fb71a27a212"
SYSTEM_TX = "0xce4cf32d950515650b42bdd934eb78c77e34a6bb0dd02f31e4b035a1f7df9ccf"
GMX_ORDER_HANDLER = "0xa5d2d45228ee2e3a18ab122b2ce84997d008f4eb"


def system_transaction(**overrides):
    record = {"blockHash": BLOCK_HASH, "blockNumber": hex(BLOCK),
              "from": ARBOS_INTERNAL_ADDRESS, "to": ARBOS_INTERNAL_ADDRESS,
              "gas": "0x0", "gasPrice": "0x0", "hash": SYSTEM_TX,
              "input": "0x6bf6a42d" + "00" * 128, "nonce": "0x0",
              "transactionIndex": "0x0", "value": "0x0", "type": "0x6a"}
    record.update(overrides)
    return record


def system_receipt(**overrides):
    record = {"blockHash": BLOCK_HASH, "blockNumber": hex(BLOCK), "contractAddress": None,
              "from": ARBOS_INTERNAL_ADDRESS, "to": ARBOS_INTERNAL_ADDRESS,
              "gasUsed": "0x0", "logs": [], "status": "0x1",
              "transactionHash": SYSTEM_TX, "transactionIndex": "0x0", "type": "0x6a"}
    record.update(overrides)
    return record


class MockRpc:
    """Serve canned read-only responses for one pinned Arbitrum block."""

    MISSING = object()

    def __init__(self, *, chain_id="0xa4b1", transactions=None, receipts=None,
                 header=None, header_after=None, boundary=MISSING, errors=()):
        self.chain_id = chain_id
        self.header = header if header is not None else {
            "number": hex(BLOCK), "hash": BLOCK_HASH, "parentHash": BOUNDARY_HASH,
            "timestamp": TIMESTAMP, "l1BlockNumber": "0x18d0be6",
            "transactions": [SYSTEM_TX, EXECUTION_TX]}
        self.boundary = {
            "number": hex(BLOCK - 1), "hash": BOUNDARY_HASH,
            "parentHash": "0x" + "7" * 64, "timestamp": TIMESTAMP,
            "l1BlockNumber": "0x18d0be4", "transactions": []
        } if boundary is MockRpc.MISSING else boundary
        self.header_after = self.header if header_after is None else header_after
        self.transactions = {0: system_transaction()} if transactions is None else transactions
        self.receipts = {SYSTEM_TX: system_receipt()} if receipts is None else receipts
        self.errors = set(errors)
        self.header_calls = 0
        self.requests = []

    def request(self, method, params):
        self.requests.append((method, params))
        if method in self.errors:
            return {"error": {"message": "https://secret@provider/key is unavailable"}}
        if method == "eth_chainId":
            return {"result": self.chain_id}
        if method == "eth_getBlockByNumber":
            if int(params[0], 16) == BLOCK - 1:
                return {"result": self.boundary}
            self.header_calls += 1
            return {"result": self.header if self.header_calls == 1 else self.header_after}
        if method == "eth_getTransactionByBlockNumberAndIndex":
            return {"result": self.transactions.get(int(params[1], 16))}
        if method == "eth_getTransactionReceipt":
            return {"result": self.receipts.get(params[0])}
        raise AssertionError("unexpected method " + method)


def capture(rpc, *, index=1, chain_id=42161, block_hash=BLOCK_HASH,
            expected_transaction_hash=EXECUTION_TX):
    return capture_system_transaction_prestate(
        rpc, chain_id=chain_id, block_number=BLOCK, block_hash=block_hash,
        transaction_index=index, endpoint_url="https://arb1.arbitrum.io/rpc",
        expected_transaction_hash=expected_transaction_hash)
