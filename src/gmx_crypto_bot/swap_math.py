"""Independent GMX swap pricing primitives and archive key definitions."""
from __future__ import annotations
from gmx_crypto_bot.price_impact import config_base_key, keccak256
from gmx_crypto_bot.prb_math import apply_exponent_factor
from gmx_crypto_bot.referral import word

P=10**30
ZERO_ID='0x'+'0'*64
# PRBMath integer rounding is reproduced; swap comparisons require exact equality.
USD_TOLERANCE=0
TOKEN_TOLERANCE=0


def call_data(signature,*args):
    return '0x'+keccak256(signature.encode())[:4].hex()+''.join(word(x) for x in args)


def key(name,*args):
    base=config_base_key(name)
    return '0x'+keccak256(bytes.fromhex(base[2:]+''.join(word(x) for x in args))).hex() if args else base


def market_keys(market):
    return {
        'fee_positive':key('SWAP_FEE_FACTOR',market,True),
        'fee_negative':key('SWAP_FEE_FACTOR',market,False),
        'impact_positive':key('SWAP_IMPACT_FACTOR',market,True),
        'impact_negative':key('SWAP_IMPACT_FACTOR',market,False),
        'exponent':key('SWAP_IMPACT_EXPONENT_FACTOR',market),
    }


def curve(a,b,delta,positive,negative,exponent):
    if min(a,b,delta,positive,negative,exponent)<0 or delta>b:raise ValueError('invalid swap pool or factors')
    na,nb=a+delta,b-delta;old=abs(a-b);new=abs(na-nb);improved=new<old
    positive=min(positive,negative)
    def impact(x,factor):return apply_exponent_factor(x,exponent)*factor//P
    if (a<=b)==(na<=nb):
        f=positive if improved else negative
        impact_usd=abs(impact(old,f)-impact(new,f))*(1 if improved else -1)
    else:impact_usd=impact(old,positive)-impact(new,negative)
    return impact_usd,improved


def price_swap(amount,prices,pool,virtual,impact_pool,factors,receiver_factor,ui_factor):
    imin,imax,omin,omax=prices
    if min(prices)<=0 or amount<0:raise ValueError('invalid swap price or amount')
    midin=(imin+imax)//2;midout=(omin+omax)//2;delta=amount*midin
    args=(factors['impact_positive'],factors['impact_negative'],factors['exponent'])
    impact,improved=curve(pool[0]*midin,pool[1]*midout,delta,*args)
    market_impact=impact
    if impact<0 and virtual is not None:
        vi,vimp=curve(virtual[0]*midin,virtual[1]*midout,delta,*args)
        if vi<impact:impact,improved=vi,vimp
    fee_factor=factors['fee_positive' if improved else 'fee_negative']
    if any(not 0<=v<=P for v in (fee_factor,receiver_factor,ui_factor)):raise ValueError('invalid swap fee factor')
    fee=amount*fee_factor//P;receiver=fee*receiver_factor//P;pool_fee=fee-receiver;ui=amount*ui_factor//P
    after=amount-fee-ui
    if after<0:raise ValueError('fees exceed input')
    input_impact=0
    if impact>0:
        uncapped=impact//omax;output_impact=min(uncapped,impact_pool[1])
        cap_diff=(uncapped-output_impact)*omax
        input_impact=min(cap_diff//imax,impact_pool[0]) if cap_diff else 0
        effective=after+input_impact
        impact_amount=output_impact
        ordinary_out=effective*imin//omax;out=ordinary_out+output_impact
    else:
        impact_amount=-((-impact+imin-1)//imin)
        effective=after+impact_amount
        if effective<=0:raise ValueError('swap impact exceeds input')
        ordinary_out=effective*imin//omax;out=ordinary_out
    return {'priceImpactUsd':impact,'priceImpactAmount':impact_amount,'tokenInPriceImpactAmount':input_impact,
            'amountInAfterFees':after,'amountOut':out,'pool_delta_in':effective+pool_fee,'pool_delta_out':-ordinary_out,
            'feeReceiverAmount':receiver,'feeAmountForPool':pool_fee,'uiFeeReceiverFactor':ui_factor,'uiFeeAmount':ui,
            'balance_was_improved':improved,'market_impact_usd':market_impact,'fee_factor':fee_factor}


MARKET_FIELDS=('MARKET_TOKEN','INDEX_TOKEN','LONG_TOKEN','SHORT_TOKEN')

def market_field_key(market,field):
    # MarketStoreUtils uses address first, unlike Keys' base-first layout.
    return '0x'+keccak256(bytes.fromhex(word(market)+config_base_key(field)[2:])).hex()
