"""Completion depends on current order coverage and fresh offline trace proofs."""

import json
import unittest
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gmx_crypto_bot_v2.checks.execution import CHECK
from gmx_crypto_bot_v2.checks.execution import (
    apply_execution_fee_proof as apply_evidence_proof,
)
from gmx_crypto_bot_v2.checks.trace import verify_trace_evidence
from gmx_crypto_bot_v2.domain.payments import PAY_SELECTOR, PaymentInput
from gmx_crypto_bot_v2.evidence.traces import TraceRepository


def apply_execution_fee_proof(recording, orders):
    return apply_evidence_proof(TraceRepository(recording), orders)


A = "0x" + "11" * 20
B = "0x" + "22" * 20
TX = "0x" + "33" * 32
BLOCK = "0x" + "44" * 32
ORDER = "0x" + "55" * 32


def order(key=ORDER, fee=100):
    return dict(
        key=key,
        terminal=dict(transaction_hash=TX, block_number=10, block_hash=BLOCK),
        final_request=dict(executionFee=fee),
        mismatches=[],
        status="matched",
        checks={"execution_fee_event_balance": "matched" if fee else "not_applicable"},
        observed_execution_fee_events=[],
        observed_order_update_topups=[],
    )


def proof(keys=(ORDER,), pending=False):
    return dict(
        transaction_hash=TX,
        block_number=10,
        payments=[
            dict(
                order_key=key,
                execution_fee=100,
                order_validation="matched",
                gas_fee_match="pending_gas_probe" if pending else "matched",
            )
            for key in keys
        ],
    )


class GateTests(unittest.TestCase):
    def fixture(self, root):
        directory = root / "execution-fee-traces"
        directory.mkdir()
        path = directory / (TX + ".json")
        path.write_text(
            json.dumps(dict(transaction_hash=TX, block_number=10, block_hash=BLOCK))
        )
        return path

    def test_all_expected_orders_joined_and_zero_fee_not_applicable(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            orders = [order(), order("zero", 0)]
            with patch(
                "gmx_crypto_bot_v2.checks.execution.verify_trace_evidence",
                return_value=proof(),
            ) as verify:
                result = apply_execution_fee_proof(root, orders)
            self.assertTrue(result["complete"])
            self.assertEqual(result["counts"], {"matched": 1, "not_applicable": 1})
            self.assertEqual(verify.call_count, 1)
            self.assertIs(verify.call_args.args[2][ORDER], orders[0])

    def test_cached_pass_cannot_hide_missing_trace(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "execution-fee-traces").mkdir()
            (root / "execution-fee-traces/verification.json").write_text(
                '{"complete":true}'
            )
            result = apply_execution_fee_proof(root, [order()])
            self.assertFalse(result["complete"])
            self.assertEqual(result["counts"], {"unavailable": 1})

    def test_batch_omission_and_duplicate_payment_fail(self):
        for verified in [proof(()), proof((ORDER, ORDER))]:
            with self.subTest(proof=verified), TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                orders = [order()]
                with patch(
                    "gmx_crypto_bot_v2.checks.execution.verify_trace_evidence",
                    return_value=verified,
                ):
                    result = apply_execution_fee_proof(root, orders)
                self.assertFalse(result["complete"])
                self.assertEqual(orders[0]["status"], "mismatch")
                self.assertIn(CHECK + "_mismatch", orders[0]["mismatches"])

    def test_pending_gas_keeps_gate_open(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            with patch(
                "gmx_crypto_bot_v2.checks.execution.verify_trace_evidence",
                return_value=proof(pending=True),
            ):
                result = apply_execution_fee_proof(root, [order()])
            self.assertFalse(result["complete"])
            self.assertEqual(result["counts"], {"unavailable": 1})

    def test_wrong_block_and_malformed_trace_fail(self):
        for content in [
            "{}",
            "invalid",
            json.dumps(dict(transaction_hash=TX, block_number=10, block_hash="wrong")),
        ]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = self.fixture(root)
                path.write_text(content)
                result = apply_execution_fee_proof(root, [order()])
                self.assertEqual(result["counts"], {"mismatch": 1})

    def test_failed_transfer_proof_is_a_mismatch(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            with patch(
                "gmx_crypto_bot_v2.checks.execution.verify_trace_evidence",
                side_effect=ValueError("refund transfer absent"),
            ):
                result = apply_execution_fee_proof(root, [order()])
            self.assertEqual(result["counts"], {"mismatch": 1})

    def test_extra_proven_payment_stays_explicit(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            verified = proof((ORDER, "extra"))
            verified["payments"][1]["order_validation"] = "not_in_order_validation"
            with patch(
                "gmx_crypto_bot_v2.checks.execution.verify_trace_evidence",
                return_value=verified,
            ):
                result = apply_execution_fee_proof(root, [order()])
            self.assertTrue(result["complete"])
            self.assertEqual(result["unindexed_payment_calls"], 1)

    def test_unresolved_order_does_not_require_payment_proof(self):
        pending = order()
        pending["terminal"] = None
        pending["status"] = "unresolved"
        with TemporaryDirectory() as tmp:
            result = apply_execution_fee_proof(Path(tmp), [order("zero", 0), pending])
        self.assertTrue(result["complete"])
        self.assertNotIn(CHECK, pending["checks"])


class TraceIdentityTests(unittest.TestCase):
    def fixture(self):
        words = [int(A, 16)] * 4 + [
            int(ORDER, 16),
            0,
            0,
            100000,
            2,
            int(A, 16),
            int(B, 16),
            0,
        ]
        data = PAY_SELECTOR + "".join(format(v, "064x") for v in words)
        transaction = dict(
            hash=TX,
            blockHash=BLOCK,
            blockNumber="0xa",
            to=A,
            input="0x1234",
            **{"from": B},
        )
        return dict(
            transaction_hash=TX,
            block_hash=BLOCK,
            block_number=10,
            receipt=dict(
                transactionHash=TX, blockHash=BLOCK, blockNumber="0xa", status="0x1"
            ),
            transaction=transaction,
            trace=dict(
                to=A,
                input="0x1234",
                calls=[dict(type="DELEGATECALL", to=A, input=data)],
                **{"from": B},
            ),
            payments=[
                dict(path=[0], input=asdict(PaymentInput.decode(data)), library=A)
            ],
        )

    def verify(self, data):
        return verify_trace_evidence(data, None, {ORDER: order(fee=0)})

    def test_zero_fee_call_and_identity(self):
        result = self.verify(self.fixture())
        self.assertEqual(
            result["payments"][0]["gas_fee_match"], "zero_fee_not_applicable"
        )

    def test_omitted_duplicate_or_changed_payment_index_rejected(self):
        for mutation in ["omit", "duplicate", "input", "library"]:
            with self.subTest(mutation=mutation):
                data = self.fixture()
                if mutation == "omit":
                    data["payments"] = []
                if mutation == "duplicate":
                    data["payments"] *= 2
                if mutation == "input":
                    data["payments"][0]["input"]["starting_gas"] += 1
                if mutation == "library":
                    data["payments"][0]["library"] = B
                with self.assertRaises(ValueError):
                    self.verify(data)

    def test_trace_transaction_mismatch_and_reversion_rejected(self):
        for mutation in ["block", "root", "receipt"]:
            data = self.fixture()
            if mutation == "block":
                data["transaction"]["blockHash"] = "wrong"
            if mutation == "root":
                data["trace"]["error"] = "reverted"
            if mutation == "receipt":
                data["receipt"]["status"] = "0x0"
            with self.assertRaises(ValueError):
                self.verify(data)
