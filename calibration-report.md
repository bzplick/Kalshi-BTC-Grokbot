# Calibration report — KXBTC15M mid-only edge model

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
