"""Read-only opening/closing inputs for accrual and liquidation fee validation."""
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
from gmx_crypto_bot.accrual_configuration import slots,decode
from gmx_crypto_bot.swap_math import call_data


def collect(recording,rpc):
    metadata=json.loads((recording/'metadata.json').read_text());q=json.loads((recording/'completeness-report.json').read_text())
    if not q.get('complete') or q.get('gaps') or q.get('reorgs'):raise ValueError('incomplete recording')
    store=metadata['contracts']['data_store'].lower();specs=slots(metadata)
    result={'schema':'GmxAccrualConfiguration','version':1,'market':metadata['market']['market_token_address'].lower(),'data_store':store}
    for label,block in [('opening',q['source_block_range']['from']-1),('closing',q['source_block_range']['to'])]:
        expected=_recorded_block_hash(recording,block)
        if rpc.call('eth_getBlockByNumber',[hex(block),False])['hash']!=expected:raise ValueError('archive block hash mismatch')
        calls={};jobs=list(specs.items())
        for i in range(0,len(jobs),40):
            group=jobs[i:i+40]
            values=rpc.call_many('eth_call',[[{'to':store,'data':call_data('get'+v['kind']+'(bytes32)',k)},hex(block)] for k,v in group])
            if len(values)!=len(group):raise ValueError('incomplete archive batch')
            for (k,v),raw in zip(group,values):decode(raw,v['kind']);calls[k]=raw
        if rpc.call('eth_getBlockByNumber',[hex(block),False])['hash']!=expected:raise ValueError('archive block changed')
        result[label]={'block_number':block,'block_hash':expected,'calls':calls}
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('recording',type=Path);a=p.parse_args()
    endpoint=os.environ.get('GMX_ARCHIVE_RPC_URL')
    if not endpoint:p.error('set GMX_ARCHIVE_RPC_URL in this terminal')
    target=a.recording/'accrual-configuration.json'
    if target.exists():p.error('accrual-configuration.json exists; refusing overwrite')
    raw=a.recording/('accrual-backfill-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:6]);raw.mkdir()
    artifacts=RawArtifactStore(raw)
    try:
        result=collect(a.recording,PublicJsonRpc(endpoint,30,artifacts));result['raw_evidence_directory']=raw.name
        with target.open('x') as stream:stream.write(json.dumps(result,sort_keys=True,indent=2)+'\n')
    except Exception as error:
        print(f'Accrual backfill failed ({type(error).__name__}); diagnostics retained in {raw.name}.');return 1
    finally:artifacts.close()
    print(f'Accrual configuration saved: {target} ({sum(len(result[k]["calls"]) for k in ("opening","closing"))} archive reads)')
    return 0

if __name__=='__main__':raise SystemExit(main())
