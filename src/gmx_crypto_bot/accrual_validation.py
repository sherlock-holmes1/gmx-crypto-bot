"""Independent market accrual replay, anchored at both ends of the recording."""
from __future__ import annotations
from collections import Counter,defaultdict
from functools import lru_cache
import json
import base64
import gzip
from gmx_crypto_bot.accrual_configuration import load_snapshot,slots
from gmx_crypto_bot.accrual_math import borrowing_rate,funding_deltas,liquidation_fee
from gmx_crypto_bot.swap_math import key
from gmx_crypto_bot.price_impact import config_base_key
from gmx_crypto_bot.referral import word

# Storage keys depend only on immutable names/arguments, not replay state.
key = lru_cache(maxsize=256)(key)

EVENTS={'OraclePriceUpdate','PoolAmountUpdated','OpenInterestUpdated','OpenInterestInTokensUpdated',
 'FundingFeeAmountPerSizeUpdated','ClaimableFundingAmountPerSizeUpdated','Funding',
 'CumulativeBorrowingFactorUpdated','Borrowing','PositionFeesCollected'}
RAW_EVENTS={'SetUint','SetBool','SetInt','PositionFeesInfo','InsolventClose'}


def coordinate(e):return e['block_number'],e['transaction_index'],e['log_index']


class AccrualReplay:
    def __init__(self,recording,metadata):
        self.metadata=metadata;self.recording=recording;self.snapshot=load_snapshot(recording,metadata)
        self.available=self.snapshot is not None;self.results=[];self.orders={};self.fee_info=defaultdict(list);self.insolvent=defaultdict(list)
        self.closing={};self.continuity_errors=[];self.errors=[]
        self.market=metadata['market']['market_token_address'].lower()
        self.tokens=[metadata['tokens'][t]['address'].lower() for t in ('long','short')]
        self.index=metadata['tokens']['index']['address'].lower()
        if self.available:self.state=dict(self.snapshot['opening'])

    def read(self,name,*args):return self.state[key(name,*args)]
    def put(self,value,name,*args):self.state[key(name,*args)]=value
    def market_value(self,name):return self.read(name,self.market)
    def set_market(self,value,name):self.put(value,name,self.market)

    def run(self,raw_events):
        if not self.available:return
        from gmx_crypto_bot.validator import _decode_recorded_log,_event_entry
        timeline=[];timestamps={};hashes={}
        with (self.recording/'events.jsonl').open() as stream:
            for line in stream:
                event=json.loads(line)
                if event['kind']=='block_header':
                    raw=event['payload']['timestamp'];timestamp=int(raw,16) if isinstance(raw,str) else raw
                    block=event['block_number']
                    if block in timestamps and timestamps[block]!=timestamp:raise ValueError('conflicting accrual timestamp')
                    timestamps[block]=timestamp
                elif event['kind']=='gmx_market_log' and event['payload'].get('event_name') in EVENTS:
                    decoded=_decode_recorded_log(event)
                    if decoded is None:raise ValueError('undecodable accrual event')
                    if decoded.values.get('market')==self.market or (decoded.event_name=='OraclePriceUpdate' and decoded.values.get('token') in self.tokens+[self.index]):
                        timeline.append(_event_entry(event,decoded))
                        if decoded.event_name in {'Funding','CumulativeBorrowingFactorUpdated','Borrowing'}:
                            block=event['block_number'];h=event['payload']['log']['blockHash']
                            if block in hashes and hashes[block]!=h:raise ValueError('conflicting accrual block hashes')
                            hashes[block]=h
        timestamps.update(load_timestamps(self.recording,hashes))
        self.replay(timeline,raw_events,timestamps)

    def replay(self,timeline,raw_events,timestamps):
        for e in raw_events:
            if e['event_name']=='PositionFeesInfo':self.fee_info[e['values']['orderKey']].append(e)
            if e['event_name']=='InsolventClose':self.insolvent[e['values']['orderKey']].append(e)
        config_index={(config_base_key(v['name']),'0x'+''.join(word(a) for a in v['args'])):k for k,v in slots(self.metadata).items()}
        configs=[e for e in raw_events if e['event_name'] in {'SetUint','SetBool','SetInt'} and (e['values'].get('baseKey'),e['values'].get('data')) in config_index]
        timeline=sorted(timeline+configs,key=coordinate)
        prices={};pending_funding=[];pending_borrow=None;latest_funding=None;latest_borrow={};tx=None;seen={}
        for e in timeline:
            c=coordinate(e)
            if c in seen:
                if seen[c]!=e:raise ValueError('conflicting accrual event')
                continue
            seen[c]=e;v=e['values'];name=e['event_name']
            if e['transaction_hash']!=tx:
                if pending_funding or pending_borrow:self.errors.append('unfinished_accrual_group')
                tx=e['transaction_hash'];prices={};pending_funding=[];pending_borrow=None;latest_funding=None;latest_borrow={}
            if name in {'SetUint','SetBool','SetInt'}:
                k=config_index[v['baseKey'],v['data']]
                if k in self.state:self.state[k]=int(v['value'])
                continue
            if name=='OraclePriceUpdate':prices[v['token']]=(v['minPrice'],v['maxPrice']);continue
            if name in {'PoolAmountUpdated','OpenInterestUpdated','OpenInterestInTokensUpdated'}:
                if name=='PoolAmountUpdated':k=key('POOL_AMOUNT',self.market,v['token'])
                else:k=key('OPEN_INTEREST' if name=='OpenInterestUpdated' else 'OPEN_INTEREST_IN_TOKENS',self.market,v['collateralToken'],v['isLong'])
                if k not in self.state:raise ValueError('unanchored accrual state cell')
                if self.state[k]+v['delta']!=v['nextValue']:self.continuity_errors.append(list(c))
                self.state[k]=v['nextValue'];continue
            if name in {'FundingFeeAmountPerSizeUpdated','ClaimableFundingAmountPerSizeUpdated'}:pending_funding.append(e);continue
            if name=='PositionFeesCollected':
                self.orders[v['orderKey']]={'funding':latest_funding,'borrowing':dict(latest_borrow),'coordinate':list(c),
                    'liquidation_factor':self.market_value('LIQUIDATION_FEE_FACTOR'),
                    'liquidation_receiver':self.read('LIQUIDATION_FEE_RECEIVER_FACTOR'),
                    'funding_values':{key(n,self.market,t,side):self.read(n,self.market,t,side) for n in ('FUNDING_FEE_AMOUNT_PER_SIZE','CLAIMABLE_FUNDING_AMOUNT_PER_SIZE') for t in self.tokens for side in (True,False)}}
                continue
            result={'event_name':name,'coordinate':list(c),'transaction_hash':tx,'status':'unavailable'}
            try:
                if self.continuity_errors:raise ValueError('broken pool/open-interest continuity')
                now=timestamps[e['block_number']]
                oracle=[prices[t] for t in (self.index,*self.tokens)]
                if min(p for pair in oracle for p in pair)<=0:raise ValueError('invalid oracle prices')
                oi=[[self.read('OPEN_INTEREST',self.market,t,side) for t in self.tokens] for side in (True,False)]
                oi_tokens=[[self.read('OPEN_INTEREST_IN_TOKENS',self.market,t,side) for t in self.tokens] for side in (True,False)]
                balance=[sum(x) for x in oi]
                if self.read('USE_OPEN_INTEREST_IN_TOKENS_FOR_BALANCE'):balance=[sum(x)*(sum(oracle[0])//2) for x in oi_tokens]
                if name=='Funding':
                    updated=self.market_value('FUNDING_UPDATED_AT');duration=now-updated if updated else 0
                    if duration<0:raise ValueError('negative funding interval')
                    config={label:self.market_value(n) for label,n in [('factor','FUNDING_FACTOR'),('exponent','FUNDING_EXPONENT_FACTOR'),('increase','FUNDING_INCREASE_FACTOR_PER_SECOND'),('decrease','FUNDING_DECREASE_FACTOR_PER_SECOND'),('minimum','MIN_FUNDING_FACTOR_PER_SECOND'),('maximum','MAX_FUNDING_FACTOR_PER_SECOND'),('stable','THRESHOLD_FOR_STABLE_FUNDING'),('decrease_threshold','THRESHOLD_FOR_DECREASE_FUNDING')]}
                    old_saved=self.market_value('SAVED_FUNDING_FACTOR_PER_SECOND')
                    model=funding_deltas(oi,balance,[x[1] for x in oracle[1:]],duration,old_saved,config)
                    expected={};deltas={}
                    for category,n in [('fee','FUNDING_FEE_AMOUNT_PER_SIZE'),('claim','CLAIMABLE_FUNDING_AMOUNT_PER_SIZE')]:
                        for i,side in enumerate((True,False)):
                            for j,t in enumerate(self.tokens):
                                k=key(n,self.market,t,side);delta=model[category][i][j]
                                self.state[k]+=delta;deltas[k]=delta
                                if delta:expected[k]=(delta,self.state[k])
                    observed={}
                    for update in pending_funding:
                        w=update['values'];n='FUNDING_FEE_AMOUNT_PER_SIZE' if update['event_name']=='FundingFeeAmountPerSizeUpdated' else 'CLAIMABLE_FUNDING_AMOUNT_PER_SIZE'
                        k=key(n,self.market,w['collateralToken'],w['isLong'])
                        if k in observed:raise ValueError('duplicate funding cell in update')
                        observed[k]=(w['delta'],w['value'])
                    result.update(status='matched' if expected==observed and model['rate']==v['fundingFactorPerSecond'] else 'mismatch',
                                  duration=duration,previous_saved_factor=old_saved,model=model,expected=expected,observed=observed)
                    self.set_market(model['saved'],'SAVED_FUNDING_FACTOR_PER_SECOND');self.set_market(now,'FUNDING_UPDATED_AT')
                elif name=='CumulativeBorrowingFactorUpdated':
                    side=v['isLong'];idx=0 if side else 1
                    config={label:self.read(n,self.market,side) for label,n in [('factor','BORROWING_FACTOR'),('exponent','BORROWING_EXPONENT_FACTOR'),('optimal','OPTIMAL_USAGE_FACTOR'),('base','BASE_BORROWING_FACTOR'),('above','ABOVE_OPTIMAL_USAGE_BORROWING_FACTOR'),('reserve','OPEN_INTEREST_RESERVE_FACTOR')]}
                    config['skip_smaller']=self.read('SKIP_BORROWING_FEE_FOR_SMALLER_SIDE')
                    reserved=sum(oi_tokens[0])*oracle[0][1] if side else sum(oi[1])
                    pool=self.read('POOL_AMOUNT',self.market,self.tokens[idx])*oracle[idx+1][0]
                    rate=borrowing_rate(reserved,pool,balance,side,config)
                    updated=self.read('CUMULATIVE_BORROWING_FACTOR_UPDATED_AT',self.market,side);duration=now-updated if updated else 0
                    if duration<0:raise ValueError('negative borrowing interval')
                    delta=duration*rate;expected=self.read('CUMULATIVE_BORROWING_FACTOR',self.market,side)+delta
                    result.update(status='matched' if delta==v['delta'] and expected==v['nextValue'] else 'mismatch',is_long=side,rate=rate,duration=duration,delta=delta,next_value=expected,observed=v)
                    self.put(expected,'CUMULATIVE_BORROWING_FACTOR',self.market,side);self.put(now,'CUMULATIVE_BORROWING_FACTOR_UPDATED_AT',self.market,side)
                elif name=='Borrowing':
                    if pending_borrow is None:raise ValueError('orphan borrowing-rate event')
                    if pending_borrow['rate']!=v['borrowingFactorPerSecond']:pending_borrow['status']='mismatch'
                    pending_borrow['observed_rate']=v['borrowingFactorPerSecond'];pending_borrow=None
                    continue
            except (KeyError,ValueError,ZeroDivisionError) as error:result['reason']=str(error)
            self.results.append(result)
            if name=='Funding':pending_funding=[];latest_funding=result
            if name=='CumulativeBorrowingFactorUpdated':
                if pending_borrow is not None:self.errors.append('missing_borrowing_rate_event')
                pending_borrow=result;latest_borrow[v['isLong']]=result
        if pending_funding or pending_borrow:self.errors.append('unfinished_accrual_group')
        specs=slots(self.metadata)
        self.closing={k:{'name':specs[k]['name'],'expected':value,'modeled':self.state[k],
                         'status':'matched' if self.state[k]==value else 'mismatch'} for k,value in self.snapshot['closing'].items()}

    def summary(self):
        counts={n:dict(Counter(r['status'] for r in self.results if r['event_name']==n)) for n in ('Funding','CumulativeBorrowingFactorUpdated')}
        complete=self.available and all(counts[n] and set(counts[n])=={'matched'} for n in counts) and bool(self.closing) and all(v['status']=='matched' for v in self.closing.values()) and not self.errors and not self.continuity_errors
        return {'available':self.available,'complete':complete,'counts':counts,'closing':self.closing,
                'continuity_errors':self.continuity_errors,'errors':self.errors,'updates':self.results}


def compare_accrual(replay,order_key,checks,errors):
    state=replay.orders.get(order_key) if replay else None
    for label,name in [('funding','independent_funding_accumulators'),('borrowing','independent_borrowing_accumulators')]:
        values=([state.get('funding')] if label=='funding' else list(state.get('borrowing',{}).values())) if state else []
        statuses=[v['status'] if v else 'unavailable' for v in values]
        expected=1 if label=='funding' else 2
        status='mismatch' if 'mismatch' in statuses else 'matched' if len(statuses)==expected and set(statuses)=={'matched'} else 'unavailable'
        checks[name]=status
        if status=='mismatch':errors.append(name+'_mismatch')


def compare_liquidation_configuration(replay,order_key,request,position,fees,impact,histories,checks,errors,referral=None,swaps=None):
    """Validate pre-settlement fees; never equate erased fees with zero settings."""
    from gmx_crypto_bot.historical_configuration import compare_historical_fees
    if request.get('orderType')!=7:return None
    state=replay.orders.get(order_key) if replay else None
    checks['historical_fee_factors_liquidation']='unavailable'
    if not state:return None
    candidate=fees;stage='collected'
    if fees['values'].get('positionFeeFactor')==0 and fees['values'].get('totalCostAmount')==0:
        candidates=[e for e in replay.fee_info.get(order_key,[]) if e['transaction_hash']==fees['transaction_hash'] and coordinate(e)<coordinate(fees) and e['values'].get('positionKey')==fees['values'].get('positionKey')]
        if not candidates or candidates[-1]['values'].get('totalCostAmount',0)==0:
            return compare_erased_liquidation(replay,state,order_key,request,position,fees,impact,histories,checks,errors,referral,swaps or [])
        candidate=candidates[-1];stage='pre_settlement_fee_info'
    values=candidate['values'];oracle=position.get('oracle_prices_at_event',{}).get(position['values'].get('collateralToken'))
    if not oracle:return {'stage':stage,'reason':'missing collateral oracle'}
    price=oracle['minPrice'];size=position['values']['sizeDeltaUsd']
    expected=liquidation_fee(size,price,state['liquidation_factor'],state['liquidation_receiver'])
    local={};local_errors=[]
    compare_historical_fees(dict(request,orderType=4),position,candidate,impact,histories,local,local_errors)
    exact=(values.get('collateralTokenPrice.min')==price and all(values.get(k,0)==v for k,v in expected.items()))
    status='mismatch' if not exact or 'mismatch' in local.values() else 'matched' if local and set(local.values())=={'matched'} else 'unavailable'
    checks['historical_fee_factors_liquidation']=status
    if status=='mismatch':errors.append('historical_fee_factors_liquidation_mismatch')
    return {'stage':stage,'coordinate':list(coordinate(candidate)),'factor':state['liquidation_factor'],
            'receiver_factor':state['liquidation_receiver'],'modeled':expected,'ordinary_fee_checks':local,
            'status':status,'settlement_verified':False}


def compare_erased_liquidation(replay,state,order_key,request,position,fees,impact,histories,checks,errors,referral,swaps):
    """Corroborate erased settings via the independently reconstructed unpaid cost.

Only full, loss-making liquidation closes at the fee-payment step are covered.
Broader liquidation collateral settlement retains its separate gate.
"""
    from gmx_crypto_bot.accrual_math import P,ceil_div
    from gmx_crypto_bot.referral import discount_amounts
    result={'stage':'erased_fee_insolvency','settlement_verified':False,'status':'unavailable'}
    try:
        v=position['values'];prev=position['pre_position'];c=coordinate(fees)
        if prev['sizeInUsd']!=v['sizeDeltaUsd'] or v['basePnlUsd']>=0:raise ValueError('unsupported erased liquidation shape')
        side=v['isLong'];token=v['collateralToken'];prices=position['oracle_prices_at_event'];price=prices[token]['minPrice']
        insolvent=[e for e in replay.insolvent[order_key] if e['transaction_hash']==fees['transaction_hash'] and coordinate(e)<c]
        if len(insolvent)!=1 or insolvent[0]['values'].get('step')!='fees':raise ValueError('missing fee-step insolvency evidence')
        if not state['funding'] or state['funding']['status']!='matched' or len(state['borrowing'])!=2 or any(r['status']!='matched' for r in state['borrowing'].values()):raise ValueError('accrual derivation not verified')
        factors={name:h.at(c) for name,h in histories.items()}
        improved=impact.get('balance_was_improved') if impact else None
        if type(improved) is not bool:raise ValueError('missing independent impact direction')
        factor=factors['position_fee_positive' if improved else 'position_fee_negative']
        position_fee=v['sizeDeltaUsd']*factor//P//price
        config=referral.at(request['account'].lower(),c)
        discount=discount_amounts(position_fee,config['code'],config['rebate_bps'],config['share_bps'],config['minimum'],config['pro_tier'],config['pro_factor'])['totalDiscountAmount']
        next_borrow=state['borrowing'][side]['next_value']
        borrowing=prev['sizeInUsd']*(next_borrow-prev['borrowingFactor'])//P//price
        next_funding=state['funding_values'][key('FUNDING_FEE_AMOUNT_PER_SIZE',replay.market,token,side)]
        funding=ceil_div(prev['sizeInUsd']*(next_funding-prev['fundingFeeAmountPerSize']),10**45)
        if min(borrowing,funding)<0:raise ValueError('negative liquidation accrual')
        liquidation=liquidation_fee(v['sizeDeltaUsd'],price,state['liquidation_factor'],state['liquidation_receiver'])
        receiver=request['uiFeeReceiver'];ui=0
        if receiver!='0x'+'0'*40:ui=v['sizeDeltaUsd']*min(factors['ui_fee:'+receiver],factors['max_ui_fee'])//P//price
        cost=position_fee+borrowing+liquidation['liquidationFeeAmount']+ui-discount
        # Base loss must itself agree with pre-position size/tokens and adverse oracle.
        index_price=prices[replay.index]['minPrice' if side else 'maxPrice']
        base=prev['sizeInTokens']*index_price-prev['sizeInUsd']
        if not side:base=-base
        if base!=v['basePnlUsd']:raise ValueError('unreconciled liquidation base loss')
        pnl_token=replay.tokens[0 if side else 1];pnl_price=prices[pnl_token]
        output=0;secondary=0
        if v['totalImpactUsd']>0:
            amount=v['totalImpactUsd']//pnl_price['maxPrice']
            if pnl_token==token:output=amount
            else:secondary=amount
        if secondary and request['decreasePositionSwapType']==1:
            hops=[e for e in swaps if e['event_name']=='SwapInfo' and coordinate(e)<coordinate(position)]
            if len(hops)!=1 or hops[0]['values']['amountIn']!=secondary or hops[0]['values']['tokenIn']!=pnl_token or hops[0]['values']['tokenOut']!=token:raise ValueError('unreconciled liquidation impact conversion')
            output=hops[0]['values']['amountOut'];secondary=0
        collateral=prev['collateralAmount']
        def pay(usd):
            nonlocal output,secondary,collateral
            needed=ceil_div(usd,price);taken=min(output,needed);output-=taken;needed-=taken
            taken=min(collateral,needed);collateral-=taken;needed-=taken
            if needed==0:return 0
            secondary_needed=needed*price//pnl_price['minPrice'];taken=min(secondary,secondary_needed);secondary-=taken
            return (secondary_needed-taken)*pnl_price['minPrice']
        if pay(funding*price) or pay(-base):raise ValueError('insolvency before fee step')
        remaining=pay(cost*price);observed=insolvent[0]['values']
        erased_fields=('positionFeeFactor','positionFeeAmount','borrowingFeeAmount','borrowingFeeReceiverFactor','positionFeeReceiverFactor','fundingFeeAmount','totalCostAmount','liquidationFeeAmount','liquidationFeeReceiverFactor','liquidationFeeAmountForFeeReceiver')
        exact=remaining>0 and remaining==observed['remainingCostUsd'] and observed['basePnlUsd']==base and observed['positionCollateralAmount']==prev['collateralAmount'] and all(fees['values'].get(n,0)==0 for n in erased_fields)
        result.update(status='matched' if exact else 'mismatch',factor=state['liquidation_factor'],receiver_factor=state['liquidation_receiver'],modeled=liquidation,
                      position_fee=position_fee,borrowing_fee=borrowing,funding_fee=funding,discount=discount,ui_fee=ui,
                      remaining_cost_usd=remaining,observed_remaining_cost_usd=observed['remainingCostUsd'])
    except (KeyError,TypeError,ValueError,ZeroDivisionError) as error:result['reason']=str(error)
    checks['historical_fee_factors_liquidation']=result['status']
    if result['status']=='mismatch':errors.append('historical_fee_factors_liquidation_mismatch')
    return result


def load_timestamps(recording,hashes):
    """Recover preserved RPC headers and tie them to execution-log block hashes."""
    wanted={};bundles=set();timestamps={}
    with (recording/'raw/manifest.jsonl').open() as stream:
        for line in stream:
            item=json.loads(line)
            if item.get('kind')!='response' or item.get('source')!='rpc-eth_getBlockByNumber':continue
            params=item.get('request',{}).get('params',[])
            if not params or not isinstance(params[0],str) or not params[0].startswith('0x'):continue
            block=int(params[0],16)
            if block in hashes:wanted[item['seq']]=block;bundles.add(item['bundle'])
    for bundle in sorted(bundles):
        with gzip.open(recording/bundle,'rt') as stream:
            for line in stream:
                item=json.loads(line)
                if item['seq'] not in wanted:continue
                response=json.loads(base64.b64decode(item['body_base64']));header=response.get('result')
                if not isinstance(header,dict):continue
                block=wanted[item['seq']]
                if int(header['number'],16)!=block or header['hash']!=hashes[block]:raise ValueError('accrual header block hash mismatch')
                timestamp=int(header['timestamp'],16)
                if block in timestamps and timestamps[block]!=timestamp:raise ValueError('conflicting raw accrual timestamps')
                timestamps[block]=timestamp
    return timestamps
