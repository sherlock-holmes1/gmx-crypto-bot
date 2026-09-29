from gmx_crypto_bot_v2.evidence.traces import TraceRepository

"""Liquidation cost ordering, fail-closed evidence, and exact payout regressions."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from gmx_crypto_bot_v2.checks.liquidation import (
    MULTICHAIN_VAULT,
    compare_liquidation_settlement,
)
from gmx_crypto_bot_v2.checks.payouts import verify_native
from gmx_crypto_bot_v2.models.settlement import Cash, settle

M, A, T, W = "market", "account", "usdc", "weth"


class SettlementMathTests(unittest.TestCase):
    def test_solvent_payment_order_and_release(self):
        result = settle(Cash(100, 20, 0, 1, 1), -30, -5, 2, 3, 10)
        self.assertEqual(result["cash"]["output"], 70)
        self.assertEqual(
            [p["step"] for p in result["payments"]],
            ["funding", "pnl", "fees", "impact", "diff"],
        )
        self.assertFalse(result["fees_erased"])
        self.assertIsNone(result["insolvent_step"])

    def test_each_insolvency_step_stops_later_costs(self):
        cases = [
            ("funding", 11, -10, 10, -10, 10),
            ("pnl", 1, -11, 10, -10, 10),
            ("fees", 1, -1, 10, -10, 10),
            ("impact", 1, -1, 1, -10, 10),
            ("diff", 1, -1, 1, -1, 10),
        ]
        for step, funding, base, fee, impact, diff in cases:
            with self.subTest(step=step):
                result = settle(Cash(10, 0, 0, 1, 1), base, impact, diff, funding, fee)
                self.assertEqual(result["insolvent_step"], step)
                self.assertEqual(result["payments"][-1]["step"], step)
                self.assertTrue(result["fees_erased"])
                self.assertEqual(result["cash"]["output"], 0)

    def test_secondary_cost_conversion_rounds_down(self):
        cash = Cash(0, 0, 10, 3, 2)
        payment = cash.pay(4)
        self.assertEqual(payment["paid_in_secondary"], 3)
        self.assertEqual(cash.secondary, 7)
        self.assertEqual(payment["remaining_cost_usd"], 0)

    def test_secondary_fee_payment_erases_fees_without_insolvency(self):
        result = settle(Cash(0, 0, 20, 1, 1), 0, 0, 0, 0, 5)
        self.assertTrue(result["fees_erased"])
        self.assertIsNone(result["insolvent_step"])
        self.assertEqual(result["cash"]["secondary"], 15)

    def test_rounding_dust_is_not_insolvency(self):
        result = settle(Cash(0, 0, 0, 1, 10), 0, 0, 0, 1, 0)
        self.assertIsNone(result["insolvent_step"])


class ComparisonTests(unittest.TestCase):
    def fixture(self):
        req = dict(
            market=M,
            receiver=A,
            account=A,
            initialCollateralDeltaAmount=0,
            decreasePositionSwapType=1,
            srcChainId=0,
            shouldUnwrapNativeToken=False,
        )
        pos = dict(
            block_number=1,
            transaction_index=0,
            log_index=10,
            transaction_hash="tx",
            values=dict(
                collateralToken=T,
                isLong=False,
                basePnlUsd=-30,
                uncappedBasePnlUsd=-30,
                totalImpactUsd=-5,
                sizeDeltaUsd=100,
                sizeInUsd=0,
                sizeInTokens=0,
                collateralAmount=0,
                collateralDeltaAmount=100,
            ),
            pre_position=dict(collateralAmount=100, sizeInUsd=100),
            oracle_prices_at_event={T: dict(minPrice=1, maxPrice=1)},
        )
        fees = dict(
            block_number=1,
            transaction_index=0,
            log_index=9,
            values=dict(totalCostAmount=13, fundingFeeAmount=3),
        )
        checks = {
            n: "matched"
            for n in (
                "historical_fee_factors_liquidation",
                "independent_funding_accumulators",
                "independent_borrowing_accumulators",
                "independent_price_impact",
                "uncapped_pnl",
                "proportional_pending_impact",
                "position_size_usd",
                "position_size_tokens",
                "decrease_tokens",
            )
        }
        payout = dict(
            event_name="ERC20Transfer", token=T, amount=52, **{"from": M, "to": A}
        )
        return dict(
            order_key="order",
            request=req,
            position=pos,
            fees=fees,
            swaps=[],
            payouts=[payout],
            metadata=dict(tokens=dict(short=dict(address=T), index=dict(address=W))),
            replay=SimpleNamespace(insolvent={}),
            impact={},
            histories={},
            referral=None,
            checks=checks,
            errors=[],
            recording=None,
            receipt=dict(payload=dict(status="0x1")),
            payout_receipt_available=True,
        )

    def run_comparison(self, args):
        with patch(
            "gmx_crypto_bot_v2.checks.liquidation.reconstruct_fees",
            return_value=dict(totalCostAmount=13, fundingFeeAmount=3),
        ):
            return compare_liquidation_settlement(**args)

    def test_exact_solvent_close(self):
        args = self.fixture()
        self.assertEqual(self.run_comparison(args)["status"], "matched")

    def test_tampered_amount_receiver_source_and_collateral_fail(self):
        for field, value in [("amount", 51), ("to", "attacker"), ("from", "attacker")]:
            args = self.fixture()
            args["payouts"][0][field] = value
            self.assertEqual(self.run_comparison(args)["status"], "mismatch")
        args = self.fixture()
        args["position"]["values"]["collateralAmount"] = 1
        self.assertEqual(self.run_comparison(args)["status"], "mismatch")

    def test_missing_receipt_does_not_prove_zero_payout(self):
        args = self.fixture()
        args["payout_receipt_available"] = False
        args["payouts"] = []
        self.assertEqual(self.run_comparison(args)["status"], "unavailable")

    def test_missing_economic_anchor_is_unavailable(self):
        args = self.fixture()
        del args["checks"]["independent_funding_accumulators"]
        self.assertEqual(self.run_comparison(args)["status"], "unavailable")

    def test_erased_fee_insolvency_and_wrong_unpaid_balance(self):
        args = self.fixture()
        args["position"]["pre_position"]["collateralAmount"] = 35
        args["position"]["values"]["collateralDeltaAmount"] = 35
        args["fees"]["values"] = dict(totalCostAmount=0, fundingFeeAmount=0)
        args["payouts"] = []
        event = dict(
            transaction_hash="tx",
            block_number=1,
            transaction_index=0,
            log_index=8,
            values=dict(
                step="fees",
                remainingCostUsd=8,
                basePnlUsd=-30,
                positionCollateralAmount=35,
            ),
        )
        args["replay"].insolvent = {"order": [event]}
        result = self.run_comparison(args)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["payments"][-1]["step"], "fees")
        event["values"]["remainingCostUsd"] = 7
        self.assertEqual(self.run_comparison(args)["status"], "mismatch")

    def test_unexpected_insolvency_event_fails(self):
        args = self.fixture()
        args["replay"].insolvent = {"order": [dict(transaction_hash="tx")]}
        self.assertEqual(self.run_comparison(args)["status"], "mismatch")

    def test_multichain_requires_both_transfer_and_matching_credit(self):
        args = self.fixture()
        args["request"]["srcChainId"] = 42161
        args["payouts"][0]["to"] = MULTICHAIN_VAULT
        args["payouts"].append(
            dict(
                event_name="MultichainTransferIn",
                values=dict(token=T, amount=52, account=A, srcChainId=42161),
            )
        )
        self.assertEqual(self.run_comparison(args)["status"], "matched")
        args["payouts"][-1]["values"]["srcChainId"] = 1
        self.assertEqual(self.run_comparison(args)["status"], "mismatch")


class NativeProofTests(unittest.TestCase):
    def test_native_requires_committed_call_and_matching_block(self):
        pos = dict(transaction_hash="0xabcdef", block_number=1, block_hash="hash")
        receipt = dict(payload=dict(status="0x1"))
        withdrawal = dict(
            type="CALL", **{"from": M, "to": W}, input="0x2e1a7d4d" + format(5, "064x")
        )
        native = dict(type="CALL", **{"from": M, "to": A}, value="0x5")
        data = dict(
            transaction_hash="0xabcdef",
            block_number=1,
            block_hash="hash",
            receipt=dict(
                transactionHash="0xabcdef",
                blockNumber="0x1",
                blockHash="hash",
                status="0x1",
            ),
            trace=dict(calls=[withdrawal, native]),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "execution-fee-traces").mkdir()
            path = root / "execution-fee-traces/0xabcdef.json"

            def check():
                path.write_text(json.dumps(data))
                return verify_native(TraceRepository(root), pos, receipt, M, A, 5, W)

            self.assertEqual(check(), "matched")
            data["trace"]["error"] = "reverted"
            self.assertEqual(check(), "mismatch")
            del data["trace"]["error"]
            native["type"] = "DELEGATECALL"
            self.assertEqual(check(), "mismatch")
            native["type"] = "CALL"
            data["block_hash"] = "wrong"
            self.assertEqual(check(), "mismatch")
        self.assertEqual(verify_native(None, pos, receipt, M, A, 5, W), "unavailable")


class BackfillCoverageTests(unittest.TestCase):
    def test_includes_liquidation_only_trader_without_pending_or_cancelled(self):
        from gmx_crypto_bot_v2.collection.referral import executed_traders

        orders = [
            dict(
                final_request=dict(account=account, orderType=kind),
                terminal=None if outcome is None else dict(event_name=outcome),
            )
            for account, kind, outcome in [
                ("ordinary", 4, "OrderExecuted"),
                ("liquidation", 7, "OrderExecuted"),
                ("pending", 7, None),
                ("cancelled", 4, "OrderCancelled"),
            ]
        ]
        self.assertEqual(
            executed_traders(dict(orders=orders)), {"ordinary", "liquidation"}
        )
