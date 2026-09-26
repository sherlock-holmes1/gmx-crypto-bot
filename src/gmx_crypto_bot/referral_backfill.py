"""Read-only referral/pro opening state and complete referral change history."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from gmx_crypto_bot.artifacts import RawArtifactStore
from gmx_crypto_bot.collector import PublicJsonRpc, SourceError
from gmx_crypto_bot.event_decoder import decode_event_log, event_name_from_data
from gmx_crypto_bot.historical_configuration import _recorded_block_hash
from gmx_crypto_bot.referral import (ZERO, ZERO_CODE, TOPICS, PRO_BASES, address, calldata,
                                    config_change, datastore_key, decode_referral_log, word, words)


def collect_referral_ranges(rpc, contract: str, start: int, end: int, chunk_size: int=50000) -> list[dict]:
    def fetch(low,high):
        try:
            logs=rpc.call('eth_getLogs',[{'address':contract,'fromBlock':hex(low),'toBlock':hex(high),'topics':[list(TOPICS)]}])
        except SourceError as error:
            message=str(error).lower()
            if low==high or not any(s in message for s in ('range','limit','too many','response size','query returned')):raise
            middle=(low+high)//2
            return fetch(low,middle)+fetch(middle+1,high)
        if not isinstance(logs,list):raise ValueError('invalid referral log response')
        return [{'from':low,'to':high,'logs':logs}]
    ranges=[]
    for low in range(start,end+1,chunk_size):ranges.extend(fetch(low,min(end,low+chunk_size-1)))
    return ranges


def collect(recording: Path, archive, logs_rpc, *, progress=print) -> dict:
    m=json.loads((recording/'metadata.json').read_text());report=json.loads((recording/'completeness-report.json').read_text())
    if not report.get('complete') or report.get('gaps') or report.get('reorgs'):raise ValueError('recording incomplete')
    start,end=report['source_block_range']['from'],report['source_block_range']['to'];opening=start-1
    expected_hash=_recorded_block_hash(recording,opening)
    initial_headers=archive.call_many('eth_getBlockByNumber',[[hex(opening),False],[hex(end),False]])
    if initial_headers[0]['hash']!=expected_hash:raise ValueError('opening archive hash mismatch')
    if [h['hash'] for h in logs_rpc.call_many('eth_getBlockByNumber',[[hex(opening),False],[hex(end),False]])]!=[h['hash'] for h in initial_headers]:
        raise ValueError('log/archive provider boundary hashes disagree')
    handler=m['contracts']['order_handler'].lower();store=m['contracts']['data_store'].lower();calls={}
    def batch(jobs):
        missing=list(dict.fromkeys((to.lower(),calldata(sig,arg)) for to,sig,arg in jobs if to.lower()+':'+calldata(sig,arg) not in calls))
        for offset in range(0,len(missing),50):
            group=missing[offset:offset+50]
            results=archive.call_many('eth_call',[[{'to':to,'data':data},hex(opening)] for to,data in group])
            for (to,data),result in zip(group,results):
                if not isinstance(result,str) or not result.startswith('0x') or len(result)<66 or (len(result)-2)%64:
                    raise ValueError('invalid archive ABI result')
                calls[to+':'+data]=result
    def read(to,sig,arg=None,count=1):return words(calls[to.lower()+':'+calldata(sig,arg)],count)
    batch([(handler,'referralStorage()',None)])
    ref=address(read(handler,'referralStorage()')[0])
    if ref==ZERO:raise ValueError('zero referral storage pointer')
    validation=json.loads((recording/'order-validation.json').read_text())
    traders={o['final_request']['account'].lower() for o in validation['orders'] if o.get('terminal',{} ) and o['terminal']['event_name']=='OrderExecuted' and o['final_request'].get('orderType')!=7}
    progress(f'Collecting referral contract changes for {len(traders)} traders; read-only RPC.')
    ranges=collect_referral_ranges(logs_rpc,ref,start,end)
    changes=[];hashes={}
    for chunk in ranges:
        for log in chunk['logs']:
            if log['address'].lower()!=ref or not chunk['from']<=int(log['blockNumber'],16)<=chunk['to']:raise ValueError('referral log outside range')
            change=decode_referral_log(log)
            if change:changes.append(change)
            block=str(int(log['blockNumber'],16))
            if block in hashes and hashes[block]!=log['blockHash']:raise ValueError('conflicting log block hashes')
            hashes[block]=log['blockHash']
    blocks=sorted(map(int,hashes))
    for offset in range(0,len(blocks),50):
        group=blocks[offset:offset+50];headers=archive.call_many('eth_getBlockByNumber',[[hex(b),False] for b in group])
        if any(h['hash']!=hashes[str(b)] for b,h in zip(group,headers)):raise ValueError('referral log/archive block mismatch')
    progress('Reading opening trader codes and pro tiers; checking preserved DataStore writes.')
    batch([(ref,'traderReferralCodes(address)',a) for a in sorted(traders)]+[(store,'getUint(bytes32)',datastore_key('PRO_TRADER_TIER',a)) for a in sorted(traders)])
    codes={'0x'+word(read(ref,'traderReferralCodes(address)',a)[0]) for a in traders}
    codes.update(value for field,arg,value in changes if field=='trader' and arg in traders);codes.discard(ZERO_CODE)
    batch([(ref,'codeOwners(bytes32)',c) for c in sorted(codes)])
    affiliates={address(read(ref,'codeOwners(bytes32)',c)[0]) for c in codes}
    affiliates.update(value for field,arg,value in changes if field=='code' and arg in codes)
    batch([(ref,sig,a) for a in sorted(affiliates) for sig in ['referrerTiers(address)','referrerDiscountShares(address)']])
    tiers={read(ref,'referrerTiers(address)',a)[0] for a in affiliates}
    tiers.update(value for field,arg,value in changes if field=='affiliate_tier' and arg in affiliates)
    pro_tiers={read(store,'getUint(bytes32)',datastore_key('PRO_TRADER_TIER',a))[0] for a in traders}
    # Global pro-tier writes were retained in raw evidence, not the market-only replay.
    from gmx_crypto_bot.validator import _iter_raw_event_emitter_logs
    for log in _iter_raw_event_emitter_logs(recording):
        if event_name_from_data(log.get('data',''))!='SetUint':continue
        decoded=decode_event_log(log['data'])
        if decoded.values.get('baseKey') not in PRO_BASES:continue
        change=config_change({'event_name':'SetUint','values':decoded.values})
        if change and change[0]=='pro_tier' and change[1] in traders:pro_tiers.add(change[2])
    pro_tiers.discard(0)
    batch([(ref,'tiers(uint256)',tier) for tier in sorted(tiers)]+
          [(store,'getUint(bytes32)',datastore_key('MIN_AFFILIATE_REWARD_FACTOR',tier)) for tier in sorted(tiers)]+
          [(store,'getUint(bytes32)',datastore_key('PRO_DISCOUNT_FACTOR',tier)) for tier in sorted(pro_tiers)])
    final_headers=archive.call_many('eth_getBlockByNumber',[[hex(opening),False],[hex(end),False]])
    if [h['hash'] for h in final_headers]!=[h['hash'] for h in initial_headers]:raise ValueError('archive boundary changed during backfill')
    progress(f'Archive evidence complete: {len(calls)} calls, {sum(len(c["logs"]) for c in ranges)} referral change logs.')
    return {'schema':'GmxReferralConfiguration','version':1,'market':m['market']['market_token_address'].lower(),
            'data_store':store,'order_handler':handler,'referral_storage':ref,'opening_block':opening,'end_block':end,
            'opening_hash':expected_hash,'end_hash':initial_headers[1]['hash'],'calls':calls,'log_ranges':ranges,'log_block_hashes':hashes,
            'traders':sorted(traders),'codes':sorted(codes),'affiliates':sorted(affiliates),'referral_tiers':sorted(tiers),'pro_tiers':sorted(pro_tiers)}


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('recording',type=Path);args=parser.parse_args()
    endpoint=os.environ.get('GMX_ARCHIVE_RPC_URL')
    if not endpoint:parser.error('set GMX_ARCHIVE_RPC_URL in this terminal')
    target=args.recording/'referral-configuration.json'
    if target.exists():parser.error('referral-configuration.json already exists; refusing overwrite')
    evidence=args.recording/('referral-backfill-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:6])
    evidence.mkdir();artifacts=RawArtifactStore(evidence)
    try:
        archive=PublicJsonRpc(endpoint,30,artifacts)
        logs_rpc=PublicJsonRpc(os.environ.get('GMX_LOGS_RPC_URL','https://arb1.arbitrum.io/rpc'),30,artifacts)
        result=collect(args.recording,archive,logs_rpc)
        result['raw_evidence_directory']=evidence.name
        with target.open('x') as stream:stream.write(json.dumps(result,indent=2,sort_keys=True)+'\n')
    except Exception as error:
        # Do not print provider exceptions or a credential-bearing URL.
        print(f'Referral backfill failed ({type(error).__name__}); raw evidence retained in {evidence.name}.')
        return 1
    finally:artifacts.close()
    print(f'Referral configuration saved: {target}')
    return 0


if __name__=='__main__':raise SystemExit(main())
