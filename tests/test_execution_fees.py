"""Execution-fee evidence boundaries and reverted-transfer behavior."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from gmx_crypto_bot.execution_fees.models import PAY_SELECTOR, PaymentInput, walk_trace
from gmx_crypto_bot.execution_fees.backfill import collect_transaction, select_transactions

A = '0x' + '11' * 20
B = '0x' + '22' * 20
H = '0x' + '33' * 32
K = '0x' + '44' * 32


def calldata():
    values = [int(A,16)] * 4 + [int(K,16), 0, 1000, 100000, 2, int(A,16), int(B,16), 0]
    return PAY_SELECTOR + ''.join(format(x, '064x') for x in values)


class ExecutionFeeTests(unittest.TestCase):
    def test_payment_library_selector_matches_deployed_contract(self):
        self.assertEqual(PAY_SELECTOR, '0xfd48e65e')

    def test_payment_abi_preserves_order_and_gas_inputs(self):
        payment = PaymentInput.decode(calldata())
        self.assertEqual(payment.order_key, K)
        self.assertEqual((payment.execution_fee, payment.starting_gas, payment.oracle_price_count), (1000,100000,2))
        self.assertEqual(payment.refund_receiver, B)
        with self.assertRaises(ValueError):
            PaymentInput.decode(calldata() + '00')

    def test_reverted_parent_invalidates_successful_descendant(self):
        trace = {'calls': [{'error':'reverted', 'calls':[{'type':'CALL','to':A,'value':'0x64'}]}, {'type':'CALL','to':B,'value':'0x64'}]}
        frames = list(walk_trace(trace))
        self.assertFalse(frames[2].committed)
        self.assertTrue(frames[3].committed)
        self.assertEqual(frames[3].path,(1,))

    def test_backfill_binds_receipt_transaction_trace_and_library_code(self):
        class RPC:
            def call(self, method, params):
                if method=='eth_getTransactionReceipt':
                    return dict(transactionHash=H,blockHash=K,blockNumber='0xa',status='0x1')
                if method=='eth_getTransactionByHash':
                    return dict(hash=H,blockHash=K,blockNumber='0xa',to=A,input='0x1234',**{'from':B})
                if method=='debug_traceTransaction':
                    return dict(to=A,input='0x1234',calls=[dict(type='DELEGATECALL',to=A,input=calldata())],**{'from':B})
                if method=='eth_getCode':return '0x6000'
                if method=='eth_getBlockByNumber':return {'hash':K}
                raise AssertionError(method)
        result=collect_transaction(RPC(),H,10,K)
        self.assertEqual(result['payments'][0]['input']['order_key'],K)
        self.assertEqual(result['library_code'],{A:'0x6000'})
        with self.assertRaisesRegex(ValueError,'identity mismatch'):
            collect_transaction(RPC(),H,10,'0xwrong')

    def test_selection_deduplicates_batched_transactions(self):
        order={'checks':{'execution_fee_event_balance':'matched'},'terminal':{'event_name':'OrderExecuted','values':{},'transaction_hash':H},'final_request':{'orderType':2}}
        report={'orders':[order,order]}
        self.assertEqual(select_transactions(report,0),[H])

from gmx_crypto_bot.execution_fees.models import TraceFrame
from gmx_crypto_bot.execution_fees.proof import GasSettings, transfer_candidates, settings_from_calls, GET_UINT_SELECTOR, SETTING_NAMES
from gmx_crypto_bot.price_impact import config_base_key

class ExecutionProofTests(unittest.TestCase):
    def test_gas_quote_uses_integer_multiplier_and_deposit_cap(self):
        settings = GasSettings(100, 20, 15 * 10**29)
        self.assertEqual(settings.keeper_payment(101,2,10,10000),2910)
        self.assertEqual(settings.keeper_payment(101,2,10,2000),2000)
        with self.assertRaises(ValueError):
            settings.keeper_payment(-1,2,10,2000)

    def test_settings_require_all_three_successful_calls(self):
        calls = [dict(type='STATICCALL',to=A,input=GET_UINT_SELECTOR+config_base_key(name)[2:],output='0x'+format(value,'064x')) for name,value in zip(SETTING_NAMES,[100,20,10**30])]
        root=TraceFrame((),{'calls':calls},True)
        self.assertEqual(settings_from_calls(root,PaymentInput.decode(calldata())),GasSettings(100,20,10**30))
        calls[0]['error']='reverted'
        with self.assertRaises(ValueError):settings_from_calls(root,PaymentInput.decode(calldata()))

    def test_transfers_reject_reverts_and_delegatecall_value(self):
        calls=[dict(type='DELEGATECALL',to=B,value='0x64',**{'from':A}),dict(type='CALL',to=B,value='0x64',error='reverted',**{'from':A}),dict(type='CALL',to=B,value='0x64',**{'from':A})]
        result=transfer_candidates(TraceFrame((4,),{'calls':calls},True),A,B,100,'0x'+'55'*20)
        self.assertEqual(result,[{'path':[4,2],'kind':'native','amount':100}])
        self.assertEqual(transfer_candidates(TraceFrame((4,),{'calls':calls},False),A,B,100,A),[])
