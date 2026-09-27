"""Typed execution-fee call inputs and committed trace traversal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from gmx_crypto_bot_v2.domain.keys import keccak256

PAY_SIGNATURE = "payExecutionFee(GasUtils.PayExecutionFeeContracts,bytes32,address,uint256,uint256,uint256,address,address,uint256)"


PAY_SELECTOR = "0x" + keccak256(PAY_SIGNATURE.encode())[:4].hex()


@dataclass(frozen=True)
class PaymentInput:
    data_store: str
    event_emitter: str
    multichain_vault: str
    bank: str
    order_key: str
    callback: str
    execution_fee: int
    starting_gas: int
    oracle_price_count: int
    keeper: str
    refund_receiver: str
    source_chain_id: int

    @classmethod
    def decode(cls, calldata: str) -> "PaymentInput":
        if not calldata.startswith(PAY_SELECTOR) or len(calldata) != 10 + 12 * 64:
            raise ValueError("invalid payExecutionFee calldata")
        words = [calldata[10 + i * 64 : 10 + (i + 1) * 64] for i in range(12)]

        def address(index: int) -> str:
            if int(words[index][:24], 16):
                raise ValueError("non-canonical payment address")
            return "0x" + words[index][-40:].lower()

        return cls(
            address(0),
            address(1),
            address(2),
            address(3),
            "0x" + words[4],
            address(5),
            int(words[6], 16),
            int(words[7], 16),
            int(words[8], 16),
            address(9),
            address(10),
            int(words[11], 16),
        )


@dataclass(frozen=True)
class TraceFrame:
    path: tuple[int, ...]
    frame: dict[str, Any]
    committed: bool


def walk_trace(
    frame: dict[str, Any], path: tuple[int, ...] = (), committed: bool = True
) -> Iterator[TraceFrame]:
    """A successful child of a reverted ancestor is not a committed transfer."""
    committed = committed and not bool(frame.get("error"))
    yield TraceFrame(path, frame, committed)
    for index, child in enumerate(frame.get("calls", [])):
        yield from walk_trace(child, path + (index,), committed)
