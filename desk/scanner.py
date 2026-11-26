"""Seat 1: the scanner. Pulls fresh Solana pools and kills everything a comparison can kill.

GeckoTerminal is free and needs no key, but allows about 10 calls a minute,
so one cycle spends `pages` calls here and nothing per token.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from config import SCAN

from desk.num import finite_number, finite_positive

GT = "https://api.geckoterminal.com/api/v2"
DEX = "https://api.dexscreener.com/latest/dex/tokens"
HEADERS = {"Accept": "application/json", "User-Agent": "ai-trading-desk/1.0"}


def _f(x) -> float:
    """Legacy zero-fill. Prefer finite_number; do not use this for gate inputs."""
    number = finite_number(x)
    return 0.0 if number is None else number


def _opt(x):
    return finite_number(x)


def _count(x):
    number = finite_number(x)
    if number is None or number < 0:
        return None
    return int(number)


def _age_minutes(iso: str | None, now: datetime | None = None) -> float | None:
    if not iso:
        return None
    try:
        created = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return max(0.0, (clock - created).total_seconds() / 60)


def parse_pool(pool: dict, symbols: dict[str, str]) -> dict | None:
    """One GeckoTerminal pool -> the desk's token dict. Field names are fixed here, once."""
    a = pool.get("attributes", {})
    rel = pool.get("relationships", {})
    base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    dex_id = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
    if not base_id.startswith("solana_"):
        return None
    mint = base_id.split("_", 1)[1]
    tx_h1 = (a.get("transactions") or {}).get("h1") or {}
    vol = a.get("volume_usd") or {}
    chg = a.get("price_change_percentage") or {}
    return {
        "mint": mint,
        "symbol": symbols.get(base_id, a.get("name", "?").split(" / ")[0]),
        "pool": a.get("address"),
        "dex": dex_id,
        "price_usd": _opt(a.get("base_token_price_usd")),
        "mcap_usd": _opt(a.get("market_cap_usd")) if a.get("market_cap_usd") not in (None, "")
        else _opt(a.get("fdv_usd")),
        "liquidity_usd": _opt(a.get("reserve_in_usd")),
        "volume_m5": _opt(vol.get("m5")), "volume_h1": _opt(vol.get("h1")),
        "volume_h6": _opt(vol.get("h6")), "volume_h24": _opt(vol.get("h24")),
        "buys_h1": _count(tx_h1.get("buys")), "sells_h1": _count(tx_h1.get("sells")),
        "buyers_h1": _count(tx_h1.get("buyers")),
        "change_m5": _opt(chg.get("m5")), "change_h1": _opt(chg.get("h1")),
        "change_h6": _opt(chg.get("h6")),
        "age_minutes": None if (age := _age_minutes(a.get("pool_created_at"))) is None else round(age, 1),
    }


def new_tokens(session: requests.Session | None = None) -> list[dict]:
    """Fresh pools on Solana, newest first, deduplicated by mint."""
    if session is None:
        raise RuntimeError("offline mode: live GeckoTerminal calls are disabled")
    s = session
    out, seen = [], set()
    for page in range(1, SCAN["pages"] + 1):
        r = s.get(f"{GT}/networks/solana/new_pools",
                  params={"page": page, "include": "base_token,dex"},
                  headers=HEADERS, timeout=20)
        if r.status_code == 429:
            time.sleep(60)          # back off a full minute, never retry in place
            break
        r.raise_for_status()
        body = r.json()
        symbols = {i["id"]: i["attributes"].get("symbol", "?")
                   for i in body.get("included", []) if i.get("type") == "token"}
        for pool in body.get("data", []):
            try:
                t = parse_pool(pool, symbols)
            except (TypeError, ValueError, KeyError):
                continue
            if not t or t["mint"] in seen:
                continue
            if SCAN["pump_only"] and t["dex"] not in SCAN["pump_dexes"]:
                continue
            seen.add(t["mint"])
            out.append(t)
    return out


def free_kill(t: dict) -> str | None:
    """Pass one. Missing or non-finite inputs are unknown, never a pass and never zero."""
    if not isinstance(t, dict):
        return "unknown"
    if finite_positive(t.get("price_usd")) is None:
        return "invalid_price"
    age = finite_number(t.get("age_minutes"))
    liquidity = finite_number(t.get("liquidity_usd"))
    volume = finite_number(t.get("volume_h1"))
    mcap = finite_number(t.get("mcap_usd"))
    buys = finite_number(t.get("buys_h1"))
    sells = finite_number(t.get("sells_h1"))
    if None in (age, liquidity, volume, mcap, buys, sells):
        return "unknown"
    if min(age, liquidity, volume, mcap, buys, sells) < 0:
        return "unknown"
    if not SCAN["min_age_minutes"] <= age <= SCAN["max_age_hours"] * 60:
        return "age"
    if liquidity < SCAN["min_liquidity_usd"]:
        return "liquidity"
    if volume < SCAN["min_volume_h1_usd"]:
        return "volume"
    if not SCAN["min_mcap_usd"] <= mcap <= SCAN["max_mcap_usd"]:
        return "mcap"
    if buys + sells < SCAN["min_trades_h1"]:
        return "trades"
    if buys == 0:
        return "unknown"
    if sells / buys < SCAN["min_sells_ratio_h1"]:
        return "no_sells"
    return None


def socials(mint: str, session: requests.Session | None = None) -> dict:
    """X / Telegram / site links as the project published them (DexScreener token info).
    Taken from the listing, never from a search: a search finds fan accounts."""
    if session is None:
        return {"x": None, "telegram": None, "website": None, "status": "unknown"}
    s = session
    try:
        pairs = s.get(f"{DEX}/{mint}", headers=HEADERS, timeout=20).json().get("pairs") or []
    except Exception:
        return {"x": None, "telegram": None, "website": None, "status": "unknown"}
    info = {}
    for p in pairs:
        if p.get("info"):
            info = p["info"]
            break
    links = {x.get("type"): x.get("url") for x in info.get("socials", [])}
    sites = info.get("websites") or []
    return {"x": links.get("twitter"), "telegram": links.get("telegram"),
            "website": sites[0].get("url") if sites else None, "status": "ok"}


def quote(mint: str, session: requests.Session | None = None) -> dict | None:
    """Live price and volume for an open position, from the deepest pair."""
    if session is None:
        return None
    s = session
    try:
        pairs = s.get(f"{DEX}/{mint}", headers=HEADERS, timeout=20).json().get("pairs") or []
    except Exception:
        return None
    if not pairs:
        return None
    p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
    vol = p.get("volume") or {}
    price = finite_positive(p.get("priceUsd"))
    if price is None:
        return None
    return {"price_usd": price,
            "liquidity_usd": _opt((p.get("liquidity") or {}).get("usd")),
            "volume_h6": _opt(vol.get("h6")) if "h6" in vol else None,
            "volume_h24": _opt(vol.get("h24")) if "h24" in vol else None}
