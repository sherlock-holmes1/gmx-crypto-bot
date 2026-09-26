"""Read-only block-hash-verified fee configuration checkpoint backfill."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from gmx_crypto_bot.historical_configuration import _recorded_block_hash, fee_keys
from gmx_crypto_bot.impact_backfill import _rpc_call
from gmx_crypto_bot.price_impact import keccak256
from gmx_crypto_bot.event_decoder import decode_event_log, event_name_from_data


def fetch_opening_fee_configuration(recording: Path, rpc) -> dict:
    metadata = json.loads((recording / 'metadata.json').read_text())
    report = json.loads((recording / 'completeness-report.json').read_text())
    if report.get('complete') is not True or report.get('gaps') or report.get('reorgs'):
        raise ValueError('configuration backfill requires a complete recording')
    block = report['source_block_range']['from'] - 1
    expected_hash = _recorded_block_hash(recording, block)
    header = rpc('eth_getBlockByNumber', [hex(block), False])
    if not isinstance(header, dict) or header.get('hash') != expected_hash:
        raise ValueError('archive opening block hash differs from recording')
    receivers = set()
    with (recording / 'events.jsonl').open() as stream:
        for line in stream:
            e = json.loads(line)
            data = e.get('payload', {}).get('log', {}).get('data', '')
            if event_name_from_data(data) == 'PositionFeesCollected':
                receiver = decode_event_log(data).values.get('uiFeeReceiver')
                if isinstance(receiver, str):
                    receivers.add(receiver.lower())
    market = metadata['market']['market_token_address'].lower()
    store = metadata['contracts']['data_store'].lower()
    selector = keccak256(b'getUint(bytes32)')[:4].hex()
    values = {}
    for field, key in fee_keys(market, receivers).items():
        data = '0x' + selector + key.storage_key[2:]
        result = rpc('eth_call', [{'to': store, 'data': data}, hex(block)])
        if not isinstance(result, str) or len(result) != 66 or not result.startswith('0x'):
            raise ValueError('invalid historical uint result for ' + field)
        values[field] = {'storage_key': key.storage_key, 'result': result, 'value': int(result, 16)}
    # Recheck after all number-pinned calls to detect an intervening reorg.
    header = rpc('eth_getBlockByNumber', [hex(block), False])
    if not isinstance(header, dict) or header.get('hash') != expected_hash:
        raise ValueError('archive opening block changed during backfill')
    return {'schema': 'GmxFeeOpeningConfiguration', 'version': 1,
            'market': market, 'data_store': store,
            'opening': {'block_number': block, 'block_hash': expected_hash, 'values': values}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recording', type=Path)
    args = parser.parse_args()
    endpoint = os.environ.get('GMX_ARCHIVE_RPC_URL')
    if not endpoint:
        parser.error('set GMX_ARCHIVE_RPC_URL to an archive-capable Arbitrum RPC URL')
    try:
        result = fetch_opening_fee_configuration(args.recording, lambda method, params: _rpc_call(endpoint, method, params))
    except Exception as error:
        # RPC/provider exceptions may include a credential-bearing URL.
        parser.exit(1, f'Historical configuration backfill failed ({type(error).__name__}); no output written.\n')
    path = args.recording / 'fee-opening-configuration.json'
    with path.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, indent=2, sort_keys=True) + '\n')
    print(f'Fee configuration saved at block {result["opening"]["block_number"]}: {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
