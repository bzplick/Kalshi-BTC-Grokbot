# Kalshi-BTC-Grokbot

15-minute **KXBTC15M** edge finder. Scores open Bitcoin 15m binaries with a
mid-only logistic (`yes_mid`, `minutes_left`, `ret_1m`), logs paper decisions,
and **never places live orders** in this tree.

`KALSHI_DRY_RUN=1` is the default. There are no secrets in the repo. Live
trading is off.

## What this is / is not

| This backend | Not this backend |
| --- | --- |
| Scan + dry-run paper log for `KXBTC15M` | Live `POST /portfolio/orders` |
| RSA-PSS client for when you add a local key | Bundled API keys or `.pem` files |
| Coexists with existing **mm-*** quoting bots | A reason to shut mm off |

Calibration is **MIXED**. Mid-only OOS at thr 0.05 was n=5 / +$7. CF
moneyness recalibration (322 paths) did not beat it. **Do not cut the mm bot
yet.** See [`calibration-report.md`](calibration-report.md).

## Setup

```bash
python -m pip install -r requirements.txt
cp .env.example .env
```

`.env.example`:

```
KALSHI_DRY_RUN=1
MAX_TRADE_DOLLARS=5
# KALSHI_SECRETS_PATH=/path/to/kalshi.json
# KALSHI_BASE=https://external-api.kalshi.com/trade-api/v2
```

Market data GETs against `https://external-api.kalshi.com/trade-api/v2` work
without keys. Authenticated calls need a local secrets file
`{ "api_key_id": "...", "private_key": "-----BEGIN ..." }` at
`KALSHI_SECRETS_PATH` (or `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY`). The
client signs `timestamp_ms + METHOD + path` with RSA-PSS SHA-256
(`KALSHI-ACCESS-KEY` / `KALSHI-ACCESS-TIMESTAMP` / `KALSHI-ACCESS-SIGNATURE`).
The signed path includes `/trade-api/v2` and **strips the query string**.
Secrets are never logged.

## Commands

```bash
python paper_once_calibrated.py
python scan.py
python calibrate.py
python fetch_cf_paths.py --n 24
```

- `paper_once_calibrated.py` — BTC-only dry run of open `KXBTC15M`. Orderbook
  mid → features → `edge_model.model_p`. Gates: **thr=0.05**, yes ask **0.40–0.65**,
  spread **≤ 0.02**, **≥ 2 min** left. JSONL decisions to stdout + `ledger.jsonl`.
  **Never POSTs orders.** ETH is skipped (noted in the output).
- `scan.py` — table of ticker, time left, yes bid/ask, model_p, edge, gates.
  BTC uses `edge_model`. Optional `python scan.py --eth` adds `KXETH15M` with a
  crude yes_mid-as-p fallback (not for trading).
- `calibrate.py` — if `raw/fills.json` and `raw/settlements.json` exist, rebuild
  windows, fit logistic, write `model.json` + this report +
  `calibration_summary.json`. Supports mid-only and optional CF moneyness.
  Hold-to-settlement backtest at thr **0.03 / 0.05 / 0.08**. Without raw dumps,
  leaves the committed mid-only model in place.
- `fetch_cf_paths.py --n 24` — `GET /live_data/events/{event_ticker}` for settled
  `KXBTC15M` windows, cache under `raw/cf_paths/`. `--all` paginates everything.
  429 backoff is built into the client.

## Model

[`model.json`](model.json) is the shipped mid-only scorer (`kind: sklearn`,
n=322). `edge_model.model_p(features)` applies a logistic sigmoid to
`coef · [yes_mid, minutes_left, ret_1m] + intercept` and imputes missing
`ret_1m` → 0. [`model_cf.json`](model_cf.json) is a **stub note** that CF
recalibration was MIXED and must not be used for scoring.

## Safety

- Dry-run default; `KalshiClient` refuses order POST/DELETE while dry-run is on.
- `paper_once_calibrated.py` never POSTs even if you set `KALSHI_DRY_RUN=0`.
- `.gitignore` drops `.env`, `*.pem`, `**/kalshi.json`, caches, and `raw/`
  dumps (fills, orders, balance, positions, settlements, candles, markets,
  cf_paths).
