#!/usr/bin/env python3
"""
Kalshi Agent v0.2 — PAPER or LIVE

- Fetch open Kalshi markets
- Filter by liquidity, spread, time to expiry, category
- Estimate agent probability vs crowd
- Compute fractional Kelly size with risk caps
- If PAPER_MODE=1: log PAPER trades only
- If PAPER_MODE=0: place real limit orders via Kalshi API

Required environment variables:
  KALSHI_API_KEY        (your Kalshi API Key ID)
  KALSHI_PRIVATE_KEY_PATH (filesystem path to your Kalshi private key file)

Optional environment variables:
  BASE_URL              (default: https://api.elections.kalshi.com/trade-api/v2)
  BANKROLL              (float, default 10000)
  MIN_VOLUME            (float, default 5000)
  MAX_FRACTIONAL_KELLY  (float, default 0.01)
  MIN_EDGE              (float, default 0.08)
  MAX_SPREAD_CENTS      (float, default 6)
  MAX_MARKETS_PER_RUN   (int,   default 5)
  MIN_DOLLAR_TRADE      (float, default 50)
  PER_MARKET_EXPOSURE_CAP (float, default 0.02)
  PAPER_MODE            ("1" for paper, "0" for live)
"""

import os
import math
import time
import uuid
import json
import datetime as dt
from typing import List, Dict, Any

import requests
import base64
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

# ---------- CONFIG ----------

BASE_URL = os.getenv(
    "BASE_URL",
    "https://api.elections.kalshi.com/trade-api/v2",
)

API_KEY_ID = os.environ.get("KALSHI_API_KEY")

if API_KEY_ID is None:
    raise RuntimeError("KALSHI_API_KEY environment variable is required for authenticated trading.")

PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
if PRIVATE_KEY_PATH is None:
    raise RuntimeError("KALSHI_PRIVATE_KEY_PATH environment variable is required for authenticated trading.")

PAPER_MODE = os.getenv("PAPER_MODE", "1")  # "1" = paper, "0" = live

BANKROLL = float(os.getenv("BANKROLL", "10000"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "5000"))
MAX_FRACTIONAL_KELLY = float(os.getenv("MAX_FRACTIONAL_KELLY", "0.01"))
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.08"))
MAX_SPREAD_CENTS = float(os.getenv("MAX_SPREAD_CENTS", "6"))
MAX_MARKETS_PER_RUN = int(os.getenv("MAX_MARKETS_PER_RUN", "5"))
MIN_DOLLAR_TRADE = float(os.getenv("MIN_DOLLAR_TRADE", "50"))
PER_MARKET_EXPOSURE_CAP = float(os.getenv("PER_MARKET_EXPOSURE_CAP", "0.02"))

ALLOWED_CATEGORIES = {
    "SPORTS",
    "ELECTIONS",
    "ECONOMIC",
}

# ---------- TIME / LOGGING ----------

def ts() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")

# ---------- AUTH HELPERS (from Kalshi docs) ---------- [web:611][web:819]

def load_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(
            f.read(),
            password=None,
            backend=default_backend(),
        )

def create_signature(private_key, timestamp: str, method: str, path: str) -> str:
    """
    Create the request signature.

    path must be the URL path (no scheme/host), e.g. /trade-api/v2/portfolio/orders.
    """
    path_without_query = path.split("?")[0]
    message = f"{timestamp}{method}{path_without_query}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")

def auth_headers(private_key, method: str, full_path: str) -> Dict[str, str]:
    """
    Build authenticated headers for a request. [web:611]
    """
    timestamp = str(int(dt.datetime.now().timestamp() * 1000))
    sign_path = urlparse(BASE_URL + full_path).path
    signature = create_signature(private_key, timestamp, method, sign_path)
    return {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }

def kalshi_get(private_key, path: str, params: Dict[str, Any] | None = None) -> requests.Response:
    headers = auth_headers(private_key, "GET", path)
    return requests.get(BASE_URL + path, headers=headers, params=params, timeout=20)

def kalshi_post(private_key, path: str, data: Dict[str, Any]) -> requests.Response:
    headers = auth_headers(private_key, "POST", path)
    headers["Content-Type"] = "application/json"
    return requests.post(BASE_URL + path, headers=headers, data=json.dumps(data), timeout=20)

# Load private key once
PRIVATE_KEY = load_private_key(PRIVATE_KEY_PATH)

# ---------- PUBLIC MARKET DATA (can be unauthenticated but we reuse helpers) ---------- [web:742][web:811]

def fetch_open_markets(limit: int = 300) -> List[Dict[str, Any]]:
    """
    Fetch open markets from Kalshi.
    """
    # either unauthenticated GET or authenticated GET; we use authenticated for consistency
    path = "/markets"
    params = {
        "limit": limit,
        "status": "open",
    }
    # Public endpoint, but using kalshi_get keeps signature pattern consistent
    r = kalshi_get(PRIVATE_KEY, path, params=params)
    r.raise_for_status()
    data = r.json()
    return data.get("markets", [])

# ---------- PRICING / FILTERS ----------

def compute_mid_yes_price_cents(m: Dict[str, Any]) -> float | None:
    bid = m.get("yes_bid")
    ask = m.get("yes_ask")
    if bid is not None and ask is not None:
        return 0.5 * (float(bid) + float(ask))
    if ask is not None:
        return float(ask)
    if bid is not None:
        return float(bid)
    return None

def basic_filters(markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    now_ts = int(time.time())

    for m in markets:
        vol = float(m.get("volume") or 0)
        category = (m.get("category") or "").upper()
        close_ts = int(m.get("close_ts") or 0)
        yes_mid = compute_mid_yes_price_cents(m)
        if yes_mid is None:
            continue

        yes_bid = float(m.get("yes_bid") or yes_mid)
        yes_ask = float(m.get("yes_ask") or yes_mid)
        spread = yes_ask - yes_bid

        if close_ts <= now_ts:
            continue
        hours_to_close = (close_ts - now_ts) / 3600.0
        if hours_to_close < 6 or hours_to_close > 30 * 24:
            continue

        if vol < MIN_VOLUME:
            continue
        if spread > MAX_SPREAD_CENTS:
            continue
        if not (15 <= yes_mid <= 85):
            continue

        if category and category not in ALLOWED_CATEGORIES:
            continue

        m["mid_yes_cents"] = yes_mid
        m["hours_to_close"] = hours_to_close
        filtered.append(m)

    print(f"{ts()} | Filtered down to {len(filtered)} candidate markets")
    return filtered

def historical_calibration_adjust(price_prob: float, category: str) -> float:
    if price_prob < 0.2:
        adj = 0.02
    elif price_prob < 0.3:
        adj = 0.02
    elif price_prob < 0.5:
        adj = 0.01
    else:
        adj = 0.0
    return max(min(price_prob + adj, 0.99), 0.01)

def news_boost_for_market(m: Dict[str, Any]) -> float:
    return 0.0

def kelly_fraction(p_agent: float, p_crowd: float) -> float:
    if p_crowd <= 0.0 or p_crowd >= 1.0:
        return 0.0

    b = (1.0 - p_crowd) / p_crowd
    if b <= 0:
        return 0.0

    numer = p_agent * b - (1.0 - p_agent)
    f = numer / b
    return max(0.0, min(f, MAX_FRACTIONAL_KELLY))

def score_market(m: Dict[str, Any]) -> Dict[str, Any]:
    yes_mid_cents = float(m["mid_yes_cents"])
    p_crowd = yes_mid_cents / 100.0
    category = (m.get("category") or "").upper()

    p_model = historical_calibration_adjust(p_crowd, category)
    p_agent = max(min(p_model + news_boost_for_market(m), 0.99), 0.01)

    edge = p_agent - p_crowd
    f_kelly = kelly_fraction(p_agent, p_crowd)

    vol = float(m.get("volume") or 0.0)
    hours_to_close = m["hours_to_close"]
    time_weight = math.exp(-abs(hours_to_close - 72.0) / 72.0)
    score = abs(edge) * math.log1p(vol) * time_weight

    return {
        "ticker": m.get("ticker"),
        "title": m.get("title"),
        "category": category,
        "p_crowd": p_crowd,
        "p_agent": p_agent,
        "edge": edge,
        "kelly": f_kelly,
        "score": score,
        "yes_mid_cents": yes_mid_cents,
        "volume": vol,
        "hours_to_close": hours_to_close,
    }

# ---------- RISK / SIZING ----------

current_exposure_by_ticker: Dict[str, float] = {}

def allowed_size_dollars(ticker: str, f_kelly: float) -> float:
    raw = BANKROLL * f_kelly
    per_market_cap = BANKROLL * PER_MARKET_EXPOSURE_CAP
    already = current_exposure_by_ticker.get(ticker, 0.0)
    remaining = max(0.0, per_market_cap - already)
    return min(raw, remaining)

def risk_filter_and_size(scored: Dict[str, Any]) -> float:
    if abs(scored["edge"]) < MIN_EDGE:
        return 0.0
    if scored["kelly"] <= 0.0:
        return 0.0

    size = allowed_size_dollars(scored["ticker"], scored["kelly"])
    if size < MIN_DOLLAR_TRADE:
        return 0.0
    return size

# ---------- ORDER PLACEMENT ---------- [web:819][web:820]

def place_order_live(scored: Dict[str, Any], size_usd: float) -> Dict[str, Any]:
    side = "yes" if scored["edge"] > 0 else "no"
    ticker = scored["ticker"]
    yes_price_cents = int(round(scored["yes_mid_cents"]))  # simple: hit mid; you can shade
    yes_price_cents = max(1, min(99, yes_price_cents))

    price_dollars = yes_price_cents / 100.0
    contracts = max(1, int(size_usd / max(price_dollars, 0.01)))

    order_data = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "type": "limit",
        "yes_price": yes_price_cents if side == "yes" else 0,
        "no_price": yes_price_cents if side == "no" else 0,
        "count": contracts,
        "client_order_id": str(uuid.uuid4()),
        "time_in_force": "fill_or_kill",
    }

    path = "/portfolio/orders"
    response = kalshi_post(PRIVATE_KEY, path, order_data)
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Order error {response.status_code}: {response.text}")

    return {
        "order_response": response.json(),
        "order_data": order_data,
    }

# ---------- MAIN LOOP ----------

def run_agent_once() -> None:
    mode_str = "PAPER" if PAPER_MODE == "1" else "LIVE"
    print("\n" + "=" * 60)
    print(f"🤖 Kalshi Agent v0.2 | {mode_str} | {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    try:
        markets = fetch_open_markets(limit=300)
    except Exception as e:
        print(f"{ts()} | Error fetching markets: {e}")
        return

    print(f"{ts()} | Fetched {len(markets)} open markets from Kalshi")

    candidates = basic_filters(markets)
    if not candidates:
        print(f"{ts()} | No candidates after filters. Done.")
        print("=" * 60 + "\n")
        return

    scored = [score_market(m) for m in candidates]
    scored.sort(key=lambda x: x["score"], reverse=True)

    trades = []
    for s in scored:
        if len(trades) >= MAX_MARKETS_PER_RUN:
            break

        size_usd = risk_filter_and_size(s)
        if size_usd <= 0.0:
            continue

        trades.append((s, size_usd))
        ticker = s["ticker"]
        current_exposure_by_ticker[ticker] = current_exposure_by_ticker.get(ticker, 0.0) + size_usd

    if not trades:
        print(f"{ts()} | No trades passed risk filters.")
        print("=" * 60 + "\n")
        return

    for s, size_usd in trades:
        side = "YES" if s["edge"] > 0 else "NO"
        yes_price = s["yes_mid_cents"] / 100.0
        title = (s["title"] or "")[:80]

        if PAPER_MODE == "1":
            contracts = max(1, int(size_usd / max(yes_price, 0.01)))
            print(
                f"{ts()} | 📋 PAPER {side} {s['ticker']} '{title}' "
                f"${size_usd:.2f} @ ${yes_price:.2f} "
                f"(p_crowd={s['p_crowd']:.2f}, p_agent={s['p_agent']:.2f}, edge={s['edge']:.2f}, "
                f"contracts={contracts})"
            )
        else:
            try:
                result = place_order_live(s, size_usd)
                order = result["order_response"].get("order", {})
                print(
                    f"{ts()} | ✅ LIVE {side} {s['ticker']} '{title}' "
                    f"${size_usd:.2f} @ ~${yes_price:.2f} "
                    f"status={order.get('status')} order_id={order.get('order_id')}"
                )
            except Exception as e:
                print(f"{ts()} | ❌ Order failed for {s['ticker']}: {e}")

    print(f"{ts()} | ✅ AGENT COMPLETE — {len(trades)} trade(s) processed in {mode_str} mode")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    run_agent_once()
