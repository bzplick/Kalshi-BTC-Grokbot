"""Kalshi Trade API client: RSA-PSS SHA-256 auth, GET/POST/DELETE, 429 backoff.

Never logs secrets. Public market-data GETs work without keys. Order POSTs are
blocked while dry-run is on (default).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger("kalshi.client")

DEFAULT_BASE = "https://external-api.kalshi.com/trade-api/v2"
API_ROOT_PATH = "/trade-api/v2"
ORDER_PATH_MARKERS = ("/portfolio/orders", "/orders")

_FALSE = {"0", "false", "no", "off"}


def load_dotenv(path: str | Path | None = None) -> None:
    """Load KEY=VALUE from .env into os.environ (does not override)."""
    p = Path(path) if path is not None else Path(__file__).resolve().parent / ".env"
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


def dry_run_enabled() -> bool:
    """DRY_RUN defaults ON. Only an explicit falsey value disables it."""
    raw = os.environ.get("KALSHI_DRY_RUN", "1")
    return str(raw).strip().lower() not in _FALSE


def max_trade_dollars() -> float:
    try:
        return float(os.environ.get("MAX_TRADE_DOLLARS", "5"))
    except ValueError:
        return 5.0


class KalshiClient:
    """Thin REST wrapper. Signs ``timestamp_ms + METHOD + path`` with RSA-PSS."""

    def __init__(
        self,
        *,
        base: str | None = None,
        secrets_path: str | None = None,
        dry_run: bool | None = None,
        timeout: float = 30.0,
        max_retries: int = 8,
    ) -> None:
        load_dotenv()
        self.base = (base or os.environ.get("KALSHI_BASE") or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.dry_run = dry_run_enabled() if dry_run is None else bool(dry_run)
        self.api_key_id: str | None = None
        self._private_key = None
        self._cooldown_until = 0.0
        self._load_secrets(secrets_path)
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "kalshi-btc-grokbot/0.1"})

    # --- secrets (never logged) ------------------------------------------------

    def _load_secrets(self, secrets_path: str | None) -> None:
        path = secrets_path or os.environ.get("KALSHI_SECRETS_PATH")
        blob: dict[str, Any] = {}
        if path:
            p = Path(path).expanduser()
            if not p.is_file():
                raise FileNotFoundError(f"KALSHI_SECRETS_PATH not found: {p}")
            blob = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(blob, dict):
                raise ValueError("secrets JSON must be an object {api_key_id, private_key}")
        self.api_key_id = (
            blob.get("api_key_id")
            or blob.get("key_id")
            or os.environ.get("KALSHI_API_KEY_ID")
            or os.environ.get("KALSHI_KEY_ID")
        )
        pem_src = blob.get("private_key") or os.environ.get("KALSHI_PRIVATE_KEY")
        pem_path = blob.get("private_key_path") or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        pem_bytes: bytes | None = None
        if isinstance(pem_src, str) and pem_src.strip():
            if "BEGIN" in pem_src:
                pem_bytes = pem_src.replace("\\n", "\n").encode("utf-8")
            elif Path(pem_src).expanduser().is_file():
                pem_bytes = Path(pem_src).expanduser().read_bytes()
            else:
                pem_bytes = pem_src.replace("\\n", "\n").encode("utf-8")
        elif pem_path:
            pem_bytes = Path(pem_path).expanduser().read_bytes()
        if pem_bytes:
            self._private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        # Public GETs work without keys; authenticated calls need both.
        if bool(self.api_key_id) ^ bool(self._private_key):
            raise ValueError("both api_key_id and private_key are required when authenticating")

    @property
    def authenticated(self) -> bool:
        return self._private_key is not None and bool(self.api_key_id)

    def _sign_path(self, path: str) -> str:
        """Path signed into the message: includes /trade-api/v2, no query."""
        raw = path.split("?", 1)[0]
        if raw.startswith("http://") or raw.startswith("https://"):
            raw = urlparse(raw).path
        if not raw.startswith("/"):
            raw = "/" + raw
        if raw.startswith(API_ROOT_PATH):
            return raw
        parsed_base = urlparse(self.base)
        base_path = parsed_base.path.rstrip("/") or API_ROOT_PATH
        if raw.startswith(base_path):
            return raw
        return base_path + raw

    def _request_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        p = path if path.startswith("/") else "/" + path
        parsed_base = urlparse(self.base)
        base_path = parsed_base.path.rstrip("/")
        if p.startswith(API_ROOT_PATH) or (base_path and p.startswith(base_path)):
            return f"{parsed_base.scheme}://{parsed_base.netloc}{p}"
        return self.base + p

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self.authenticated:
            return {}
        timestamp_ms = str(int(time.time() * 1000))
        sign_path = self._sign_path(path)
        message = f"{timestamp_ms}{method.upper()}{sign_path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": str(self.api_key_id),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        }

    def _is_order_write(self, method: str, path: str) -> bool:
        if method.upper() not in {"POST", "DELETE"}:
            return False
        lowered = path.lower()
        return any(m in lowered for m in ORDER_PATH_MARKERS)

    def _wait_cooldown(self) -> None:
        wait = self._cooldown_until - time.time()
        if wait > 0:
            time.sleep(wait)

    def _sleep_429(self, attempt: int, response: requests.Response | None) -> None:
        retry_after = None
        if response is not None:
            raw = response.headers.get("Retry-After")
            if raw:
                try:
                    retry_after = float(raw)
                except ValueError:
                    retry_after = None
        delay = retry_after if retry_after is not None else min(30.0, 1.0 * (2**attempt))
        delay += random.uniform(0.0, 0.25)
        log.warning("HTTP 429; backing off %.2fs (attempt %s)", delay, attempt + 1)
        self._cooldown_until = time.time() + delay
        time.sleep(delay)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        require_auth: bool = False,
    ) -> Any:
        method_u = method.upper()
        if self.dry_run and self._is_order_write(method_u, path):
            raise RuntimeError(
                f"dry-run: refusing {method_u} {path} (KALSHI_DRY_RUN default on; live trading is off)"
            )
        if require_auth and not self.authenticated:
            raise RuntimeError("authenticated endpoint requested but no secrets loaded")

        url = self._request_url(path)
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self._wait_cooldown()
            headers = {"Content-Type": "application/json"} if json_body is not None else {}
            headers.update(self._auth_headers(method_u, path))
            try:
                resp = self.session.request(
                    method_u,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(min(10.0, 0.4 * (2**attempt)))
                continue
            if resp.status_code == 429:
                self._sleep_429(attempt, resp)
                continue
            if resp.status_code >= 500 and attempt + 1 < self.max_retries:
                time.sleep(min(10.0, 0.4 * (2**attempt)))
                continue
            if resp.status_code >= 400:
                # Body may contain error details; never echo request auth headers.
                snippet = (resp.text or "")[:400]
                raise requests.HTTPError(
                    f"{resp.status_code} {method_u} {self._sign_path(path)}: {snippet}",
                    response=resp,
                )
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

        if last_exc:
            raise last_exc
        raise RuntimeError(f"exhausted retries for {method_u} {path}")

    def get(self, path: str, params: dict[str, Any] | None = None, *, require_auth: bool = False) -> Any:
        return self.request("GET", path, params=params, require_auth=require_auth)

    def post(self, path: str, json_body: Any = None, *, require_auth: bool = True) -> Any:
        return self.request("POST", path, json_body=json_body, require_auth=require_auth)

    def delete(self, path: str, *, require_auth: bool = True) -> Any:
        return self.request("DELETE", path, require_auth=require_auth)

    # --- market helpers --------------------------------------------------------

    def iter_markets(
        self,
        *,
        series_ticker: str,
        status: str = "open",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "series_ticker": series_ticker,
                "status": status,
                "limit": limit,
            }
            if cursor:
                params["cursor"] = cursor
            data = self.get("/markets", params=params) or {}
            out.extend(data.get("markets") or [])
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return out

    def iter_events(
        self,
        *,
        series_ticker: str,
        status: str | None = "settled",
        limit: int = 200,
        with_nested_markets: bool = False,
        max_pages: int | None = None,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        while True:
            page_limit = limit
            if max_items is not None:
                remaining = max_items - len(out)
                if remaining <= 0:
                    break
                page_limit = max(1, min(limit, remaining))
            params: dict[str, Any] = {"series_ticker": series_ticker, "limit": page_limit}
            if status:
                params["status"] = status
            if with_nested_markets:
                params["with_nested_markets"] = "true"
            if cursor:
                params["cursor"] = cursor
            data = self.get("/events", params=params) or {}
            batch = data.get("events") or []
            out.extend(batch)
            pages += 1
            if max_items is not None and len(out) >= max_items:
                out = out[:max_items]
                break
            cursor = data.get("cursor") or None
            if not cursor or not batch:
                break
            if max_pages is not None and pages >= max_pages:
                break
        return out

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return self.get(f"/markets/{ticker}/orderbook") or {}

    def get_event_live_data(self, event_ticker: str, range_hint: str | None = "15min") -> dict[str, Any]:
        params = {"range": range_hint} if range_hint else None
        return self.get(f"/live_data/events/{event_ticker}", params=params) or {}

    def get_candlesticks(
        self,
        series_ticker: str,
        ticker: str,
        *,
        start_ts: int,
        end_ts: int,
        period_interval: int = 1,
    ) -> list[dict[str, Any]]:
        data = self.get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params={
                "start_ts": int(start_ts),
                "end_ts": int(end_ts),
                "period_interval": int(period_interval),
            },
        ) or {}
        return list(data.get("candlesticks") or [])


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("close_dollars", "close", "dollars", "price"):
            if key in value:
                return _as_float(value[key])
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _best_level(levels: Any) -> float | None:
    """Highest bid from [[price, qty], ...] (cents or dollar strings)."""
    if not levels:
        return None
    best: float | None = None
    for row in levels:
        price = None
        if isinstance(row, (list, tuple)) and row:
            price = _as_float(row[0])
        elif isinstance(row, dict):
            price = _as_float(row.get("price") or row.get("price_dollars"))
        else:
            price = _as_float(row)
        if price is None:
            continue
        # Legacy integer cents (1-99) vs dollars (0-1).
        if price > 1.5:
            price = price / 100.0
        if best is None or price > best:
            best = price
    return best


def parse_orderbook(payload: Mapping[str, Any] | None) -> dict[str, float | None]:
    """Best YES bid/ask/mid/spread from orderbook_fp or legacy orderbook."""
    payload = dict(payload or {})
    ob_fp = payload.get("orderbook_fp")
    ob = payload.get("orderbook")
    yes_levels = no_levels = None
    if isinstance(ob_fp, dict):
        yes_levels = ob_fp.get("yes_dollars") or ob_fp.get("yes")
        no_levels = ob_fp.get("no_dollars") or ob_fp.get("no")
    if yes_levels is None and isinstance(ob, dict):
        yes_levels = ob.get("yes")
        no_levels = ob.get("no")
    yes_bid = _best_level(yes_levels)
    no_bid = _best_level(no_levels)
    yes_ask = (1.0 - no_bid) if no_bid is not None else None
    mid = None
    if yes_bid is not None and yes_ask is not None:
        mid = (yes_bid + yes_ask) / 2.0
    spread = None
    if yes_bid is not None and yes_ask is not None:
        spread = yes_ask - yes_bid
    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_mid": mid,
        "no_bid": no_bid,
        "spread": spread,
    }


def quotes_from_market(market: dict[str, Any], orderbook: dict[str, Any] | None = None) -> dict[str, float | None]:
    """Prefer live orderbook; fall back to market yes_bid/ask dollar fields."""
    q = parse_orderbook(orderbook or {})
    if q["yes_bid"] is None:
        q["yes_bid"] = _as_float(market.get("yes_bid_dollars") or market.get("yes_bid"))
        if q["yes_bid"] is not None and q["yes_bid"] > 1.5:
            q["yes_bid"] = q["yes_bid"] / 100.0
    if q["yes_ask"] is None:
        q["yes_ask"] = _as_float(market.get("yes_ask_dollars") or market.get("yes_ask"))
        if q["yes_ask"] is not None and q["yes_ask"] > 1.5:
            q["yes_ask"] = q["yes_ask"] / 100.0
    if q["yes_mid"] is None and q["yes_bid"] is not None and q["yes_ask"] is not None:
        q["yes_mid"] = (q["yes_bid"] + q["yes_ask"]) / 2.0
    if q["spread"] is None and q["yes_bid"] is not None and q["yes_ask"] is not None:
        q["spread"] = q["yes_ask"] - q["yes_bid"]
    return q


def parse_close_ts(market: dict[str, Any]) -> float | None:
    for key in ("close_time", "latest_expiration_time", "expiration_time"):
        raw = market.get(key)
        if not raw:
            continue
        if isinstance(raw, (int, float)):
            ts = float(raw)
            return ts / 1000.0 if ts > 1e12 else ts
        text = str(raw).replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            continue
    return None


def minutes_left(market: dict[str, Any], *, now: float | None = None) -> float | None:
    close = parse_close_ts(market)
    if close is None:
        return None
    t = time.time() if now is None else now
    return (close - t) / 60.0


def ret_1m_from_candles(candles: list[dict[str, Any]]) -> float:
    """1-minute mid return from YES bid/ask candle closes. 0 if unavailable."""
    mids: list[float] = []
    for c in candles or []:
        bid = _as_float((c.get("yes_bid") or {}).get("close_dollars") if isinstance(c.get("yes_bid"), dict) else None)
        ask = _as_float((c.get("yes_ask") or {}).get("close_dollars") if isinstance(c.get("yes_ask"), dict) else None)
        if bid is None:
            bid = _as_float(c.get("yes_bid"))
        if ask is None:
            ask = _as_float(c.get("yes_ask"))
        if bid is not None and bid > 1.5:
            bid = bid / 100.0
        if ask is not None and ask > 1.5:
            ask = ask / 100.0
        if bid is not None and ask is not None:
            mids.append((bid + ask) / 2.0)
        else:
            px = _as_float((c.get("price") or {}).get("close_dollars") if isinstance(c.get("price"), dict) else None)
            if px is not None:
                if px > 1.5:
                    px = px / 100.0
                mids.append(px)
    if len(mids) < 2:
        return 0.0
    prev, last = mids[-2], mids[-1]
    if prev == 0:
        return 0.0
    return (last - prev) / prev


def fetch_ret_1m(client: KalshiClient, market: dict[str, Any]) -> float:
    """Best-effort 1m mid return from live candlesticks; 0 on any failure."""
    ticker = market.get("ticker") or ""
    series = market.get("series_ticker") or series_from_ticker(ticker)
    if not ticker or not series:
        return 0.0
    end_ts = int(time.time())
    start_ts = end_ts - 180
    try:
        candles = client.get_candlesticks(series, ticker, start_ts=start_ts, end_ts=end_ts, period_interval=1)
        return ret_1m_from_candles(candles)
    except Exception:
        return 0.0


def series_from_ticker(ticker: str) -> str:
    if ticker.startswith("KXBTC15M"):
        return "KXBTC15M"
    if ticker.startswith("KXETH15M"):
        return "KXETH15M"
    parts = ticker.split("-", 1)
    return parts[0] if parts else ticker

