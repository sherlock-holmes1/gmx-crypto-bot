"""Pure fixed-precision funding, borrowing and liquidation-fee formulas."""

from __future__ import annotations

from gmx_crypto_bot_v2.models.prb import apply_exponent_factor

P = 10**30


def ceil_div(a, b):
    return (a + b - 1) // b


def borrowing_rate(reserved, pool, oi_balance, side, settings):
    if reserved == 0:
        return 0
    if settings["skip_smaller"] and oi_balance[int(not side)] < oi_balance[int(side)]:
        return 0
    if pool <= 0:
        raise ValueError("empty borrowing pool")
    optimal = settings["optimal"]
    if not optimal:
        return (
            (apply_exponent_factor(reserved, settings["exponent"]) * P // pool)
            * settings["factor"]
            // P
        )
    max_reserved = pool * settings["reserve"] // P
    usage = reserved * P // max_reserved
    rate = usage * settings["base"] // P
    if usage > optimal and P > optimal:
        rate += (
            max(0, settings["above"] - settings["base"])
            * (usage - optimal)
            // (P - optimal)
        )
    return rate


def funding_rate(balance, duration, saved, c):
    long, short = balance
    diff = abs(long - short)
    total = long + short
    if diff == 0 and c["increase"] == 0:
        return 0, True, 0
    if total == 0:
        raise ValueError("empty funding balance")
    skew = apply_exponent_factor(diff, c["exponent"]) * P // total
    if c["increase"] == 0:
        return min(skew * c["factor"] // P, c["maximum"]), long > short, 0
    same = (saved > 0 and long > short) or (saved < 0 and short > long)
    next_saved = saved
    if not same or skew > c["stable"]:
        change = (skew * c["increase"] // P) * duration
        next_saved += change if long >= short else -change
    elif skew < c["decrease_threshold"] and saved != 0:
        next_saved = (1 if saved > 0 else -1) * max(
            1, abs(saved) - c["decrease"] * duration
        )
    next_saved = (1 if next_saved >= 0 else -1) * min(abs(next_saved), c["maximum"])
    bounded = (1 if next_saved >= 0 else -1) * min(
        max(abs(next_saved), c["minimum"]), c["maximum"]
    )
    return abs(bounded), bounded > 0, next_saved


def funding_deltas(oi, balance, prices, duration, saved, settings):
    """oi[side][collateral], side 0=long; prices are collateral maxima."""
    totals = [sum(side) for side in oi]
    fee = [[0, 0], [0, 0]]
    claim = [[0, 0], [0, 0]]
    if not all(totals):
        return dict(rate=0, longs_pay=False, saved=0, fee=fee, claim=claim)
    rate, longs_pay, next_saved = funding_rate(balance, duration, saved, settings)
    payer = 0 if longs_pay else 1
    receiver = 1 - payer
    usd = totals[payer] * (duration * rate) // P
    for token in (0, 1):
        portion = usd * oi[payer][token] // totals[payer]
        if portion == 0:
            continue
        fee[payer][token] = ceil_div(
            ceil_div(portion * 10**45, oi[payer][token]), prices[token]
        )
        claim[receiver][token] = (portion * 10**45 // totals[receiver]) // prices[token]
    return dict(rate=rate, longs_pay=longs_pay, saved=next_saved, fee=fee, claim=claim)


def liquidation_fee(size, price, factor, receiver):
    if min(size, factor, receiver) < 0 or price <= 0 or receiver > P:
        raise ValueError("invalid liquidation fee input")
    if factor == 0:
        return dict(
            liquidationFeeAmount=0,
            liquidationFeeReceiverFactor=0,
            liquidationFeeAmountForFeeReceiver=0,
        )
    amount = ceil_div(size * factor // P, price)
    return dict(
        liquidationFeeAmount=amount,
        liquidationFeeReceiverFactor=receiver,
        liquidationFeeAmountForFeeReceiver=amount * receiver // P,
    )
