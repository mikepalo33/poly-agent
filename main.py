#!/usr/bin/env python3
"""
Kalshi Agent v0.1 — PAPER ONLY

- Fetch open Kalshi markets (public API)
- Filter by liquidity, spread, time to expiry, category
- Estimate agent probability vs crowd
- Compute fractional Kelly size
- Apply simple risk caps
- Log PAPER trades (no live orders)

Environment variables used:
  BANKROLL           (float, default 10000)
  MIN_VOLUME         (float, default 5000)
  MAX_FRACTIONAL_KELLY (float, default 0.01)
"""

import os
import math
import time
import datetime as dt
from typing import List, Dict, Any

import requests


# ---------- CONFIG ----------

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

BANKROLL = float(os.getenv("BANKROLL", "10000"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "5000"))          # min 24h volume in dollars
MAX_FRACTIONAL_KELLY = float(os.getenv("MAX_FRACTIONAL_KELLY", "0.01"))  # 1% of bankroll per trade
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.08"))              # min abs(edge) to trade
MAX_SPREAD_CENTS = float(os.getenv("MAX_SPREAD_CENTS", "6")) # max allowed bid/ask spread
MAX_MARKETS_PER_RUN = int(os.getenv("MAX_MARKETS_PER_RUN", "5"))
MIN_DOLLAR_TRADE = float(os.getenv("MIN_DOLLAR_TRADE", "50"))
PER_MARKET_EXPOSURE_CAP = float(os.getenv("PER_MARKET_EXPOSURE_CAP", "0.02"))  # 2% of bankroll per ticker

# Simple category whitelist; adjust once you see real categories from Kalshi
ALLOWED_CATEGORIES = {
    "SPORTS",
    "ELECTIONS",
    "ECONOMIC",
}


# ---------- HELPERS ----------

def ts() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def fetch_open_markets(limit: int = 300) -> List[Dict[str, Any]]:
    """
    Fetch open markets from Kalshi (public endpoint).
    """
    url = f"{KALSHI_BASE_URL}/markets"
    params = {
        "limit": limit,
        "status": "open",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data.get("markets", [])


def compute_mid_yes_price_cents(m: Dict[str, Any]) -> float:
    """
    Compute a mid YES price in cents from bid/ask.
    """
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
    """
    Apply filters: time window, liquidity, spread, price band, category.
    """
    filtered = []
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

        # 1. status/time
        if close_ts <= now_ts:
            continue
        hours_to_close = (close_ts - now_ts) / 3600.0
        if hours_to_close < 6 or hours_to_close > 30 * 24:
            continue

        # 2. liquidity & price
        if vol < MIN_VOLUME:
            continue
        if spread > MAX_SPREAD_CENTS:
            continue
        if not (15 <= yes_mid <= 85):  # focus on midband for now
            continue

        # 3. category whitelist
        if category and category not in ALLOWED_CATEGORIES:
            continue

        m["mid_yes_cents"] = yes_mid
        m["hours_to_close"] = hours_to_close
        filtered.append(m)

    print(f"{ts()} | Filtered down to {len(filtered)} candidate markets")
    return filtered


def historical_calibration_adjust(price_prob: float, category: str) -> float:
    """
    Placeholder calibration: nudge crowd probability based on band.
    In production this would read actual calibration stats per category.
    """
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
    """
    Stub for news-based adjustment. For now returns 0 (no boost).
    Later you can call a news API + LLM to map headlines to markets.
    """
    return 0.0


def kelly_fraction(p_agent: float, p_crowd: float) -> float:
    """
    Kelly fraction for buying YES.
    """
    if p_crowd <= 0.0 or p_crowd >= 1.0:
        return 0.0
    b = (1.0 - p_crowd) / p_crowd
    numer = p_agent * b - (1.0 - p_agent)
    if b <= 0:
        return 0.0
    f = numer / b
    return max(0.0, min(f, MAX_FRACTIONAL_KELLY))


def score_market(m: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute p_crowd, p_agent, edge, Kelly fraction, and a composite score.
    """
    yes_mid_cents = float(m["mid_yes_cents"])
    p_crowd = yes_mid_cents / 100.0
    category = (m.get("category") or "").upper()

    p_model = historical_calibration_adjust(p_crowd, category)
    p_agent = max(min(p_model + news_boost_for_market(m), 0.99), 0.01)

    edge = p_agent - p_crowd
    f_kelly = kelly_fraction(p_agent, p_crowd)

    vol = float(m.get("volume") or 0.0)
    hours_to_close = m["hours_to_close"]
    time_weight = math.exp(-abs(hours_to_close - 72.0) / 72.0)  # prefer ~3 days to close
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


# Exposure tracking (in-memory per run; in production you’d persist this)
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


# ---------- MAIN LOOP ----------

def run_agent_once():
    print("\n" + "=" * 60)
    print(f"🤖 Kalshi Agent v0.1 | PAPER | {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
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

        ticker = s["ticker"]
        title = (s["title"] or "")[:80]
        yes_price = s["yes_mid_cents"] / 100.0
        contracts = max(1, int(size_usd / max(yes_price, 0.01)))

        trade = {
            "ticker": ticker,
            "title": title,
            "side": "YES" if s["edge"] > 0 else "NO",
            "size_usd": size_usd,
            "contracts": contracts,
            "price_dollars": yes_price,
            "edge": s["edge"],
            "p_crowd": s["p_crowd"],
            "p_agent": s["p_agent"],
        }
        trades.append(trade)
        current_exposure_by_ticker[ticker] = current_exposure_by_ticker.get(ticker, 0.0) + size_usd

    for t in trades:
        print(
            f"{ts()} | 📋 PAPER {t['side']} {t['ticker']} '{t['title']}' "
            f"${t['size_usd']:.2f} @ ${t['price_dollars']:.2f} "
            f"(p_crowd={t['p_crowd']:.2f}, p_agent={t['p_agent']:.2f}, edge={t['edge']:.2f})"
        )

    print(f"{ts()} | ✅ AGENT COMPLETE — {len(trades)} paper trade(s) selected")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    run_agent_once()
