#!/usr/bin/env python3
"""
PolyWhale Agent v2.0 — LIVE REAL MARKETS
Railway cron: every 6 hours
Env vars needed in Railway Variables:
  POLYMARKET_PRIVATE_KEY   → your Polygon wallet private key
  POLYMARKET_FUNDER        → your wallet address (same key)
  OPENAI_API_KEY           → for CrewAI reasoning
  NEWS_API_KEY             → for NewsAPI sentiment
  PAPER_MODE               → set to "true" to skip real orders (default "false")
"""

import os, json, requests
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────
PRIVATE_KEY   = os.getenv("POLYMARKET_PRIVATE_KEY")
FUNDER        = os.getenv("POLYMARKET_FUNDER")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY")
NEWS_KEY      = os.getenv("NEWS_API_KEY")
PAPER_MODE    = os.getenv("PAPER_MODE", "false").lower() == "true"

GAMMA_API     = "https://gamma-api.polymarket.com"
CLOB_API      = "https://clob.polymarket.com"
CHAIN_ID      = 137            # Polygon mainnet

BANKROLL      = 10_000         # Sim bankroll (update manually or pull from wallet)
MAX_KELLY     = 0.05           # Cap at 5% per trade
MIN_VOLUME    = 50_000         # Skip illiquid markets
MAX_POSITIONS = 3              # Max simultaneous open trades
WHALE_EDGE    = 0.75           # Assumed whale win rate

# ── WHALES TO TRACK ────────────────────────────────────────────────────────────
WHALE_IDS = [
    "0xd218...axios",   # Axios — 96% win rate
    "kch123",
    "HaileyWelsh",
    "swisstony",
    "majorexploit",
    "bcda",
    "0xd218",
]

ts = lambda: datetime.now().strftime("%H:%M:%S")

# ── STEP 1: FETCH LIVE MARKETS ─────────────────────────────────────────────────
def get_live_markets():
    """Pull top-volume active markets from Gamma API (no auth needed)."""
    params = {
        "active": "true",
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
        "limit": 50,
    }
    r = requests.get(f"{GAMMA_API}/markets", params=params, timeout=15)
    r.raise_for_status()
    markets = r.json()
    candidates = []
    for m in markets:
        try:
            prices    = json.loads(m.get("outcomePrices", "[]"))
            volume    = float(m.get("volume24hr", 0))
            yes_price = float(prices[0]) if prices else 0
            token_ids = json.loads(m.get("clobTokenIds", "[]"))
            # Only take liquid markets in the sweet spot: 15¢–50¢ YES (max edge)
            if volume >= MIN_VOLUME and 0.15 <= yes_price <= 0.50 and token_ids:
                m["yes_price"]    = yes_price
                m["yes_token_id"] = token_ids[0]
                candidates.append(m)
        except Exception:
            continue
    print(f"{ts()} | Found {len(candidates)} liquid markets in 0.15–0.50 range")
    return candidates

# ── STEP 2: KELLY SIZING ───────────────────────────────────────────────────────
def kelly_size(our_edge: float, market_price: float) -> float:
    """Full Kelly fraction, capped at MAX_KELLY."""
    implied_odds = (1 - market_price) / market_price   # decimal odds on YES
    f = (our_edge * implied_odds - (1 - our_edge)) / implied_odds
    return round(min(max(f, 0), MAX_KELLY), 4)

# ── STEP 3: NEWS SENTIMENT ─────────────────────────────────────────────────────
def news_boost(question: str) -> float:
    """Return a sentiment edge boost from NewsAPI headlines. +0.05 if bullish."""
    if not NEWS_KEY:
        return 0.0
    keywords = " ".join(question.split()[:4])
    r = requests.get(
        "https://newsapi.org/v2/everything",
        params={"q": keywords, "sortBy": "publishedAt", "pageSize": 5, "apiKey": NEWS_KEY},
        timeout=10,
    )
    if r.status_code != 200:
        return 0.0
    articles = r.json().get("articles", [])
    positive_words = ["win", "lead", "surge", "ahead", "likely", "favored"]
    hits = sum(
        1 for a in articles
        if any(w in (a.get("title", "") + a.get("description", "")).lower() for w in positive_words)
    )
    boost = 0.05 if hits >= 2 else 0.0
    if boost:
        print(f"  {ts()} | News boost +0.05 ({hits} positive headlines)")
    return boost

# ── STEP 4: PLACE ORDER (real or paper) ───────────────────────────────────────
def place_order(token_id: str, price: float, size_usd: float, question: str):
    if PAPER_MODE or not PRIVATE_KEY:
        print(f"  {ts()} | 📋 PAPER  BUY YES '{question[:50]}' ${size_usd:.0f} @{price:.2f}")
        return {"status": "PAPER_TRADE", "size": size_usd, "price": price}

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        client = ClobClient(CLOB_API, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                            signature_type=1, funder=FUNDER)
        creds = client.derive_api_key()
        client.set_api_creds(creds)

        # Convert USD size to shares
        shares = round(size_usd / price, 2)
        order  = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        signed = client.create_order(order)
        resp   = client.post_order(signed, OrderType.GTC)
        print(f"  {ts()} | 💰 LIVE  BUY YES '{question[:50]}' {shares} shares @{price:.2f} → {resp}")
        return resp
    except ImportError:
        print(f"  {ts()} | ⚠️  py-clob-client not installed — add to requirements.txt")
        return None
    except Exception as e:
        print(f"  {ts()} | ❌ Order failed: {e}")
        return None

# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    mode_str = "PAPER" if (PAPER_MODE or not PRIVATE_KEY) else "LIVE 🔴"
    print(f"\n{'='*60}")
    print(f"  🤖 PolyWhale Agent v2.0 | {mode_str} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    print(f"{ts()} | 1/5  Fetching live Polymarket markets...")
    markets = get_live_markets()
    if not markets:
        print(f"{ts()} | No qualifying markets found. Done.")
        return

    trades_fired = 0
    for m in markets[:10]:   # scan top 10 by volume
        if trades_fired >= MAX_POSITIONS:
            break

        q         = m.get("question", "Unknown")
        yes_price = m["yes_price"]
        volume    = float(m.get("volume24hr", 0))
        token_id  = m["yes_token_id"]

        print(f"\n{ts()} | Scanning: '{q[:60]}'")
        print(f"         YES={yes_price:.2f}  Vol24h=${volume:,.0f}")

        # Edge = whale base + news boost
        edge = WHALE_EDGE + news_boost(q)

        # Kelly sizing
        f = kelly_size(edge, yes_price)
        size_usd = round(BANKROLL * f)

        print(f"  {ts()} | Edge={edge:.0%}  Kelly f={f:.1%}  Size=${size_usd}")

        if f <= 0 or size_usd < 10:
            print(f"  {ts()} | PASS — no positive edge")
            continue

        place_order(token_id, yes_price, size_usd, q)
        trades_fired += 1

    print(f"\n{ts()} | {'='*40}")
    print(f"{ts()} | ✅ AGENT COMPLETE — {trades_fired} signal(s) fired")
    print(f"{ts()} | Cron will repeat in 6 hours")
    print(f"{ts()} | {'='*40}\n")

if __name__ == "__main__":
    main()
