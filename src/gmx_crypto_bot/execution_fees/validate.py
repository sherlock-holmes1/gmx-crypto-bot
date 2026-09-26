"""Offline checks for captured GMX execution-fee traces."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from gmx_crypto_bot.event_decoder import decode_event_log, event_name_from_data
from gmx_crypto_bot.execution_fees.models import PaymentInput, TraceFrame, walk_trace
from gmx_crypto_bot.execution_fees.proof import GasSettings, settings_from_calls, transfer_candidates
from gmx_crypto_bot.price_impact import keccak256

TRANSFER_TOPIC = '0x' + keccak256(b'Transfer(address,address,uint256)').hex()
WNT_ARBITRUM = '0x82af49447d8a07e3bd95bd0d56f35241523fbab1'
CALIBRATED_GAS_PROFILE = {
    'runtime_sha256': 'a7d82fadf9efbb24f80ca2348fd58efbf132de540b0f203716047c51b84f2130',
    'calldata_bytes': 388,
    'first_gas_offset': 1703,
    'second_gas_offset': 1845,
}


def _word_address(topic: str) -> str:
    return '0x' + topic[-40:].lower()


def _opcode_positions(bytecode: str, opcode: int) -> set[int]:
    code = bytes.fromhex(bytecode.removeprefix('0x'))
    positions: set[int] = set()
    pc = 0
    while pc < len(code):
        current = code[pc]
        if current == opcode:
            positions.add(pc)
        pc += 1 + (current - 0x5f if 0x60 <= current <= 0x7f else 0)
    return positions


def _receipt_transfers(receipt: dict[str, Any], token: str) -> list[dict[str, Any]]:
    matches = []
    for log in receipt.get('logs', []):
        topics = log.get('topics', [])
        if log.get('address', '').lower() != token or not topics or topics[0].lower() != TRANSFER_TOPIC or len(topics) != 3:
            continue
        matches.append({'from': _word_address(topics[1]), 'to': _word_address(topics[2]),
                        'amount': int(log['data'], 16), 'log_index': int(log['logIndex'], 16)})
    return matches


def _events(receipt: dict[str, Any], emitter: str) -> list[dict[str, Any]]:
    result = []
    for log in receipt.get('logs', []):
        if log.get('address', '').lower() != emitter:
            continue
        name = event_name_from_data(log.get('data', ''))
        if name is None:
            continue
        event = decode_event_log(log['data'])
        result.append({'name': event.event_name, 'values': event.values,
                       'log_index': int(log['logIndex'], 16)})
    return result


def _fee_events(order: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    events = order.get('observed_execution_fee_events', [])
    keeper = [event for event in events if event['event_name'] == 'KeeperExecutionFee']
    refund = [event for event in events if event['event_name'] in ('ExecutionFeeRefund', 'ExecutionFeeRefundCallback')]
    if len(keeper) != 1 or len(refund) > 1:
        raise ValueError('missing or ambiguous execution-fee events')
    return keeper[0], refund[0] if refund else None


def _trace_fee_events(payment_frame: TraceFrame, payment: PaymentInput,
                      receipt: dict[str, Any], used_log_indexes: set[int]
                      ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Bind fee events to an otherwise-unindexed payment through its traced logs."""
    found: list[dict[str, Any]] = []

    def visit(frame: dict[str, Any]) -> None:
        for log in frame.get('logs', []):
            if log.get('address', '').lower() != payment.event_emitter:
                continue
            name = event_name_from_data(log.get('data', ''))
            if name not in ('KeeperExecutionFee', 'ExecutionFeeRefund', 'ExecutionFeeRefundCallback'):
                continue
            matches = [item for item in receipt.get('logs', [])
                       if item.get('address', '').lower() == payment.event_emitter
                       and int(item['logIndex'], 16) not in used_log_indexes
                       and [topic.lower() for topic in item.get('topics', [])]
                       == [topic.lower() for topic in log.get('topics', [])]
                       and item.get('data', '').lower() == log.get('data', '').lower()]
            if not matches:
                raise ValueError('traced fee event has no unused receipt log')
            matches.sort(key=lambda item: int(item['logIndex'], 16))
            log_index = int(matches[0]['logIndex'], 16)
            used_log_indexes.add(log_index)
            decoded = decode_event_log(log['data'])
            found.append({'event_name': decoded.event_name, 'values': decoded.values,
                          'transaction_hash': receipt['transactionHash'],
                          'log_index': log_index})
        for child in frame.get('calls', []):
            visit(child)

    visit(payment_frame.frame)
    keepers = [event for event in found if event['event_name'] == 'KeeperExecutionFee']
    refunds = [event for event in found if event['event_name'] in ('ExecutionFeeRefund', 'ExecutionFeeRefundCallback')]
    if len(keepers) != 1 or len(refunds) > 1:
        raise ValueError('payment trace has missing or ambiguous fee events')
    return keepers[0], refunds[0] if refunds else None


def _transfer_proof(payment_frame: TraceFrame, payment: PaymentInput,
                    keeper_event: dict[str, Any], refund_event: dict[str, Any] | None,
                    receipt: dict[str, Any]) -> dict[str, Any]:
    keeper_values = keeper_event['values']
    keeper_amount = keeper_values['executionFeeAmount']
    keeper = keeper_values['keeper'].lower()
    keeper_calls = transfer_candidates(payment_frame, payment.bank, keeper, keeper_amount, WNT_ARBITRUM)
    if len(keeper_calls) != 1 or keeper_calls[0]['kind'] != 'native':
        raise ValueError('keeper event has no unique committed native transfer')

    result = {'keeper': keeper_calls[0], 'refund': None, 'multichain_credit': None}
    if refund_event is None:
        if keeper_amount != payment.execution_fee:
            raise ValueError('keeper fee does not consume the full execution-fee deposit')
        return result

    values = refund_event['values']
    refund_amount = values.get('refundFeeAmount')
    if refund_amount is None or keeper_amount + refund_amount != payment.execution_fee:
        raise ValueError('keeper and refund events do not balance to the execution-fee deposit')
    if refund_event['event_name'] == 'ExecutionFeeRefundCallback':
        raise ValueError('callback refund route requires a separate callback proof')
    if values.get('receiver', '').lower() != payment.refund_receiver:
        raise ValueError('refund event receiver differs from the payment call input')

    if payment.source_chain_id:
        destination = payment.multichain_vault
        kind = 'wrapped_native'
    else:
        destination = values['receiver'].lower()
        kind = None
    refund_calls = transfer_candidates(payment_frame, payment_frame.frame['from'].lower(),
                                       destination, refund_amount, WNT_ARBITRUM)
    if kind:
        refund_calls = [call for call in refund_calls if call['kind'] == kind]
    if len(refund_calls) != 1:
        raise ValueError('refund event has no unique committed transfer on its expected route')

    refund_call = refund_calls[0]
    result['refund'] = refund_call
    if refund_call['kind'] == 'wrapped_native':
        token_transfers = _receipt_transfers(receipt, WNT_ARBITRUM)
        corroborated = [transfer for transfer in token_transfers
                        if transfer['amount'] == refund_amount and transfer['to'] == destination]
        if len(corroborated) != 1:
            raise ValueError('wrapped-native call is not corroborated by one receipt Transfer log')
        result['refund']['receipt_transfer_log_index'] = corroborated[0]['log_index']

    if payment.source_chain_id:
        credits = [event for event in _events(receipt, payment.event_emitter)
                   if event['name'] == 'MultichainTransferIn'
                   and event['values'].get('token', '').lower() == WNT_ARBITRUM
                   and event['values'].get('account', '').lower() == payment.refund_receiver
                   and event['values'].get('amount') == refund_amount
                   and event['values'].get('srcChainId') == 0
                   and keeper_event['log_index'] < event['log_index'] < refund_event['log_index']]
        if len(credits) != 1:
            raise ValueError('multichain refund lacks its exact receipt credit event')
        result['multichain_credit'] = credits[0]
    return result


def verify_trace(trace_path: Path, gas_path: Path | None, orders: dict[str, dict[str, Any]]) -> dict[str, Any]:
    trace_record = json.loads(trace_path.read_text())
    receipt = trace_record['receipt']
    if trace_record['transaction_hash'] != receipt['transactionHash']:
        raise ValueError('trace and receipt transaction hashes differ')
    if trace_record['block_hash'] != receipt['blockHash'] or trace_record['block_number'] != int(receipt['blockNumber'], 16):
        raise ValueError('trace and receipt block identity differs')
    trace_nodes = {node.path: node for node in walk_trace(trace_record['trace'])}
    gas_probe = json.loads(gas_path.read_text()) if gas_path and gas_path.exists() else None
    gas_samples = gas_probe['samples'] if gas_probe else []
    if gas_probe and (gas_probe.get('schema') != 'GmxExecutionFeeGasProbe' or gas_probe.get('version') != 2):
        raise ValueError('unsupported gas probe schema version')
    if gas_probe and gas_probe.get('transaction_hash') != trace_record['transaction_hash']:
        raise ValueError('gas probe transaction hash differs')
    paid_items = [item for item in trace_record['payments'] if int(item['input']['execution_fee']) > 0]
    if gas_probe and len(gas_samples) != len(paid_items) * 2:
        raise ValueError('gas probe reading count differs from payment call count')

    payment_results = []
    used_receipt_fee_logs = {
        event['log_index'] for order in orders.values()
        for event in order.get('observed_execution_fee_events', [])
        if event.get('transaction_hash') == trace_record['transaction_hash']
    }
    gas_sample_index = 0
    for item in trace_record['payments']:
        frame = trace_nodes.get(tuple(item['path']))
        if frame is None or not frame.committed:
            raise ValueError('payment call is absent or uncommitted')
        payment = PaymentInput(**item['input'])
        order = orders.get(payment.order_key)
        if order is not None:
            expected_fee = order['final_request'].get('executionFee', 0) + sum(
                topup['amount'] for topup in order.get('observed_order_update_topups', []))
            if payment.execution_fee != expected_fee:
                raise ValueError('payment call fee differs from the validated request and top-ups')
        if payment.execution_fee == 0:
            if order is None or order['final_request'].get('executionFee') != 0:
                raise ValueError('zero-fee payment call has no matching zero-fee order request')
            for child in walk_trace(frame.frame):
                if any(log.get('address', '').lower() == payment.event_emitter
                       and event_name_from_data(log.get('data', '')) in (
                           'KeeperExecutionFee', 'ExecutionFeeRefund', 'ExecutionFeeRefundCallback')
                       for log in child.frame.get('logs', [])):
                    raise ValueError('zero-fee payment unexpectedly emitted a fee event')
            payment_results.append({
                'order_key': payment.order_key, 'execution_fee': 0,
                'order_validation': 'matched', 'gas_fee_match': 'zero_fee_not_applicable',
                'gas_settings': None,
                'transfer_proof': {'keeper': None, 'refund': None, 'multichain_credit': None},
            })
            continue
        settings = settings_from_calls(frame, payment)
        keeper_event, refund_event = (_fee_events(order) if order is not None
                                      else _trace_fee_events(frame, payment, receipt, used_receipt_fee_logs))
        if keeper_event['transaction_hash'] != trace_record['transaction_hash']:
            raise ValueError('keeper event belongs to a different transaction')
        if refund_event and refund_event['transaction_hash'] != trace_record['transaction_hash']:
            raise ValueError('refund event belongs to a different transaction')
        transfers = _transfer_proof(frame, payment, keeper_event, refund_event, receipt)
        item_result: dict[str, Any] = {
            'order_key': payment.order_key,
            'execution_fee': payment.execution_fee,
            'order_validation': 'matched' if order is not None else 'not_in_order_validation',
            'oracle_price_count': payment.oracle_price_count,
            'gas_settings': {'base_amount': settings.base_amount,
                             'per_oracle_price': settings.per_oracle_price,
                             'multiplier_factor': settings.multiplier_factor},
            'transfer_proof': transfers,
            'gas_fee_match': 'pending_gas_probe',
        }
        runtime_code = bytes.fromhex(trace_record['library_code'][item['library']].removeprefix('0x'))
        runtime_hash = hashlib.sha256(runtime_code).hexdigest()
        frame_input = frame.frame.get('input', '')
        if gas_probe:
            pair = gas_samples[gas_sample_index:gas_sample_index + 2]
            gas_sample_index += 2
            valid_pcs = _opcode_positions(trace_record['library_code'][item['library']], 0x5a)
            if any(int(sample['pc']) not in valid_pcs for sample in pair):
                raise ValueError('gas probe program counter is not GAS in recorded library bytecode')
            if [int(sample['pc']) for sample in pair] != [0xEBA, 0xED1]:
                raise ValueError('gas probe does not identify GMX payExecutionFee gas reads')
            if any(int(sample.get('depth', -1)) != len(item['path']) + 1 for sample in pair):
                raise ValueError('gas probe call depth differs from payment frame')
            first_gas, second_gas = (int(sample['gas']) - int(sample['gas_cost']) for sample in pair)
            if runtime_hash == CALIBRATED_GAS_PROFILE['runtime_sha256']:
                entry_gas = int(frame.frame['gas'], 16)
                if (len(frame_input.removeprefix('0x')) // 2 != CALIBRATED_GAS_PROFILE['calldata_bytes']
                        or entry_gas - first_gas != CALIBRATED_GAS_PROFILE['first_gas_offset']
                        or entry_gas - second_gas != CALIBRATED_GAS_PROFILE['second_gas_offset']):
                    raise ValueError('opcode probe differs from the calibrated payment prologue profile')
            measured_gas = payment.starting_gas - first_gas // 63 - second_gas
            if measured_gas < 0:
                raise ValueError('derived internal gas is negative')
            gas_price = int(receipt.get('effectiveGasPrice', trace_record['transaction']['gasPrice']), 16)
            settings_quote = settings.keeper_payment(measured_gas, payment.oracle_price_count,
                                                     gas_price, payment.execution_fee)
            observed_keeper_fee = keeper_event['values']['executionFeeAmount']
            item_result.update({
                'gas_probe': {'first_gasleft': first_gas, 'second_gasleft': second_gas,
                              'opcode_gas_costs': [int(sample['gas_cost']) for sample in pair],
                              'measured_gas': measured_gas,
                              'gas_program_counters': [sample['pc'] for sample in pair]},
                'gas_price': gas_price,
                'modeled_keeper_fee': settings_quote,
                'observed_keeper_fee': observed_keeper_fee,
                'gas_fee_match': 'matched' if settings_quote == observed_keeper_fee else 'mismatch',
            })
            if settings_quote != observed_keeper_fee:
                raise ValueError('independently reconstructed keeper execution fee differs')
        elif (runtime_hash == CALIBRATED_GAS_PROFILE['runtime_sha256']
              and len(frame_input.removeprefix('0x')) // 2 == CALIBRATED_GAS_PROFILE['calldata_bytes']
              and payment.execution_fee > 0):
            entry_gas = int(frame.frame['gas'], 16)
            first_gas = entry_gas - CALIBRATED_GAS_PROFILE['first_gas_offset']
            second_gas = entry_gas - CALIBRATED_GAS_PROFILE['second_gas_offset']
            measured_gas = payment.starting_gas - first_gas // 63 - second_gas
            if measured_gas < 0:
                raise ValueError('derived internal gas is negative')
            gas_price = int(receipt.get('effectiveGasPrice', trace_record['transaction']['gasPrice']), 16)
            settings_quote = settings.keeper_payment(measured_gas, payment.oracle_price_count,
                                                     gas_price, payment.execution_fee)
            observed_keeper_fee = keeper_event['values']['executionFeeAmount']
            item_result.update({
                'gas_probe': {'first_gasleft': first_gas, 'second_gasleft': second_gas,
                              'measured_gas': measured_gas,
                              'source': 'calibrated_frame_offsets',
                              'runtime_sha256': runtime_hash,
                              'gas_program_counters': [0xEBA, 0xED1]},
                'gas_price': gas_price,
                'modeled_keeper_fee': settings_quote,
                'observed_keeper_fee': observed_keeper_fee,
                'gas_fee_match': 'matched' if settings_quote == observed_keeper_fee else 'mismatch',
            })
            if settings_quote != observed_keeper_fee:
                raise ValueError('calibrated GMX gas profile does not reproduce keeper execution fee')
        payment_results.append(item_result)

    return {'transaction_hash': trace_record['transaction_hash'],
            'block_number': trace_record['block_number'], 'payments': payment_results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recording', type=Path)
    args = parser.parse_args()
    validation = json.loads((args.recording / 'order-validation.json').read_text())
    orders = {order['key']: order for order in validation['orders']}
    output = args.recording / 'execution-fee-traces'
    traces = sorted(path for path in output.glob('0x*.json')
                    if not path.name.endswith(('.gas.json', '.gas-v2.json')))
    results = []
    for trace_path in traces:
        gas_path = trace_path.with_name(trace_path.stem + '.gas-v2.json')
        results.append(verify_trace(trace_path, gas_path if gas_path.exists() else None, orders))
    all_payments = [payment for transaction in results for payment in transaction['payments']]
    report = {
        'schema': 'GmxExecutionFeeTraceValidation', 'version': 1,
        'transactions': results,
        'summary': {
            'transactions': len(results), 'payments': len(all_payments),
            'historical_gas_settings_matched': sum(p['gas_settings'] is not None for p in all_payments),
            'order_requests_matched': sum(p.get('order_validation') == 'matched'
                                          and p['execution_fee'] > 0 for p in all_payments),
            'fee_paying_calls': sum(p['execution_fee'] > 0 for p in all_payments),
            'unmatched_order_keys': sum(p.get('order_validation') == 'not_in_order_validation' for p in all_payments),
            'zero_fee_calls': sum(p['gas_fee_match'] == 'zero_fee_not_applicable' for p in all_payments),
            'keeper_transfers_proven': sum(bool(p['transfer_proof']['keeper']) for p in all_payments),
            'refund_transfers_proven': sum(p['transfer_proof']['refund'] is not None for p in all_payments),
            'multichain_refund_credits_proven': sum(p['transfer_proof']['multichain_credit'] is not None for p in all_payments),
            'gas_fee_matches': sum(p['gas_fee_match'] == 'matched' for p in all_payments),
            'gas_fee_pending': sum(p['gas_fee_match'] == 'pending_gas_probe' for p in all_payments),
        },
    }
    report_path = output / 'verification.json'
    temporary = report_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')
    temporary.replace(report_path)
    print(f"Execution-fee verification saved: {report_path} ({len(results)} transactions, {len(all_payments)} payments).")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
