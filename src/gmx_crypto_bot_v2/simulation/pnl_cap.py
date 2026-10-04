"""GMX trader PnL cap from independent historical market state.

Mirrors PositionUtils.getPositionPnlUsd and MarketUtils.getCappedPnl.
"""

from __future__ import annotations

from gmx_crypto_bot_v2.simulation.evidence import EvidenceState, UnavailableEvidence

P = 10**30


def capped_position_pnl(state: EvidenceState, is_long: bool, total_pnl: int) -> int:
    if total_pnl <= 0:
        return total_pnl
    side = "long" if is_long else "short"
    factor = state.configuration.get("risk:max_pnl_factor_for_traders_" + side)
    if type(factor) is not int or factor < 0:
        raise UnavailableEvidence("historical max-PnL cap unavailable")
    token = state.long_token if is_long else state.short_token
    token_price = state.oracle.get(token, (None, None))[0]
    index_range = state.oracle.get(state.index_token)
    if type(token_price) is not int or not index_range:
        raise UnavailableEvidence("pool valuation oracle unavailable for PnL cap")
    pool_usd = state.pool_amount[token] * token_price
    side_tokens = state.open_interest_tokens[side]
    side_usd = state.open_interest_usd[side]
    pool_pnl = (side_tokens * index_range[1] - side_usd if is_long else
                side_usd - side_tokens * index_range[0])
    capped_pool_pnl = min(pool_pnl, pool_usd * factor // P) if pool_pnl >= 0 else pool_pnl
    if capped_pool_pnl != pool_pnl and pool_pnl > 0:
        return total_pnl * capped_pool_pnl // pool_pnl
    return total_pnl
