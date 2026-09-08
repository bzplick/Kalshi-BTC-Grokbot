"""Scan open KXBTC15M markets (optional KXETH15M with a crude fallback).

Prints a table: ticker, time left, yes bid/ask, model_p, edge, gates.
BTC uses edge_model; ETH is a crude mid-as-p fallback and is off by default.
"""

from __future__ import annotations

import argparse
import sys

from client import (
    KalshiClient,
    load_dotenv,
    minutes_left,
    quotes_from_market,
)
from edge_model import THR, edge_yes, gate_flags, gates_pass
from filters import DEFAULT_CLIP, MAX_CLIP, MAX_ENTRY_PREMIUM, clip_from_dollars, should_skip_entry
from paper_once_calibrated import evaluate_btc_market

BTC_SERIES = "KXBTC15M"
ETH_SERIES = "KXETH15M"


def _fmt(x, digits=3) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):.{digits}f}"
    except (TypeError, ValueError):
        return str(x)


def evaluate_eth_crude(client: KalshiClient, market: dict) -> dict:
    """Crude ETH fallback: treat yes_mid as p (efficient-market, no edge_model)."""
    ticker = market.get("ticker") or ""
    quotes = quotes_from_market(market)
    try:
        book = client.get_orderbook(ticker)
        quotes = quotes_from_market(market, book)
    except Exception as exc:
        quotes["orderbook_error"] = str(exc)[:200]
    left = minutes_left(market)
    yes_mid = quotes.get("yes_mid")
    yes_ask = quotes.get("yes_ask")
    spread = quotes.get("spread")
    p = yes_mid
    edge = edge_yes(p, yes_ask) if p is not None and yes_ask is not None else None
    flags = {}
    if yes_ask is not None and spread is not None and left is not None and edge is not None:
        flags = gate_flags(yes_ask=yes_ask, spread=spread, minutes_left=left, edge=edge, thr=THR)
    clip = clip_from_dollars(yes_ask) if yes_ask is not None else DEFAULT_CLIP
    filter_skip, filter_reason = should_skip_entry(yes_ask, clip)
    return {
        "series": ETH_SERIES,
        "ticker": ticker,
        "yes_bid": quotes.get("yes_bid"),
        "yes_ask": yes_ask,
        "yes_mid": yes_mid,
        "spread": spread,
        "minutes_left": left,
        "ret_1m": None,
        "model_p": p,
        "edge": edge,
        "gates": flags,
        "clip": clip,
        "max_clip": MAX_CLIP,
        "filter_skip": filter_skip,
        "filter_reason": filter_reason,
        "model": "crude_fallback",
        "note": "ETH crude fallback (yes_mid as p); not edge_model. Do not trade.",
        "decision": "skip",
        "reason": filter_reason or "eth_crude_fallback",
    }


def print_table(rows: list[dict]) -> None:
    headers = (
        "ticker",
        "left_m",
        "yes_bid",
        "yes_ask",
        "model_p",
        "edge",
        "gates",
        "skip",
        "clip",
        "model",
    )
    body = []
    for r in rows:
        flags = r.get("gates") or {}
        if r.get("filter_skip") and r.get("filter_reason"):
            gate_s = str(r.get("filter_reason"))
        elif flags:
            gate_s = "PASS" if gates_pass(flags) else ",".join(
                k for k, v in flags.items() if not v
            ) or "FAIL"
        else:
            gate_s = "—"
        skip_s = str(r.get("reason") or "—") if r.get("decision") == "skip" else "—"
        body.append(
            [
                str(r.get("ticker") or ""),
                _fmt(r.get("minutes_left"), 1),
                _fmt(r.get("yes_bid")),
                _fmt(r.get("yes_ask")),
                _fmt(r.get("model_p")),
                _fmt(r.get("edge")),
                gate_s,
                skip_s,
                str(r.get("clip") if r.get("clip") is not None else "—"),
                str(r.get("model") or "edge_model"),
            ]
        )
    widths = [len(h) for h in headers]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for row in body:
        print(fmt.format(*row))


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Scan 15m crypto markets (BTC edge_model; ETH optional crude).")
    parser.add_argument("--eth", action="store_true", help="Also scan KXETH15M with a crude yes_mid fallback")
    parser.add_argument("--json", action="store_true", help="Print JSON rows after the table")
    args = parser.parse_args(argv)

    client = KalshiClient(dry_run=True)
    rows: list[dict] = []

    btc_markets = client.iter_markets(series_ticker=BTC_SERIES, status="open")
    if not btc_markets:
        print("No open KXBTC15M markets.")
    for market in btc_markets:
        row = evaluate_btc_market(client, market)
        row["model"] = "edge_model"
        rows.append(row)

    if args.eth:
        print("ETH: crude fallback only (not edge_model); skipped for trading decisions.")
        eth_markets = client.iter_markets(series_ticker=ETH_SERIES, status="open")
        if not eth_markets:
            print("No open KXETH15M markets.")
        for market in eth_markets:
            rows.append(evaluate_eth_crude(client, market))
    else:
        print("ETH skipped (pass --eth for crude fallback table rows). BTC uses edge_model.")

    if rows:
        print_table(rows)
    takes = [r for r in rows if r.get("decision") == "take_yes_paper"]
    print()
    print(
        f"dry_run=1  live_trading=off  max_premium={MAX_ENTRY_PREMIUM}  max_clip={MAX_CLIP}  "
        f"default_clip={DEFAULT_CLIP}  btc_rows={sum(1 for r in rows if r.get('series')==BTC_SERIES)}  "
        f"paper_takes={len(takes)}  "
        f"skip_premium={sum(1 for r in rows if r.get('reason')=='premium_gt_065')}  "
        f"skip_clip={sum(1 for r in rows if r.get('reason')=='clip_gt_5')}  (never POSTs)"
    )
    if args.json:
        import json

        for r in rows:
            print(json.dumps(r, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
