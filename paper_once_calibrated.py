"""Dry-run BTC-only scan of open KXBTC15M markets.

Uses orderbook mid, edge_model.model_p, and paper gates. Logs JSONL decisions.
Never POSTs orders. Skips ETH.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from client import (
    KalshiClient,
    dry_run_enabled,
    fetch_ret_1m,
    load_dotenv,
    max_trade_dollars,
    minutes_left,
    quotes_from_market,
)
from edge_model import THR, edge_yes, gate_flags, gates_pass, model_p

ROOT = Path(__file__).resolve().parent
BTC_SERIES = "KXBTC15M"
ETH_SERIES = "KXETH15M"
LEDGER_PATH = ROOT / "ledger.jsonl"
REPORT_PATH = ROOT / "last_routine_report.json"
PAPER_STATE_PATH = ROOT / "paper_state.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def evaluate_btc_market(client: KalshiClient, market: dict) -> dict:
    ticker = market.get("ticker") or ""
    quotes = quotes_from_market(market)
    try:
        book = client.get_orderbook(ticker)
        quotes = quotes_from_market(market, book)
    except Exception as exc:
        quotes["orderbook_error"] = str(exc)[:200]

    left = minutes_left(market)
    ret = fetch_ret_1m(client, market)
    yes_mid = quotes.get("yes_mid")
    yes_ask = quotes.get("yes_ask")
    yes_bid = quotes.get("yes_bid")
    spread = quotes.get("spread")

    row: dict = {
        "ts": utc_now_iso(),
        "series": BTC_SERIES,
        "ticker": ticker,
        "event_ticker": market.get("event_ticker"),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_mid": yes_mid,
        "spread": spread,
        "minutes_left": left,
        "ret_1m": ret,
        "dry_run": True,
        "would_post": False,
        "posted": False,
    }

    if yes_mid is None or yes_ask is None or left is None or spread is None:
        row.update(
            {
                "model_p": None,
                "edge": None,
                "gates": {},
                "decision": "skip",
                "reason": "incomplete_quotes",
            }
        )
        return row

    p = model_p({"yes_mid": yes_mid, "minutes_left": left, "ret_1m": ret})
    edge = edge_yes(p, yes_ask)
    flags = gate_flags(yes_ask=yes_ask, spread=spread, minutes_left=left, edge=edge, thr=THR)
    take = gates_pass(flags)
    size_cap = max_trade_dollars()
    contracts = int(size_cap / yes_ask) if take and yes_ask > 0 else 0
    row.update(
        {
            "model_p": p,
            "edge": edge,
            "gates": flags,
            "thr": THR,
            "max_trade_dollars": size_cap,
            "hypothetical_contracts": contracts,
            "decision": "take_yes_paper" if take else "skip",
            "reason": "gates_pass" if take else "gates_fail",
        }
    )
    return row


def skip_eth_note() -> dict:
    return {
        "ts": utc_now_iso(),
        "series": ETH_SERIES,
        "decision": "skip",
        "reason": "ETH skipped: paper_once_calibrated is BTC-only (KXBTC15M). No ETH scoring, no orders.",
        "dry_run": True,
        "would_post": False,
        "posted": False,
    }


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        print("usage: python paper_once_calibrated.py")
        print("Dry-run BTC-only KXBTC15M scan. Never POSTs orders. ETH is skipped.")
        return 0

    if not dry_run_enabled():
        print(
            "NOTE: KALSHI_DRY_RUN is not 1, but paper_once_calibrated.py never POSTs orders.",
            file=sys.stderr,
        )

    client = KalshiClient(dry_run=True)
    rows: list[dict] = []
    print(json.dumps({
        "event": "start",
        "dry_run": True,
        "live_trading": False,
        "series": BTC_SERIES,
        "skip_eth": True,
        "thr": THR,
        "ask_band": [0.40, 0.65],
        "max_spread": 0.02,
        "min_minutes_left": 2.0,
        "authenticated": client.authenticated,
    }))

    try:
        markets = client.iter_markets(series_ticker=BTC_SERIES, status="open")
    except Exception as exc:
        err = {
            "ts": utc_now_iso(),
            "decision": "error",
            "reason": f"markets_fetch_failed: {exc}",
            "dry_run": True,
            "would_post": False,
        }
        print(json.dumps(err, default=str))
        append_jsonl(LEDGER_PATH, [err])
        return 1

    if not markets:
        empty = {
            "ts": utc_now_iso(),
            "series": BTC_SERIES,
            "decision": "skip",
            "reason": "no_open_KXBTC15M_markets",
            "dry_run": True,
            "would_post": False,
        }
        rows.append(empty)
        print(json.dumps(empty))
    else:
        for market in markets:
            row = evaluate_btc_market(client, market)
            rows.append(row)
            print(json.dumps(row, default=str))

    eth_note = skip_eth_note()
    rows.append(eth_note)
    print(json.dumps(eth_note))

    takes = [r for r in rows if r.get("decision") == "take_yes_paper"]
    summary = {
        "ts": utc_now_iso(),
        "event": "summary",
        "n_btc_markets": sum(1 for r in rows if r.get("series") == BTC_SERIES and r.get("ticker")),
        "n_take_paper": len(takes),
        "n_skip": sum(1 for r in rows if r.get("decision") == "skip"),
        "dry_run": True,
        "posted_orders": 0,
        "would_post": False,
        "eth_skipped": True,
        "tickers_take": [r.get("ticker") for r in takes],
    }
    print(json.dumps(summary))
    append_jsonl(LEDGER_PATH, rows + [summary])
    REPORT_PATH.write_text(json.dumps({"rows": rows, "summary": summary}, indent=2, default=str), encoding="utf-8")
    PAPER_STATE_PATH.write_text(
        json.dumps({"last_run_ts": time.time(), "summary": summary}, indent=2, default=str),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
