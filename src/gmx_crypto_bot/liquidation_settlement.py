"""Offline liquidation cash settlement, including early insolvency returns.

Payment order and rounding follow DecreasePositionCollateralUtils.processCollateral
and payForCost. Observed impact is used only after independent impact validation;
its existing USD tolerance is retained. All cash comparisons are exact integers.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, asdict
import json
from pathlib import Path
from typing import Any

from gmx_crypto_bot.accrual_math import P, ceil_div, liquidation_fee
from gmx_crypto_bot.execution_fees.models import walk_trace
from gmx_crypto_bot.referral import discount_amounts
from gmx_crypto_bot.swap_math import key

ZERO = '0x' + '0' * 40
MULTICHAIN_VAULT = '0xceaadfaf6a8c489b250e407987877c5fdfcdbe6e'


def coordinate(event: dict) -> tuple[int, int, int]:
    return tuple(event[name] for name in ('block_number', 'transaction_index', 'log_index'))


@dataclass
class Cash:
    collateral: int
    output: int
    secondary: int
    collateral_price: int
    secondary_price: int

    def pay(self, usd: int) -> dict[str, int]:
        if usd < 0 or min(self.collateral_price, self.secondary_price) <= 0:
            raise ValueError('invalid settlement cost or price')
        needed = ceil_div(usd, self.collateral_price)
        output_paid = min(self.output, needed)
        self.output -= output_paid
        needed -= output_paid
        collateral_paid = min(self.collateral, needed)
        self.collateral -= collateral_paid
        needed -= collateral_paid
        secondary_needed = needed * self.collateral_price // self.secondary_price
        secondary_paid = min(self.secondary, secondary_needed)
        self.secondary -= secondary_paid
        return {
            'cost_usd': usd,
            'paid_in_collateral': output_paid + collateral_paid,
            'paid_in_secondary': secondary_paid,
            'remaining_cost_usd': (secondary_needed - secondary_paid) * self.secondary_price,
        }


def settle(cash: Cash, base: int, impact: int, impact_diff: int,
           funding: int, fees_excluding_funding: int) -> dict[str, Any]:
    """Stop at the first unpaid cost; never charge later costs after insolvency."""
    payments = []
    insolvent_step = None
    fees_erased = False
    for step, cost in (
        ('funding', funding * cash.collateral_price),
        ('pnl', max(-base, 0)),
        ('fees', fees_excluding_funding * cash.collateral_price),
        ('impact', max(-impact, 0)),
        ('diff', impact_diff),
    ):
        payment = cash.pay(cost)
        payments.append(dict(step=step, **payment))
        if step == 'fees' and payment['paid_in_collateral'] < fees_excluding_funding:
            fees_erased = True
        if payment['remaining_cost_usd']:
            insolvent_step = step
            fees_erased = True
            break
    # DecreasePositionUtils releases all collateral on a full close.
    cash.output += cash.collateral
    cash.collateral = 0
    return dict(cash=asdict(cash), payments=payments,
                insolvent_step=insolvent_step, fees_erased=fees_erased)


def reconstruct_fees(replay: Any, order_key: str, request: dict, position: dict,
                     fees: dict, impact: dict, histories: dict, referral: Any) -> dict:
    state = replay.orders[order_key]
    previous = position['pre_position']
    values = position['values']
    c = coordinate(fees)
    token = values['collateralToken']
    side = values['isLong']
    price = position['oracle_prices_at_event'][token]['minPrice']
    factors = {name: history.at(c) for name, history in histories.items()}
    improved = impact['balance_was_improved']
    if type(improved) is not bool:
        raise ValueError('missing independently reconstructed impact direction')
    position_fee = values['sizeDeltaUsd'] * factors[
        'position_fee_positive' if improved else 'position_fee_negative'] // P // price
    config = referral.at(request['account'].lower(), c)
    discounts = discount_amounts(position_fee, config['code'], config['rebate_bps'],
                                 config['share_bps'], config['minimum'],
                                 config['pro_tier'], config['pro_factor'])
    borrowing_usd = previous['sizeInUsd'] * (
        state['borrowing'][side]['next_value'] - previous['borrowingFactor']) // P
    latest_funding = state['funding_values'][key(
        'FUNDING_FEE_AMOUNT_PER_SIZE', replay.market, token, side)]
    funding_delta = latest_funding - previous['fundingFeeAmountPerSize']
    funding = ceil_div(previous['sizeInUsd'] * funding_delta, 10**45)
    if min(borrowing_usd, funding_delta) < 0:
        raise ValueError('negative liquidation accrual')
    liquidation = liquidation_fee(values['sizeDeltaUsd'], price,
                                  state['liquidation_factor'], state['liquidation_receiver'])
    receiver = request['uiFeeReceiver']
    ui = 0 if receiver == ZERO else values['sizeDeltaUsd'] * min(
        factors['ui_fee:' + receiver], factors['max_ui_fee']) // P // price
    result = dict(positionFeeAmount=position_fee, borrowingFeeUsd=borrowing_usd,
                  borrowingFeeAmount=borrowing_usd // price, fundingFeeAmount=funding,
                  uiFeeAmount=ui, latestFundingFeeAmountPerSize=latest_funding, **liquidation)
    result['totalCostAmount'] = (position_fee + borrowing_usd // price + funding
                                + ui + liquidation['liquidationFeeAmount'] - discounts['totalDiscountAmount'])
    for side_name, collateral in zip(('Long', 'Short'), replay.tokens):
        latest = state['funding_values'][key('CLAIMABLE_FUNDING_AMOUNT_PER_SIZE', replay.market, collateral, side)]
        result['latest' + side_name + 'TokenClaimableFundingAmountPerSize'] = latest
        delta = latest - previous[side_name.lower() + 'TokenClaimableFundingAmountPerSize']
        if delta < 0:
            raise ValueError('negative claimable funding accrual')
        result['claimable' + side_name + 'TokenAmount'] = previous['sizeInUsd'] * delta // 10**45
    return result


def verify_native(recording: Path | None, position: dict, receipt: dict,
                  market: str, receiver: str, amount: int, wnt: str) -> str:
    if recording is None:
        return 'unavailable'
    path = recording / 'execution-fee-traces' / (position['transaction_hash'] + '.json')
    if not path.exists():
        return 'unavailable'
    data = json.loads(path.read_text())
    raw_receipt = data['receipt']
    if (data['transaction_hash'] != position['transaction_hash']
            or raw_receipt['transactionHash'] != position['transaction_hash']
            or data['block_number'] != position['block_number']
            or data['block_hash'] != raw_receipt['blockHash']
            or int(raw_receipt['blockNumber'], 16) != position['block_number']
            or raw_receipt['status'] != '0x1'
            or raw_receipt['blockHash'] != position['block_hash']):
        return 'mismatch'
    frames = list(walk_trace(data['trace']))
    native = [node for node in frames if node.committed and node.frame.get('type') == 'CALL'
              and node.frame.get('from') == market and node.frame.get('to') == receiver
              and int(node.frame.get('value', '0x0'), 16) == amount]
    withdrawals = [node for node in frames if node.committed and node.frame.get('type') == 'CALL'
                   and node.frame.get('from') == market and node.frame.get('to') == wnt
                   and node.frame.get('input') == '0x2e1a7d4d' + format(amount, '064x')]
    # Require a unique withdrawal and recipient CALL in the same transfer scope.
    return 'matched' if len(native) == len(withdrawals) == 1 and (
        native[0].path[:-1] == withdrawals[0].path[:-1]
        and native[0].path > withdrawals[0].path) else 'mismatch'


def verify_payouts(expected: Counter, request: dict, position: dict, payouts: list,
                   metadata: dict, recording: Path | None, receipt: dict) -> str:
    market, receiver = request['market'], request['receiver']
    transfers = [e for e in payouts if e['event_name'] == 'ERC20Transfer' and e['amount'] > 0]
    credits = [e['values'] for e in payouts if e['event_name'] == 'MultichainTransferIn'
               and e['values'].get('amount', 0) > 0]
    if request.get('srcChainId'):
        actual = Counter((e['token'], e['amount']) for e in transfers
                         if e['from'] == market and e['to'] == MULTICHAIN_VAULT)
        credited = Counter((v['token'], v['amount']) for v in credits
                           if v['account'] == receiver and v['srcChainId'] == request['srcChainId'])
        return 'matched' if actual == credited == expected and len(transfers) == sum(expected.values()) and len(credits) == sum(expected.values()) else 'mismatch'
    if credits:
        return 'mismatch'
    wnt = metadata['tokens']['index']['address'].lower()
    actual = Counter()
    native_status = 'matched'
    for event in transfers:
        if event['from'] != market:
            return 'mismatch'
        if event['to'] == receiver:
            actual[(event['token'], event['amount'])] += 1
        elif (event['to'] == ZERO and event['token'] == wnt
              and request.get('shouldUnwrapNativeToken')):
            actual[(wnt, event['amount'])] += 1
            native_status = verify_native(recording, position, receipt, market, receiver, event['amount'], wnt)
        else:
            return 'mismatch'
    return native_status if actual == expected else 'mismatch'


def compare_liquidation_settlement(order_key: str, request: dict, position: dict,
                                  fees: dict, swaps: list, payouts: list, metadata: dict,
                                  replay: Any, impact: dict, histories: dict, referral: Any,
                                  checks: dict, errors: list, recording: Path | None,
                                  receipt: dict | None, payout_receipt_available: bool) -> dict:
    result: dict[str, Any] = {'status': 'unavailable'}
    checks['liquidation_settlement'] = 'unavailable'
    required = ('historical_fee_factors_liquidation', 'independent_funding_accumulators',
                'independent_borrowing_accumulators', 'independent_price_impact',
                'uncapped_pnl', 'proportional_pending_impact',
                'position_size_usd', 'position_size_tokens', 'decrease_tokens')
    if any(checks.get(name) != 'matched' for name in required):
        result['reason'] = 'unverified liquidation economics or pre-position'
        return result
    if not payout_receipt_available or receipt is None or receipt['payload'].get('status') != '0x1':
        result['reason'] = 'missing successful payout receipt'
        return result
    try:
        values, previous = position['values'], position['pre_position']
        if values['basePnlUsd'] != values['uncappedBasePnlUsd']:
            raise ValueError('capped liquidation PnL is not independently modeled')
        if values['sizeDeltaUsd'] != previous['sizeInUsd'] or request['initialCollateralDeltaAmount'] != 0:
            raise ValueError('unsupported partial liquidation or withdrawal')
        token = values['collateralToken']
        pnl_token = metadata['tokens']['long' if values['isLong'] else 'short']['address'].lower()
        prices = position['oracle_prices_at_event']
        price, pnl_price = prices[token]['minPrice'], prices[pnl_token]
        reconstructed = reconstruct_fees(replay, order_key, request, position, fees, impact, histories, referral)
        profit = max(values['basePnlUsd'], 0) // pnl_price['maxPrice'] + max(values['totalImpactUsd'], 0) // pnl_price['maxPrice']
        cash = Cash(previous['collateralAmount'], profit if pnl_token == token else 0,
                    profit if pnl_token != token else 0, price, pnl_price['minPrice'])
        hops = [e for e in swaps if e['event_name'] == 'SwapInfo']
        if request.get('decreasePositionSwapType') not in (0, 1):
            raise ValueError('unsupported liquidation output swap mode')
        if hops:
            if any(checks.get(name) != 'matched' for name in (
                    'historical_swap_fees', 'independent_swap_price_impact',
                    'independent_swap_state', 'independent_swap_output')):
                raise ValueError('unverified liquidation swap')
            hop = hops[0]['values']
            if (len(hops) != 1 or coordinate(hops[0]) >= coordinate(position)
                    or request['decreasePositionSwapType'] != 1
                    or hop['tokenIn'] != pnl_token or hop['tokenOut'] != token
                    or hop['amountIn'] != cash.secondary or hop['receiver'] != request['market']):
                raise ValueError('unsupported liquidation swap path')
            cash.output += hop['amountOut']
            cash.secondary = 0
        result.update(settle(cash, values['basePnlUsd'], values['totalImpactUsd'],
                             values.get('values.priceImpactDiffUsd', 0), reconstructed['fundingFeeAmount'],
                             reconstructed['totalCostAmount'] - reconstructed['fundingFeeAmount']))
        result['reconstructed_fees'] = reconstructed
        expected = Counter()
        for output_token, amount in ((token, cash.output), (pnl_token, cash.secondary)):
            if amount:
                expected[(output_token, amount)] += 1
        payout_status = verify_payouts(expected, request, position, payouts, metadata, recording, receipt)
        observed_insolvency = [e for e in replay.insolvent.get(order_key, [])
                              if e['transaction_hash'] == position['transaction_hash']]
        if result['insolvent_step'] is None:
            insolvency_matches = not observed_insolvency
        else:
            insolvency_matches = len(observed_insolvency) == 1 and all(
                observed_insolvency[0]['values'].get(name) == value for name, value in {
                    'step': result['insolvent_step'], 'remainingCostUsd': result['payments'][-1]['remaining_cost_usd'],
                    'basePnlUsd': values['basePnlUsd'], 'positionCollateralAmount': previous['collateralAmount'],
                }.items()) and coordinate(observed_insolvency[0]) < coordinate(fees)
        expected_fees = dict(reconstructed)
        if result['fees_erased']:
            expected_fees = {name: value if name.startswith(('claimable', 'latest')) else 0
                             for name, value in expected_fees.items()}
        fees_match = all(fees['values'].get(name, 0) == value for name, value in expected_fees.items())
        if result['fees_erased']:
            erased_fields = (
                'positionFeeFactor', 'positionFeeReceiverFactor', 'borrowingFeeReceiverFactor',
                'borrowingFeeAmountForFeeReceiver', 'uiFeeReceiverFactor', 'protocolFeeAmount',
                'feeReceiverAmount', 'feeAmountForPool', 'positionFeeAmountForPool',
                'totalCostAmountExcludingFunding', 'totalDiscountAmount',
                'referral.totalRebateAmount', 'referral.traderDiscountAmount',
                'referral.affiliateRewardAmount', 'pro.traderDiscountAmount',
            )
            fees_match &= all(fees['values'].get(name, 0) == 0 for name in erased_fields)
        collateral_matches = (values['collateralAmount'] == 0
                              and values['collateralDeltaAmount'] == previous['collateralAmount']
                              and values['sizeInUsd'] == values['sizeInTokens'] == 0)
        result.update(collateral_matches=collateral_matches, fees_match=fees_match,
                      insolvency_matches=insolvency_matches, payout_status=payout_status,
                      expected_outputs=[dict(token=t, amount=a, count=n) for (t, a), n in expected.items()])
        exact = collateral_matches and fees_match and insolvency_matches
        result['status'] = ('mismatch' if not exact or payout_status == 'mismatch'
                            else payout_status)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        result['reason'] = str(error)
    checks['liquidation_settlement'] = result['status']
    if result['status'] == 'mismatch':
        errors.append('liquidation_settlement_mismatch')
    return result
