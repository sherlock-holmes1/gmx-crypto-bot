"""Archive inputs for funding, borrowing, and liquidation fee configuration."""
from __future__ import annotations
import json
from gmx_crypto_bot.swap_math import key,call_data
from gmx_crypto_bot.historical_configuration import _recorded_block_hash

FUNDING_CONFIG=('FUNDING_FACTOR','FUNDING_EXPONENT_FACTOR','FUNDING_INCREASE_FACTOR_PER_SECOND',
 'FUNDING_DECREASE_FACTOR_PER_SECOND','MIN_FUNDING_FACTOR_PER_SECOND','MAX_FUNDING_FACTOR_PER_SECOND',
 'THRESHOLD_FOR_STABLE_FUNDING','THRESHOLD_FOR_DECREASE_FUNDING')
BORROWING_CONFIG=('BORROWING_FACTOR','BORROWING_EXPONENT_FACTOR','OPTIMAL_USAGE_FACTOR',
 'BASE_BORROWING_FACTOR','ABOVE_OPTIMAL_USAGE_BORROWING_FACTOR','OPEN_INTEREST_RESERVE_FACTOR')
BOOL_CONFIG=('SKIP_BORROWING_FEE_FOR_SMALLER_SIDE','USE_OPEN_INTEREST_IN_TOKENS_FOR_BALANCE')


def slots(metadata):
    market=metadata['market']['market_token_address'].lower()
    tokens=[metadata['tokens'][t]['address'].lower() for t in ('long','short')]
    if tokens[0]==tokens[1]:raise ValueError('single-token accrual market unsupported')
    result={}
    def add(kind,name,*args):result[key(name,*args)]={'kind':kind,'name':name,'args':list(args)}
    for name in FUNDING_CONFIG+('LIQUIDATION_FEE_FACTOR',):add('Uint',name,market)
    add('Uint','LIQUIDATION_FEE_RECEIVER_FACTOR')
    for name in BOOL_CONFIG:add('Bool',name)
    add('Int','SAVED_FUNDING_FACTOR_PER_SECOND',market)
    add('Uint','FUNDING_UPDATED_AT',market)
    for side in (True,False):
        for name in BORROWING_CONFIG+('CUMULATIVE_BORROWING_FACTOR','CUMULATIVE_BORROWING_FACTOR_UPDATED_AT'):add('Uint',name,market,side)
        for token in tokens:
            for name in ('OPEN_INTEREST','OPEN_INTEREST_IN_TOKENS','FUNDING_FEE_AMOUNT_PER_SIZE','CLAIMABLE_FUNDING_AMOUNT_PER_SIZE'):add('Uint',name,market,token,side)
    for token in tokens:add('Uint','POOL_AMOUNT',market,token)
    return result


def decode(raw,kind):
    if not isinstance(raw,str) or len(raw)!=66 or not raw.startswith('0x'):raise ValueError('invalid archive ABI word')
    value=int(raw,16)
    if kind=='Int' and value>=2**255:value-=2**256
    if kind=='Bool' and value not in (0,1):raise ValueError('invalid archive boolean')
    return value


def load_snapshot(recording,metadata):
    path=recording/'accrual-configuration.json'
    if not path.exists():return None
    s=json.loads(path.read_text());q=json.loads((recording/'completeness-report.json').read_text())
    if not q.get('complete') or q.get('gaps') or q.get('reorgs'):raise ValueError('incomplete accrual recording')
    if s.get('schema')!='GmxAccrualConfiguration' or s.get('version')!=1 or s.get('market')!=metadata['market']['market_token_address'].lower() or s.get('data_store')!=metadata['contracts']['data_store'].lower():raise ValueError('accrual snapshot identity mismatch')
    specs=slots(metadata);points={}
    for label,block in [('opening',q['source_block_range']['from']-1),('closing',q['source_block_range']['to'])]:
        point=s[label]
        if point['block_number']!=block or point['block_hash']!=_recorded_block_hash(recording,block):raise ValueError('accrual snapshot block mismatch')
        if set(point['calls'])!=set(specs):raise ValueError('accrual snapshot storage coverage mismatch')
        points[label]={k:decode(point['calls'][k],v['kind']) for k,v in specs.items()}
    return points
