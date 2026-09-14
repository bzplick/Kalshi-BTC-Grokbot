"""Default Kalshi Trade API host is the elections production URL."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from client import PROD_BASE, KalshiClient


class ProdBaseTests(unittest.TestCase):
    def test_prod_base_is_elections_host(self) -> None:
        self.assertEqual(PROD_BASE, "https://api.elections.kalshi.com/trade-api/v2")
        self.assertNotIn("external-api.kalshi.com", PROD_BASE)

    def test_client_defaults_to_prod_base(self) -> None:
        with patch("client.load_dotenv"), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KALSHI_BASE", None)
            client = KalshiClient(dry_run=True)
        self.assertEqual(client.base, PROD_BASE)
        self.assertEqual(
            client._request_url("/markets"),
            "https://api.elections.kalshi.com/trade-api/v2/markets",
        )
        self.assertNotIn("external-api.kalshi.com", client._request_url("/portfolio/balance"))

    def test_kalshi_base_env_override(self) -> None:
        override = "https://demo-api.kalshi.co/trade-api/v2"
        with patch("client.load_dotenv"), patch.dict(os.environ, {"KALSHI_BASE": override}):
            client = KalshiClient(dry_run=True)
        self.assertEqual(client.base, override)
        self.assertEqual(
            client._request_url("/markets"),
            "https://demo-api.kalshi.co/trade-api/v2/markets",
        )

    def test_request_url_derives_host_from_base(self) -> None:
        client = KalshiClient(
            base="https://example.test/trade-api/v2",
            dry_run=True,
        )
        self.assertEqual(
            client._request_url("/trade-api/v2/markets"),
            "https://example.test/trade-api/v2/markets",
        )
        self.assertEqual(
            client._request_url("events"),
            "https://example.test/trade-api/v2/events",
        )


if __name__ == "__main__":
    unittest.main()
