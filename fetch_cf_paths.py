"""Fetch GET /live_data/events/{event_ticker} CF paths for settled KXBTC15M windows.

Caches JSON under raw/cf_paths/. Supports --n N (most recent) and --all.
Retries on HTTP 429.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from client import KalshiClient, load_dotenv

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "raw" / "cf_paths"
SUMMARY_PATH = ROOT / "raw" / "cf_paths_summary.json"
BTC_SERIES = "KXBTC15M"


def event_cache_path(event_ticker: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in event_ticker)
    return CACHE_DIR / f"{safe}.json"


def fetch_one(client: KalshiClient, event: dict, *, range_hint: str) -> dict:
    ticker = event.get("event_ticker") or event.get("ticker")
    if not ticker:
        raise ValueError("event missing event_ticker")
    dest = event_cache_path(str(ticker))
    payload = client.get_event_live_data(str(ticker), range_hint=range_hint)
    record = {
        "event_ticker": ticker,
        "series_ticker": event.get("series_ticker") or BTC_SERIES,
        "status": event.get("status"),
        "title": event.get("title"),
        "live_data": payload.get("live_data", payload),
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return {"event_ticker": ticker, "path": str(dest), "cached": True}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Cache CF/live-data paths for settled KXBTC15M events.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--n", type=int, default=24, help="Fetch the N most recently listed settled events (default 24)")
    group.add_argument("--all", action="store_true", help="Paginate all settled KXBTC15M events")
    parser.add_argument("--range", dest="range_hint", default="15min", help="live_data range hint (default 15min)")
    parser.add_argument("--refresh", action="store_true", help="Re-fetch even if a cache file already exists")
    args = parser.parse_args(argv)

    client = KalshiClient(dry_run=True)
    events = client.iter_events(series_ticker=BTC_SERIES, status="settled", with_nested_markets=False)
    if not events:
        print("No settled KXBTC15M events returned.")
        return 0
    if not args.all:
        events = events[: max(0, int(args.n))]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fetched = 0
    skipped = 0
    errors = []
    results = []
    for event in events:
        ticker = event.get("event_ticker") or event.get("ticker")
        dest = event_cache_path(str(ticker)) if ticker else None
        if dest and dest.is_file() and not args.refresh:
            skipped += 1
            results.append({"event_ticker": ticker, "path": str(dest), "cached": True, "skipped": True})
            print(f"skip  {ticker}  (cached)")
            continue
        try:
            rec = fetch_one(client, event, range_hint=args.range_hint)
            fetched += 1
            results.append(rec)
            print(f"ok    {ticker}  -> {rec['path']}")
        except Exception as exc:
            errors.append({"event_ticker": ticker, "error": str(exc)[:300]})
            print(f"fail  {ticker}  {exc}", file=sys.stderr)

    summary = {
        "series": BTC_SERIES,
        "requested": "all" if args.all else args.n,
        "listed_events": len(events),
        "fetched": fetched,
        "skipped_cached": skipped,
        "errors": errors,
        "n_ok": fetched + skipped,
    }
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("listed_events", "fetched", "skipped_cached", "n_ok")}, indent=2))
    return 1 if errors and fetched == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
