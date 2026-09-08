"""Build dashboard/data.json from ledger.jsonl (and settlements if present).

Tolerant of mixed JSONL schemas from paper_once_calibrated.py and older local
scanners. Idempotent: overwrites dashboard/data.json and dashboard/data.js.
Stdlib only. Never reads API secrets.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "dashboard"
LEDGER_PATH = ROOT / "ledger.jsonl"
SAMPLE_LEDGER = DASHBOARD / "sample_ledger.jsonl"
SAMPLE_SETTLEMENTS = DASHBOARD / "sample_settlements.json"
REPORT_PATH = ROOT / "last_routine_report.json"
STATE_PATH = ROOT / "paper_state.json"
OUT_JSON = DASHBOARD / "data.json"
RAW_DIR = ROOT / "raw"

ET = ZoneInfo("America/New_York")

META_EVENTS = frozenset({"start", "summary", "routine_start", "mm_overlay_summary"})
OVERLAY_EVENTS = frozenset({"mm_would_skip", "mm_overlay", "mm_compare"})
SKIP_EVENTS = frozenset({"paper_skip", "no_entry"})
TAKE_DECISIONS = frozenset({"take_yes_paper", "take_paper", "paper_take"})

SETTLEMENT_GLOBS = ("*settlement*.json", "*settlements*.json")


def pick(row: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] is not None and row[key] != "":
            return row[key]
    return default


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x > 1.5 and x <= 100:
        return x / 100.0
    return x


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_ts(dt: datetime | None) -> tuple[str | None, str | None]:
    if dt is None:
        return None, None
    utc = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    et = dt.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    return utc, et


def load_jsonl(path: Path) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    if not path.is_file():
        return rows, skipped
    with path.open(encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if isinstance(obj, dict):
                rows.append(obj)
            else:
                skipped += 1
    return rows, skipped


def load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def as_dict_list(blob: Any, *keys: str) -> list[dict]:
    if blob is None:
        return []
    if isinstance(blob, list):
        return [x for x in blob if isinstance(x, dict)]
    if isinstance(blob, dict):
        for key in keys:
            inner = blob.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        data = blob.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(data, list) and isinstance(x, dict)]
        if any(k in blob for k in ("ticker", "market_ticker", "event_ticker", "market_result")):
            return [blob]
    return []


def find_settlements_path(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit if explicit.is_file() else None
    preferred = [
        RAW_DIR / "settlements.json",
        RAW_DIR / "settlement.json",
        RAW_DIR / "market_settlements.json",
    ]
    for path in preferred:
        if path.is_file():
            return path
    if RAW_DIR.is_dir():
        found: list[Path] = []
        for pattern in SETTLEMENT_GLOBS:
            found.extend(RAW_DIR.glob(pattern))
        files = [p for p in found if p.is_file()]
        if files:
            return sorted(files)[0]
    return None


def settlement_yes(row: dict) -> int | None:
    for key in ("market_result", "result", "yes_result", "settlement"):
        val = row.get(key)
        if val is None:
            continue
        s = str(val).strip().lower()
        if s in {"yes", "1", "true", "long", "win"}:
            return 1
        if s in {"no", "0", "false", "short", "loss"}:
            return 0
    y = row.get("yes_count") or row.get("yes")
    n = row.get("no_count") or row.get("no")
    try:
        if y is not None and n is not None:
            yf, nf = float(y), float(n)
            if yf > 0 and nf == 0:
                return 1
            if nf > 0 and yf == 0:
                return 0
    except (TypeError, ValueError):
        pass
    return None


def load_settlement_index(path: Path | None) -> dict[str, dict]:
    """Map ticker / event_ticker -> {yes, raw}."""
    if path is None:
        return {}
    rows = as_dict_list(load_json(path), "settlements", "markets")
    index: dict[str, dict] = {}
    for row in rows:
        y = settlement_yes(row)
        rec = {"yes": y, "raw": row}
        for key in ("ticker", "market_ticker", "event_ticker", "event"):
            val = row.get(key)
            if val:
                index[str(val)] = rec
    return index


def lookup_settlement(row: dict, index: dict[str, dict]) -> dict | None:
    for key in ("ticker", "market_ticker", "event_ticker", "event"):
        val = pick(row, key)
        if val and str(val) in index:
            return index[str(val)]
    return None


def event_name(row: dict) -> str:
    return str(row.get("event") or "").strip()


def decision_name(row: dict) -> str:
    return str(row.get("decision") or "").strip()


def classify(row: dict) -> str:
    """Return overlay | meta | take | skip | scan | other."""
    event = event_name(row)
    decision = decision_name(row)
    event_l = event.lower()

    if event in OVERLAY_EVENTS or (row.get("would_skip") is True and event_l.startswith("mm")):
        return "overlay"
    if event in META_EVENTS:
        return "meta"
    if decision in TAKE_DECISIONS:
        return "take"
    if event_l.startswith("paper_calibrated"):
        if "skip" in event_l:
            return "skip"
        if any(tok in event_l for tok in ("take", "yes", "entry")):
            return "take"
        return "scan"
    if decision == "skip" or event in SKIP_EVENTS or decision == "error":
        return "skip"
    if event == "scan":
        return "scan"
    if pick(row, "ticker", "market_ticker") and (
        decision or "model_p" in row or "yes_mid" in row or "yes_ask" in row
    ):
        return "scan"
    return "other"


def skip_reason_key(row: dict) -> str:
    reason = str(pick(row, "reason", "skip_reason", "filter_reason", default="") or "")
    series = str(row.get("series") or "")
    if series == "KXETH15M" or reason.lower().startswith("eth"):
        return "eth_skipped"
    if reason:
        if len(reason) > 48:
            return reason.split(":")[0][:48]
        return reason
    event = event_name(row)
    return event or "unspecified"


def display_decision(kind: str, row: dict) -> str:
    decision = decision_name(row)
    if decision:
        return decision
    event = event_name(row)
    if kind == "take":
        return decision or event or "take_yes_paper"
    if kind == "skip":
        return decision or event or "skip"
    if kind == "scan":
        return event or "scan"
    return decision or event or kind


def paper_pnl(yes_result: int | None, ask: float | None, clip: int | None) -> float | None:
    if yes_result is None or ask is None:
        return None
    n = clip if clip is not None and clip > 0 else 1
    return round(n * (yes_result - ask), 4)


def relpath(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def last_run_from_aux() -> datetime | None:
    state = load_json(STATE_PATH)
    if isinstance(state, dict):
        dt = parse_ts(state.get("last_run_ts"))
        if dt is None:
            summary = state.get("summary")
            if isinstance(summary, dict):
                dt = parse_ts(summary.get("ts"))
        if dt is not None:
            return dt
    report = load_json(REPORT_PATH)
    if isinstance(report, dict):
        summary = report.get("summary")
        if isinstance(summary, dict):
            dt = parse_ts(summary.get("ts"))
            if dt is not None:
                return dt
        rows = report.get("rows")
        if isinstance(rows, list) and rows:
            last = rows[-1]
            if isinstance(last, dict):
                return parse_ts(last.get("ts"))
    return None


def normalize_decision(row: dict, kind: str, index: int, settle_index: dict[str, dict]) -> dict:
    ts_dt = parse_ts(pick(row, "ts", "timestamp", "created_time", "t", "ts_et"))
    ts_utc, ts_et = format_ts(ts_dt)
    ticker = pick(row, "ticker", "market_ticker", "market")
    event_ticker = pick(row, "event_ticker")
    yes_mid = as_float(pick(row, "yes_mid", "mid", "yes_px"))
    yes_ask = as_float(pick(row, "yes_ask", "ask", "premium", "yes_price_dollars", "yes_price"))
    model_p = as_float(pick(row, "model_p", "p_hat", "p_yes", "prob"))
    edge = as_float(pick(row, "edge", "edge_yes"))
    clip = as_int(pick(row, "clip", "contracts", "count", "hypothetical_contracts", "quantity"))
    reason = pick(row, "reason", "skip_reason", "filter_reason")
    decision = display_decision(kind, row)

    outcome = "n/a"
    pnl = None
    settled = False
    if kind == "take":
        hit = lookup_settlement(row, settle_index)
        if hit and hit.get("yes") is not None:
            settled = True
            y = int(hit["yes"])
            pnl = paper_pnl(y, yes_ask, clip)
            outcome = "win" if y == 1 else "loss"
        else:
            outcome = "n/a"
    elif kind != "take":
        outcome = ""

    return {
        "id": index,
        "ts": ts_dt.isoformat() if ts_dt else None,
        "ts_utc": ts_utc,
        "ts_et": ts_et,
        "ticker": str(ticker) if ticker else "",
        "event_ticker": str(event_ticker) if event_ticker else "",
        "series": str(row.get("series") or ""),
        "yes_mid": yes_mid,
        "yes_ask": yes_ask,
        "model_p": model_p,
        "edge": edge,
        "clip": clip,
        "decision": decision,
        "reason": str(reason) if reason else (event_name(row) or ""),
        "reason_key": skip_reason_key(row) if kind == "skip" else None,
        "event": event_name(row) or None,
        "kind": kind,
        "outcome": outcome,
        "pnl": pnl,
        "settled": settled,
        "dry_run": bool(row.get("dry_run", True)),
    }


def normalize_overlay(row: dict, index: int) -> dict:
    ts_dt = parse_ts(pick(row, "ts", "timestamp", "created_time"))
    ts_utc, ts_et = format_ts(ts_dt)
    ticker = pick(row, "ticker", "market_ticker", "market")
    ask = as_float(pick(row, "ask", "yes_ask", "premium", "yes_price_dollars", "price"))
    count = as_int(pick(row, "count", "clip", "contracts", "quantity"))
    return {
        "id": index,
        "ts": ts_dt.isoformat() if ts_dt else None,
        "ts_utc": ts_utc,
        "ts_et": ts_et,
        "ticker": str(ticker) if ticker else "",
        "ask": ask,
        "count": count,
        "would_skip": bool(row.get("would_skip", event_name(row) == "mm_would_skip")),
        "reason": str(pick(row, "reason", "skip_reason", default="") or ""),
        "event": event_name(row) or "mm_would_skip",
        "note": str(
            row.get("note")
            or "mm overlay: comparison only. Does not cancel, replace, or post live orders."
        ),
    }


def build_payload(
    rows: list[dict],
    *,
    ledger_path: Path,
    settlements_path: Path | None,
    skipped_lines: int,
) -> dict:
    settle_index = load_settlement_index(settlements_path)
    decisions: list[dict] = []
    overlays: list[dict] = []
    n_runs = 0
    kinds_seen: dict[str, int] = {}
    dry_run = True

    for row in rows:
        kind = classify(row)
        kinds_seen[kind] = kinds_seen.get(kind, 0) + 1
        if row.get("dry_run") is False:
            dry_run = False
        if kind == "meta":
            if event_name(row) in {"start", "routine_start"}:
                n_runs += 1
            continue
        if kind == "overlay":
            overlays.append(normalize_overlay(row, len(overlays) + 1))
            continue
        if kind in {"take", "skip", "scan"}:
            decisions.append(normalize_decision(row, kind, len(decisions) + 1, settle_index))

    if n_runs == 0:
        n_runs = kinds_seen.get("meta", 0)

    def ts_key(item: dict) -> str:
        return item.get("ts") or ""

    decisions.sort(key=ts_key, reverse=True)
    overlays.sort(key=ts_key, reverse=True)
    for i, row in enumerate(decisions, start=1):
        row["id"] = i
    for i, row in enumerate(overlays, start=1):
        row["id"] = i

    takes = [d for d in decisions if d["kind"] == "take"]
    skips = [d for d in decisions if d["kind"] == "skip"]
    scans_n = sum(1 for d in decisions if d.get("ticker") and d["kind"] in {"take", "skip", "scan"})

    skips_by_reason: dict[str, int] = {}
    for d in skips:
        key = d.get("reason_key") or d.get("reason") or "unspecified"
        skips_by_reason[key] = skips_by_reason.get(key, 0) + 1

    settled_takes = [t for t in takes if t.get("settled")]
    wins = [t for t in settled_takes if t.get("outcome") == "win"]
    losses = [t for t in settled_takes if t.get("outcome") == "loss"]
    pnl_vals = [t["pnl"] for t in settled_takes if t.get("pnl") is not None]
    settlements_available = bool(settle_index)

    last_dt = last_run_from_aux()
    if last_dt is None:
        stamped = [parse_ts(d.get("ts")) for d in decisions + overlays]
        stamped = [x for x in stamped if x is not None]
        if stamped:
            last_dt = max(stamped)
        else:
            meta_ts = [parse_ts(pick(r, "ts", "timestamp")) for r in rows]
            meta_ts = [x for x in meta_ts if x is not None]
            if meta_ts:
                last_dt = max(meta_ts)
    last_utc, last_et = format_ts(last_dt)

    using_sample = ledger_path.resolve() == SAMPLE_LEDGER.resolve()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "ledger": relpath(ledger_path) or str(ledger_path),
            "settlements": relpath(settlements_path),
            "sample": using_sample,
            "skipped_jsonl_lines": skipped_lines,
        },
        "dry_run": dry_run,
        "live_trading": False,
        "last_run_ts": last_dt.isoformat() if last_dt else None,
        "last_run_ts_utc": last_utc,
        "last_run_ts_et": last_et,
        "summary": {
            "n_scans": scans_n,
            "n_runs": n_runs,
            "n_paper_takes": len(takes),
            "n_skips": len(skips),
            "skips_by_reason": dict(sorted(skips_by_reason.items(), key=lambda kv: (-kv[1], kv[0]))),
            "n_overlay": len(overlays),
            "settlements_available": settlements_available,
            "n_settled_takes": len(settled_takes),
            "n_wins": len(wins),
            "n_losses": len(losses),
            "win_rate": round(len(wins) / len(settled_takes), 4) if settled_takes else None,
            "pnl": round(sum(pnl_vals), 4) if pnl_vals else None,
            "pnl_na": not settlements_available or not takes or not settled_takes,
        },
        "decisions": decisions,
        "overlays": overlays,
        "empty_takes_copy": (
            "No paper takes yet. Scans and skips are logged below — filters "
            "(premium > 0.65, clip > 5, gates) are working. Live trading is off."
        ),
    }


def write_outputs(payload: dict, out_json: Path) -> None:
    out_json.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, default=str)
    out_json.write_text(text + "\n", encoding="utf-8")
    js_path = out_json.with_suffix(".js")
    js_path.write_text("window.DASHBOARD_DATA = " + text + ";\n", encoding="utf-8")


def resolve_ledger(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    if LEDGER_PATH.is_file():
        return LEDGER_PATH
    return SAMPLE_LEDGER


def resolve_settlements(explicit: Path | None, ledger: Path) -> Path | None:
    found = find_settlements_path(explicit)
    if found is not None:
        return found
    if ledger.resolve() == SAMPLE_LEDGER.resolve() and SAMPLE_SETTLEMENTS.is_file():
        return SAMPLE_SETTLEMENTS
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild dashboard/data.json from ledger.jsonl (or the committed sample)."
    )
    parser.add_argument("--ledger", type=Path, default=None, help="JSONL ledger (default: ledger.jsonl or sample)")
    parser.add_argument("--settlements", type=Path, default=None, help="Optional settlements JSON")
    parser.add_argument("--out", type=Path, default=OUT_JSON, help="Output data.json path")
    args = parser.parse_args(argv)

    ledger = resolve_ledger(args.ledger)
    if not ledger.is_file():
        print(f"No ledger at {ledger}. Add ledger.jsonl or dashboard/sample_ledger.jsonl.", file=sys.stderr)
        return 1
    settlements = resolve_settlements(args.settlements, ledger)
    rows, skipped = load_jsonl(ledger)
    payload = build_payload(
        rows,
        ledger_path=ledger,
        settlements_path=settlements,
        skipped_lines=skipped,
    )
    write_outputs(payload, args.out)
    summary = payload["summary"]
    print(
        json.dumps(
            {
                "wrote": str(args.out),
                "ledger": str(ledger),
                "settlements": str(settlements) if settlements else None,
                "n_rows": len(rows),
                "n_scans": summary["n_scans"],
                "n_paper_takes": summary["n_paper_takes"],
                "n_skips": summary["n_skips"],
                "n_overlay": summary["n_overlay"],
                "dry_run": payload["dry_run"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
