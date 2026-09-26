"""Collect resumable, block-bound call traces for execution-fee proofs."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4
from typing import Any
from gmx_crypto_bot.artifacts import RawArtifactStore
from gmx_crypto_bot.collector import PublicJsonRpc
from gmx_crypto_bot.execution_fees.models import PAY_SELECTOR, PaymentInput, walk_trace

PAYMENT_GAS_PROGRAM_COUNTERS = (0xEBA, 0xED1)


def select_transactions(report: dict[str, Any], limit: int) -> list[str]:
    orders = [order for order in report['orders']
              if order['checks'].get('execution_fee_event_balance') == 'matched']
    selected: list[str] = []
    # First cover common and exceptional paths, then complete canonical coverage.
    predicates = [
        lambda o: o['terminal']['event_name'] == 'OrderExecuted' and o['final_request'].get('orderType') == 2,
        lambda o: o['terminal']['event_name'] == 'OrderExecuted' and o['final_request'].get('orderType') == 4,
        lambda o: o['terminal']['values'].get('reason') == 'USER_INITIATED_CANCEL',
        lambda o: o['terminal']['values'].get('reason') == 'AUTO_CANCEL',
        lambda o: bool(o['final_request'].get('srcChainId')),
        lambda o: o['final_request'].get('callbackContract', '0x' + '0' * 40) != '0x' + '0' * 40,
    ]
    for predicate in predicates:
        match = next((o for o in orders if predicate(o) and o['terminal']['transaction_hash'] not in selected), None)
        if match:
            selected.append(match['terminal']['transaction_hash'])
    for order in orders:
        transaction = order['terminal']['transaction_hash']
        if transaction not in selected:
            selected.append(transaction)
    return selected[:limit] if limit else selected


def recorded_hashes(recording: Path, transactions: set[str]) -> dict[str, tuple[int, str]]:
    hashes: dict[str, tuple[int, str]] = {}
    with (recording / 'events.jsonl').open() as stream:
        for line in stream:
            event = json.loads(line)
            if event['kind'] != 'gmx_market_log':
                continue
            log = event['payload']['log']
            transaction = log['transactionHash'].lower()
            if transaction not in transactions:
                continue
            point = (int(log['blockNumber'], 16), log['blockHash'])
            if transaction in hashes and hashes[transaction] != point:
                raise ValueError('conflicting recorded transaction block')
            hashes[transaction] = point
    if set(hashes) != transactions:
        raise ValueError('missing recorded transaction block hashes')
    return hashes


def collect_transaction(rpc: PublicJsonRpc, transaction_hash: str, block: int, block_hash: str) -> dict[str, Any]:
    receipt = rpc.call('eth_getTransactionReceipt', [transaction_hash])
    transaction = rpc.call('eth_getTransactionByHash', [transaction_hash])
    for item in (receipt, transaction):
        identity = item.get('transactionHash', item.get('hash'))
        if identity != transaction_hash or item.get('blockHash') != block_hash or int(item['blockNumber'], 16) != block:
            raise ValueError('transaction or receipt identity mismatch')
    if receipt.get('status') != '0x1':
        raise ValueError('terminal transaction reverted')
    trace = rpc.call('debug_traceTransaction', [transaction_hash, {
        'tracer': 'callTracer', 'tracerConfig': {'onlyTopCall': False, 'withLog': True}, 'timeout': '60s',
    }])
    if (trace.get('error') or trace.get('from', '').lower() != transaction['from'].lower()
            or trace.get('to', '').lower() != (transaction.get('to') or '').lower()
            or trace.get('input') != transaction['input']):
        raise ValueError('call trace root disagrees with transaction')
    payments = []
    code = {}
    for node in walk_trace(trace):
        calldata = node.frame.get('input', '')
        if not node.committed or not calldata.startswith(PAY_SELECTOR):
            continue
        payment = PaymentInput.decode(calldata)
        address = node.frame['to'].lower()
        if address not in code:
            code[address] = rpc.call('eth_getCode', [address, hex(block)])
            if code[address] == '0x':
                raise ValueError('missing payment library code')
        payments.append({'path': list(node.path), 'input': asdict(payment), 'library': address})
    if not payments:
        raise ValueError('no committed execution-fee calls in trace')
    header = rpc.call('eth_getBlockByNumber', [hex(block), False])
    if header.get('hash') != block_hash:
        raise ValueError('transaction block changed during trace collection')
    return {'schema': 'GmxExecutionFeeTrace', 'version': 1, 'transaction_hash': transaction_hash,
            'block_number': block, 'block_hash': block_hash, 'receipt': receipt, 'transaction': transaction,
            'trace': trace, 'payments': payments, 'library_code': code}


def _number(value: Any) -> int:
    if isinstance(value, int):
        return value
    return int(value, 16 if str(value).startswith('0x') else 10)


def collect_gas_probe(rpc: PublicJsonRpc, transaction_hash: str,
                      trace_record: dict[str, Any]) -> dict[str, Any]:
    """Use the built-in opcode tracer and retain only GMX payment GAS readings."""
    raw = rpc.call('debug_traceTransaction', [transaction_hash, {
        'disableMemory': True, 'disableStack': True, 'disableStorage': True, 'timeout': '60s',
    }])
    if not isinstance(raw, dict) or not isinstance(raw.get('structLogs'), list):
        raise ValueError('opcode trace did not return structLogs')

    paid_payments = [payment for payment in trace_record['payments']
                     if int(payment['input']['execution_fee']) > 0]
    target_depths = {len(payment['path']) + 1 for payment in paid_payments}
    for payment in paid_payments:
        code = trace_record['library_code'][payment['library']]
        for pc in PAYMENT_GAS_PROGRAM_COUNTERS:
            if code[2 + pc * 2:4 + pc * 2].lower() != '5a':
                raise ValueError('payment library bytecode changed at a required GAS opcode')
    samples = []
    for entry in raw['structLogs']:
        if entry.get('op') != 'GAS' or _number(entry.get('depth', -1)) not in target_depths:
            continue
        if _number(entry.get('pc', -1)) not in PAYMENT_GAS_PROGRAM_COUNTERS:
            continue
        samples.append({'pc': _number(entry['pc']), 'gas': _number(entry['gas']),
                        'gas_cost': _number(entry['gasCost']), 'depth': _number(entry['depth'])})

    if len(samples) != len(paid_payments) * len(PAYMENT_GAS_PROGRAM_COUNTERS):
        raise ValueError('opcode trace has missing or ambiguous GMX payment GAS readings')
    for index, payment in enumerate(paid_payments):
        pair = samples[index * 2:index * 2 + 2]
        if [sample['pc'] for sample in pair] != list(PAYMENT_GAS_PROGRAM_COUNTERS):
            raise ValueError('payment GAS readings are out of order or overlap another call frame')
        if any(sample['depth'] != len(payment['path']) + 1 for sample in pair):
            raise ValueError('payment GAS reading has unexpected call depth')
    return {'schema': 'GmxExecutionFeeGasProbe', 'version': 2,
            'transaction_hash': transaction_hash, 'samples': samples}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recording', type=Path)
    parser.add_argument('--limit', type=int, default=6, help='total transactions to collect; 0 means all')
    parser.add_argument('--gas-probes', action='store_true',
                        help='collect full opcode traces for exact GAS readings (large responses)')
    args = parser.parse_args()
    if args.limit < 0:
        parser.error('--limit must be nonnegative')
    endpoint = os.environ.get('GMX_ARCHIVE_RPC_URL')
    if not endpoint:
        parser.error('set GMX_ARCHIVE_RPC_URL in this terminal')
    quality = json.loads((args.recording / 'completeness-report.json').read_text())
    if not quality.get('complete') or quality.get('gaps') or quality.get('reorgs'):
        parser.error('recording is incomplete')
    report = json.loads((args.recording / 'order-validation.json').read_text())
    transactions = select_transactions(report, args.limit)
    anchors = recorded_hashes(args.recording, set(transactions))
    output = args.recording / 'execution-fee-traces'
    output.mkdir(exist_ok=True)
    raw = output / ('capture-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:6])
    raw.mkdir()
    artifacts = RawArtifactStore(raw)
    completed = 0
    try:
        rpc = PublicJsonRpc(endpoint, 90, artifacts)
        for transaction_hash in transactions:
            target = output / (transaction_hash + '.json')
            block, block_hash = anchors[transaction_hash]
            if target.exists():
                existing = json.loads(target.read_text())
                if (existing.get('schema') != 'GmxExecutionFeeTrace' or existing.get('version') != 1
                        or existing.get('transaction_hash') != transaction_hash or existing.get('block_hash') != block_hash):
                    raise ValueError('existing trace identity mismatch')
            else:
                evidence = collect_transaction(rpc, transaction_hash, block, block_hash)
                evidence['raw_evidence_directory'] = raw.name
                temporary = target.with_suffix('.tmp-' + uuid4().hex[:6])
                temporary.write_text(json.dumps(evidence, sort_keys=True) + '\n')
                temporary.replace(target)
            gas_target = output / (transaction_hash + '.gas-v2.json')
            if args.gas_probes and not gas_target.exists():
                trace_record = json.loads(target.read_text())
                evidence = collect_gas_probe(rpc, transaction_hash, trace_record)
                evidence['raw_evidence_directory'] = raw.name
                temporary = gas_target.with_suffix('.tmp-' + uuid4().hex[:6])
                temporary.write_text(json.dumps(evidence, sort_keys=True) + '\n')
                temporary.replace(gas_target)
            completed += 1
            print(f'Execution-fee trace {completed}/{len(transactions)} saved or reused.', flush=True)
    except Exception as error:
        print(f'Trace collection failed ({type(error).__name__}); diagnostics retained in {raw}.')
        return 1
    finally:
        artifacts.close()
    print(f'Execution-fee trace sample saved: {output} ({completed} transactions).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
