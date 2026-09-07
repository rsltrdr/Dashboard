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

# Only chains the FOMO app can execute on, since that is where orders go.
# FOMO routes swaps against on-chain liquidity rather than a curated token list,
# so anything with real liquidity on these chains is tradeable there.
FOMO_CHAINS = ["Solana", "Ethereum", "Base", "BNB Chain", "Monad", "Robinhood Chain"]

# GeckoTerminal network ids for the ones that are stable and known. Anything not
# listed here gets resolved by name against their networks endpoint at runtime,
# so a new chain only needs adding to FOMO_CHAINS above.
KNOWN_NETWORK_IDS = {
    "Solana": "solana",
    "Ethereum": "eth",
    "Base": "base",
    "BNB Chain": "bsc",
}

NETWORKS = []        # [(label, geckoterminal_id)], filled in at startup
NETWORK_LABEL = {}   # geckoterminal_id -> label

MIN_MCAP_USD = 20_000
MAX_MCAP_USD = 550_000        # a little above the €500k screen so nothing borderline is lost early

# Volume against market cap is a band, not a floor. Healthy micro-cap activity
# runs roughly 20-80% of cap; sustained readings above 100% are a recognised
# wash-trading signature rather than a bullish one. Tokens in their first day
# get more room, because a genuine launch does spike.
MIN_VOL_MCAP_RATIO = 0.5
MAX_VOL_MCAP_RATIO = 3.0
MAX_VOL_MCAP_RATIO_YOUNG = 10.0
YOUNG_HOURS = 24

MIN_LIQUIDITY_USD = 15_000    # absolute floor
MIN_LIQUIDITY_PCT = 0.10      # and at least this share of market cap, or you can't exit

MIN_AGE_HOURS = 6             # skip the launch-minute lottery
MAX_AGE_HOURS = 72            # but stay inside the early window

# Volume spread thin across many wallets is retail; the same volume concentrated
# in a handful is bots cycling. This is the cheapest real/fake discriminator.
MIN_DISTINCT_BUYERS = 15
MAX_VOLUME_PER_BUYER_USD = 2_000

# Movement thresholds: what counts as a candidate accelerating against itself,
# measured between consecutive hourly scans.
MOVE_MCAP_PCT = 25.0
MOVE_VOLUME_PCT = 60.0
MOVE_GROWTH_PCT = 8.0

PAGES_PER_ENDPOINT = 3        # depth of the new/trending sweep per chain
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


def post_json(url, payload, timeout=25):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "meme-coin-radar/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} for {url}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def fetch(url, headers=None, timeout=25, retry_on_429=2):
    """
    GeckoTerminal's free tier throttles aggressively, so a 429 gets a patient
    retry rather than being treated as a dead end.
    """
    for attempt in range(retry_on_429 + 1):
        req = urllib.request.Request(url, headers=headers or {"User-Agent": "meme-coin-radar/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode()), None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retry_on_429:
                time.sleep(12 * (attempt + 1))
                continue
            return None, f"HTTP {e.code} for {url.split('?')[0]}"
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
    return None, "retries exhausted"


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

def normalize(name):
    return "".join(ch for ch in name.lower() if ch.isalnum())


def resolve_networks(max_pages=6):
    """
    Turn FOMO_CHAINS into GeckoTerminal network ids. The well-known ones are
    hardcoded; anything else is matched by name against their networks endpoint,
    so adding a chain to FOMO_CHAINS is all that's needed when FOMO adds one.
    """
    resolved = []
    unknown = [c for c in FOMO_CHAINS if c not in KNOWN_NETWORK_IDS]

    for label in FOMO_CHAINS:
        if label in KNOWN_NETWORK_IDS:
            resolved.append((label, KNOWN_NETWORK_IDS[label]))

    if not unknown:
        return resolved

    catalogue = {}
    for page in range(1, max_pages + 1):
        data, err = fetch(f"https://api.geckoterminal.com/api/v2/networks?page={page}",
                          {"Accept": "application/json;version=20230302",
                           "User-Agent": "meme-coin-radar/1.0"})
        if err or not data or not data.get("data"):
            if err:
                note("networks", f"page {page}: {err}")
            break
        for row in data["data"]:
            name = dig(row, "attributes", "name", default="")
            if name and row.get("id"):
                catalogue[normalize(name)] = (name, row["id"])
        if len(data["data"]) < 50:
            break
        time.sleep(3)

    for label in unknown:
        key = normalize(label)
        hit = catalogue.get(key)
        if not hit:
            # try a looser match, e.g. "Robinhood Chain" against "Robinhood"
            for cat_key, val in catalogue.items():
                if cat_key.startswith(key) or key.startswith(cat_key):
                    hit = val
                    break
        if hit:
            note("networks", f"resolved {label} -> {hit[1]} ({hit[0]})")
            resolved.append((label, hit[1]))
        else:
            note("networks", f"{label} is not indexed by GeckoTerminal; skipped")

    return resolved


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
    # distinct wallets, not transaction count: the closest free stand-in for
    # holder growth now that Solscan's holder endpoint is out of reach
    buyers = dig(attrs, "transactions", "h24", "buyers")
    buyers = int(as_float(buyers)) if buyers is not None else None

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
        "buyers24h": buyers,
        "createdAt": created_ts.isoformat() if created_ts else None,
        "ageHours": round((datetime.now(timezone.utc) - created_ts).total_seconds() / 3600, 2)
        if created_ts else None,
        "poolAddress": attrs.get("address") or "",
    }


def discover(network):
    """
    New pools plus trending pools, deduplicated by token address.

    Paginated, because one page per endpoint left most of the in-band universe
    unseen: the bulk of what comes back sits outside the market cap band, so
    depth matters more than loosening the thresholds would.
    """
    found = {}
    for endpoint in ("new_pools", "trending_pools"):
        for page in range(1, PAGES_PER_ENDPOINT + 1):
            url = (f"https://api.geckoterminal.com/api/v2/networks/{network}/"
                   f"{endpoint}?include=base_token&page={page}")
            data, err = fetch(url, {"Accept": "application/json;version=20230302",
                                    "User-Agent": "meme-coin-radar/1.0"})
            if err or not data:
                note(f"discover:{network}:{endpoint}:p{page}", err or "no data")
                break

            rows = data.get("data")
            if not isinstance(rows, list):
                note(f"discover:{network}:{endpoint}:p{page}", f"unexpected shape, keys={list(data)[:5]}")
                break
            if not rows:
                break  # ran off the end of this endpoint

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

            time.sleep(5)  # GeckoTerminal's free tier throttled us at 2.5s between calls

            if len(rows) < 20:
                break  # short page means there is no next one

    return list(found.values())


# ---------------------------------------------------------------- holders

# Solscan rejected the "token" header with a 401 on the first run, so we try
# each documented auth style once and remember whichever is accepted.
SOLSCAN_AUTH_STYLES = [
    ("token header", lambda k: {"token": k}),
    ("bearer", lambda k: {"Authorization": f"Bearer {k}"}),
    ("apikey header", lambda k: {"apikey": k}),
]
_solscan_style = None
_solscan_dead = False


def solana_holder_count(address):
    global _solscan_style, _solscan_dead
    if not SOLSCAN_KEY or _solscan_dead:
        return None

    url = f"https://pro-api.solscan.io/v2.0/token/meta?address={address}"
    styles = [_solscan_style] if _solscan_style else SOLSCAN_AUTH_STYLES

    data = None
    for style in styles:
        label, build = style
        headers = build(SOLSCAN_KEY)
        headers["User-Agent"] = "meme-coin-radar/1.0"
        data, err = fetch(url, headers, retry_on_429=1)
        if data:
            if _solscan_style is None:
                _solscan_style = style
                note("solscan", f"auth accepted via {label}")
            break
        if _solscan_style:
            note("solscan", err or "no data")
            return None
    else:
        # Rejected once means rejected all run; stop hammering it per candidate.
        _solscan_dead = True
        note("solscan", "all auth styles rejected; falling back to buyer counts "
                        "(the key's plan likely excludes /v2.0/token/meta)")
        return None

    if not data:
        return None

    body = data.get("data") if isinstance(data.get("data"), dict) else data
    for key in ("holder", "holder_count", "holders", "holderCount"):
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


def apply_growth(candidate, state, now_iso):
    """
    Measure whether participation is expanding, comparing this run against the
    last one. True holder counts are preferred; when they aren't available we
    fall back to distinct 24h buyers, which is flow rather than stock but is
    free and moves for the same reasons.

    Either way growth needs two observations, so a token seen for the first
    time reports no growth yet. That is inherent, not a failure.
    """
    address = candidate["address"]
    holders = solana_holder_count(address) if candidate["network"] == "solana" else None
    buyers = candidate.get("buyers24h")

    entry = state.get(address) or {"history": []}
    history = entry["history"]

    candidate["holderCount"] = holders
    candidate["growthPct"] = None
    candidate["growthBasis"] = "unavailable"
    candidate["growthStatus"] = "unavailable"

    # Prefer holders; fall back to distinct buyers.
    basis, value = ("holders", holders) if holders is not None else ("buyers24h", buyers)

    if value is None:
        candidate["growthStatus"] = "no source"
    else:
        prior = [h for h in history if h.get("basis") == basis and h.get("value")]
        if prior:
            before = prior[-1]["value"]
            if before > 0:
                candidate["growthPct"] = round((value - before) / before * 100, 1)
                candidate["growthBasis"] = basis
                candidate["growthStatus"] = "measured"
                candidate["growthBefore"] = before
                candidate["growthBeforeAt"] = prior[-1]["ts"]
        else:
            candidate["growthBasis"] = basis
            candidate["growthStatus"] = "first sighting"
        history.append({"ts": now_iso, "basis": basis, "value": value})

    # Movement against this token's own recent history. A candidate already on
    # the board that suddenly accelerates never triggered anything before,
    # because alerts only fired on new arrivals. AOBS ran 13x while sitting
    # quietly in the file.
    prev_snap = entry.get("lastSnapshot") or {}
    moves = []
    if prev_snap:
        pm, pv = prev_snap.get("mcap") or 0, prev_snap.get("volume") or 0
        if pm > 0:
            mcap_chg = (candidate["mcapUsd"] - pm) / pm * 100
            candidate["mcapChangePct"] = round(mcap_chg, 1)
            if mcap_chg >= MOVE_MCAP_PCT:
                moves.append(f"market cap +{mcap_chg:.0f}%")
        if pv > 0:
            vol_chg = (candidate["volume24hUsd"] - pv) / pv * 100
            candidate["volumeChangePct"] = round(vol_chg, 1)
            if vol_chg >= MOVE_VOLUME_PCT:
                moves.append(f"volume +{vol_chg:.0f}%")
        pg = prev_snap.get("growthPct")
        if pg is not None and candidate.get("growthPct") is not None:
            if candidate["growthPct"] >= MOVE_GROWTH_PCT and candidate["growthPct"] >= pg * 2:
                moves.append(f"participation growth jumped to +{candidate['growthPct']:.1f}%")
    entry["lastSnapshot"] = {
        "ts": now_iso, "mcap": candidate["mcapUsd"],
        "volume": candidate["volume24hUsd"], "growthPct": candidate.get("growthPct"),
    }
    candidate["moves"] = moves
    candidate["isMover"] = bool(moves)

    # One positive reading is noise. Track how many consecutive scans have shown
    # growth, so the screen can insist on a trend rather than a blip.
    streak = entry.get("positiveStreak", 0)
    if candidate["growthPct"] is None:
        pass                      # nothing measured, leave the streak untouched
    elif candidate["growthPct"] > 0:
        streak += 1
    else:
        streak = 0
    entry["positiveStreak"] = streak
    candidate["positiveStreak"] = streak
    candidate["growthConfirmed"] = streak >= 2

    # kept for the dashboard, which scores an "adoption growth" number
    candidate["holderGrowthPct"] = candidate["growthPct"]
    candidate["holderGrowthStatus"] = candidate["growthStatus"]

    entry["history"] = history
    entry["lastSeen"] = now_iso
    entry["symbol"] = candidate["symbol"]
    state[address] = entry
    return candidate


# ---------------------------------------------------------------- CoinGecko

# ---------------------------------------------------------------- distribution

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
DIST_MIN_DAY_VOLUME_USD = 5_000_000   # ignore markets too thin for the signal to mean anything
DIST_MIN_RUNUP_PCT = 10.0             # it has to have actually run before it can distribute
DIST_STATE_PATH = "state/perps.json"
MAX_DISTRIBUTION = 10

# Majors are not flow-driven the way meme coins and mid-cap alts are, so this
# signal is mostly noise on them. Excluded rather than surfaced and ignored.
DIST_EXCLUDE = {"BTC", "ETH", "SOL", "USDC", "USDT"}


def load_perp_state():
    try:
        with open(DIST_STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_perp_state(state, now_iso):
    os.makedirs(os.path.dirname(DIST_STATE_PATH), exist_ok=True)
    for coin in list(state):
        state[coin]["history"] = state[coin]["history"][-12:]
    with open(DIST_STATE_PATH, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def distribution_watch(now_iso):
    """
    Look for crowded longs starting to unwind: something that has run hard,
    where participation is now draining away while positioning stays heavy.

    Hyperliquid publishes open interest and funding alongside price and volume,
    which matter more than volume alone. Funding tells you who is paying to hold
    the position, and open interest tells you whether they are still in it.
    """
    data, err = post_json(HL_INFO_URL, {"type": "metaAndAssetCtxs"})
    if err or not isinstance(data, list) or len(data) < 2:
        note("distribution", err or f"unexpected shape: {type(data).__name__}")
        return []

    universe = dig(data[0], "universe", default=[])
    contexts = data[1]
    if not isinstance(universe, list) or not isinstance(contexts, list):
        note("distribution", "universe/contexts not both lists")
        return []

    state = load_perp_state()
    rows = []

    for i, market in enumerate(universe):
        if i >= len(contexts):
            break
        name = (market or {}).get("name") or ""
        ctx = contexts[i] or {}
        if not name or name in DIST_EXCLUDE:
            continue

        volume = as_float(ctx.get("dayNtlVlm"))
        mark = as_float(ctx.get("markPx"))
        prev = as_float(ctx.get("prevDayPx"))
        oi = as_float(ctx.get("openInterest"))
        funding = as_float(ctx.get("funding"))

        if volume < DIST_MIN_DAY_VOLUME_USD or mark <= 0 or prev <= 0:
            continue

        change24h = (mark - prev) / prev * 100

        entry = state.get(name) or {"history": []}
        history = entry["history"]
        prior = history[-1] if history else None

        row = {
            "market": name,
            "price": mark,
            "change24hPct": round(change24h, 2),
            "dayVolumeUsd": round(volume),
            "openInterest": round(oi, 2),
            "fundingRate": funding,
            "signals": [],
        }

        if prior:
            prior_vol = prior.get("volume") or 0
            prior_oi = prior.get("oi") or 0
            # A rolling 24h volume that falls hour over hour means the hour just
            # added was quieter than the one that dropped off: participation draining.
            if prior_vol > 0:
                row["volumeChangePct"] = round((volume - prior_vol) / prior_vol * 100, 2)
                if volume < prior_vol * 0.9:
                    row["signals"].append("volume draining")
            if prior_oi > 0:
                row["oiChangePct"] = round((oi - prior_oi) / prior_oi * 100, 2)
                # Positions still open while volume dries up is the trapped-holder shape.
                if oi >= prior_oi * 0.97:
                    row["signals"].append("open interest holding")
            prior_price = prior.get("price") or 0
            if prior_price > 0 and mark < prior_price:
                row["signals"].append("price rolling over")

        if change24h >= DIST_MIN_RUNUP_PCT:
            row["signals"].append("ran up 24h")
        if funding > 0:
            row["signals"].append("longs paying funding")

        row["signalCount"] = len(row["signals"])
        row["observations"] = len(history) + 1

        history.append({"ts": now_iso, "price": mark, "volume": volume, "oi": oi, "funding": funding})
        entry["history"] = history
        state[name] = entry

        rows.append(row)

    save_perp_state(state, now_iso)

    # Needs the run-up plus at least two of the unwind signals to be worth showing.
    flagged = [r for r in rows
               if "ran up 24h" in r["signals"] and r["signalCount"] >= 3]
    flagged.sort(key=lambda r: (r["signalCount"], r["change24hPct"]), reverse=True)
    return flagged[:MAX_DISTRIBUTION]


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
    global NETWORKS, NETWORK_LABEL
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    NETWORKS = resolve_networks()
    NETWORK_LABEL = {net_id: label for label, net_id in NETWORKS}
    print(f"scanning: {[f'{l} ({n})' for l, n in NETWORKS]}", file=sys.stderr)

    rate, rate_live = usd_to_eur_rate()
    state = load_state()

    raw = []
    for _label, network in NETWORKS:
        raw.extend(discover(network))
    print(f"discovered {len(raw)} pools across {len(NETWORKS)} networks", file=sys.stderr)

    # --- apply the screen -------------------------------------------------
    screened = []
    rejects = {}

    def reject(reason):
        rejects[reason] = rejects.get(reason, 0) + 1

    for c in raw:
        age = c["ageHours"]

        if c["mcapUsd"] <= 0 or not (MIN_MCAP_USD <= c["mcapUsd"] <= MAX_MCAP_USD):
            reject("market cap outside band")
            continue

        if c["liquidityUsd"] < max(MIN_LIQUIDITY_USD, c["mcapUsd"] * MIN_LIQUIDITY_PCT):
            reject("liquidity too thin to exit")
            continue

        if age is None or not (MIN_AGE_HOURS <= age <= MAX_AGE_HOURS):
            reject("outside the 6-72h window")
            continue

        ratio = c["volume24hUsd"] / c["mcapUsd"]
        ceiling = MAX_VOL_MCAP_RATIO_YOUNG if age < YOUNG_HOURS else MAX_VOL_MCAP_RATIO
        if ratio < MIN_VOL_MCAP_RATIO:
            reject("volume too low against cap")
            continue
        if ratio > ceiling:
            reject("volume implausibly high against cap")
            continue

        buyers = c.get("buyers24h")
        if buyers is not None:
            if buyers < MIN_DISTINCT_BUYERS:
                reject("too few distinct buyers")
                continue
            per_buyer = c["volume24hUsd"] / buyers if buyers else 0
            if per_buyer > MAX_VOLUME_PER_BUYER_USD:
                reject("volume concentrated in few wallets")
                continue
            c["volumePerBuyerUsd"] = round(per_buyer, 2)
        else:
            c["volumePerBuyerUsd"] = None

        c["volMcapRatio"] = round(ratio, 2)
        screened.append(c)

    print(f"{len(screened)} passed the screen; rejected: {rejects}", file=sys.stderr)

    # strongest volume signal first, then check holders on a bounded shortlist
    screened.sort(key=lambda c: c["volMcapRatio"], reverse=True)
    shortlist = screened[:20]

    for c in shortlist:
        apply_growth(c, state, now_iso)
        if c["network"] == "solana" and SOLSCAN_KEY:
            time.sleep(1)

    # A confirmed decline drops out, EXCEPT when the token is moving hard on cap
    # or volume: buyer counts can dip in the hour a price move starts, and that
    # is the worst possible moment to hide something.
    keep = [c for c in shortlist
            if c["growthPct"] is None or c["growthPct"] > 0 or c.get("isMover")]
    # confirmed trends first, then size of growth, then activity
    keep.sort(key=lambda c: (c.get("growthConfirmed", False),
                             c["growthPct"] or 0,
                             c["volMcapRatio"]), reverse=True)
    final = keep[:MAX_CANDIDATES]

    for c in final:
        c["coingecko"] = coingecko_enrich(c)
        c["mcapEur"] = round(c["mcapUsd"] * rate)
        c["liquidityEur"] = round(c["liquidityUsd"] * rate)
        c["volume24hEur"] = round(c["volume24hUsd"] * rate)
        c["dexscreener"] = f"https://dexscreener.com/{c['network']}/{c['address']}"
        time.sleep(1.5)

    save_state(state)

    distribution = distribution_watch(now_iso)
    print(f"{len(distribution)} distribution candidate(s)", file=sys.stderr)

    out = {
        "generatedAt": now_iso,
        "usdToEur": round(rate, 4),
        "usdToEurLive": rate_live,
        "venue": "FOMO app",
        "networksScanned": [label for label, _ in NETWORKS],
        "screen": {
            "mcapUsd": [MIN_MCAP_USD, MAX_MCAP_USD],
            "volMcapRatio": [MIN_VOL_MCAP_RATIO, MAX_VOL_MCAP_RATIO],
            "volMcapRatioUnder24h": [MIN_VOL_MCAP_RATIO, MAX_VOL_MCAP_RATIO_YOUNG],
            "minLiquidityUsd": MIN_LIQUIDITY_USD,
            "minLiquidityPctOfMcap": MIN_LIQUIDITY_PCT,
            "ageHours": [MIN_AGE_HOURS, MAX_AGE_HOURS],
            "minDistinctBuyers": MIN_DISTINCT_BUYERS,
            "maxVolumePerBuyerUsd": MAX_VOLUME_PER_BUYER_USD,
        },
        "counts": {
            "discovered": len(raw),
            "passedScreen": len(screened),
            "shortlisted": len(shortlist),
            "returned": len(final),
        },
        "rejectedBy": rejects,
        "candidates": final,
        "movers": [
            {"symbol": c["symbol"], "chain": c["chain"], "address": c["address"],
             "moves": c["moves"], "mcapEur": c.get("mcapEur"), "volMcapRatio": c.get("volMcapRatio"),
             "growthPct": c.get("growthPct"), "ageHours": c.get("ageHours"),
             "mcapChangePct": c.get("mcapChangePct"), "volumeChangePct": c.get("volumeChangePct"),
             "dexscreener": c.get("dexscreener")}
            for c in shortlist if c.get("isMover")
        ],
        "moveThresholds": {"mcapPct": MOVE_MCAP_PCT, "volumePct": MOVE_VOLUME_PCT, "growthPct": MOVE_GROWTH_PCT},
        "distribution": distribution,
        "distributionScreen": {
            "venue": "Hyperliquid perps (what FOMO routes to)",
            "minDayVolumeUsd": DIST_MIN_DAY_VOLUME_USD,
            "minRunUpPct": DIST_MIN_RUNUP_PCT,
            "excluded": sorted(DIST_EXCLUDE),
            "note": "Needs a 24h run-up plus at least two unwind signals. Majors are excluded because the signal is noise on them.",
        },
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
