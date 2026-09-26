"""Historical fee configuration, independent of PositionFeesCollected values.

SetUint is read from the unfiltered EventEmitter evidence. UI fee updates use
UiFeeFactorUpdated rather than SetUint. Missing anchors remain unavailable.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from gmx_crypto_bot.price_impact import config_base_key, config_market_data, config_market_side_data, keccak256

ZERO_ADDRESS = '0x' + '0' * 40
PRECISION = 10**30


@dataclass(frozen=True)
class ConfigKey:
    name: str
    data: str = '0x'
    pinned_field: str | None = None

    @property
    def base_key(self) -> str:
        return config_base_key(self.name)

    @property
    def storage_key(self) -> str:
        if self.data == '0x':
            return self.base_key
        return '0x' + keccak256(bytes.fromhex(self.base_key[2:] + self.data[2:])).hex()


def fee_keys(market: str, receivers: set[str]) -> dict[str, ConfigKey]:
    keys = {
        'position_fee_positive': ConfigKey('POSITION_FEE_FACTOR', config_market_side_data(market, True), 'position_fee_factor_for_balance_was_improved'),
        'position_fee_negative': ConfigKey('POSITION_FEE_FACTOR', config_market_side_data(market, False), 'position_fee_factor_for_balance_was_not_improved'),
        'position_fee_receiver': ConfigKey('POSITION_FEE_RECEIVER_FACTOR'),
        'borrowing_fee_receiver': ConfigKey('BORROWING_FEE_RECEIVER_FACTOR'),
        'max_ui_fee': ConfigKey('MAX_UI_FEE_FACTOR'),
    }
    for receiver in sorted(receivers - {ZERO_ADDRESS}):
        keys['ui_fee:' + receiver] = ConfigKey('UI_FEE_FACTOR', config_market_data(receiver))
    return keys


@dataclass(frozen=True)
class ConfigHistory:
    start_block: int
    end_block: int
    changes: tuple[tuple[tuple[int, int, int], int], ...]
    opening: int | None = None
    closing: int | None = None

    def __post_init__(self):
        if self.start_block > self.end_block or tuple(sorted(self.changes)) != self.changes:
            raise ValueError('invalid configuration bounds or event order')
        if any(not self.start_block <= c[0] <= self.end_block or type(v) is not int or v < 0 for c, v in self.changes):
            raise ValueError('configuration write outside evidence interval or invalid value')
        for value in (self.opening, self.closing):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('invalid configuration anchor')
        final = self.changes[-1][1] if self.changes else self.opening
        if final is not None and self.closing is not None and final != self.closing:
            raise ValueError('configuration history disagrees with closing anchor')

    def at(self, coordinate: tuple[int, int, int]) -> int | None:
        if not self.start_block <= coordinate[0] <= self.end_block:
            return None
        index = bisect_right([c for c, _ in self.changes], coordinate) - 1
        if index >= 0:
            return self.changes[index][1]
        if self.opening is not None:
            return self.opening
        # A complete no-write interval permits a closing anchor to establish
        # its unchanged value. Never carry a later write back in time.
        return self.closing if not self.changes else None


def build_fee_histories(recording: Path, metadata: dict, config_events: list[dict], receivers: set[str]) -> dict[str, ConfigHistory]:
    report_path = recording / 'completeness-report.json'
    if not report_path.exists():
        return {}
    report = json.loads(report_path.read_text())
    if report.get('complete') is not True or report.get('gaps') or report.get('reorgs'):
        return {}
    bounds = report.get('source_block_range', {})
    start, end = bounds.get('from'), bounds.get('to')
    if type(start) is not int or type(end) is not int:
        return {}
    keys = fee_keys(metadata['market']['market_token_address'].lower(), receivers)
    wanted = {(key.base_key, key.data): field for field, key in keys.items()}
    changes: dict[str, dict[tuple[int, int, int], int]] = {field: {} for field in keys}
    for entry in config_events:
        v = entry['values']
        if entry['event_name'] == 'UiFeeFactorUpdated':
            field = 'ui_fee:' + v.get('account', '').lower()
            value = v.get('uiFeeFactor')
        else:
            field = wanted.get((v.get('baseKey'), v.get('data')))
            value = v.get('value')
        if field not in changes:
            continue
        coordinate = (entry['block_number'], entry['transaction_index'], entry['log_index'])
        if not start <= coordinate[0] <= end:
            continue
        if coordinate in changes[field] and changes[field][coordinate] != value:
            raise ValueError('conflicting duplicate configuration writes')
        changes[field][coordinate] = value
    snapshots = {}
    sidecar = recording / 'fee-opening-configuration.json'
    if sidecar.exists():
        snapshots = json.loads(sidecar.read_text())
        if (snapshots.get('schema') != 'GmxFeeOpeningConfiguration' or snapshots.get('version') != 1
                or snapshots.get('market') != metadata['market']['market_token_address'].lower()
                or snapshots.get('data_store') != metadata['contracts']['data_store'].lower()):
            raise ValueError('fee configuration sidecar identity mismatch')
        for label, block in [('opening', start - 1)]:
            point = snapshots.get(label, {})
            expected_hash = _recorded_block_hash(recording, block)
            if point.get('block_number') != block or point.get('block_hash') != expected_hash:
                raise ValueError('fee configuration sidecar block mismatch')
            for field, item in point.get('values', {}).items():
                if field not in keys or item.get('storage_key') != keys[field].storage_key:
                    raise ValueError('fee configuration sidecar storage key mismatch')
                raw = item.get('result', '')
                if len(raw) != 66 or not raw.startswith('0x') or int(raw, 16) != item.get('value'):
                    raise ValueError('fee configuration sidecar value mismatch')
    histories = {}
    for field, key in keys.items():
        opening = snapshots.get('opening', {}).get('values', {}).get(field, {}).get('value')
        closing = None
        if closing is None and metadata.get('pinned_configuration_anchor_block') == end and key.pinned_field:
            pinned = metadata.get('pinned_configuration_raw', {}).get(key.pinned_field)
            closing = int(pinned) if pinned is not None else None
        histories[field] = ConfigHistory(start, end, tuple(sorted(changes[field].items())), opening, closing)
    return histories


def _recorded_block_hash(recording: Path, block: int) -> str:
    with (recording / 'events.jsonl').open() as stream:
        for line in stream:
            event = json.loads(line)
            if event.get('kind') == 'opening_state_checkpoint' and event['payload'].get('block_number') == block:
                return event['payload']['block_hash']
            if event.get('kind') == 'block_header' and event.get('block_number') == block:
                payload = event['payload']
                return payload.get('hash') or payload.get('block_hash') or payload['header']['hash']
    raise ValueError(f'missing recorded header for configuration anchor {block}')


def compare_historical_fees(request: dict, position: dict, fees: dict, model: dict | None,
                            histories: dict[str, ConfigHistory], checks: dict, mismatches: list) -> dict:
    coordinate = (fees['block_number'], fees['transaction_index'], fees['log_index'])
    factors = {field: history.at(coordinate) for field, history in histories.items()}
    values = fees['values']
    if request.get('orderType') == 7:
        # Insolvent liquidations may erase the fee struct. Do not interpret its
        # zero factors as a configuration change or declare a passing check.
        checks['historical_fee_factors_liquidation'] = 'unavailable'
        return factors

    def compare(name, actual, expected):
        if expected is None or actual is None:
            checks[name] = 'unavailable'
        elif actual == expected:
            checks[name] = 'matched'
        else:
            checks[name] = 'mismatch'
            mismatches.append(name + '_mismatch')

    improved = model.get('balance_was_improved') if model else None
    expected = factors.get('position_fee_positive' if improved else 'position_fee_negative') if type(improved) is bool else None
    compare('historical_position_fee_factor', values.get('positionFeeFactor'), expected)
    price, size = values.get('collateralTokenPrice.min'), position['values'].get('sizeDeltaUsd')
    amount = size * expected // PRECISION // price if expected is not None and type(price) is int and price > 0 and type(size) is int else None
    compare('historical_position_fee_amount', values.get('positionFeeAmount'), amount)
    for label, observed in [('position_fee_receiver', 'positionFeeReceiverFactor'), ('borrowing_fee_receiver', 'borrowingFeeReceiverFactor')]:
        compare('historical_' + label + '_factor', values.get(observed), factors.get(label))
    receiver = request.get('uiFeeReceiver')
    ui = 0 if receiver == ZERO_ADDRESS else None
    if receiver and receiver != ZERO_ADDRESS:
        raw, cap = factors.get('ui_fee:' + receiver.lower()), factors.get('max_ui_fee')
        if raw is not None and cap is not None:
            ui = min(raw, cap)
    compare('historical_ui_fee_factor', values.get('uiFeeReceiverFactor'), ui)
    ui_amount = size * ui // PRECISION // price if ui is not None and type(price) is int and price > 0 and type(size) is int else None
    compare('historical_ui_fee_amount', values.get('uiFeeAmount'), ui_amount)
    return factors
