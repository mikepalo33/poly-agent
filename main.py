#!/usr/bin/env python3
"""
Kalshi Agent v0.3 — PAPER or LIVE

Signal Stack (in order of priority):
  1. Polymarket cross-reference arbitrage (real-time price comparison)
  2. NewsAPI headline sentiment (keyword search on market title)
  3. Baseline calibration nudge (conservative floor)

Required environment variables:
  KALSHI_API_KEY              Kalshi API Key ID
  KALSHI_PRIVATE_KEY_PATH     Path to Kalshi PEM private key file

Optional environment variables:
  BASE_URL                    (default: https://api.elections.kalshi.com/trade-api/v2)
  NEWSAPI_KEY                 NewsAPI key for headline sentiment
  PAPER_MODE                  "1" = paper log only, "0" = place real orders (default: "1")
  BANKROLL                    float (default: 10000)
  MIN_VOLUME                  float (default: 1000)
  MAX_FRACTIONAL_KELLY        float (default: 0.01)
  MIN_EDGE                    float (default: 0.03)
  MAX_SPREAD_CENTS            float (default: 8)
  MAX_MARKETS_PER_RUN         int   (default: 5)
  MIN_DOLLAR_TRADE            float (default: 25)
  PER_MARKET_EXPOSURE_CAP     float (default: 0.02)
  POLY_WEIGHT                 float (default: 0.6)  weight given to polymarket signal
  NEWS_WEIGHT                 float (default: 0.2)  weight given to news sentiment
  POSITIONS_FILE              path  (default: /app/positions.json)
"""

import os
import math
import time
import uuid
import json
import difflib
import datetime as dt
from typing import List, Dict, Any, Optional

import requests
import base64
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

# ───────────────────────── CONFIG ──────────────────────────

BASE_URL = os.getenv("BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
API_KEY_ID = os.environ["KALSHI_API_KEY"]
PRIVATE_KEY_PATH = os.environ["KALSHI_PRIVATE_KEY_PATH"]
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
PAPER_MODE = os.getenv("PAPER_MODE", "1")
POSITIONS_FILE = os.getenv("POSITIONS_FILE", "/app/positions.json")

BANKROLL = float(os.getenv("BANKROLL", "10000"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "1000"))
MAX_FRACTIONAL_KELLY = float(os.getenv("MAX_FRACTIONAL_KELLY", "0.01"))
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.03"))
MAX_SPREAD_CENTS = float(os.getenv("MAX_SPREAD_CENTS", "8"))
MAX_MARKETS_PER_RUN = int(os.getenv("MAX_MARKETS_PER_RUN", "5"))
MIN_DOLLAR_TRADE = float(os.getenv("MIN_DOLLAR_TRADE", "25"))
PER_MARKET_EXPOSURE_CAP = float(os.getenv("PER_MARKET_EXPOSURE_CAP", "0.02"))
POLY_WEIGHT = float(os.getenv("POLY_WEIGHT", "0.6"))
NEWS_WEIGHT = float(os.getenv("NEWS_WEIGHT", "0.2"))

# ───────────────────────── LOGGING ─────────────────────────

def ts() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")

def log(msg: str) -> None:
    print(f"{ts()} | {msg}", flush=True)

# ───────────────────────── AUTH ────────────────────────────

def load_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )

def create_signature(private_key, timestamp: str, method: str, path: str) -> str:
    path_no_query = path.split("?")[0]
    message = f"{timestamp}{method}{path_no_query}".encode()
    sig = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()

def auth_headers(private_key, method: str, full_path: str) -> Dict[str, str]:
    ts_ms = str(int(dt.datetime.now().timestamp() * 1000))
    sign_path = urlparse(BASE_URL + full_path).path
    sig = create_signature(private_key, ts_ms, method, sign_path)
    return {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
    }

def kalshi_get(pkey, path: str, params: Optional[Dict] = None) -> requests.Response:
    return requests.get(BASE_URL + path, headers=auth_headers(pkey, "GET", path), params=params, timeout=20)

def kalshi_post(pkey, path: str, data: Dict) -> requests.Response:
    hdrs = auth_headers(pkey, "POST", path)
    hdrs["Content-Type"] = "application/json"
    return requests.post(BASE_URL + path, headers=hdrs, data=json.dumps(data), timeout=20)

PRIVATE_KEY = load_private_key(PRIVATE_KEY_PATH)

# ───────────────────────── POSITION PERSISTENCE ────────────

def load_positions() -> Dict[str, float]:
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_positions(positions: Dict[str, float]) -> None:
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        log(f"⚠️  Could not save positions: {e}")

# ───────────────────────── POLYMARKET SIGNAL ───────────────

_poly_cache: Optional[List[Dict]] = None
_poly_cache_ts: float = 0.0
POLY_CACHE_TTL = 300  # seconds

def fetch_polymarket_markets() -> List[Dict]:
    global _poly_cache, _poly_cache_ts
    now = time.time()
    if _poly_cache and (now - _poly_cache_ts) < POLY_CACHE_TTL:
        return _poly_cache
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "order": "volume", "limit": 200},
            timeout=15,
        )
        r.raise_for_status()
        _poly_cache = r.json()
        _poly_cache_ts = now
        log(f"📊 Polymarket: loaded {len(_poly_cache)} markets")
        return _poly_cache
    except Exception as e:
        log(f"⚠️  Polymarket fetch failed: {e}")
        return []

def fuzzy_match_poly(kalshi_title: str, poly_markets: List[Dict], cutoff: float = 0.55) -> Optional[Dict]:
    """
    Return the best-matching Polymarket market by question similarity.
    """
    if not poly_markets:
        return None
    k_lower = kalshi_title.lower()
    best_ratio = 0.0
    best_market = None
    for pm in poly_markets:
        q = (pm.get("question") or "").lower()
        ratio = difflib.SequenceMatcher(None, k_lower, q).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_market = pm
    if best_ratio >= cutoff:
        return best_market
    return None

def polymarket_signal(kalshi_market: Dict, poly_markets: List[Dict]) -> float:
    """
    Compare Kalshi YES mid price to matched Polymarket YES price.
    Returns raw probability adjustment (positive = Kalshi underpriced vs Poly).
    """
    title = kalshi_market.get("title") or ""
    pm = fuzzy_match_poly(title, poly_markets)
    if pm is None:
        return 0.0

    prices = pm.get("outcomePrices")
    if not prices:
        return 0.0
    try:
        poly_yes = float(prices[0])
    except (ValueError, IndexError):
        return 0.0

    kalshi_mid = kalshi_market.get("mid_yes_cents", 50.0) / 100.0
    delta = poly_yes - kalshi_mid
    return delta

# ───────────────────────── NEWS SIGNAL ─────────────────────

_news_cache: Dict[str, float] = {}
_news_cache_ts: Dict[str, float] = {}
NEWS_CACHE_TTL = 600  # 10 minutes

POSITIVE_WORDS = {
    "win", "surge", "rise", "up", "gain", "beat", "approve", "pass", "confirm",
    "victory", "lead", "ahead", "strong", "boost", "increase", "record", "high",
}
NEGATIVE_WORDS = {
    "lose", "fall", "drop", "down", "miss", "fail", "reject", "decline", "cut",
    "crash", "weak", "low", "loss", "defeat", "behind", "concern", "risk",
}

def extract_keywords(title: str) -> str:
    stopwords = {"will", "the", "a", "an", "in", "of", "be", "by", "for",
                 "at", "to", "is", "or", "and", "on", "have", "with", "above", "below"}
    words = [w for w in title.lower().split() if w.isalpha() and w not in stopwords and len(w) > 2]
    return " ".join(words[:5])

def news_boost_for_market(m: Dict[str, Any]) -> float:
    """
    Fetch top headlines for the market title and return a sentiment boost [-0.1, +0.1].
    Returns 0 if NEWSAPI_KEY is not set or on error.
    """
    if not NEWSAPI_KEY:
        return 0.0

    title = m.get("title") or ""
    query = extract_keywords(title)
    if not query:
        return 0.0

    now = time.time()
    if query in _news_cache and (now - _news_cache_ts.get(query, 0)) < NEWS_CACHE_TTL:
        return _news_cache[query]

    try:
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query,
                "language": "en",
                "sortBy": "relevancy",
                "pageSize": 10,
                "apiKey": NEWSAPI_KEY,
            },
            timeout=10,
        )
        r.raise_for_status()
        articles = r.json().get("articles", [])
        if not articles:
            _news_cache[query] = 0.0
            _news_cache_ts[query] = now
            return 0.0

        pos, neg = 0, 0
        for art in articles:
            text = ((art.get("title") or "") + " " + (art.get("description") or "")).lower()
            pos += sum(1 for w in POSITIVE_WORDS if w in text)
            neg += sum(1 for w in NEGATIVE_WORDS if w in text)

        total = pos + neg
        if total == 0:
            boost = 0.0
        else:
            boost = round(((pos - neg) / total) * 0.10, 4)

        _news_cache[query] = boost
        _news_cache_ts[query] = now
        return boost

    except Exception as e:
        log(f"⚠️  NewsAPI error for '{query}': {e}")
        return 0.0

# ───────────────────────── KALSHI MARKETS ──────────────────

def fetch_open_markets(limit: int = 300) -> List[Dict[str, Any]]:
    r = kalshi_get(PRIVATE_KEY, "/markets", {"limit": limit, "status": "open"})
    r.raise_for_status()
    return r.json().get("markets", [])

def compute_mid_yes_price_cents(m: Dict[str, Any]) -> Optional[float]:
    # Try numeric cent fields first
    bid = m.get("yes_bid")
    ask = m.get("yes_ask")

    # If missing, fall back to dollar-string fields and convert to cents
    if bid is None and ask is None:
        bid_d = m.get("yes_bid_dollars")
        ask_d = m.get("yes_ask_dollars")
        if bid_d is not None:
            bid = float(bid_d) * 100.0
        if ask_d is not None:
            ask = float(ask_d) * 100.0

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

    reasons = {
        "no_close_time": 0,
        "expired": 0,
        "too_soon": 0,
        "too_far": 0,
        "low_volume": 0,
        "high_spread": 0,
        "price_band": 0,
        "no_mid": 0,
    }

    for m in markets:
        vol = float(m.get("volume_24h") or m.get("volume") or 0)

        _ct = m.get("close_time") or m.get("close_ts") or ""
        try:
            close_ts = (
                int(_ct)
                if str(_ct).isdigit()
                else int(dt.datetime.fromisoformat(_ct.replace("Z", "+00:00")).timestamp())
            )
        except Exception:
            reasons["no_close_time"] += 1
            continue

        yes_mid = compute_mid_yes_price_cents(m)
        if yes_mid is None:
            reasons["no_mid"] += 1
            continue

        yes_bid = float(m.get("yes_bid") or yes_mid)
        yes_ask = float(m.get("yes_ask") or yes_mid)
        spread = yes_ask - yes_bid

        if close_ts <= now_ts:
            reasons["expired"] += 1
            continue
        hours_to_close = (close_ts - now_ts) / 3600.0
        if hours_to_close < 6:
            reasons["too_soon"] += 1
            continue
        if hours_to_close > 30 * 24:
            reasons["too_far"] += 1
            continue
        if vol < MIN_VOLUME:
            reasons["low_volume"] += 1
            continue
        if spread > MAX_SPREAD_CENTS:
            reasons["high_spread"] += 1
            continue
        if not (10 <= yes_mid <= 90):
            reasons["price_band"] += 1
            continue

        m["mid_yes_cents"] = yes_mid
        m["hours_to_close"] = hours_to_close
        m["volume_used"] = vol
        filtered.append(m)

    log(f"Filter breakdown: {reasons}")
    log(f"Filtered down to {len(filtered)} candidate markets")
    return filtered
