"""Tests for dashboard JSONL ingest (mixed schemas, settlements, sample build)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import build_dashboard as bd

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "dashboard" / "sample_ledger.jsonl"
SAMPLE_SETTLE = ROOT / "dashboard" / "sample_settlements.json"


class ClassifyTests(unittest.TestCase):
    def test_new_take_and_skips(self) -> None:
        self.assertEqual(bd.classify({"decision": "take_yes_paper"}), "take")
        self.assertEqual(bd.classify({"decision": "skip", "reason": "premium_gt_065"}), "skip")
        self.assertEqual(bd.classify({"decision": "skip", "reason": "incomplete_quotes"}), "skip")
        self.assertEqual(bd.classify({"event": "summary"}), "meta")
        self.assertEqual(bd.classify({"event": "start"}), "meta")

    def test_old_events(self) -> None:
        self.assertEqual(bd.classify({"event": "scan", "ticker": "X"}), "scan")
        self.assertEqual(bd.classify({"event": "routine_start"}), "meta")
        self.assertEqual(bd.classify({"event": "no_entry", "reason": "gates_fail"}), "skip")
        self.assertEqual(bd.classify({"event": "paper_skip", "reason": "clip_gt_5"}), "skip")
        self.assertEqual(bd.classify({"event": "paper_calibrated_take"}), "take")
        self.assertEqual(bd.classify({"event": "mm_would_skip", "would_skip": True}), "overlay")
        self.assertEqual(bd.classify({"event": "mm_compare", "would_skip": True}), "overlay")

    def test_malformed_jsonl_skipped(self) -> None:
        rows, skipped = bd.load_jsonl(SAMPLE)
        self.assertGreaterEqual(skipped, 1)
        self.assertGreater(len(rows), 10)


class BuildSampleTests(unittest.TestCase):
    def test_sample_payload_counts_and_settlement(self) -> None:
        rows, skipped = bd.load_jsonl(SAMPLE)
        payload = bd.build_payload(
            rows,
            ledger_path=SAMPLE,
            settlements_path=SAMPLE_SETTLE,
            skipped_lines=skipped,
        )
        json.dumps(payload)  # must be serializable
        s = payload["summary"]
        self.assertEqual(s["n_paper_takes"], 2)  # paper_calibrated_take + take_yes_paper
        self.assertGreaterEqual(s["n_skips"], 5)
        self.assertGreaterEqual(s["n_scans"], 1)
        self.assertEqual(s["n_overlay"], 2)
        self.assertIn("premium_gt_065", s["skips_by_reason"])
        self.assertIn("clip_gt_5", s["skips_by_reason"])
        self.assertIn("gates_fail", s["skips_by_reason"])
        self.assertIn("incomplete_quotes", s["skips_by_reason"])
        self.assertTrue(s["settlements_available"])
        self.assertEqual(s["n_wins"], 1)
        self.assertIsNotNone(s["pnl"])
        take = next(d for d in payload["decisions"] if d["decision"] == "take_yes_paper")
        self.assertEqual(take["outcome"], "win")
        self.assertEqual(take["side"], "YES")
        self.assertEqual(take["entry_ask"], 0.51)
        self.assertEqual(take["pnl"], round(4 * (1 - 0.51), 4))
        cal = next(d for d in payload["decisions"] if d["event"] == "paper_calibrated_take")
        self.assertEqual(cal["outcome"], "n/a")
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["live_trading"])

    def test_missing_settlements_are_na(self) -> None:
        rows, skipped = bd.load_jsonl(SAMPLE)
        payload = bd.build_payload(
            rows,
            ledger_path=SAMPLE,
            settlements_path=None,
            skipped_lines=skipped,
        )
        self.assertTrue(payload["summary"]["pnl_na"])
        self.assertIsNone(payload["summary"]["win_rate"])
        take = next(d for d in payload["decisions"] if d["kind"] == "take")
        self.assertEqual(take["outcome"], "n/a")

    def test_empty_takes_copy_when_no_takes(self) -> None:
        rows = [
            {"event": "scan", "ts": "2026-09-08T12:00:00Z", "ticker": "KXBTC15M-X", "yes_mid": 0.4},
            {"decision": "skip", "reason": "gates_fail", "ticker": "KXBTC15M-X", "ts": "2026-09-08T12:00:01Z"},
            {"event": "mm_would_skip", "ticker": "KXBTC15M-Y", "reason": "premium_gt_065", "would_skip": True},
        ]
        payload = bd.build_payload(rows, ledger_path=SAMPLE, settlements_path=None, skipped_lines=0)
        self.assertEqual(payload["summary"]["n_paper_takes"], 0)
        self.assertIn("No paper takes yet", payload["empty_takes_copy"])
        self.assertEqual(payload["summary"]["n_overlay"], 1)


class NoSideTests(unittest.TestCase):
    def test_disabled_no_is_skip_and_dry_entry_is_take(self) -> None:
        self.assertEqual(
            bd.classify({"event": "paper_calibrated_no_entry", "reason": "no_side_disabled", "side": "NO"}),
            "skip",
        )
        self.assertEqual(
            bd.classify({"event": "paper_calibrated_no_entry", "reason": "gates"}),
            "scan",
        )
        self.assertNotEqual(
            bd.classify({"event": "paper_calibrated_no_entry", "reason": "gates"}),
            "take",
        )
        self.assertEqual(
            bd.classify({"event": "paper_calibrated_dry_entry", "side": "NO"}),
            "take",
        )

    def test_no_entry_does_not_inflate_takes_and_dupes_collapse(self) -> None:
        rows = [
            {
                "event": "paper_calibrated_dry_entry",
                "ts": "2026-09-08T12:00:00Z",
                "ticker": "KXBTC15M-T",
                "side": "YES",
                "ask": 0.50,
                "qty": 4,
            },
            {
                "event": "paper_calibrated_dry_entry",
                "ts": "2026-09-08T13:00:00Z",
                "ticker": "KXBTC15M-T",
                "side": "YES",
                "ask": 0.51,
                "qty": 4,
            },
            {
                "event": "paper_calibrated_dry_entry",
                "ts": "2026-09-08T13:05:00Z",
                "ticker": "KXBTC15M-T",
                "side": "NO",
                "ask": "0.42",
                "yes_ask": 0.58,
                "qty": 4,
            },
            {
                "event": "paper_calibrated_no_entry",
                "ts": "2026-09-08T13:06:00Z",
                "ticker": "KXBTC15M-U",
                "side": "NO",
                "reason": "no_side_disabled",
                "counterfactual": {"ticker": "KXBTC15M-U", "side": "NO", "ask": "0.40", "qty": 4},
            },
            {
                "event": "paper_calibrated_no_entry",
                "ts": "2026-09-08T13:07:00Z",
                "ticker": "KXBTC15M-V",
                "reason": "gates",
            },
        ]
        payload = bd.build_payload(rows, ledger_path=SAMPLE, settlements_path=None, skipped_lines=0)
        self.assertEqual(payload["summary"]["n_paper_takes"], 2)
        self.assertGreaterEqual(payload["summary"]["n_skips"], 1)
        self.assertIn("no_side_disabled", payload["summary"]["skips_by_reason"])
        kinds = [d["kind"] for d in payload["decisions"] if d["ticker"] == "KXBTC15M-T"]
        self.assertEqual(kinds.count("take"), 3)

    def test_no_take_pnl_uses_no_ask_not_yes_ask(self) -> None:
        row = {
            "event": "paper_calibrated_dry_entry",
            "ts": "2026-09-08T20:00:00Z",
            "ticker": "KXBTC15M-NO",
            "side": "NO",
            "ask": "0.42",
            "yes_ask": 0.58,
            "qty": 4,
            "model_p": 0.4,
            "dry_run": True,
        }
        win = bd.normalize_decision(row, "take", 1, {"KXBTC15M-NO": {"yes": 0, "raw": {}}})
        self.assertEqual(win["side"], "NO")
        self.assertEqual(win["outcome"], "win")
        self.assertEqual(win["entry_ask"], 0.42)
        self.assertEqual(win["yes_ask"], 0.58)
        self.assertEqual(win["pnl"], round(4 * (1 - 0.42), 4))
        self.assertNotEqual(win["pnl"], round(4 * (1 - 0.58), 4))

        loss = bd.normalize_decision(row, "take", 1, {"KXBTC15M-NO": {"yes": 1, "raw": {}}})
        self.assertEqual(loss["outcome"], "loss")
        self.assertEqual(loss["pnl"], round(4 * (0 - 0.42), 4))

        explicit = dict(row)
        explicit["no_ask"] = 0.40
        explicit["ask"] = "0.42"
        via_no = bd.normalize_decision(explicit, "take", 1, {"KXBTC15M-NO": {"yes": 0, "raw": {}}})
        self.assertEqual(via_no["entry_ask"], 0.40)
        self.assertEqual(via_no["pnl"], round(4 * (1 - 0.40), 4))

        yes_only = {
            "event": "paper_calibrated_dry_entry",
            "ticker": "KXBTC15M-NO",
            "side": "NO",
            "yes_ask": 0.58,
            "qty": 4,
        }
        missing = bd.normalize_decision(yes_only, "take", 1, {"KXBTC15M-NO": {"yes": 0, "raw": {}}})
        self.assertIsNone(missing["pnl"])
        self.assertEqual(missing["yes_ask"], 0.58)


if __name__ == "__main__":
    unittest.main()
