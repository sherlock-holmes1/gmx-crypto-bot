import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from gmx_crypto_bot_v2.accrual_configuration import slots, decode, load_snapshot
from gmx_crypto_bot_v2.accrual_backfill import collect

M = "0x" + "11" * 20
A = "0x" + "22" * 20
B = "0x" + "33" * 20
D = "0x" + "44" * 20


class ArchiveTests(unittest.TestCase):
    def fixture(self, root):
        m = {
            "market": {"market_token_address": M},
            "contracts": {"data_store": D},
            "tokens": {
                "long": {"address": A},
                "short": {"address": B},
                "index": {"address": A},
            },
        }
        (root / "metadata.json").write_text(json.dumps(m))
        (root / "completeness-report.json").write_text(
            json.dumps(
                dict(
                    complete=True,
                    gaps=[],
                    reorgs=[],
                    source_block_range={"from": 10, "to": 20},
                )
            )
        )
        (root / "events.jsonl").write_text(
            json.dumps(
                {
                    "kind": "opening_state_checkpoint",
                    "payload": {"block_number": 9, "block_hash": "0xh9"},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "kind": "block_header",
                    "block_number": 20,
                    "payload": {"hash": "0xh20"},
                }
            )
            + "\n"
        )
        return m

    def test_both_anchor_blocks_all_typed_slots_and_signed_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m = self.fixture(root)
            calls = []

            class RPC:
                def call(self, method, args):
                    return {"hash": "0xh" + str(int(args[0], 16))}

                def call_many(self, method, args):
                    calls.extend(args)
                    return ["0x" + "0" * 64] * len(args)

            s = collect(root, RPC())
            (root / "accrual-configuration.json").write_text(json.dumps(s))
            self.assertEqual(len(calls), 2 * len(slots(m)))
            self.assertEqual({a[1] for a in calls}, {"0x9", "0x14"})
            self.assertEqual(set(load_snapshot(root, m)), {"opening", "closing"})
            s["closing"]["block_hash"] = "bad"
            (root / "accrual-configuration.json").write_text(json.dumps(s))
            with self.assertRaisesRegex(ValueError, "block mismatch"):
                load_snapshot(root, m)
        self.assertEqual(decode("0x" + "f" * 64, "Int"), -1)
        with self.assertRaises(ValueError):
            decode("0x" + format(2, "064x"), "Bool")

    def test_rejects_hash_mismatch_before_archive_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)

            class RPC:
                def call(self, method, args):
                    return {"hash": "bad"}

                def call_many(self, *args):
                    raise AssertionError("must not read wrong fork")

            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                collect(root, RPC())


from gmx_crypto_bot_v2.accrual_math import (
    P,
    borrowing_rate,
    funding_rate,
    funding_deltas,
    liquidation_fee,
)
from gmx_crypto_bot_v2.accrual_validation import AccrualReplay, compare_accrual
from gmx_crypto_bot_v2.swap_math import key


class AccrualMathTests(unittest.TestCase):
    def config(self, **overrides):
        return dict(
            dict(
                factor=P // 10,
                exponent=P,
                increase=0,
                decrease=P // 1000,
                minimum=0,
                maximum=P,
                stable=P // 4,
                decrease_threshold=P // 10,
            ),
            **overrides,
        )

    def test_static_funding_collateral_split_and_asymmetric_rounding(self):
        x = funding_deltas(
            [[200 * P, 100 * P], [40 * P, 60 * P]],
            [300 * P, 100 * P],
            [P, P],
            2,
            0,
            self.config(),
        )
        self.assertEqual(x["rate"], P // 20)
        self.assertEqual(x["fee"], [[10**14, 10**14], [0, 0]])
        self.assertEqual(x["claim"], [[0, 0], [2 * 10**14, 10**14]])
        self.assertEqual(x["saved"], 0)
        x = funding_deltas([[0, 0], [P, P]], [0, 2 * P], [P, P], 10, P, self.config())
        self.assertEqual((x["rate"], x["saved"]), (0, 0))

    def test_adaptive_growth_flip_decay_and_minimum_separate_from_saved(self):
        c = self.config(increase=P // 100, maximum=P // 25)
        self.assertEqual(
            funding_rate([300 * P, 100 * P], 10, 0, c), (P // 25, True, P // 25)
        )
        self.assertEqual(
            funding_rate([100 * P, 300 * P], 10, P // 100, c),
            (P // 25, False, -P // 25),
        )
        c = self.config(increase=P // 100, minimum=P // 100)
        self.assertEqual(
            funding_rate([101 * P, 100 * P], 10, P // 1000, c), (P // 100, True, 1)
        )
        self.assertEqual(
            funding_rate([100 * P, 101 * P], 10, -P // 1000, c), (P // 100, False, -1)
        )
        self.assertEqual(funding_rate([100 * P, 100 * P], 0, 0, c), (P // 100, True, 0))

    def test_borrowing_legacy_kink_and_skip_smaller_side(self):
        c = dict(
            skip_smaller=0,
            optimal=0,
            exponent=P,
            factor=P // 100,
            reserve=P,
            base=P // 100,
            above=P // 20,
        )
        self.assertEqual(
            borrowing_rate(300 * P, 1000 * P, [300 * P, 100 * P], True, c),
            3 * P // 1000,
        )
        c["skip_smaller"] = 1
        self.assertEqual(
            borrowing_rate(100 * P, 1000 * P, [300 * P, 100 * P], False, c), 0
        )
        self.assertEqual(
            borrowing_rate(100 * P, 1000 * P, [100 * P, 100 * P], False, c), P // 1000
        )
        c.update(optimal=P // 2, skip_smaller=0)
        self.assertEqual(
            borrowing_rate(750 * P, 1000 * P, [P, P], True, c), 275 * P // 10000
        )
        with self.assertRaises(ValueError):
            borrowing_rate(P, 0, [P, P], True, c)

    def test_liquidation_rounds_up_before_receiver_split(self):
        self.assertEqual(
            liquidation_fee(101 * P, 3 * P, P // 10, P // 2),
            dict(
                liquidationFeeAmount=4,
                liquidationFeeReceiverFactor=P // 2,
                liquidationFeeAmountForFeeReceiver=2,
            ),
        )
        self.assertEqual(
            liquidation_fee(101 * P, 3 * P, 0, P // 2),
            dict(
                liquidationFeeAmount=0,
                liquidationFeeReceiverFactor=0,
                liquidationFeeAmountForFeeReceiver=0,
            ),
        )


class AccrualReplayTests(unittest.TestCase):
    fixture = ArchiveTests.fixture

    def setup_replay(self, root):
        metadata = self.fixture(root)

        class RPC:
            def call(self, method, args):
                return {"hash": "0xh" + str(int(args[0], 16))}

            def call_many(self, method, args):
                return ["0x" + "0" * 64] * len(args)

        snapshot = collect(root, RPC())
        (root / "accrual-configuration.json").write_text(json.dumps(snapshot))
        replay = AccrualReplay(root, metadata)
        for side, usd, tokens in [
            (True, [200 * P, 100 * P], [200, 100]),
            (False, [40 * P, 60 * P], [40, 60]),
        ]:
            for token, u, t in zip([A, B], usd, tokens):
                replay.put(u, "OPEN_INTEREST", M, token, side)
                replay.put(t, "OPEN_INTEREST_IN_TOKENS", M, token, side)
            replay.put(P, "BORROWING_EXPONENT_FACTOR", M, side)
            replay.put(P // 100, "BORROWING_FACTOR", M, side)
            replay.put(1, "CUMULATIVE_BORROWING_FACTOR_UPDATED_AT", M, side)
        for t in (A, B):
            replay.put(1000, "POOL_AMOUNT", M, t)
        for n, v in [
            ("FUNDING_FACTOR", P // 10),
            ("FUNDING_EXPONENT_FACTOR", P),
            ("MAX_FUNDING_FACTOR_PER_SECOND", P),
            ("FUNDING_UPDATED_AT", 9),
        ]:
            replay.set_market(v, n)
        return replay

    def events(self):
        entries = []

        def e(name, **values):
            entries.append(
                dict(
                    event_name=name,
                    block_number=10,
                    transaction_index=0,
                    log_index=len(entries),
                    transaction_hash="0xtx",
                    values=dict(market=M, **values),
                )
            )

        for t in (A, B):
            e("OraclePriceUpdate", token=t, minPrice=P, maxPrice=P)
        for t, delta in [(A, 10**14), (B, 10**14)]:
            e(
                "FundingFeeAmountPerSizeUpdated",
                collateralToken=t,
                isLong=True,
                delta=delta,
                value=delta,
            )
        for t, delta in [(A, 2 * 10**14), (B, 10**14)]:
            e(
                "ClaimableFundingAmountPerSizeUpdated",
                collateralToken=t,
                isLong=False,
                delta=delta,
                value=delta,
            )
        e("Funding", fundingFactorPerSecond=P // 20)
        for side, rate in [(True, 3 * P // 1000), (False, P // 1000)]:
            e(
                "CumulativeBorrowingFactorUpdated",
                isLong=side,
                delta=10 * rate,
                nextValue=10 * rate,
            )
            e("Borrowing", borrowingFactorPerSecond=rate)
        e("PositionFeesCollected", orderKey="order")
        return entries

    def test_rates_deltas_and_per_order_association(self):
        with tempfile.TemporaryDirectory() as tmp:
            replay = self.setup_replay(Path(tmp))
            replay.replay(self.events(), [], {10: 11})
            self.assertEqual(len(replay.results), 3)
            self.assertEqual({r["status"] for r in replay.results}, {"matched"})
            checks = {}
            errors = []
            compare_accrual(replay, "order", checks, errors)
            self.assertEqual(set(checks.values()), {"matched"})
            self.assertFalse(errors)
            self.assertEqual(replay.market_value("FUNDING_UPDATED_AT"), 11)

    def test_boolean_configuration_changes_apply_at_canonical_coordinate(self):
        from gmx_crypto_bot_v2.price_impact import config_base_key

        with tempfile.TemporaryDirectory() as tmp:
            replay = self.setup_replay(Path(tmp))
            events = self.events()
            update = dict(
                event_name="SetBool",
                block_number=10,
                transaction_index=0,
                log_index=-1,
                transaction_hash="tx-config",
                values=dict(
                    baseKey=config_base_key("SKIP_BORROWING_FEE_FOR_SMALLER_SIDE"),
                    data="0x",
                    value=True,
                ),
            )
            replay.replay(events, [update], {10: 11})
            short = next(r for r in replay.results if r.get("is_long") is False)
            self.assertEqual(short["rate"], 0)
            self.assertEqual(short["status"], "mismatch")

    def test_tampered_accumulator_does_not_seed_modeled_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            replay = self.setup_replay(Path(tmp))
            events = self.events()
            events[2]["values"]["value"] += 1
            replay.replay(events, [], {10: 11})
            self.assertEqual(replay.results[0]["status"], "mismatch")
            self.assertEqual(
                replay.read("FUNDING_FEE_AMOUNT_PER_SIZE", M, A, True), 10**14
            )

    def test_missing_oracle_and_broken_state_cannot_pass(self):
        for variant in ("oracle", "continuity"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                replay = self.setup_replay(Path(tmp))
                events = self.events()
                if variant == "oracle":
                    events.pop(0)
                else:
                    events.insert(
                        0,
                        dict(
                            event_name="PoolAmountUpdated",
                            block_number=10,
                            transaction_index=0,
                            log_index=-1,
                            transaction_hash="0xtx",
                            values=dict(market=M, token=A, delta=1, nextValue=2000),
                        ),
                    )
                replay.replay(events, [], {10: 11})
                self.assertFalse(replay.summary()["complete"])
                self.assertIn("unavailable", {r["status"] for r in replay.results})


from gmx_crypto_bot_v2.accrual_validation import compare_erased_liquidation
from gmx_crypto_bot_v2.historical_configuration import ConfigHistory
from gmx_crypto_bot_v2.referral import ZERO_CODE
from types import SimpleNamespace


class LiquidationTests(unittest.TestCase):
    def test_erased_fee_residual_reconstructed_without_trusting_zero_fee_struct(self):
        position = dict(
            block_number=10,
            transaction_index=0,
            log_index=20,
            transaction_hash="tx",
            values=dict(
                sizeDeltaUsd=100 * P,
                basePnlUsd=-P,
                isLong=True,
                collateralToken=B,
                totalImpactUsd=0,
            ),
            pre_position=dict(
                sizeInUsd=100 * P,
                sizeInTokens=100,
                collateralAmount=2,
                borrowingFactor=0,
                fundingFeeAmountPerSize=0,
            ),
            oracle_prices_at_event={
                A: dict(minPrice=99 * P // 100, maxPrice=99 * P // 100),
                B: dict(minPrice=P, maxPrice=P),
            },
        )
        fees = dict(
            block_number=10,
            transaction_index=0,
            log_index=19,
            transaction_hash="tx",
            values={},
        )
        insolvency = dict(
            block_number=10,
            transaction_index=0,
            log_index=18,
            transaction_hash="tx",
            values=dict(
                step="fees",
                remainingCostUsd=594 * P // 100,
                basePnlUsd=-P,
                positionCollateralAmount=2,
            ),
        )
        replay = SimpleNamespace(
            market=M, index=A, tokens=[A, B], insolvent={"order": [insolvency]}
        )
        state = dict(
            funding={"status": "matched"},
            borrowing={
                True: dict(status="matched", next_value=0),
                False: dict(status="matched", next_value=0),
            },
            liquidation_factor=P // 20,
            liquidation_receiver=P // 2,
            funding_values={key("FUNDING_FEE_AMOUNT_PER_SIZE", M, B, True): 0},
        )
        request = dict(
            account=A, uiFeeReceiver="0x" + "0" * 40, decreasePositionSwapType=1
        )
        history = {"position_fee_negative": ConfigHistory(10, 20, (), P // 50)}

        class Referral:
            def at(self, *args):
                return dict(
                    code=ZERO_CODE,
                    rebate_bps=0,
                    share_bps=0,
                    minimum=0,
                    pro_tier=0,
                    pro_factor=0,
                )

        checks = {}
        errors = []
        result = compare_erased_liquidation(
            replay,
            state,
            "order",
            request,
            position,
            fees,
            {"balance_was_improved": False},
            history,
            checks,
            errors,
            Referral(),
            [],
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["modeled"]["liquidationFeeAmount"], 5)
        self.assertEqual(result["position_fee"], 2)
        self.assertFalse(errors)
        insolvency["values"]["remainingCostUsd"] += 1
        result = compare_erased_liquidation(
            replay,
            state,
            "order",
            request,
            position,
            fees,
            {"balance_was_improved": False},
            history,
            checks,
            errors,
            Referral(),
            [],
        )
        self.assertEqual(result["status"], "mismatch")
        state["funding"]["status"] = "unavailable"
        result = compare_erased_liquidation(
            replay,
            state,
            "order",
            request,
            position,
            fees,
            {"balance_was_improved": False},
            history,
            checks,
            errors,
            Referral(),
            [],
        )
        self.assertEqual(result["status"], "unavailable")


class HeaderTests(unittest.TestCase):
    def test_raw_header_timestamp_is_bound_to_execution_block_hash(self):
        import gzip, base64
        from gmx_crypto_bot_v2.accrual_validation import load_timestamps

        with tempfile.TemporaryDirectory() as tmp:
            from gmx_crypto_bot_v2.evidence.artifacts import RawArtifactStore

            root = Path(tmp)
            (root / "metadata.json").write_text("{}")
            artifacts = RawArtifactStore(root)
            header = dict(number="0xa", hash="0xhash", timestamp="0x123")
            artifacts.response(
                "rpc-eth_getBlockByNumber",
                {"method": "eth_getBlockByNumber", "params": ["0xa", False]},
                json.dumps({"result": header}).encode(),
            )
            artifacts.close()
            self.assertEqual(load_timestamps(root, {10: "0xhash"}), {10: 291})
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                load_timestamps(root, {10: "0xwrong"})
            self.assertEqual(load_timestamps(root, {11: "0xabsent"}), {})
