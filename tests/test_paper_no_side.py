"""NO-side paper entries stay off unless allow_no is set. Never posts orders."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import paper_once_calibrated as paper

PASS = {"ask_band": True, "spread": True, "time": True, "thr": True}
FAIL = {"ask_band": True, "spread": True, "time": True, "thr": False}

CF_KEYS = {
    "ticker",
    "side",
    "ask",
    "qty",
    "edge",
    "model_p",
    "model_source",
    "yes_mid",
    "minutes_left",
    "ret_1m",
    "gate_reason",
}


def _row(**overrides: object) -> dict:
    row = {
        "series": "KXBTC15M",
        "ticker": "KXBTC15M-T",
        "model_p": 0.42,
        "model_source": "edge_model",
        "yes_mid": 0.48,
        "yes_ask": 0.62,
        "yes_bid": 0.55,
        "edge": 0.02,
        "gates": FAIL,
        "edge_no": 0.12,
        "no_ask": 0.45,
        "no_gates": PASS,
        "minutes_left": 8.0,
        "ret_1m": 0.001,
        "decision": "skip",
        "reason": "gates_fail",
        "clip": 0,
        "dry_run": True,
        "would_post": False,
        "posted": False,
    }
    row.update(overrides)
    return row


class NoSidePaperTests(unittest.TestCase):
    def test_flag_defaults_off(self) -> None:
        import os

        old = os.environ.pop("PAPER_ALLOW_NO", None)
        try:
            self.assertFalse(paper.allow_no_entries())
        finally:
            if old is not None:
                os.environ["PAPER_ALLOW_NO"] = old
        os.environ["PAPER_ALLOW_NO"] = "1"
        try:
            self.assertTrue(paper.allow_no_entries())
        finally:
            os.environ.pop("PAPER_ALLOW_NO", None)
            if old is not None:
                os.environ["PAPER_ALLOW_NO"] = old

    def test_best_no_disabled_logs_counterfactual_not_dry_entry(self) -> None:
        row = _row()
        out = paper.apply_best_side([row], allow_no=False)
        self.assertEqual(out["result"], "NO_ENTRY")
        self.assertIsNone(out["side"])
        self.assertIsNone(out["qty"])
        self.assertTrue(out["lines"][0].startswith("NO_ENTRY (no_side_disabled) would-be NO KXBTC15M-T "))
        self.assertIn("ask=0.45", out["lines"][0])
        events = out["extra_events"]
        self.assertEqual([e["event"] for e in events], ["paper_calibrated_no_entry"])
        event = events[0]
        self.assertEqual(event["reason"], "no_side_disabled")
        self.assertIn("no_side_disabled", event["skip_reason"])
        self.assertIn("PAPER_ALLOW_NO=0", event["skip_reason"])
        self.assertEqual(event["side"], "NO")
        self.assertFalse(event["would_post"])
        self.assertFalse(event["posted"])
        self.assertTrue(event["dry_run"])
        self.assertEqual(set(event["counterfactual"]), CF_KEYS)
        cf = event["counterfactual"]
        self.assertEqual(cf["ticker"], "KXBTC15M-T")
        self.assertEqual(cf["side"], "NO")
        self.assertEqual(cf["ask"], "0.45")
        self.assertEqual(cf["model_p"], 0.42)
        self.assertEqual(cf["model_source"], "edge_model")
        self.assertEqual(cf["yes_mid"], 0.48)
        self.assertEqual(cf["minutes_left"], 8.0)
        self.assertEqual(cf["ret_1m"], 0.001)
        self.assertEqual(cf["gate_reason"], "gates_pass")
        self.assertIsInstance(cf["qty"], int)
        self.assertGreaterEqual(cf["qty"], 1)
        self.assertNotIn("paper_calibrated_dry_entry", json.dumps(events))
        self.assertNotIn("DRY_ENTRY", out["lines"][0])
        self.assertEqual(row["decision"], "skip")

    def test_yes_entry_unchanged_when_yes_is_best(self) -> None:
        row = _row(
            ticker="KXBTC15M-Y",
            edge=0.10,
            gates=PASS,
            edge_no=0.04,
            no_gates=FAIL,
            decision="take_yes_paper",
            reason="gates_pass",
            clip=4,
            hypothetical_contracts=4,
            yes_ask=0.50,
        )
        out = paper.apply_best_side([row], allow_no=False)
        self.assertEqual(out["result"], "DRY_ENTRY")
        self.assertEqual(out["side"], "YES")
        self.assertEqual(out["extra_events"], [])
        self.assertEqual(out["lines"], [])
        self.assertEqual(row["decision"], "take_yes_paper")
        self.assertNotEqual(out["event"], "paper_calibrated_dry_entry")

    def test_equal_edge_prefers_yes(self) -> None:
        row = _row(
            edge=0.10,
            gates=PASS,
            edge_no=0.10,
            no_gates=PASS,
            decision="take_yes_paper",
            reason="gates_pass",
            clip=4,
            hypothetical_contracts=4,
            yes_ask=0.50,
            no_ask=0.50,
        )
        out = paper.apply_best_side([row], allow_no=False)
        self.assertEqual(out["side"], "YES")
        self.assertEqual(out["extra_events"], [])

    def test_allow_no_dry_entry_uses_no_ask_and_does_not_post(self) -> None:
        row = _row(yes_ask=0.70)
        out = paper.apply_best_side([row], allow_no=True)
        self.assertEqual(out["result"], "DRY_ENTRY")
        self.assertEqual(out["side"], "NO")
        self.assertTrue(out["lines"][0].startswith("DRY_ENTRY NO KXBTC15M-T "))
        event = out["extra_events"][0]
        self.assertEqual(event["event"], "paper_calibrated_dry_entry")
        self.assertEqual(event["side"], "NO")
        self.assertEqual(event["no_ask"], 0.45)
        self.assertEqual(event["ask"], "0.45")
        self.assertNotEqual(float(event["ask"]), row["yes_ask"])
        self.assertFalse(event["would_post"])
        self.assertFalse(event["posted"])
        self.assertTrue(event["dry_run"])
        self.assertFalse(event["live_trading"])
        blob = json.dumps(event)
        self.assertNotIn("portfolio/orders", blob)

    def test_disabled_no_wins_over_lower_yes_take_without_a_dry_entry(self) -> None:
        yes = _row(
            ticker="KXBTC15M-Y",
            edge=0.06,
            gates=PASS,
            no_gates=FAIL,
            edge_no=0.01,
            decision="take_yes_paper",
            reason="gates_pass",
            clip=4,
            hypothetical_contracts=4,
            yes_ask=0.50,
        )
        no = _row(ticker="KXBTC15M-N", edge_no=0.15, no_ask=0.40)
        out = paper.apply_best_side([yes, no], allow_no=False)
        self.assertEqual(out["result"], "NO_ENTRY")
        self.assertEqual(out["extra_events"][0]["event"], "paper_calibrated_no_entry")
        self.assertEqual(out["extra_events"][0]["counterfactual"]["ask"], "0.4")
        self.assertEqual(yes["decision"], "take_yes_paper")
        self.assertNotIn("paper_calibrated_dry_entry", json.dumps(out["extra_events"]))

    def test_report_written_for_each_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last_routine_report.json"
            path.write_text(json.dumps({"dashboard_rebuilt": True, "rows": [{"old": True}]}), encoding="utf-8")
            original = paper.REPORT_PATH
            paper.REPORT_PATH = path
            try:
                for result, event in (
                    ("NO_ENTRY", "paper_calibrated_no_entry"),
                    ("PAPER_SKIP", "paper_skip"),
                    ("DRY_ENTRY", "take_yes_paper"),
                ):
                    paper.write_report(
                        result,
                        row={"ticker": "KXBTC15M-T", "yes_ask": 0.5, "no_ask": 0.45, "model_p": 0.6, "edge": 0.1, "edge_no": 0.05, "minutes_left": 4},
                        side="YES" if result != "NO_ENTRY" else None,
                        qty=4 if result != "NO_ENTRY" else None,
                        skip=None if result == "DRY_ENTRY" else "example",
                        event=event,
                        rows=[{"ticker": "KXBTC15M-T"}],
                        summary={"ts": "2026-09-08T00:00:00+00:00", "event": "summary"},
                    )
                    rep = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(rep["result"], result)
                    self.assertEqual(rep["event"], event)
                    self.assertTrue(rep["live_off"])
                    self.assertTrue(rep["dashboard_rebuilt"])
                    self.assertFalse(rep["auth_failed"])
                    self.assertIn("summary", rep)
                    self.assertIn("rows", rep)
            finally:
                paper.REPORT_PATH = original

    def test_scanner_source_has_no_order_post(self) -> None:
        text = Path(paper.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/portfolio/orders", text)
        self.assertNotIn("create_order", text)
        self.assertNotIn(".post(", text)


if __name__ == "__main__":
    unittest.main()
