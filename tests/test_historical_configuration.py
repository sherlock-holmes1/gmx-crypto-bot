from __future__ import annotations
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from gmx_crypto_bot.historical_configuration import ConfigHistory, fee_keys, build_fee_histories, compare_historical_fees, ZERO_ADDRESS
from gmx_crypto_bot.price_impact import FLOAT_PRECISION as P, predict_current_impact
from gmx_crypto_bot.fee_config_backfill import fetch_opening_fee_configuration

MARKET = '0x' + '11' * 20
STORE = '0x' + '22' * 20

class HistoricalConfigurationTests(unittest.TestCase):
    def test_no_lookahead_same_block_and_bounds(self):
        h=ConfigHistory(10,20,(((12,1,4),9),((12,2,1),11)),5,11)
        self.assertIsNone(h.at((9,0,0)))
        self.assertEqual(h.at((12,1,3)),5)
        self.assertEqual(h.at((12,1,4)),9)
        self.assertEqual(h.at((12,2,0)),9)
        self.assertEqual(h.at((12,2,1)),11)
        self.assertIsNone(h.at((21,0,0)))
        self.assertIsNone(ConfigHistory(10,20,(((12,1,4),9),),None,9).at((11,0,0)))
        self.assertEqual(ConfigHistory(10,20,(),None,9).at((10,0,0)),9)
        with self.assertRaises(ValueError): ConfigHistory(10,20,(((12,1,4),9),),5,10)

    def fixture(self, root):
        metadata={'market':{'market_token_address':MARKET},'contracts':{'data_store':STORE},
                  'pinned_configuration_anchor_block':20,
                  'pinned_configuration_raw':{'position_fee_factor_for_balance_was_improved':'4','position_fee_factor_for_balance_was_not_improved':'6'}}
        report={'complete':True,'gaps':[],'reorgs':[],'source_block_range':{'from':10,'to':20}}
        (root/'metadata.json').write_text(json.dumps(metadata))
        (root/'completeness-report.json').write_text(json.dumps(report))
        (root/'events.jsonl').write_text(json.dumps({'kind':'opening_state_checkpoint','payload':{'block_number':9,'block_hash':'0xhash'}})+'\n')
        return metadata,report

    def test_only_complete_interval_allows_terminal_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m,r=self.fixture(root)
            histories=build_fee_histories(root,m,[],set())
            self.assertEqual(histories['position_fee_positive'].at((10,0,0)),4)
            self.assertIsNone(histories['position_fee_receiver'].at((10,0,0)))
            m['pinned_configuration_anchor_block']=21
            self.assertIsNone(build_fee_histories(root,m,[],set())['position_fee_positive'].at((10,0,0)))
            r['gaps']=[{'from':12,'to':13}];(root/'completeness-report.json').write_text(json.dumps(r))
            self.assertEqual(build_fee_histories(root,m,[],set()),{})

    def test_opening_archive_provenance_and_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m,r=self.fixture(root);calls=[]
            def rpc(method,params):
                calls.append((method,params))
                return {'hash':'0xhash'} if method=='eth_getBlockByNumber' else '0x'+f'{7:064x}'
            snapshot=fetch_opening_fee_configuration(root,rpc)
            self.assertEqual(len(snapshot['opening']['values']),5)
            self.assertEqual(sum(x[0]=='eth_getBlockByNumber' for x in calls),2)
            self.assertTrue(all(p[-1]=='0x9' for method,p in calls if method=='eth_call'))
            m['pinned_configuration_raw']={}
            path=root/'fee-opening-configuration.json';path.write_text(json.dumps(snapshot))
            self.assertEqual(build_fee_histories(root,m,[],set())['position_fee_receiver'].at((10,0,0)),7)
            snapshot['opening']['values']['position_fee_receiver']['value']=8;path.write_text(json.dumps(snapshot))
            with self.assertRaises(ValueError):build_fee_histories(root,m,[],set())
            with self.assertRaises(ValueError):fetch_opening_fee_configuration(root,lambda *args:{'hash':'wrong'})

    def test_global_and_ui_changes_are_versioned(self):
        receiver='0x'+'33'*20
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m,_=self.fixture(root);key=fee_keys(MARKET,{receiver})['position_fee_receiver']
            events=[{'event_name':'SetUint','block_number':11,'transaction_index':0,'log_index':2,'values':{'baseKey':key.base_key,'data':'0x','value':8}},
                    {'event_name':'UiFeeFactorUpdated','block_number':12,'transaction_index':0,'log_index':2,'values':{'account':receiver,'uiFeeFactor':3}}]
            histories=build_fee_histories(root,m,events,{receiver})
            self.assertIsNone(histories['position_fee_receiver'].at((10,0,0)))
            self.assertEqual(histories['position_fee_receiver'].at((11,0,3)),8)
            self.assertEqual(histories['ui_fee:'+receiver].at((12,0,3)),3)

    def test_fee_selection_uses_balance_not_impact_sign(self):
        histories={k:ConfigHistory(10,20,(),v,v) for k,v in {'position_fee_positive':P//100,'position_fee_negative':P//50}.items()}
        position={'values':{'sizeDeltaUsd':1000*P}}
        fees={'block_number':12,'transaction_index':0,'log_index':1,'values':{'positionFeeFactor':P//100,'positionFeeAmount':10,'collateralTokenPrice.min':P,'uiFeeReceiverFactor':0,'uiFeeAmount':0}}
        checks={};errors=[]
        compare_historical_fees({'uiFeeReceiver':ZERO_ADDRESS},position,fees,{'balance_was_improved':True,'selected_impact_usd':-5},histories,checks,errors)
        self.assertEqual(checks['historical_position_fee_amount'],'matched')
        self.assertEqual(checks['historical_position_fee_receiver_factor'],'unavailable')
        fees['values']['positionFeeAmount']=11
        compare_historical_fees({'uiFeeReceiver':ZERO_ADDRESS},position,fees,{'balance_was_improved':True},histories,{},errors)
        self.assertIn('historical_position_fee_amount_mismatch',errors)

    def test_virtual_curve_supplies_selected_balance_flag(self):
        factors={'position_impact_factor_positive':P//10,'position_impact_factor_negative':P//5,'position_impact_exponent_factor_positive':P,'position_impact_exponent_factor_negative':P}
        # Market crossover improves imbalance but remains negative because the
        # adverse coefficient is larger. Virtual inventory worsens instead.
        result=predict_current_impact(200,100,500,180,False,P,P,factors)
        self.assertLess(result['virtual_impact_usd'],result['market_impact_usd'])
        self.assertFalse(result['balance_was_improved'])

    def test_liquidation_and_missing_configuration_do_not_pass(self):
        checks={}
        compare_historical_fees({'orderType':7},{'values':{}},{'block_number':12,'transaction_index':0,'log_index':1,'values':{}},None,{},checks,[])
        self.assertEqual(checks,{'historical_fee_factors_liquidation':'unavailable'})

if __name__=='__main__':unittest.main()
