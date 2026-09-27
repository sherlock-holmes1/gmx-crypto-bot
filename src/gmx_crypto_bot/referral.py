"""Canonical referral/pro state and exact integer discount calculations.

Source rules: GMX ReferralStorage, ReferralUtils and PositionPricingUtils.
Observed fee events are comparison targets, never historical state anchors.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from gmx_crypto_bot.price_impact import config_base_key, keccak256
from gmx_crypto_bot.historical_configuration import _recorded_block_hash

P = 10**30
ZERO = '0x' + '0' * 40
ZERO_CODE = '0x' + '0' * 64
SIGNATURES = {
    'SetTraderReferralCode(address,bytes32)': ('trader', ('address', 'bytes32')),
    'SetTier(uint256,uint256,uint256)': ('tier', ('uint', 'uint', 'uint')),
    'SetReferrerTier(address,uint256)': ('affiliate_tier', ('address', 'uint')),
    'SetReferrerDiscountShare(address,uint256)': ('share', ('address', 'uint')),
    'RegisterCode(address,bytes32)': ('register', ('address', 'bytes32')),
    'SetCodeOwner(address,address,bytes32)': ('owner', ('address', 'address', 'bytes32')),
    'GovSetCodeOwner(bytes32,address)': ('gov_owner', ('bytes32', 'address')),
}
TOPICS = {'0x' + keccak256(sig.encode()).hex(): spec for sig, spec in SIGNATURES.items()}
PRO_NAMES = {'PRO_TRADER_TIER': 'pro_tier', 'PRO_DISCOUNT_FACTOR': 'pro_factor', 'MIN_AFFILIATE_REWARD_FACTOR': 'minimum'}
PRO_BASES = {config_base_key(name): field for name, field in PRO_NAMES.items()}


def word(value: int | str) -> str:
    return f'{value if isinstance(value, int) else int(value,16):064x}'


def calldata(signature: str, arg: int | str | None = None) -> str:
    return '0x' + keccak256(signature.encode())[:4].hex() + (word(arg) if arg is not None else '')


def datastore_key(name: str, arg: int | str) -> str:
    return '0x' + keccak256(bytes.fromhex(config_base_key(name)[2:] + word(arg))).hex()


def words(raw: str, count: int) -> list[int]:
    if not isinstance(raw,str) or not raw.startswith('0x') or len(raw) != 2+64*count:
        raise ValueError('invalid ABI result width')
    return [int(raw[2+i*64:2+(i+1)*64],16) for i in range(count)]


def address(value: int) -> str:
    if value < 0 or value >= 2**160:
        raise ValueError('invalid ABI address')
    return '0x'+f'{value:040x}'


def decode_referral_log(log: dict) -> tuple[str, Any, Any] | None:
    if log.get('removed'):
        raise ValueError('removed referral log')
    topics=log.get('topics',[])
    if not topics or topics[0].lower() not in TOPICS:
        return None
    if len(topics) != 1:
        raise ValueError('unexpected indexed referral event fields')
    name,types=TOPICS[topics[0].lower()]
    values=words(log['data'],len(types))
    values=[address(v) if t=='address' else '0x'+word(v) if t=='bytes32' else v for v,t in zip(values,types)]
    if name=='register':return 'code',values[1],values[0]
    if name=='owner':return 'code',values[2],values[1]
    if name=='gov_owner':return 'code',values[0],values[1]
    return name,values[0],tuple(values[1:]) if name=='tier' else values[1]


def config_change(entry: dict) -> tuple[str, Any, int] | None:
    if entry.get('event_name') != 'SetUint':return None
    v=entry['values'];field=PRO_BASES.get(v.get('baseKey'))
    if field is None:return None
    arg=words(v['data'],1)[0]
    return field,address(arg) if field=='pro_tier' else arg,v['value']


@dataclass
class History:
    opening: Any
    changes: list[tuple[tuple[int,int,int],Any]]

    def at(self, coordinate):
        index=bisect_right([c for c,_ in self.changes],coordinate)-1
        return self.changes[index][1] if index>=0 else self.opening


def discount_amounts(fee: int, code: str, rebate_bps: int, share_bps: int,
                     minimum: int, pro_tier: int, pro_factor: int) -> dict[str,int]:
    if not 0<=rebate_bps<=10000 or not 0<=share_bps<=10000:
        raise ValueError('referral tier outside basis-point bounds')
    if not 0<=pro_factor<=P or not 0<=minimum<=P or fee<0:
        raise ValueError('invalid discount factor')
    total=rebate_bps*P//10000 if code!=ZERO_CODE else 0
    # Solidity rounds the basis-point product before converting to 30 decimals.
    discount=(rebate_bps*share_bps//10000)*P//10000 if code!=ZERO_CODE else 0
    affiliate=total-discount
    effective_pro=pro_factor if pro_tier else 0
    adjusted=affiliate
    if code!=ZERO_CODE and effective_pro>discount:
        adjusted=max(minimum,total-effective_pro) if effective_pro<=total else minimum
    referral_amount=fee*discount//P
    affiliate_amount=fee*adjusted//P if code!=ZERO_CODE else 0
    pro_amount=fee*effective_pro//P
    maximum=max(referral_amount,pro_amount)
    protocol=fee-affiliate_amount-maximum
    if protocol<0:raise ValueError('discounts exceed position fee')
    return {'referral.totalRebateFactor':total,'referral.traderDiscountFactor':discount,
            'referral.adjustedAffiliateRewardFactor':adjusted,
            'referral.affiliateRewardAmount':affiliate_amount,'referral.traderDiscountAmount':referral_amount,
            'referral.totalRebateAmount':affiliate_amount+referral_amount,
            'pro.traderTier':pro_tier,'pro.traderDiscountFactor':effective_pro,
            'pro.traderDiscountAmount':pro_amount,'protocolFeeAmount':protocol,
            'totalDiscountAmount':maximum}


class ReferralState:
    def __init__(self, recording: Path, metadata: dict, config_events: list[dict]):
        self.histories: dict[tuple[str,Any],History]={}
        self.available=False
        path=recording/'liquidation-referral-configuration.json'
        if not path.exists():path=recording/'referral-configuration.json'
        if not path.exists():return
        s=json.loads(path.read_text());complete=json.loads((recording/'completeness-report.json').read_text())
        start,end=complete['source_block_range']['from'],complete['source_block_range']['to']
        if not complete.get('complete') or complete.get('gaps') or complete.get('reorgs'):
            raise ValueError('referral history requires complete recording')
        self.start,self.end=start,end
        if (s.get('schema')!='GmxReferralConfiguration' or s.get('version')!=1
            or s.get('market')!=metadata['market']['market_token_address'].lower()
            or s.get('data_store')!=metadata['contracts']['data_store'].lower()
            or s.get('order_handler')!=metadata['contracts']['order_handler'].lower()
            or s.get('opening_block')!=start-1 or s.get('end_block')!=end
            or s.get('opening_hash')!=_recorded_block_hash(recording,start-1)):
            raise ValueError('referral snapshot identity mismatch')
        raw_calls=s['calls']
        def read(to,sig,arg=None,count=1):
            key=to.lower()+':'+calldata(sig,arg)
            raw=raw_calls[key]
            return words(raw,count)
        ref=address(read(s['order_handler'],'referralStorage()')[0])
        if ref!=s.get('referral_storage') or ref==ZERO:raise ValueError('referral storage pointer mismatch')
        self.referral_storage=ref
        # Decode only call-proven anchors, never trust duplicated derived values.
        for account in s['traders']:
            self.histories['trader',account]=History('0x'+word(read(ref,'traderReferralCodes(address)',account)[0]),[])
            tier=read(s['data_store'],'getUint(bytes32)',datastore_key('PRO_TRADER_TIER',account))[0]
            self.histories['pro_tier',account]=History(tier,[])
        for code in s['codes']:
            self.histories['code',code]=History(address(read(ref,'codeOwners(bytes32)',code)[0]),[])
        for affiliate in s['affiliates']:
            self.histories['affiliate_tier',affiliate]=History(read(ref,'referrerTiers(address)',affiliate)[0],[])
            self.histories['share',affiliate]=History(read(ref,'referrerDiscountShares(address)',affiliate)[0],[])
        for tier in s['referral_tiers']:
            self.histories['tier',tier]=History(tuple(read(ref,'tiers(uint256)',tier,2)),[])
            self.histories['minimum',tier]=History(read(s['data_store'],'getUint(bytes32)',datastore_key('MIN_AFFILIATE_REWARD_FACTOR',tier))[0],[])
        for tier in s['pro_tiers']:
            self.histories['pro_factor',tier]=History(read(s['data_store'],'getUint(bytes32)',datastore_key('PRO_DISCOUNT_FACTOR',tier))[0],[])
        seen={};next_block=start
        for chunk in s['log_ranges']:
            if chunk['from']!=next_block or chunk['to']<chunk['from'] or chunk['to']>end:
                raise ValueError('referral log coverage gap or overlap')
            next_block=chunk['to']+1
            for log in chunk['logs']:
                block=int(log['blockNumber'],16)
                if log['address'].lower()!=ref or not chunk['from']<=block<=chunk['to']:
                    raise ValueError('referral log outside requested range or contract')
                if s['log_block_hashes'].get(str(block))!=log['blockHash']:
                    raise ValueError('referral log block hash mismatch')
                coordinate=(block,int(log['transactionIndex'],16),int(log['logIndex'],16))
                if coordinate in seen and seen[coordinate]!=log:raise ValueError('conflicting referral logs')
                if coordinate in seen:continue
                seen[coordinate]=log
                change=decode_referral_log(log)
                if change and (change[0],change[1]) in self.histories:
                    self.histories[change[0],change[1]].changes.append((coordinate,change[2]))
        if next_block!=end+1:raise ValueError('incomplete referral log coverage')
        for e in config_events:
            change=config_change(e)
            if change and (change[0],change[1]) in self.histories:
                coordinate=(e['block_number'],e['transaction_index'],e['log_index'])
                if start<=coordinate[0]<=end:self.histories[change[0],change[1]].changes.append((coordinate,change[2]))
        for h in self.histories.values():h.changes.sort(key=lambda item:item[0])
        self.available=True

    def at(self, account: str, coordinate: tuple) -> dict:
        if not self.available or not self.start<=coordinate[0]<=self.end:raise KeyError('referral evidence unavailable')
        def value(field,arg):return self.histories[field,arg].at(coordinate)
        code=value('trader',account);affiliate=ZERO;rebate=share=minimum=0
        if code!=ZERO_CODE:
            affiliate=value('code',code);tier=value('affiliate_tier',affiliate)
            rebate,share=value('tier',tier);minimum=value('minimum',tier)
            custom=value('share',affiliate)
            if custom:share=custom
        pro_tier=value('pro_tier',account)
        return {'code':code,'affiliate':affiliate,'rebate_bps':rebate,'share_bps':share,
                'minimum':minimum,'pro_tier':pro_tier,'pro_factor':value('pro_factor',pro_tier) if pro_tier else 0}


def compare_referral(state: ReferralState, request: dict, position: dict, fees: dict,
                     fee_factors: dict, model: dict | None, checks: dict, errors: list) -> dict | None:
    if request.get('orderType')==7:return None  # separate liquidation settlement gate
    names=('historical_referral_identity','historical_referral_discount','historical_pro_discount','historical_protocol_fee')
    def unavailable():
        for name in names:checks[name]='unavailable'
    coordinate=(fees['block_number'],fees['transaction_index'],fees['log_index'])
    if state is None:unavailable();return None
    try:s=state.at(request['account'].lower(),coordinate)
    except KeyError:unavailable();return None
    v=fees['values'];improved=model.get('balance_was_improved') if model else None
    factor=fee_factors.get('position_fee_positive' if improved else 'position_fee_negative')
    price=v.get('collateralTokenPrice.min');size=position['values'].get('sizeDeltaUsd')
    if type(improved) is not bool or factor is None or type(price) is not int or price<=0 or type(size) is not int:
        unavailable();return None
    fee=size*factor//P//price
    expected=discount_amounts(fee,s['code'],s['rebate_bps'],s['share_bps'],s['minimum'],s['pro_tier'],s['pro_factor'])
    def check(name,passed):
        checks[name]='matched' if passed else 'mismatch'
        if not passed:errors.append(name+'_mismatch')
    check(names[0],v.get('trader')==request['account'].lower() and v.get('referralCode')==s['code'] and v.get('affiliate')==s['affiliate'])
    # GMX omits conditional referral/pro event fields for zero code / zero tier.
    check(names[1],all(v.get(k,0)==n for k,n in expected.items() if k.startswith('referral.')))
    check(names[2],all(v.get(k,0)==n for k,n in expected.items() if k.startswith('pro.')))
    check(names[3],v.get('protocolFeeAmount')==expected['protocolFeeAmount'])
    return {'configuration':s,'position_fee_amount':fee,'expected':expected}
