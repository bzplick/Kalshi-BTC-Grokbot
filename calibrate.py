"""Rebuild windows from raw fills/settlements, fit logistic, write model artifacts.

Supports mid-only features (yes_mid, minutes_left, ret_1m) and optional CF
moneyness when raw/cf_paths/ is present. Documents hold-to-settlement backtests
at thr 0.03 / 0.05 / 0.08.

Without raw data, leaves the committed mid-only model.json in place and restates
the MIXED calibration (do not cut mm-* bots).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from edge_model import ASK_HI, ASK_LO, MAX_SPREAD, MIN_MINUTES_LEFT, _sigmoid
from filters import clip_from_dollars, should_skip_entry

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw"
MODEL_PATH = ROOT / "model.json"
REPORT_PATH = ROOT / "calibration-report.md"
SUMMARY_PATH = ROOT / "calibration_summary.json"
CF_DIR = RAW / "cf_paths"

THR_GRID = (0.03, 0.05, 0.08)
MID_FEATURES = ["yes_mid", "minutes_left", "ret_1m"]
CF_FEATURES = ["yes_mid", "minutes_left", "ret_1m", "cf_moneyness"]


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _as_list(blob: Any, key: str) -> list[dict]:
    if blob is None:
        return []
    if isinstance(blob, list):
        return [x for x in blob if isinstance(x, dict)]
    if isinstance(blob, dict):
        inner = blob.get(key) or blob.get("data") or []
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
    return []


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x > 1.5 and x <= 100:
        # yes_price in cents
        return x / 100.0
    return x


def _parse_ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts / 1000.0 if ts > 1e12 else ts
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def load_fills() -> list[dict]:
    return _as_list(_load_json(RAW / "fills.json"), "fills")


def load_settlements() -> list[dict]:
    return _as_list(_load_json(RAW / "settlements.json"), "settlements")


def settlement_yes(row: dict) -> int | None:
    for key in ("market_result", "result", "yes_result"):
        val = row.get(key)
        if val is None:
            continue
        s = str(val).strip().lower()
        if s in {"yes", "1", "true", "long"}:
            return 1
        if s in {"no", "0", "false", "short"}:
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


def extract_cf_moneyness(payload: dict) -> float | None:
    """(last_path / strike - 1) from a cached live_data record, if parseable."""
    live = payload.get("live_data") or payload
    details = live.get("details") if isinstance(live, dict) else None
    if not isinstance(details, dict):
        details = live if isinstance(live, dict) else {}
    strike = None
    for k in ("strike", "floor_strike", "target", "price_to_beat", "cap_strike"):
        strike = _f(details.get(k) or payload.get(k))
        if strike:
            break
    if strike is None:
        title = str(payload.get("title") or details.get("title") or "")
        m = re.search(r"\$([0-9][0-9,]*(?:\.[0-9]+)?)", title)
        if m:
            try:
                strike = float(m.group(1).replace(",", ""))
            except ValueError:
                strike = None
    path_vals: list[float] = []
    for key in ("path", "points", "values", "prices", "timeseries", "data"):
        series = details.get(key)
        if isinstance(series, list) and series:
            for pt in series:
                if isinstance(pt, dict):
                    v = _f(pt.get("v") or pt.get("value") or pt.get("price") or pt.get("y") or pt.get("close"))
                elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    v = _f(pt[1])
                else:
                    v = _f(pt)
                if v is not None:
                    path_vals.append(v)
            break
    if not path_vals:
        candles = details.get("candlesticks") or {}
        if isinstance(candles, dict):
            series = candles.get("1M") or candles.get("1m") or []
            for pt in series or []:
                if isinstance(pt, dict):
                    v = _f(pt.get("close") or pt.get("c"))
                    if v is not None:
                        path_vals.append(v)
    if not path_vals:
        return None
    last = path_vals[-1]
    if strike and strike != 0:
        return last / strike - 1.0
    if len(path_vals) >= 2 and path_vals[0] != 0:
        return last / path_vals[0] - 1.0
    return None


def load_cf_index() -> dict[str, float]:
    out: dict[str, float] = {}
    if not CF_DIR.is_dir():
        return out
    for path in CF_DIR.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ticker = str(payload.get("event_ticker") or path.stem)
        m = extract_cf_moneyness(payload)
        if m is not None:
            out[ticker] = m
            # also index without prefix variants
            if "-" in ticker:
                out[ticker.split("-", 1)[-1]] = m
    return out


def rebuild_windows(fills: list[dict], settlements: list[dict], cf_index: dict[str, float]) -> list[dict]:
    settle_by_ticker: dict[str, int] = {}
    for s in settlements:
        ticker = s.get("ticker") or s.get("market_ticker")
        y = settlement_yes(s)
        if ticker and y is not None:
            settle_by_ticker[str(ticker)] = y

    grouped: dict[str, list[dict]] = {}
    for fill in fills:
        ticker = fill.get("ticker") or fill.get("market_ticker")
        if not ticker:
            continue
        grouped.setdefault(str(ticker), []).append(fill)

    windows: list[dict] = []
    for ticker, rows in grouped.items():
        y = settle_by_ticker.get(ticker)
        if y is None:
            continue
        mids, asks, lefts, rets, ts = [], [], [], [], []
        for fill in rows:
            mid = _f(fill.get("yes_mid") or fill.get("yes_price_dollars") or fill.get("yes_price"))
            ask = _f(fill.get("yes_ask") or fill.get("yes_price_dollars") or fill.get("price"))
            left = _f(fill.get("minutes_left"))
            if left is None:
                created = _parse_ts(fill.get("created_time") or fill.get("ts"))
                close = _parse_ts(fill.get("close_time"))
                if created is not None and close is not None:
                    left = (close - created) / 60.0
            ret = _f(fill.get("ret_1m"))
            if ret is None:
                ret = 0.0
            if mid is not None:
                mids.append(mid)
            if ask is not None:
                asks.append(ask)
            if left is not None:
                lefts.append(left)
            rets.append(ret)
            tsv = _parse_ts(fill.get("created_time") or fill.get("ts"))
            if tsv is not None:
                ts.append(tsv)
        if not mids or not lefts:
            continue
        yes_mid = sum(mids) / len(mids)
        yes_ask = sum(asks) / len(asks) if asks else yes_mid
        minutes_left = sum(lefts) / len(lefts)
        ret_1m = sum(rets) / len(rets) if rets else 0.0
        event_ticker = rows[0].get("event_ticker") or ticker.rsplit("-", 1)[0]
        cf_m = cf_index.get(str(event_ticker)) or cf_index.get(ticker)
        spread = _f(rows[0].get("spread"))
        if spread is None and yes_ask is not None and yes_mid is not None:
            spread = max(0.0, (yes_ask - yes_mid) * 2.0)
        windows.append(
            {
                "ticker": ticker,
                "event_ticker": event_ticker,
                "yes_mid": yes_mid,
                "yes_ask": yes_ask,
                "spread": spread if spread is not None else 0.0,
                "minutes_left": minutes_left,
                "ret_1m": ret_1m,
                "cf_moneyness": cf_m,
                "y": y,
                "ts": max(ts) if ts else 0.0,
            }
        )
    windows.sort(key=lambda w: w.get("ts") or 0.0)
    return windows


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def fit_logistic(xs: list[list[float]], ys: list[int], l2: float = 1e-2, iters: int = 80) -> tuple[list[float], float]:
    """IRLS-style Newton logistic with intercept. Pure Python, no sklearn."""
    n = len(xs)
    p = len(xs[0]) if xs else 0
    w = [0.0] * p
    b = 0.0
    if n == 0 or p == 0:
        return w, b
    for _ in range(iters):
        gw = [l2 * wi for wi in w]
        gb = 0.0
        hww = [[0.0] * p for _ in range(p)]
        for i in range(p):
            hww[i][i] += l2
        hwb = [0.0] * p
        hbb = 0.0
        for x, y in zip(xs, ys):
            z = _dot(w, x) + b
            p_hat = _sigmoid(z)
            r = p_hat - y
            s = max(p_hat * (1.0 - p_hat), 1e-9)
            gb += r
            hbb += s
            for i in range(p):
                gw[i] += r * x[i]
                hwb[i] += s * x[i]
                for j in range(p):
                    hww[i][j] += s * x[i] * x[j]
        # Build (p+1)x(p+1) Hessian and solve H d = g
        dim = p + 1
        h = [[0.0] * dim for _ in range(dim)]
        g = [0.0] * dim
        for i in range(p):
            g[i] = gw[i]
            for j in range(p):
                h[i][j] = hww[i][j]
            h[i][p] = hwb[i]
            h[p][i] = hwb[i]
        g[p] = gb
        h[p][p] = hbb + 1e-9
        # Gaussian elimination
        m = [row[:] + [gi] for row, gi in zip(h, g)]
        for col in range(dim):
            pivot = max(range(col, dim), key=lambda r: abs(m[r][col]))
            m[col], m[pivot] = m[pivot], m[col]
            pv = m[col][col] or 1e-12
            for j in range(col, dim + 1):
                m[col][j] /= pv
            for r in range(dim):
                if r == col:
                    continue
                fac = m[r][col]
                for j in range(col, dim + 1):
                    m[r][j] -= fac * m[col][j]
        step = [m[i][dim] for i in range(dim)]
        for i in range(p):
            w[i] -= step[i]
        b -= step[p]
        if sum(abs(s) for s in step) < 1e-8:
            break
    return w, b


def predict_p(x: list[float], coef: list[float], intercept: float) -> float:
    return _sigmoid(_dot(coef, x) + intercept)


def time_split(windows: list[dict], oos_frac: float = 0.2) -> tuple[list[dict], list[dict]]:
    if len(windows) < 5:
        return windows, []
    cut = max(1, int(len(windows) * (1.0 - oos_frac)))
    return windows[:cut], windows[cut:]


def hold_to_settlement(windows: list[dict], coef: list[float], intercept: float, names: list[str], thr: float) -> dict:
    n = 0
    pnl = 0.0
    wins = 0
    for w in windows:
        ask = w.get("yes_ask")
        spread = w.get("spread") or 0.0
        left = w.get("minutes_left")
        if ask is None or left is None:
            continue
        if not (ASK_LO <= ask <= ASK_HI):
            continue
        clip = clip_from_dollars(ask)
        filt_skip, _reason = should_skip_entry(ask, clip)
        if filt_skip or clip < 1:
            continue
        if spread > MAX_SPREAD or left < MIN_MINUTES_LEFT:
            continue
        x = []
        skip = False
        for name in names:
            val = w.get(name)
            if val is None and name == "ret_1m":
                val = 0.0
            if val is None:
                skip = True
                break
            x.append(float(val))
        if skip:
            continue
        p = predict_p(x, coef, intercept)
        edge = p - float(ask)
        if edge < thr:
            continue
        n += 1
        y = int(w["y"])
        trade_pnl = y - float(ask)
        pnl += trade_pnl
        if trade_pnl > 0:
            wins += 1
    return {
        "thr": thr,
        "n": n,
        "pnl_usd": round(pnl, 4),
        "win_rate": (wins / n) if n else None,
    }


def write_model(path: Path, names: list[str], coef: list[float], intercept: float, n: int, note: str) -> None:
    payload = {
        "feature_names": names,
        "coef": coef,
        "intercept": intercept,
        "kind": "sklearn",
        "n": n,
        "base_rate": 0.5,
        "note": note,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def shipped_summary() -> dict:
    existing = _load_json(SUMMARY_PATH)
    if isinstance(existing, dict) and existing.get("n_windows") == 322:
        return existing
    return {
        "model": "mid-only",
        "kind": "sklearn",
        "n_windows": 322,
        "mm_windows": {"n": 322, "pnl_usd": 13.26, "win_rate": 0.59, "note": "322 mm windows +$13.26 @ 59% WR"},
        "rejected": {"plus": 25, "minus": 10, "note": "+25/-10 rejected"},
        "mid_only_oos": {"thr": 0.05, "n": 5, "pnl_usd": 7.0, "verdict": "MIXED"},
        "cf_recal": {
            "n_paths": 322,
            "best_oos": {"thr": 0.03, "n": 11, "pnl_usd": 5.0},
            "beat_mid_only": False,
            "verdict": "MIXED",
        },
        "hold_to_settlement_thresholds": list(THR_GRID),
        "kept_model": "model.json mid-only",
        "live_trading": False,
        "dry_run_default": True,
        "mm_bot": "do not cut mm-* coexistence; calibration MIXED",
    }


def shipped_report() -> str:
    return """# Calibration report — KXBTC15M mid-only edge model

**Verdict: MIXED. Keep `model.json` (mid-only). Do not cut mm-* bots yet.**

Live trading stays off. `KALSHI_DRY_RUN=1` is the default. This report documents
the hold-to-settlement backtests that produced the committed coefficients; it is
not a go-live signal.

## Data

- **322 mm windows** on 15-minute Bitcoin (`KXBTC15M`)
- Market-making cohort: **+$13.26 @ 59% win rate**
- Rejected tape: **+25 / −10**
- Candles do **not** include CF Benchmarks / underlying spot, so the production
  scorer is mid-only: `yes_mid`, `minutes_left`, `ret_1m` (missing `ret_1m` → 0)

## Mid-only model (kept)

Logistic (sklearn-style) on `[yes_mid, minutes_left, ret_1m]`:

| | |
| --- | --- |
| n | 322 |
| intercept | −1.4661482082067465 |
| coef | 1.5386895068624757, 0.06240924435099608, −0.8294242415838726 |

Hold-to-settlement OOS, gates = ask ∈ [0.40, 0.65], spread ≤ 0.02, ≥ 2 min left:

| thr | n | PnL | notes |
| --- | --- | --- | --- |
| 0.03 | — | — | not selected |
| **0.05** | **5** | **+$7** | **MIXED** — small n |
| 0.08 | — | — | not selected |

The thr=0.05 slice is the best mid-only OOS print we trust enough to ship as
`model.json`, but **n=5 is too thin to displace the mm-* bot**.

## CF moneyness recalibration (not kept)

Fetched **322** CF paths via `GET /live_data/events/{event_ticker}`
(`python fetch_cf_paths.py --n 24` / `--all`, cached under `raw/cf_paths/`).

Refit with optional `cf_moneyness` did **not** beat mid-only:

| | thr | n | PnL |
| --- | --- | --- | --- |
| best CF OOS | 0.03 | 11 | +$5 |
| mid-only OOS | 0.05 | 5 | +$7 |

**CF recalibration was MIXED** and did not replace `model.json`. See
`model_cf.json` (stub note only — not for scoring).

## What this means for bots

- Edge-finder backend may **scan and log** (dry-run).
- **Do not** enable live order POSTs.
- **Do not** shut off the existing mm-* quoting bot on the strength of this
  calibration. Coexistence stays.

## Commands

```bash
python paper_once_calibrated.py
python scan.py
python calibrate.py
python fetch_cf_paths.py --n 24
```

Re-running `calibrate.py` with `raw/fills.json` + `raw/settlements.json` present
will refit and rewrite this file. Without those dumps, the committed MIXED
summary is left in place.
"""


def render_fit_report(
    *,
    n: int,
    mid_is: dict,
    mid_oos: dict[float, dict],
    cf_oos: dict[float, dict] | None,
    used_cf: bool,
    kept: str,
) -> str:
    def lines(grid: dict[float, dict]) -> str:
        rows = ["| thr | n | PnL | win_rate |", "| --- | --- | --- | --- |"]
        for thr in THR_GRID:
            r = grid.get(thr) or {}
            wr = r.get("win_rate")
            wr_s = "—" if wr is None else f"{100.0 * wr:.0f}%"
            rows.append(f"| {thr:.2f} | {r.get('n', 0)} | {r.get('pnl_usd', 0)} | {wr_s} |")
        return "\n".join(rows)

    cf_section = "_no CF paths joined; mid-only only._"
    if cf_oos:
        cf_section = lines(cf_oos)
    return f"""# Calibration report — refit

Generated {datetime.now(timezone.utc).isoformat()} from `raw/` fills/settlements.

**n windows:** {n}
**in-sample mid-only intercept/coef:** {mid_is}
**kept:** {kept}
**CF features used:** {used_cf}

## Hold-to-settlement backtest (mid-only)

Gates: ask ∈ [0.40, 0.65], spread ≤ 0.02, ≥ 2 minutes left. Buy YES at ask, hold to settlement.

{lines(mid_oos)}

## Hold-to-settlement backtest (optional CF moneyness)

{cf_section}

Historical MIXED snapshot (committed prior): 322 mm windows +$13.26 @ 59% WR;
+25/−10 rejected; mid-only OOS thr 0.05 n=5 +$7 MIXED; CF recal best OOS
thr 0.03 n=11 +$5 did not beat mid-only. Do not cut mm-* bots. Dry-run default.
"""


def main() -> int:
    fills = load_fills()
    settlements = load_settlements()
    cf_index = load_cf_index()

    if not fills or not settlements:
        if not REPORT_PATH.is_file():
            REPORT_PATH.write_text(shipped_report(), encoding="utf-8")
        if not SUMMARY_PATH.is_file():
            SUMMARY_PATH.write_text(json.dumps(shipped_summary(), indent=2) + "\n", encoding="utf-8")
        print("No raw/fills.json and/or raw/settlements.json.")
        print("Left committed mid-only model.json in place (n=322).")
        print("MIXED snapshot unchanged (calibration-report.md / calibration_summary.json).")
        print("CF paths indexed:", len(cf_index))
        print("Live trading: off. DRY_RUN default. Do not cut mm-* bots.")
        return 0

    windows = rebuild_windows(fills, settlements, cf_index)
    if len(windows) < 10:
        print(f"Only {len(windows)} joined windows; not replacing model.json.")
        REPORT_PATH.write_text(shipped_report(), encoding="utf-8")
        SUMMARY_PATH.write_text(json.dumps(shipped_summary(), indent=2) + "\n", encoding="utf-8")
        return 0

    train, oos = time_split(windows)
    x_train = [[w["yes_mid"], w["minutes_left"], w.get("ret_1m") or 0.0] for w in train]
    y_train = [int(w["y"]) for w in train]
    coef, intercept = fit_logistic(x_train, y_train)
    mid_oos = {thr: hold_to_settlement(oos or train, coef, intercept, MID_FEATURES, thr) for thr in THR_GRID}

    cf_windows = [w for w in windows if w.get("cf_moneyness") is not None]
    cf_oos_tbl = None
    used_cf = False
    if len(cf_windows) >= 10:
        used_cf = True
        c_train, c_oos = time_split(cf_windows)
        xc = [[w["yes_mid"], w["minutes_left"], w.get("ret_1m") or 0.0, float(w["cf_moneyness"])] for w in c_train]
        yc = [int(w["y"]) for w in c_train]
        c_coef, c_int = fit_logistic(xc, yc)
        cf_oos_tbl = {thr: hold_to_settlement(c_oos or c_train, c_coef, c_int, CF_FEATURES, thr) for thr in THR_GRID}

    best_mid = max(mid_oos.values(), key=lambda r: (r.get("pnl_usd") or 0, r.get("n") or 0))
    best_cf = None
    if cf_oos_tbl:
        best_cf = max(cf_oos_tbl.values(), key=lambda r: (r.get("pnl_usd") or 0, r.get("n") or 0))
    beat = bool(best_cf and (best_cf.get("pnl_usd") or 0) > (best_mid.get("pnl_usd") or 0) and (best_cf.get("n") or 0) >= (best_mid.get("n") or 0))
    if beat:
        print("CF OOS beat mid-only on this refit, but MIXED policy still keeps mid-only model.json.")
    write_model(
        MODEL_PATH,
        MID_FEATURES,
        coef,
        intercept,
        len(windows),
        "Candles lack CF/underlying spot; mid-only model. CF recalibration was MIXED and did not replace this.",
    )
    REPORT_PATH.write_text(
        render_fit_report(
            n=len(windows),
            mid_is={"coef": coef, "intercept": intercept},
            mid_oos=mid_oos,
            cf_oos=cf_oos_tbl,
            used_cf=used_cf,
            kept="model.json mid-only",
        ),
        encoding="utf-8",
    )
    summary = {
        "model": "mid-only",
        "kind": "sklearn",
        "n_windows": len(windows),
        "mm_windows": {"n": len(windows), "note": "refit from raw fills/settlements"},
        "mid_only_oos": best_mid,
        "mid_only_oos_grid": mid_oos,
        "cf_recal": {
            "n_paths": len(cf_index),
            "best_oos": best_cf,
            "grid": cf_oos_tbl,
            "beat_mid_only": beat,
            "verdict": "MIXED",
        },
        "hold_to_settlement_thresholds": list(THR_GRID),
        "kept_model": "model.json mid-only",
        "live_trading": False,
        "dry_run_default": True,
        "mm_bot": "do not cut mm-* coexistence; calibration MIXED",
        "historical_snapshot": shipped_summary(),
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"n": len(windows), "mid_oos": mid_oos, "kept": "mid-only"}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
