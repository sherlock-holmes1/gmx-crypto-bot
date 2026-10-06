"""Exercise the installed simulator command path with bounded evidence."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gmx_crypto_bot_v2.application.simulation import main
from tests.v2.test_simulation_economics import I, P, S
from tests.v2.test_simulation_scenarios import FakeEvidence, risk_inputs


class Adapter(FakeEvidence):
    start, end = 10, 12
    tokens = (I, I, S)

    def __init__(self):
        super().__init__()
        self.events = [{"event_name": "OrderExecuted", "block_number": 11,
                        "transaction_index": 0, "log_index": 9}]
        self.timestamps = {11: 100, 12: 110}

    @staticmethod
    def coordinate(event):
        return event["block_number"], event["transaction_index"], event["log_index"]

    def risk_configuration_at(self, coordinate):
        return risk_inputs()["risk_configuration"][coordinate]

    def virtual_inventory_at(self, coordinate):
        return 0

    def referral_at(self, account, coordinate):
        return risk_inputs()["risk_referral"][(11, 0, 9)]


class SimulationCliTest(unittest.TestCase):
    def test_command_writes_scenario_report(self):
        plan = {
            "account": "0x" + "11" * 20,
            "risk_window": {"from": [11, 0, 9], "through": [12, 0, 9]},
            "initial": {"is_long": True, "collateral_token": S,
                        "cash_usdc": "100", "cash_eth_wei": "20"},
            "scenario": {"name": "cli-check", "inclusion_delay_blocks": 0,
                         "keeper_delay_blocks": 0, "execution_fee_wei": "3",
                         "acceptable_price_override": None,
                         "liquidation_buffer_usd": str(2 * P)},
            "orders": [{"request_coordinate": [10, 0, 1],
                        "kind": "market_increase", "size_delta_usd": str(10 * P),
                        "size_delta_tokens": "10", "collateral_delta_amount": "50"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "order-validation.json").write_text("{}")
            plan_path, output = root / "plan.json", root / "report.json"
            plan_path.write_text(json.dumps(plan))
            with patch("gmx_crypto_bot_v2.application.simulation.EvidenceAdapter.load",
                       return_value=Adapter()):
                self.assertEqual(main(["--recording", str(root), "--plan",
                                       str(plan_path), "--output", str(output)]), 0)
            report = json.loads(output.read_text())
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["orders"][0]["status"], "filled_estimate")
        self.assertEqual(len(report["risk"]), 2)


if __name__ == "__main__":
    unittest.main()
