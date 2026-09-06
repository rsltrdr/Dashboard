#!/usr/bin/env python3
"""
Meme Coin Radar - discovery scanner.

Runs hourly on GitHub Actions (which has unrestricted internet access, unlike
the Claude sandbox). Finds small-cap tokens trading well above their own size
with a growing holder count, and writes the survivors to data/candidates.json
for the dashboard to pick up.

Screen:
  - market cap under MAX_MCAP_USD
  - 24h volume at least MIN_VOL_MCAP_RATIO x market cap
  - holders growing (needs two sightings before it can be confirmed)
  - a liquidity floor, so nothing unsellable gets through

Sources:
  GeckoTerminal  new + trending pools per network      (free, no key)
  CoinGecko      live FX rate and listed-token data    (free demo key)
  Solscan        Solana holder counts                  (your key)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------- settings

NETWORKS = ["solana", "eth", "bsc", "base"]          # GeckoTerminal network ids
NETWORK_LABEL = {"solana": "Solana", "eth": "Ethereum", "bsc": "BNB Chain", "base": "Base"}

MAX_MCAP_USD = 550_000        # a little above the €500k screen so nothing borderline is lost early
MIN_VOL_MCAP_RATIO = 2.0      # 24h volume must be at least 2x market cap
MIN_LIQUIDITY_USD = 15_000    # below this it isn't reliably sellable
MAX_AGE_DAYS = 7              # stay in the early window
MAX_CANDIDATES = 10           # what the dashboard shows

COINGECKO_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()
SOLSCAN_KEY = os.environ.get("SOLSCAN_API_KEY", "").strip()

# CoinGecko asset platform ids, for looking up a token by contract address
CG_PLATFORM = {"solana": "solana", "eth": "ethereum", "bsc": "binance-smart-chain", "base": "base"}

STATE_PATH = "state/holders.json"
OUT_PATH = "data/candidates.json"

diagnostics = []


def note(stage, message):
    """Record anything that went sideways, so the first run tells us what to fix."""
    diagnostics.append({"stage": stage, "message": str(message)[:300]})


def fetch(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "meme-coin-radar/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} for {url.split('?')[0]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def dig(obj, *path, default=None):
    """Walk nested dicts without exploding on a missing key."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if cur is not None else default


def as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- FX

def usd_to_eur_rate():
    """Live rate from CoinGecko, falling back to a fixed approximation."""
    url = "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=eur"
    headers = {"User-Agent": "meme-coin-radar/1.0"}
    if COINGECKO_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_KEY
    data, err = fetch(url, headers)
    if err or not data:
        note("fx", err or "no data")
        return 0.92, False
    rate = as_float(dig(data, "tether", "eur"))
    if rate <= 0:
        note("fx", f"unexpected shape: {list(data)[:5]}")
        return 0.92, False
    return rate, True


# ---------------------------------------------------------------- discovery

def parse_pool(item, included_by_id, network):
    """
    Pull one pool out of a GeckoTerminal response. Field names are read
    defensively: if the API shape has shifted, we record it rather than
    silently producing wrong numbers.
    """
    attrs = item.get("attributes") or {}

    mcap = as_float(attrs.get("market_cap_usd")) or as_float(attrs.get("fdv_usd"))
    liquidity = as_float(attrs.get("reserve_in_usd"))
    volume = as_float(dig(attrs, "volume_usd", "h24"))
    buys = int(as_float(dig(attrs, "transactions", "h24", "buys")))
    sells = int(as_float(dig(attrs, "transactions", "h24", "sells")))

    created_raw = attrs.get("pool_created_at")
    created_ts = None
    if created_raw:
        try:
            created_ts = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        except ValueError:
            note("parse", f"unparseable pool_created_at: {created_raw}")

    token_id = dig(item, "relationships", "base_token", "data", "id", default="")
    address = token_id.split("_", 1)[1] if "_" in token_id else ""

    token = included_by_id.get(token_id) or {}
    tattrs = token.get("attributes") or {}
    symbol = (tattrs.get("symbol") or "").strip()
    name = (tattrs.get("name") or "").strip()
    if not address:
        address = (tattrs.get("address") or "").strip()

    if not symbol:
        # pool name looks like "SYMBOL / SOL", so fall back to its left side
        pool_name = attrs.get("name") or ""
        symbol = pool_name.split("/")[0].strip() or "?"

    return {
        "address": address,
        "symbol": symbol,
        "name": name or symbol,
        "network": network,
        "chain": NETWORK_LABEL.get(network, network),
        "mcapUsd": mcap,
        "liquidityUsd": liquidity,
        "volume24hUsd": volume,
        "buys": buys,
        "sells": sells,
        "createdAt": created_ts.isoformat() if created_ts else None,
        "ageHours": round((datetime.now(timezone.utc) - created_ts).total_seconds() / 3600, 2)
        if created_ts else None,
        "poolAddress": attrs.get("address") or "",
    }


def discover(network):
    """New pools plus trending pools, deduplicated by token address."""
    found = {}
    for endpoint in ("new_pools", "trending_pools"):
        url = (f"https://api.geckoterminal.com/api/v2/networks/{network}/"
               f"{endpoint}?include=base_token&page=1")
        data, err = fetch(url, {"Accept": "application/json;version=20230302",
                                "User-Agent": "meme-coin-radar/1.0"})
        if err or not data:
            note(f"discover:{network}:{endpoint}", err or "no data")
            continue

        rows = data.get("data")
        if not isinstance(rows, list):
            note(f"discover:{network}:{endpoint}", f"unexpected shape, keys={list(data)[:5]}")
            continue

        included_by_id = {}
        for inc in (data.get("included") or []):
            if inc.get("id"):
                included_by_id[inc["id"]] = inc

        for item in rows:
            pool = parse_pool(item, included_by_id, network)
            if not pool["address"]:
                continue
            prev = found.get(pool["address"])
            # keep whichever pool for this token holds the most liquidity
            if not prev or pool["liquidityUsd"] > prev["liquidityUsd"]:
                found[pool["address"]] = pool

        time.sleep(2.5)  # GeckoTerminal free tier is ~30 calls/min

    return list(found.values())


# ---------------------------------------------------------------- holders

def solana_holder_count(address):
    if not SOLSCAN_KEY:
        return None
    url = f"https://pro-api.solscan.io/v2.0/token/meta?address={address}"
    data, err = fetch(url, {"token": SOLSCAN_KEY, "User-Agent": "meme-coin-radar/1.0"})
    if err or not data:
        note("solscan", err or "no data")
        return None
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    for key in ("holder", "holder_count", "holders"):
        if key in body:
            count = int(as_float(body[key]))
            if count > 0:
                return count
    note("solscan", f"no holder field, keys={list(body)[:8]}")
    return None


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    # keep the file from growing without bound
    for addr in list(state):
        state[addr]["history"] = state[addr]["history"][-12:]
    if len(state) > 800:
        ranked = sorted(state.items(), key=lambda kv: kv[1].get("lastSeen", ""), reverse=True)
        state = dict(ranked[:800])
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def apply_holder_growth(candidate, state, now_iso):
    """
    Compare this run's holder count against the last one recorded.
    A token seen for the first time has no growth to report yet, which is
    inherent: growth needs two observations.
    """
    address = candidate["address"]
    count = solana_holder_count(address) if candidate["network"] == "solana" else None

    entry = state.get(address) or {"history": []}
    history = entry["history"]

    candidate["holderCount"] = count
    candidate["holderGrowthPct"] = None
    candidate["holderGrowthStatus"] = "unavailable"

    if count is None:
        # no holder source for this chain yet
        if candidate["network"] != "solana":
            candidate["holderGrowthStatus"] = "no source for this chain"
    else:
        prior = [h for h in history if h.get("count")]
        if prior:
            before = prior[-1]["count"]
            if before > 0:
                candidate["holderGrowthPct"] = round((count - before) / before * 100, 1)
                candidate["holderGrowthStatus"] = "measured"
                candidate["holderCountBefore"] = before
                candidate["holderCountBeforeAt"] = prior[-1]["ts"]
        else:
            candidate["holderGrowthStatus"] = "first sighting"
        history.append({"ts": now_iso, "count": count})

    entry["history"] = history
    entry["lastSeen"] = now_iso
    entry["symbol"] = candidate["symbol"]
    state[address] = entry
    return candidate


# ---------------------------------------------------------------- CoinGecko

def coingecko_enrich(candidate):
    """
    Most brand-new meme coins are not listed on CoinGecko, so a miss here is
    normal and not an error. When a token IS listed, this adds the wider
    context: categories, price history, community following.
    """
    platform = CG_PLATFORM.get(candidate["network"])
    if not platform or not candidate["address"]:
        return None
    url = (f"https://api.coingecko.com/api/v3/coins/{platform}/contract/{candidate['address']}"
           "?localization=false&tickers=false&developer_data=false&sparkline=false")
    headers = {"User-Agent": "meme-coin-radar/1.0"}
    if COINGECKO_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_KEY
    data, err = fetch(url, headers)
    if err or not data:
        return None  # unlisted, which is expected for new tokens
    return {
        "id": data.get("id"),
        "categories": [c for c in (data.get("categories") or []) if c][:4],
        "marketCapRank": data.get("market_cap_rank"),
        "priceChange24h": dig(data, "market_data", "price_change_percentage_24h"),
        "priceChange7d": dig(data, "market_data", "price_change_percentage_7d"),
        "athChangePct": dig(data, "market_data", "ath_change_percentage", "usd"),
        "twitterFollowers": dig(data, "community_data", "twitter_followers"),
        "url": f"https://www.coingecko.com/en/coins/{data.get('id')}" if data.get("id") else None,
    }


# ---------------------------------------------------------------- main

def main():
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    rate, rate_live = usd_to_eur_rate()
    state = load_state()

    raw = []
    for network in NETWORKS:
        raw.extend(discover(network))
    print(f"discovered {len(raw)} pools across {len(NETWORKS)} networks", file=sys.stderr)

    # --- apply the screen -------------------------------------------------
    screened = []
    for c in raw:
        if c["mcapUsd"] <= 0 or c["mcapUsd"] > MAX_MCAP_USD:
            continue
        if c["liquidityUsd"] < MIN_LIQUIDITY_USD:
            continue
        if c["ageHours"] is not None and c["ageHours"] > MAX_AGE_DAYS * 24:
            continue
        ratio = c["volume24hUsd"] / c["mcapUsd"]
        if ratio < MIN_VOL_MCAP_RATIO:
            continue
        c["volMcapRatio"] = round(ratio, 2)
        screened.append(c)

    print(f"{len(screened)} passed the cap/volume/liquidity screen", file=sys.stderr)

    # strongest volume signal first, then check holders on a bounded shortlist
    screened.sort(key=lambda c: c["volMcapRatio"], reverse=True)
    shortlist = screened[:20]

    for c in shortlist:
        apply_holder_growth(c, state, now_iso)
        if c["network"] == "solana":
            time.sleep(1)

    # holders growing, or not yet measurable; a confirmed decline drops out
    keep = [c for c in shortlist if c["holderGrowthPct"] is None or c["holderGrowthPct"] > 0]
    keep.sort(key=lambda c: (c["holderGrowthPct"] or 0, c["volMcapRatio"]), reverse=True)
    final = keep[:MAX_CANDIDATES]

    for c in final:
        c["coingecko"] = coingecko_enrich(c)
        c["mcapEur"] = round(c["mcapUsd"] * rate)
        c["liquidityEur"] = round(c["liquidityUsd"] * rate)
        c["volume24hEur"] = round(c["volume24hUsd"] * rate)
        c["dexscreener"] = f"https://dexscreener.com/{c['network']}/{c['address']}"
        time.sleep(1.5)

    save_state(state)

    out = {
        "generatedAt": now_iso,
        "usdToEur": round(rate, 4),
        "usdToEurLive": rate_live,
        "screen": {
            "maxMcapUsd": MAX_MCAP_USD,
            "minVolMcapRatio": MIN_VOL_MCAP_RATIO,
            "minLiquidityUsd": MIN_LIQUIDITY_USD,
            "maxAgeDays": MAX_AGE_DAYS,
        },
        "counts": {
            "discovered": len(raw),
            "passedScreen": len(screened),
            "shortlisted": len(shortlist),
            "returned": len(final),
        },
        "candidates": final,
        "diagnostics": diagnostics[:20],
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=1)

    print(f"wrote {len(final)} candidates to {OUT_PATH}", file=sys.stderr)
    if diagnostics:
        print(f"{len(diagnostics)} diagnostic note(s):", file=sys.stderr)
        for d in diagnostics[:20]:
            print(f"  [{d['stage']}] {d['message']}", file=sys.stderr)


if __name__ == "__main__":
    main()
