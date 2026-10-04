"""Opt-in check against the complete September recording."""

import json
import os
import unittest
from dataclasses import replace
from pathlib import Path

from gmx_crypto_bot_v2.simulation.evidence import EvidenceAdapter
from gmx_crypto_bot_v2.simulation.economics import (
    EconomicOrder, PositionBefore, calculate, compare_validated_order,
)
from gmx_crypto_bot_v2.simulation.ledger import (
    CounterfactualBook, PositionLedger, apply_fill,
    fee_only_increase_pool_effect,
    fee_only_decrease_pool_effect,
)
from gmx_crypto_bot_v2.simulation.orders import KeeperOpportunity, OrderKind, Request
from gmx_crypto_bot_v2.simulation.scenarios import PlannedOrder, Scenario, run_scenario


class RecordedEvidenceTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("GMX_STEP4_RECORDING"),
                         "set GMX_STEP4_RECORDING for historical integration")
    def test_recorded_long_and_short_open_close_lifecycle(self):
        recording = Path(os.environ["GMX_STEP4_RECORDING"])
        validation = json.loads((recording / "order-validation.json").read_text())
        adapter = EvidenceAdapter.load(recording, validation)
        pairs = (
            ("0xf9e6df798381ad17b7f1f1ef04d4e1089c58640ff5e938552780190d17587bb9",
             "0x06924beb4b316d10a39aa54ba2d4b3296f8f3c272a2d36d8b0412e880938291b"),
            ("0x68df5a935bfec2fc4b2e27f801c58ea0608cfecdfdf6c71eb38ef4ff622e50dd",
             "0xa70b9aecc85ca9a0bbfc3e1e4328faf1adbb40dd74a5db67c07fa3524c6708a4"),
        )

        def inverted(book):
            return CounterfactualBook(
                -book.long_oi_usd, -book.short_oi_usd,
                -book.long_oi_tokens, -book.short_oi_tokens,
                {token: -value for token, value in book.pool_amount.items()},
                -book.impact_pool_tokens,
                {cell: -value for cell, value in book.oi_usd_cells.items()},
                {cell: -value for cell, value in book.oi_token_cells.items()},
            )

        def exclude(state, book):
            return replace(inverted(book).apply_to(state),
                           counterfactual_applied=False)

        for increase_key, decrease_key in pairs:
            with self.subTest(increase=increase_key, decrease=decrease_key):
                reports = [next(item for item in validation["orders"]
                                if item["key"] == order_key)
                           for order_key in (increase_key, decrease_key)]
                positions = [next(entry for entry in item["observed_execution_events"]
                                  if entry["event_name"] in
                                  {"PositionIncrease", "PositionDecrease"})
                             for item in reports]
                position_coords = [tuple(entry[field] for field in
                                         ("block_number", "transaction_index", "log_index"))
                                   for entry in positions]
                terminal_coords = [tuple(item["terminal"][field] for field in
                                         ("block_number", "transaction_index", "log_index"))
                                   for item in reports]
                is_long = positions[0]["values"]["isLong"]
                self.assertEqual(positions[1]["values"]["isLong"], is_long)
                self.assertEqual(positions[1]["values"]["sizeInUsd"], 0)
                requests = [item["final_request"] for item in reports]
                self.assertEqual(requests[1]["decreasePositionSwapType"], 0)
                raw_pre = [adapter.at(coordinate) for coordinate in position_coords]
                raw_post = [adapter.at(coordinate) for coordinate in terminal_coords]
                orders = [EconomicOrder(
                    index == 0, is_long, position["values"]["sizeDeltaUsd"],
                    position["values"]["sizeDeltaInTokens"],
                    position["values"]["collateralToken"],
                    request["acceptablePrice"],
                    adapter.virtual_inventory_at(coordinate),
                    request["uiFeeReceiver"], 0,
                    withdrawal_amount=(request["initialCollateralDeltaAmount"]
                                       if index else 0),
                    collateral_delta_amount=(request["initialCollateralDeltaAmount"]
                                             if index == 0 else 0),
                ) for index, (position, request, coordinate) in enumerate(
                    zip(positions, requests, position_coords))]
                initial = PositionLedger(is_long, orders[0].collateral_token,
                                         orders[0].collateral_delta_amount, 0)
                first_model = calculate(raw_pre[0], orders[0], initial.before(),
                    adapter.referral_at(requests[0]["account"], position_coords[0]).terms)
                self.assertEqual(set(compare_validated_order(first_model, reports[0]).values()),
                                 {"matched"})
                first = apply_fill(initial, CounterfactualBook(), raw_pre[0],
                                   orders[0], first_model,
                                   fee_only_increase_pool_effect(raw_pre[0], orders[0],
                                                                 first_model))
                second_raw = exclude(raw_pre[1], first.market)
                second_state = first.market.apply_to(second_raw)
                second_model = calculate(second_state, orders[1], first.ledger.before(),
                    adapter.referral_at(requests[1]["account"], position_coords[1]).terms)
                self.assertEqual(set(compare_validated_order(second_model, reports[1]).values()),
                                 {"matched"})
                second = apply_fill(first.ledger, first.market, second_raw,
                                    orders[1], second_model,
                                    fee_only_decrease_pool_effect(second_state,
                                                                  orders[1], second_model))
                # Verify the independently modeled deltas against the raw
                # before/after order states before excluding these actual trades.
                for pre, post, previous, next_book in (
                    (raw_pre[0], raw_post[0], CounterfactualBook(), first.market),
                    (raw_pre[1], raw_post[1], first.market, second.market),
                ):
                    for side in ("long", "short"):
                        expected = getattr(next_book, side + "_oi_usd") - getattr(
                            previous, side + "_oi_usd")
                        self.assertEqual(post.open_interest_usd[side]
                                         - pre.open_interest_usd[side], expected)
                    for token in pre.pool_amount:
                        expected = (next_book.pool_amount.get(token, 0)
                                    - previous.pool_amount.get(token, 0))
                        self.assertEqual(post.pool_amount[token]
                                         - pre.pool_amount[token], expected)
                    self.assertEqual(post.impact_pool_amount - pre.impact_pool_amount,
                                     next_book.impact_pool_tokens
                                     - previous.impact_pool_tokens)

                opportunities = []
                for coordinate, state in zip(terminal_coords, raw_pre):
                    low, high = state.oracle[adapter.tokens[0]]
                    opportunities.append(KeeperOpportunity(*coordinate, low, high))
                intervening_risk = tuple(
                    coordinate for coordinate in adapter.required_risk_coordinates()
                    if terminal_coords[0] < coordinate < terminal_coords[1])
                self.assertTrue(intervening_risk,
                                "recorded lifecycle unexpectedly has no intervening risk changes")

                class ExcludingEvidence:
                    require_full_window = False

                    def keeper_opportunities(self):
                        return opportunities

                    def state_for_opportunity(self, opportunity):
                        state = adapter.state_for_opportunity(opportunity)
                        return (state if opportunity == opportunities[0]
                                else exclude(state, first.market))

                    def at(self, coordinate):
                        state = adapter.at(coordinate)
                        return exclude(state, first.market if coordinate == terminal_coords[0]
                                       else second.market)

                    def required_risk_coordinates(self):
                        return (tuple(terminal_coords[:1]) + intervening_risk
                                + tuple(terminal_coords[1:])
                                if self.require_full_window else tuple(terminal_coords))

                plans = []
                for index, (item, order) in enumerate(zip(reports, orders)):
                    created = item["request"]
                    plans.append(PlannedOrder(
                        tuple(created[field] for field in
                              ("block_number", "transaction_index", "log_index")),
                        Request(OrderKind.MARKET_INCREASE if index == 0
                                else OrderKind.MARKET_DECREASE,
                                is_long, created["block_number"]),
                        order,
                        adapter.referral_at(requests[index]["account"],
                                            position_coords[index]).terms,
                    ))
                risk_inputs = {
                    "risk_coordinates": tuple(terminal_coords),
                    "risk_configuration": {c: adapter.risk_configuration_at(c)
                                           for c in terminal_coords + position_coords},
                    "risk_virtual_inventory": {c: adapter.virtual_inventory_at(c)
                                               for c in terminal_coords + position_coords},
                    "risk_referral": {c: adapter.referral_at(requests[0]["account"], c)
                                      for c in terminal_coords + position_coords},
                    "timestamps": adapter.timestamps,
                }
                result = run_scenario(
                    ExcludingEvidence(), tuple(plans),
                    Scenario("recorded-lifecycle", 0, 0, 0, None, {}, 0),
                    initial, **risk_inputs)
                self.assertEqual(result.status, "complete", result.unavailable_reasons)
                self.assertEqual([entry.status for entry in result.orders],
                                 ["filled_estimate", "filled_estimate"])
                self.assertEqual(result.orders[-1].ledger_after["size_usd"], 0)
                self.assertEqual(result.orders[-1].ledger_after["size_tokens"], 0)
                # The complete recorded interval includes intervening risk
                # changes. Declaring only terminal marks must be unavailable,
                # even though the two-order economic parity above succeeds.
                full_window = ExcludingEvidence()
                full_window.require_full_window = True
                incomplete = run_scenario(
                    full_window, tuple(plans),
                    Scenario("incomplete-recorded-risk-window", 0, 0, 0, None, {}, 0),
                    initial, **risk_inputs)
                self.assertEqual(incomplete.status, "unavailable")
                self.assertIn("declared risk coverage differs from recorded state changes",
                              incomplete.unavailable_reasons)

    @unittest.skipUnless(os.environ.get("GMX_STEP4_RECORDING"),
                         "set GMX_STEP4_RECORDING for historical integration")
    def test_recorded_increases_through_scenario_and_risk_mark(self):
        recording = Path(os.environ["GMX_STEP4_RECORDING"])
        validation = json.loads((recording / "order-validation.json").read_text())
        adapter = EvidenceAdapter.load(recording, validation)
        for is_long in (True, False):
            with self.subTest(is_long=is_long):
                report = next(item for item in validation["orders"]
                              if item["status"] == "matched" and any(
                                  entry["event_name"] == "PositionIncrease"
                                  and entry["values"].get("isLong") is is_long
                                  and entry.get("pre_position") is None
                                  for entry in item["observed_execution_events"]))
                position = next(entry for entry in report["observed_execution_events"]
                                if entry["event_name"] == "PositionIncrease")
                values, request, terminal = (position["values"],
                                             report["final_request"], report["terminal"])
                position_coordinate = (position["block_number"],
                                       position["transaction_index"],
                                       position["log_index"])
                terminal_coordinate = (terminal["block_number"],
                                       terminal["transaction_index"],
                                       terminal["log_index"])
                low, high = adapter.at(position_coordinate).oracle[adapter.tokens[0]]
                opportunity = KeeperOpportunity(*terminal_coordinate, low, high)

                class BoundedEvidence:
                    def keeper_opportunities(self):
                        return [opportunity]

                    def state_for_opportunity(self, candidate):
                        return adapter.state_for_opportunity(candidate)

                    def at(self, coordinate):
                        return adapter.at(coordinate)

                    def required_risk_coordinates(self):
                        return (terminal_coordinate,)

                created = report["request"]
                planned = PlannedOrder(
                    (created["block_number"], created["transaction_index"],
                     created["log_index"]),
                    Request(OrderKind.MARKET_INCREASE, is_long,
                            created["block_number"]),
                    EconomicOrder(True, is_long, values["sizeDeltaUsd"],
                                  values["sizeDeltaInTokens"], values["collateralToken"],
                                  request["acceptablePrice"],
                                  adapter.virtual_inventory_at(position_coordinate),
                                  request["uiFeeReceiver"], request["executionFee"],
                                  collateral_delta_amount=request["initialCollateralDeltaAmount"]),
                    adapter.referral_at(request["account"], position_coordinate).terms,
                )
                risk_inputs = {
                    "risk_coordinates": (terminal_coordinate,),
                    "risk_configuration": {terminal_coordinate:
                        adapter.risk_configuration_at(terminal_coordinate)},
                    "risk_virtual_inventory": {terminal_coordinate:
                        adapter.virtual_inventory_at(terminal_coordinate)},
                    "risk_referral": {terminal_coordinate:
                        adapter.referral_at(request["account"], terminal_coordinate)},
                    "timestamps": adapter.timestamps,
                }
                scenario = Scenario("recorded-execution", 0, 0,
                                    request["executionFee"], None, {}, 0)
                result = run_scenario(
                    BoundedEvidence(), (planned,), scenario,
                    PositionLedger(is_long, values["collateralToken"],
                                   request["initialCollateralDeltaAmount"],
                                   request["executionFee"]), **risk_inputs)
                self.assertEqual(result.status, "complete", result.unavailable_reasons)
                self.assertEqual(result.orders[0].status, "filled_estimate")
                self.assertEqual(result.orders[0].ledger_after["size_usd"],
                                 values["sizeInUsd"])
                self.assertEqual(result.orders[0].ledger_after["size_tokens"],
                                 values["sizeInTokens"])
                self.assertEqual(result.orders[0].ledger_after["collateral_usdc"],
                                 values["collateralAmount"])
                self.assertEqual(result.risk[0].point.status, "available")

    @unittest.skipUnless(os.environ.get("GMX_STEP4_RECORDING"),
                         "set GMX_STEP4_RECORDING for historical integration")
    def test_long_and_short_recorded_economic_parity(self):
        recording = Path(os.environ["GMX_STEP4_RECORDING"])
        validation = json.loads((recording / "order-validation.json").read_text())
        adapter = EvidenceAdapter.load(recording, validation)
        accounts = json.loads((recording / "liquidation-referral-configuration.json").read_text())
        account = accounts["traders"][0]
        for is_long in (True, False):
            with self.subTest(is_long=is_long):
                report = next(item for item in validation["orders"]
                              if item["status"] == "matched" and any(
                                  entry["event_name"] == "PositionIncrease"
                                  and entry["values"].get("isLong") is is_long
                                  and entry.get("pre_position") is None
                                  for entry in item["observed_execution_events"]))
                position = next(entry for entry in report["observed_execution_events"]
                                if entry["event_name"] == "PositionIncrease")
                values = position["values"]
                request = report["final_request"]
                coordinate = (position["block_number"], position["transaction_index"],
                              position["log_index"])
                state = adapter.at(coordinate)
                self.assertEqual(state.coordinate, coordinate)
                self.assertEqual(len(state.oracle), len(set(adapter.tokens)))
                self.assertIsInstance(adapter.virtual_inventory_at(coordinate), int)
                self.assertGreater(adapter.risk_configuration_at(
                    coordinate).min_collateral_usd, 0)
                self.assertTrue(adapter.referral_at(account, coordinate).source)
                order = EconomicOrder(
                    True, is_long, values["sizeDeltaUsd"], values["sizeDeltaInTokens"],
                    values["collateralToken"], request["acceptablePrice"],
                    adapter.virtual_inventory_at(coordinate),
                    request["uiFeeReceiver"], request["executionFee"],
                    collateral_delta_amount=request["initialCollateralDeltaAmount"],
                )
                terms = adapter.referral_at(request["account"], coordinate).terms
                initial = PositionLedger(is_long, order.collateral_token,
                                         order.collateral_delta_amount,
                                         order.execution_fee_wei)
                book = CounterfactualBook()
                modeled_state = book.apply_to(state)
                modeled = calculate(modeled_state, order, initial.before(), terms)
                self.assertEqual(modeled.status, "eligible", modeled.reason)
                comparison = compare_validated_order(modeled, report)
                self.assertEqual(set(comparison.values()), {"matched"}, comparison)
                effect = fee_only_increase_pool_effect(modeled_state, order, modeled)
                transition = apply_fill(initial, book, state, order, modeled, effect)
                self.assertEqual(transition.ledger.size_usd, values["sizeInUsd"])
                self.assertEqual(transition.ledger.size_tokens, values["sizeInTokens"])
                self.assertEqual(transition.ledger.collateral_usdc,
                                 values["collateralAmount"])
