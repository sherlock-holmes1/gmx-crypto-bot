"""Read-only opening swap configuration/state for all recorded route markets."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
from uuid import uuid4
from gmx_crypto_bot.artifacts import RawArtifactStore
from gmx_crypto_bot.collector import PublicJsonRpc
from gmx_crypto_bot.historical_configuration import _recorded_block_hash
from gmx_crypto_bot.referral import address,words
from gmx_crypto_bot.swap_math import call_data,key,market_keys,ZERO_ID,MARKET_FIELDS,market_field_key


def collect(recording,rpc):
    m=json.loads((recording/'metadata.json').read_text());quality=json.loads((recording/'completeness-report.json').read_text())
    if not quality.get('complete') or quality.get('gaps') or quality.get('reorgs'):raise ValueError('incomplete recording')
    block=quality['source_block_range']['from']-1;expected_hash=_recorded_block_hash(recording,block)
    if rpc.call('eth_getBlockByNumber',[hex(block),False])['hash']!=expected_hash:raise ValueError('opening hash mismatch')
    report=json.loads((recording/'order-validation.json').read_text());events=[e for o in report['orders'] for e in o.get('observed_swap_events',[])]
    markets=sorted({e['values']['market'] for e in events if e['event_name']=='SwapInfo'})
    receivers=sorted({e['values']['uiFeeReceiver'] for e in events if e['event_name']=='SwapFeesCollected'})
    store=m['contracts']['data_store'].lower();calls={}
    def batch(jobs):
        jobs=list(dict.fromkeys((to.lower(),data) for to,data in jobs if to.lower()+':'+data not in calls))
        for i in range(0,len(jobs),50):
            group=jobs[i:i+50]
            results=rpc.call_many('eth_call',[[{'to':to,'data':data},hex(block)] for to,data in group])
            if len(results)!=len(group):raise ValueError('incomplete archive batch')
            for (to,data),result in zip(group,results):
                if not isinstance(result,str) or not result.startswith('0x') or len(result)<66 or (len(result)-2)%64:raise ValueError('invalid archive result')
                calls[to+':'+data]=result
    batch([(store,call_data('getAddress(bytes32)',market_field_key(market,field))) for market in markets for field in MARKET_FIELDS]+
          [(store,call_data('getBytes32(bytes32)',key('VIRTUAL_MARKET_ID',market))) for market in markets])
    tokens={};virtual_ids=set()
    jobs=[(store,call_data('getUint(bytes32)',key(name))) for name in ('SWAP_FEE_RECEIVER_FACTOR','MAX_UI_FEE_FACTOR')]
    for market in markets:
        props=[address(words(calls[store+':'+call_data('getAddress(bytes32)',market_field_key(market,field))],1)[0]) for field in MARKET_FIELDS]
        if props[0]!=market or props[2]==props[3]:raise ValueError('invalid or single-token swap market')
        tokens[market]=props[2:]
        virtual=calls[store+':'+call_data('getBytes32(bytes32)',key('VIRTUAL_MARKET_ID',market))]
        words(virtual,1)
        if virtual!=ZERO_ID:virtual_ids.add(virtual)
        jobs.extend((store,call_data('getUint(bytes32)',k)) for k in market_keys(market).values())
        for token in tokens[market]:
            jobs.extend((store,call_data('getUint(bytes32)',key(name,market,token))) for name in ('POOL_AMOUNT','SWAP_IMPACT_POOL_AMOUNT'))
    jobs.extend((store,call_data('getUint(bytes32)',key('UI_FEE_FACTOR',receiver))) for receiver in receivers)
    jobs.extend((store,call_data('getUint(bytes32)',key('VIRTUAL_INVENTORY_FOR_SWAPS',vid,side))) for vid in sorted(virtual_ids) for side in (True,False))
    batch(jobs)
    if rpc.call('eth_getBlockByNumber',[hex(block),False])['hash']!=expected_hash:raise ValueError('opening hash changed during backfill')
    return {'schema':'GmxSwapOpeningState','version':1,'opening_block':block,'opening_hash':expected_hash,
            'market':m['market']['market_token_address'].lower(),'data_store':store,
            'markets':markets,'receivers':receivers,'calls':calls}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('recording',type=Path);a=p.parse_args()
    endpoint=os.environ.get('GMX_ARCHIVE_RPC_URL')
    if not endpoint:p.error('set GMX_ARCHIVE_RPC_URL in this terminal')
    target=a.recording/'swap-opening-state.json'
    if target.exists():p.error('swap-opening-state.json already exists; refusing overwrite')
    raw=a.recording/('swap-backfill-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:6]);raw.mkdir()
    artifacts=RawArtifactStore(raw)
    try:
        result=collect(a.recording,PublicJsonRpc(endpoint,30,artifacts));result['raw_evidence_directory']=raw.name
        with target.open('x') as stream:stream.write(json.dumps(result,sort_keys=True,indent=2)+'\n')
    except Exception as error:
        print(f'Swap backfill failed ({type(error).__name__}); diagnostics retained in {raw.name}.');return 1
    finally:artifacts.close()
    print(f'Swap opening state saved: {target} ({len(result["markets"])} markets, {len(result["calls"])} archive reads)')
    return 0


if __name__=='__main__':raise SystemExit(main())
