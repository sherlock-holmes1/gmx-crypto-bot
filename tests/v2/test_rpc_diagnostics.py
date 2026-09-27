import unittest
import urllib.error
from unittest.mock import Mock, patch

from gmx_crypto_bot_v2.sources.rpc import PublicJsonRpc, SourceError


class RpcDiagnosticsTests(unittest.TestCase):
    def test_http_status_survives_without_credentials(self):
        for status, attempts in ((401, 1), (429, 5), (503, 5)):
            with self.subTest(status=status):
                sink = Mock()
                client = PublicJsonRpc("https://example.invalid/secret", 1, sink)
                failure = urllib.error.HTTPError(
                    client.endpoint, status, "secret", {}, None
                )
                with (
                    patch("urllib.request.urlopen", side_effect=failure),
                    patch("time.sleep"),
                ):
                    with self.assertRaises(SourceError) as caught:
                        client.call("eth_chainId", [])
                self.assertIn(f"HTTP {status}", caught.exception.safe_detail)
                self.assertIn(f"{attempts} attempt", caught.exception.safe_detail)
                self.assertNotIn("secret", caught.exception.safe_detail)
                self.assertIn(f"HTTP {status}", sink.error.call_args.args[2])
