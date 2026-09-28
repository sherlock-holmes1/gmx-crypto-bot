"""Regressions from the September 20–27 recording."""

import unittest

from gmx_crypto_bot_v2.checks.orders import _reconstruct_terminal_reason


class TerminalReasonTests(unittest.TestCase):
    def test_invalid_decrease_size_matches_prior_position(self):
        account, market, token = "a", "m", "t"
        request = {
            "account": account,
            "market": market,
            "initialCollateralToken": token,
            "isLong": False,
            "orderType": 4,
            "sizeDeltaUsd": 201,
        }
        terminal = {
            "event_name": "OrderCancelled",
            "values": {
                "reason": "",
                "reasonBytes": "0x9fbe2cbc" + f"{201:064x}{100:064x}",
            },
            "block_number": 10,
            "transaction_index": 0,
            "log_index": 0,
        }
        opening = {
            "position": {
                "account": account,
                "market": market,
                "collateralToken": token,
                "isLong": False,
                "sizeInUsd": 100,
            }
        }
        mismatches = []
        self.assertEqual(
            _reconstruct_terminal_reason(
                request, terminal, [], opening, [], mismatches
            ),
            "matched",
        )
        self.assertEqual(mismatches, [])
        request["sizeDeltaUsd"] = 200
        self.assertEqual(
            _reconstruct_terminal_reason(
                request, terminal, [], opening, [], mismatches
            ),
            "mismatch",
        )

    def test_zero_position_size_requires_zero_increase(self):
        terminal = {
            "event_name": "OrderCancelled",
            "values": {"reason": "", "reasonBytes": "0xbff65b3f" + "0" * 128},
        }
        request = {"orderType": 2, "sizeDeltaUsd": 0}
        self.assertEqual(
            _reconstruct_terminal_reason(request, terminal, [], {}, [], []), "matched"
        )
        request["sizeDeltaUsd"] = 1
        self.assertEqual(
            _reconstruct_terminal_reason(request, terminal, [], {}, [], []), "mismatch"
        )
