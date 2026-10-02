"""Dry-run BTC-only scan of open KXBTC15M markets.

Uses orderbook mid, edge_model.model_p, and paper gates. Logs JSONL decisions.
Never POSTs orders. Skips ETH.

NO-side paper entries are off unless PAPER_ALLOW_NO=1. Scoring of 110 paper
takes showed NO lost (40.8% win, -11.9% ROI; the model overestimates NO by
~17 points) while YES was fine. A disabled would-be NO entry is logged as
paper_calibrated_no_entry with a counterfactual — not as a dry entry.
"""

from __future__ import annotations

import json
import os
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
from filters import (
    DEFAULT_CLIP,
    MAX_CLIP,
    MAX_ENTRY_PREMIUM,
    clip_from_dollars,
    should_skip_entry,
)

ROOT = Path(__file__).resolve().parent
BTC_SERIES = "KXBTC15M"
ETH_SERIES = "KXETH15M"
LEDGER_PATH = ROOT / "ledger.jsonl"
REPORT_PATH = ROOT / "last_routine_report.json"
PAPER_STATE_PATH = ROOT / "paper_state.json"

_PAPER_TRUE = {"1", "true", "yes", "on"}


def allow_no_entries() -> bool:
    """NO paper entries stay off unless PAPER_ALLOW_NO is explicitly enabled."""
    return os.environ.get("PAPER_ALLOW_NO", "").strip().lower() in _PAPER_TRUE


# Default False. Read again via allow_no_entries() at run time.
ALLOW_NO_ENTRIES = allow_no_entries()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _px(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


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

    no_ask = _px(1.0 - float(yes_bid)) if yes_bid is not None else None
    if yes_mid is None or yes_ask is None or left is None or spread is None:
        row.update(
            {
                "model_p": None,
                "edge": None,
                "edge_no": None,
                "no_ask": no_ask,
                "no_gates": {},
                "model_source": "edge_model",
                "gates": {},
                "decision": "skip",
                "reason": "incomplete_quotes",
            }
        )
        return row

    p = model_p({"yes_mid": yes_mid, "minutes_left": left, "ret_1m": ret})
    edge = edge_yes(p, yes_ask)
    flags = gate_flags(yes_ask=yes_ask, spread=spread, minutes_left=left, edge=edge, thr=THR)
    size_cap = max_trade_dollars()
    clip = clip_from_dollars(yes_ask, size_cap)
    filter_skip, filter_reason = should_skip_entry(yes_ask, clip)
    take = gates_pass(flags) and not filter_skip and clip >= 1
    if filter_skip:
        decision, reason = "skip", filter_reason
    elif not gates_pass(flags):
        decision, reason = "skip", "gates_fail"
    elif clip < 1:
        decision, reason = "skip", "clip_zero"
    else:
        decision, reason = "take_yes_paper", "gates_pass"
    edge_no = None
    no_flags: dict = {}
    if no_ask is not None:
        edge_no_raw = (1.0 - p) - no_ask
        edge_no = _px(edge_no_raw)
        no_flags = gate_flags(yes_ask=no_ask, spread=spread, minutes_left=left, edge=edge_no_raw, thr=THR)
    row.update(
        {
            "model_p": p,
            "model_source": "edge_model",
            "edge": edge,
            "edge_no": edge_no,
            "no_ask": no_ask,
            "no_gates": no_flags,
            "gates": flags,
            "thr": THR,
            "max_trade_dollars": size_cap,
            "max_entry_premium": MAX_ENTRY_PREMIUM,
            "max_clip": MAX_CLIP,
            "default_clip": DEFAULT_CLIP,
            "hypothetical_contracts": clip if take else 0,
            "clip": clip,
            "filter_skip": filter_skip,
            "filter_reason": filter_reason,
            "decision": decision,
            "reason": reason,
        }
    )
    if filter_skip:
        row["event"] = "paper_skip"
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


def _f(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _gate_open(edge: float | None, ask: float | None, flags: object) -> bool:
    if edge is None or ask is None or not isinstance(flags, dict) or not flags:
        return False
    return gates_pass(flags)


def select_best_side(rows: list[dict]) -> dict | None:
    """Highest gate-passing side across the scan. Equal edge prefers YES.

    Filters are applied later. YES rows already recorded by evaluate_btc_market
    are not rewritten here.
    """
    best: dict | None = None
    for row in rows:
        if row.get("series") not in (None, BTC_SERIES) or not row.get("ticker"):
            continue
        if row.get("model_p") is None:
            continue
        cands = []
        yes_edge = _f(row.get("edge"))
        yes_ask = _f(row.get("yes_ask"))
        if _gate_open(yes_edge, yes_ask, row.get("gates")):
            cands.append(
                {
                    "row": row,
                    "side": "YES",
                    "ask": yes_ask,
                    "edge": yes_edge,
                    "gate_reason": "gates_pass",
                }
            )
        no_edge = _f(row.get("edge_no"))
        no_ask = _f(row.get("no_ask"))
        if _gate_open(no_edge, no_ask, row.get("no_gates")):
            cands.append(
                {
                    "row": row,
                    "side": "NO",
                    "ask": no_ask,
                    "edge": no_edge,
                    "gate_reason": "gates_pass",
                }
            )
        for cand in cands:
            if best is None or cand["edge"] > best["edge"]:
                best = cand
            elif cand["edge"] == best["edge"] and cand["side"] == "YES" and best["side"] == "NO":
                best = cand
    return best


def _nearest_btc(rows: list[dict]) -> dict | None:
    btc = [r for r in rows if r.get("series") in (None, BTC_SERIES)]
    if not btc:
        return None
    with_ticker = [r for r in btc if r.get("ticker")]
    pool = with_ticker or btc
    return min(
        pool,
        key=lambda r: abs(r["minutes_left"]) if isinstance(r.get("minutes_left"), (int, float)) else 1e9,
    )


def _ledger_event(event: str, **fields: object) -> dict:
    row = {
        "ts": utc_now_iso(),
        "event": event,
        "dry_run": True,
        "would_post": False,
        "posted": False,
        "live_trading": False,
    }
    row.update(fields)
    return row


def apply_best_side(rows: list[dict], *, allow_no: bool) -> dict:
    """Choose one best paper side. Never places an order.

    YES market rows stay as evaluate_btc_market wrote them. When the best side
    is NO and allow_no is false, there is no DRY_ENTRY and no
    paper_calibrated_dry_entry — only a paper_calibrated_no_entry counterfactual.
    """
    best = select_best_side(rows)
    if best is None:
        row = _nearest_btc(rows)
        skip = (row.get("reason") if row else None) or "gates"
        event = _ledger_event(
            "paper_calibrated_no_entry",
            reason="gates",
            ticker=row.get("ticker") if row else None,
        )
        return {
            "result": "NO_ENTRY",
            "lines": ["NO_ENTRY (gates)", json.dumps(event, default=str)],
            "extra_events": [event],
            "side": None,
            "qty": None,
            "skip": skip,
            "event": "paper_calibrated_no_entry",
            "row": row,
        }

    row = best["row"]
    side = best["side"]
    ask = best["ask"]
    edge = best["edge"]
    ticker = row.get("ticker")

    if side == "NO" and not allow_no:
        qty = clip_from_dollars(ask, max_trade_dollars())
        if qty < 1:
            qty = DEFAULT_CLIP
        reason = "no_side_disabled"
        why = best["gate_reason"]
        cf = {
            "ticker": ticker,
            "side": "NO",
            "ask": str(ask),
            "qty": qty,
            "edge": str(edge),
            "model_p": row.get("model_p"),
            "model_source": row.get("model_source") or "edge_model",
            "yes_mid": row.get("yes_mid"),
            "minutes_left": row.get("minutes_left"),
            "ret_1m": row.get("ret_1m"),
            "gate_reason": why,
        }
        event = _ledger_event(
            "paper_calibrated_no_entry",
            reason=reason,
            skip_reason=f"{reason}: best side NO ({why}); PAPER_ALLOW_NO=0",
            ticker=ticker,
            side="NO",
            counterfactual=cf,
        )
        model_p = float(row["model_p"])
        line = (
            f"NO_ENTRY ({reason}) would-be NO {ticker} ask={ask} "
            f"edge={edge} model_p={model_p:.4f} [counterfactual logged]"
        )
        return {
            "result": "NO_ENTRY",
            "lines": [line, json.dumps(event, default=str)],
            "extra_events": [event],
            "side": None,
            "qty": None,
            "skip": f"{reason} (cf NO ask={ask} edge={edge})",
            "event": "paper_calibrated_no_entry",
            "row": row,
        }

    if side == "YES":
        # Remote YES decision is already on the row (take or skip). Do not add
        # a second ledger event and do not retake a row the YES path skipped.
        if row.get("decision") == "take_yes_paper":
            qty = row.get("hypothetical_contracts") or row.get("clip") or DEFAULT_CLIP
            return {
                "result": "DRY_ENTRY",
                "lines": [],
                "extra_events": [],
                "side": "YES",
                "qty": qty,
                "skip": None,
                "event": "take_yes_paper",
                "row": row,
            }
        return {
            "result": "PAPER_SKIP" if row.get("filter_skip") else "NO_ENTRY",
            "lines": [],
            "extra_events": [],
            "side": "YES",
            "qty": row.get("clip"),
            "skip": row.get("reason"),
            "event": row.get("event") or "paper_skip",
            "row": row,
        }

    qty = clip_from_dollars(ask, max_trade_dollars())
    if qty < 1:
        qty = DEFAULT_CLIP
    skip, skip_reason = should_skip_entry(ask, qty)
    if skip:
        event = _ledger_event(
            "paper_skip",
            reason=skip_reason,
            ticker=ticker,
            side="NO",
            ask=str(ask),
            no_ask=ask,
            qty=qty,
            edge=str(edge),
            model_p=row.get("model_p"),
        )
        line = f"PAPER_SKIP {skip_reason} NO {ticker} ask={ask} qty={qty}"
        return {
            "result": "PAPER_SKIP",
            "lines": [line, json.dumps(event, default=str)],
            "extra_events": [event],
            "side": "NO",
            "qty": qty,
            "skip": skip_reason,
            "event": "paper_skip",
            "row": row,
        }

    event = _ledger_event(
        "paper_calibrated_dry_entry",
        ticker=ticker,
        side="NO",
        ask=str(ask),
        no_ask=ask,
        qty=qty,
        clip=qty,
        edge=str(edge),
        model_p=row.get("model_p"),
        model_source=row.get("model_source") or "edge_model",
        yes_mid=row.get("yes_mid"),
        yes_ask=row.get("yes_ask"),
        minutes_left=row.get("minutes_left"),
        ret_1m=row.get("ret_1m"),
        reason=best["gate_reason"],
        max_clip=MAX_CLIP,
        max_entry_premium=MAX_ENTRY_PREMIUM,
    )
    src = event["model_source"]
    line = (
        f"DRY_ENTRY NO {ticker} ask={ask} qty={qty} "
        f"edge={edge} model_p={float(row['model_p']):.4f} src={src} ({best['gate_reason']}) "
        f"filters=premium<={MAX_ENTRY_PREMIUM} clip<={MAX_CLIP}"
    )
    return {
        "result": "DRY_ENTRY",
        "lines": [line, json.dumps(event, default=str)],
        "extra_events": [event],
        "side": "NO",
        "qty": qty,
        "skip": None,
        "event": "paper_calibrated_dry_entry",
        "row": row,
    }


def write_report(
    result: str,
    *,
    row: dict | None = None,
    side: str | None = None,
    qty: int | None = None,
    skip: str | None = None,
    event: str | None = None,
    rows: list[dict] | None = None,
    summary: dict | None = None,
) -> None:
    """Write last_routine_report.json for NO_ENTRY / PAPER_SKIP / DRY_ENTRY.

    Keeps the remote rows+summary block the dashboard already reads, and adds
    the flat result fields. Best effort — a report failure must not crash the scan.
    """
    prev: dict = {}
    try:
        loaded = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            prev = loaded
    except Exception:
        prev = {}
    snap = row or {}
    rep = {
        "ts": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "result": result,
        "ticker": snap.get("ticker"),
        "side": side,
        "yes_ask": snap.get("yes_ask"),
        "no_ask": snap.get("no_ask"),
        "model_p": snap.get("model_p"),
        "edge_yes": snap.get("edge"),
        "edge_no": snap.get("edge_no"),
        "qty": qty,
        "skip": skip,
        "minutes_left": snap.get("minutes_left"),
        "live_off": True,
        "event": event,
        "dashboard_rebuilt": prev.get("dashboard_rebuilt", False),
        "auth_failed": False,
        "ledger_appended": True,
        "rows": rows or [],
        "summary": summary or {},
    }
    try:
        REPORT_PATH.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    except Exception as exc:
        print(f"report write failed: {str(exc)[:80]}")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        print("usage: python paper_once_calibrated.py")
        print("Dry-run BTC-only KXBTC15M scan. Never POSTs orders. ETH is skipped.")
        print("NO-side paper entries are off unless PAPER_ALLOW_NO=1.")
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
        "ask_band": [0.40, MAX_ENTRY_PREMIUM],
        "max_entry_premium": MAX_ENTRY_PREMIUM,
        "max_clip": MAX_CLIP,
        "default_clip": DEFAULT_CLIP,
        "max_spread": 0.02,
        "min_minutes_left": 2.0,
        "authenticated": client.authenticated,
        "allow_no": allow_no_entries(),
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
        write_report("NO_ENTRY", skip=err["reason"], event=None, rows=[err], summary=None)
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

    allow_no = allow_no_entries()
    outcome = apply_best_side(rows, allow_no=allow_no)
    for line in outcome["lines"]:
        print(line)
    for extra in outcome["extra_events"]:
        if extra.get("would_post") or extra.get("posted"):
            raise RuntimeError("paper scan tried to mark an order as posted")
    logged = rows + outcome["extra_events"]
    takes = [r for r in logged if r.get("decision") == "take_yes_paper"]
    dry_no = [r for r in outcome["extra_events"] if r.get("event") == "paper_calibrated_dry_entry"]
    summary = {
        "ts": utc_now_iso(),
        "event": "summary",
        "n_btc_markets": sum(1 for r in rows if r.get("series") == BTC_SERIES and r.get("ticker")),
        "n_take_paper": len(takes),
        "n_skip": sum(1 for r in rows if r.get("decision") == "skip"),
        "n_skip_premium_gt_065": sum(1 for r in rows if r.get("reason") == "premium_gt_065"),
        "n_skip_clip_gt_5": sum(1 for r in rows if r.get("reason") == "clip_gt_5"),
        "dry_run": True,
        "posted_orders": 0,
        "would_post": False,
        "eth_skipped": True,
        "tickers_take": [r.get("ticker") for r in takes],
        "max_entry_premium": MAX_ENTRY_PREMIUM,
        "max_clip": MAX_CLIP,
        "default_clip": DEFAULT_CLIP,
        "allow_no_entries": allow_no,
        "best_side_result": outcome["result"],
        "best_side": outcome["side"],
        "n_no_dry_entry": len(dry_no),
    }
    print(json.dumps(summary))
    append_jsonl(LEDGER_PATH, logged + [summary])
    write_report(
        outcome["result"],
        row=outcome["row"],
        side=outcome["side"],
        qty=outcome["qty"],
        skip=outcome["skip"],
        event=outcome["event"],
        rows=logged,
        summary=summary,
    )
    PAPER_STATE_PATH.write_text(
        json.dumps({"last_run_ts": time.time(), "summary": summary}, indent=2, default=str),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
