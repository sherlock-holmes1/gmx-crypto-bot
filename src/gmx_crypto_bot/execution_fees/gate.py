"""Join fresh observed-order results to offline execution-fee trace proofs."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any

from gmx_crypto_bot.execution_fees.validate import verify_trace

CHECK = 'execution_fee_gas_and_transfer_proof'


def apply_execution_fee_proof(recording: Path, orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Reverify evidence against this run's orders, never a cached PASS report.

Missing traces or gas evidence leave the gate unavailable. Corrupt traces,
wrong identities, duplicate payments, and failed proofs mark the affected
transaction's orders as mismatches. Extra proven calls retain their separate
order-record reconciliation status.
"""
    by_key = {order['key']: order for order in orders}
    by_transaction: dict[str, list[dict]] = defaultdict(list)
    results: list[dict] = []

    def mark(order: dict, status: str, reason: str | None = None) -> None:
        order['checks'][CHECK] = status
        if reason:
            order['execution_fee_proof_reason'] = reason
        if status == 'mismatch':
            error = CHECK + '_mismatch'
            if error not in order['mismatches']:
                order['mismatches'].append(error)
            order['status'] = 'mismatch'

    for order in orders:
        terminal = order.get('terminal')
        if terminal is None:
            continue
        balance = order['checks'].get('execution_fee_event_balance')
        if (balance == 'not_applicable' and order['final_request'].get('executionFee') == 0
                and not order.get('observed_execution_fee_events')
                and not order.get('observed_order_update_topups')):
            mark(order, 'not_applicable')
        elif balance != 'matched':
            mark(order, 'unavailable', 'execution-fee request/event balance is not verified')
        else:
            mark(order, 'unavailable')
            by_transaction[terminal['transaction_hash']].append(order)

    for transaction_hash, transaction_orders in sorted(by_transaction.items()):
        path = recording / 'execution-fee-traces' / (transaction_hash + '.json')
        if not path.exists():
            for order in transaction_orders:
                mark(order, 'unavailable', 'missing execution-fee trace: ' + path.name)
            continue
        try:
            captured = json.loads(path.read_text())
            for order in transaction_orders:
                terminal = order['terminal']
                if (captured['transaction_hash'] != transaction_hash
                        or captured['block_number'] != terminal['block_number']
                        or not terminal.get('block_hash')
                        or captured['block_hash'] != terminal['block_hash']):
                    raise ValueError('execution-fee trace differs from the current terminal block/transaction')
            gas_path = path.with_name(path.stem + '.gas-v2.json')
            verified = verify_trace(path, gas_path if gas_path.exists() else None, by_key,
                                    trace_record=captured)
            payments: dict[str, list[dict]] = defaultdict(list)
            for payment in verified['payments']:
                payments[payment['order_key']].append(payment)
            if any(len(items) != 1 for items in payments.values()):
                raise ValueError('duplicate execution-fee payment for one order')
            for order in transaction_orders:
                if len(payments.get(order['key'], [])) != 1:
                    raise ValueError('fee-paying terminal order has no unique payment in its trace')
            results.append(verified)
            for order in transaction_orders:
                payment = payments[order['key']][0]
                if payment['gas_fee_match'] == 'matched' and payment['order_validation'] == 'matched':
                    mark(order, 'matched')
                else:
                    mark(order, 'unavailable', 'execution-fee gas proof is incomplete')
        except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
            for order in transaction_orders:
                mark(order, 'mismatch', str(error))

    counts = Counter(order['checks'][CHECK] for order in orders if CHECK in order['checks'])
    payments = [payment for result in results for payment in result['payments']]
    return {
        'complete': bool(counts) and set(counts) <= {'matched', 'not_applicable'},
        'counts': dict(sorted(counts.items())),
        'transactions_verified': len(results),
        'fee_paying_calls': sum(payment['execution_fee'] > 0 for payment in payments),
        'unindexed_payment_calls': sum(payment['order_validation'] == 'not_in_order_validation'
                                       for payment in payments),
        'transactions': results,
    }
