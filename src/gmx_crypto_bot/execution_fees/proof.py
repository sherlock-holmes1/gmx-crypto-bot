"""Small, independently testable execution-fee arithmetic and transfer checks."""
from dataclasses import dataclass
from typing import Any
from gmx_crypto_bot.price_impact import config_base_key, keccak256
from gmx_crypto_bot.execution_fees.models import PaymentInput, TraceFrame, walk_trace

PRECISION = 10**30
GET_UINT_SELECTOR = '0x' + keccak256(b'getUint(bytes32)')[:4].hex()
TRANSFER_SELECTOR = '0xa9059cbb'
SETTING_NAMES = (
    'EXECUTION_GAS_FEE_BASE_AMOUNT_V2_1',
    'EXECUTION_GAS_FEE_PER_ORACLE_PRICE',
    'EXECUTION_GAS_FEE_MULTIPLIER_FACTOR',
)


@dataclass(frozen=True)
class GasSettings:
    base_amount: int
    per_oracle_price: int
    multiplier_factor: int

    def keeper_payment(self, measured_gas: int, oracle_count: int, gas_price: int, available_fee: int) -> int:
        if min(measured_gas, oracle_count, gas_price, available_fee,
               self.base_amount, self.per_oracle_price, self.multiplier_factor) < 0:
            raise ValueError('negative gas fee input')
        adjusted = self.base_amount + self.per_oracle_price * oracle_count
        adjusted += measured_gas * self.multiplier_factor // PRECISION
        return min(adjusted * gas_price, available_fee)


def settings_from_calls(payment_frame: TraceFrame, payment: PaymentInput) -> GasSettings:
    """Read configuration from successful DataStore calls in the payment trace.

No fee event or inferred gas amount is used to reconstruct these settings.
Ambiguous or missing calls prevent a proof.
"""
    if not payment_frame.committed:
        raise ValueError('payment call reverted')
    wanted = {GET_UINT_SELECTOR + config_base_key(name)[2:]: name for name in SETTING_NAMES}
    observed: dict[str, list[int]] = {name: [] for name in SETTING_NAMES}
    for node in walk_trace(payment_frame.frame):
        frame = node.frame
        name = wanted.get(frame.get('input'))
        if (not node.committed or name is None or frame.get('type') != 'STATICCALL'
                or frame.get('to', '').lower() != payment.data_store):
            continue
        output = frame.get('output', '')
        if not output.startswith('0x') or len(output) != 66:
            raise ValueError('invalid DataStore gas-setting return value')
        observed[name].append(int(output, 16))
    if any(len(values) != 1 for values in observed.values()):
        raise ValueError('missing or ambiguous historical gas settings')
    return GasSettings(*(observed[name][0] for name in SETTING_NAMES))


def transfer_candidates(payment_frame: TraceFrame, sender: str, receiver: str,
                        amount: int, wrapped_native: str) -> list[dict[str, Any]]:
    """Find committed CALL transfers within one payment, excluding delegate value.

Returned paths preserve call identity for later per-order association. This is
transfer evidence only: it does not independently prove the gas-based amount.
"""
    if amount <= 0 or not payment_frame.committed:
        return []
    matches = []
    for node in walk_trace(payment_frame.frame, payment_frame.path):
        frame = node.frame
        if not node.committed or frame.get('type') != 'CALL' or frame.get('from', '').lower() != sender:
            continue
        destination = frame.get('to', '').lower()
        if destination == receiver and int(frame.get('value', '0x0'), 16) == amount:
            matches.append({'path': list(node.path), 'kind': 'native', 'amount': amount})
        calldata = frame.get('input', '')
        if destination != wrapped_native or not calldata.startswith(TRANSFER_SELECTOR) or len(calldata) != 138:
            continue
        recipient_word, amount_word = calldata[10:74], calldata[74:138]
        if int(recipient_word[:24], 16) or '0x' + recipient_word[-40:].lower() != receiver:
            continue
        if int(amount_word, 16) != amount or frame.get('output') != '0x' + format(1, '064x'):
            continue
        matches.append({'path': list(node.path), 'kind': 'wrapped_native', 'amount': amount})
    return matches
