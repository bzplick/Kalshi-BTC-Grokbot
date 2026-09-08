"""Paper risk filters from the mm loser autopsy.

Skip paper entry if ask/premium > 0.65 or clip count > 5.
Default clip is 4 when sizing from dollars. Never size 6–8.
Live trading stays off; these filters do not POST orders.

``mm_overlay`` is comparison-only: flag historical mm fills that would have
been skipped. It does not cancel, replace, or otherwise touch live orders.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

MAX_ENTRY_PREMIUM = 0.65
MAX_CLIP = 5
DEFAULT_CLIP = 4

REASON_PREMIUM = "premium_gt_065"
REASON_CLIP = "clip_gt_5"


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x > 1.5 and x <= 100:
        return x / 100.0
    return x


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def should_skip_entry(ask, count) -> tuple[bool, str | None]:
    """Return (skip, reason) for a paper entry.

    Reasons: ``premium_gt_065`` if ask > 0.65, ``clip_gt_5`` if count > 5.
    """
    ask_f = _as_float(ask)
    if ask_f is not None and ask_f > MAX_ENTRY_PREMIUM:
        return True, REASON_PREMIUM
    n = _as_int(count)
    if n is not None and n > MAX_CLIP:
        return True, REASON_CLIP
    return False, None


def clip_from_dollars(ask, dollars: float | None = None) -> int:
    """Size a paper clip from a dollar budget.

    Default clip is 4. Hard-cap at 5 so dollar math can never produce 6–8.
    If the budget cannot afford 1 contract, returns 0.
    """
    ask_f = _as_float(ask)
    if dollars is None:
        return min(DEFAULT_CLIP, MAX_CLIP)
    try:
        budget = float(dollars)
    except (TypeError, ValueError):
        return min(DEFAULT_CLIP, MAX_CLIP)
    if budget <= 0:
        return 0
    if ask_f is None or ask_f <= 0:
        return min(DEFAULT_CLIP, MAX_CLIP)
    affordable = int(budget / ask_f)
    if affordable < 1:
        return 0
    # Default 4 when sizing from dollars; never raise into the 6–8 loser clips.
    return min(DEFAULT_CLIP, affordable, MAX_CLIP)


def cap_clip(count: int | None) -> int:
    """Hard-cap a requested clip at 5. Never 6–8."""
    n = _as_int(count)
    if n is None or n < 0:
        return 0
    return min(n, MAX_CLIP)


def mm_overlay_flag(fill: dict) -> dict:
    """Comparison-only: would this mm fill have been skipped by paper filters?

    Does not interact with live orders.
    """
    ask = (
        fill.get("yes_price_dollars")
        or fill.get("yes_ask")
        or fill.get("yes_price")
        or fill.get("price")
        or fill.get("premium")
    )
    count = fill.get("count") or fill.get("count_fp") or fill.get("contracts") or fill.get("quantity")
    skip, reason = should_skip_entry(ask, count)
    return {
        "event": "mm_overlay",
        "comparison_only": True,
        "live_interaction": False,
        "ticker": fill.get("ticker") or fill.get("market_ticker"),
        "ask": _as_float(ask),
        "count": _as_int(count),
        "would_skip": skip,
        "reason": reason,
        "note": "mm overlay: flag only. Do not cancel/replace/post live orders.",
    }


def overlay_mm_fills(fills: list[dict]) -> list[dict]:
    """Flag mm fills that paper filters would skip (premium>0.65 or count 6–8)."""
    out = []
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        flag = mm_overlay_flag(fill)
        if flag["would_skip"]:
            out.append(flag)
    return out


def _load_fills(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    blob = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(blob, list):
        return [x for x in blob if isinstance(x, dict)]
    if isinstance(blob, dict):
        inner = blob.get("fills") or blob.get("data") or []
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Paper risk filters (dry-run). Optional mm_overlay comparison against fills."
    )
    parser.add_argument(
        "--mm-overlay",
        metavar="FILLS_JSON",
        help="Flag historical mm fills that would be skipped (comparison only; no live orders)",
    )
    args = parser.parse_args(argv)
    if args.mm_overlay:
        path = Path(args.mm_overlay)
        fills = _load_fills(path)
        flagged = overlay_mm_fills(fills)
        summary = {
            "event": "mm_overlay_summary",
            "comparison_only": True,
            "live_interaction": False,
            "n_fills": len(fills),
            "n_would_skip": len(flagged),
            "n_premium_gt_065": sum(1 for f in flagged if f.get("reason") == REASON_PREMIUM),
            "n_clip_gt_5": sum(1 for f in flagged if f.get("reason") == REASON_CLIP),
            "note": "Observation only. Do not interact with live mm-* orders.",
        }
        print(json.dumps(summary))
        for row in flagged:
            print(json.dumps(row, default=str))
        return 0
    print(
        json.dumps(
            {
                "MAX_ENTRY_PREMIUM": MAX_ENTRY_PREMIUM,
                "MAX_CLIP": MAX_CLIP,
                "DEFAULT_CLIP": DEFAULT_CLIP,
                "dry_run_default": True,
                "live_trading": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
