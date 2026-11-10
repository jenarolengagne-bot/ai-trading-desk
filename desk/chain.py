"""Seat 2: chain facts. Straight from a Solana RPC node, no model involved.

Facts kill before judgements do: an open mint authority is not an opinion,
so it never reaches the analyst.
"""
from __future__ import annotations

import os

import requests

from config import CHAIN

from desk.num import finite_number, finite_positive

RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SYSTEM_PROGRAM = "11111111111111111111111111111111"


def rpc(method: str, params: list, session: requests.Session | None = None):
    if session is None:
        raise RuntimeError("offline mode: live Solana RPC calls are disabled")
    s = session
    r = s.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
               timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method}: {body['error']}")
    return body["result"]


def facts(mint: str, session: requests.Session | None = None) -> dict:
    """Mint/freeze authority, supply, and holder concentration of REAL wallets.

    The bonding curve and the AMM pool hold huge balances by design. Their token
    accounts belong to program-owned addresses, not to wallets, so we look up who
    owns each top account and drop anything not owned by the System Program.
    """
    info = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}], session)
    parsed = info["value"]["data"]["parsed"]["info"]
    decimals = int(parsed["decimals"])
    supply = int(parsed["supply"]) / 10 ** decimals
    if "mintAuthority" not in parsed or "freezeAuthority" not in parsed:
        authorities_known = False
        mint_authority = None
        freeze_authority = None
    else:
        authorities_known = True
        mint_authority = parsed.get("mintAuthority")
        freeze_authority = parsed.get("freezeAuthority")

    largest = rpc("getTokenLargestAccounts", [mint], session)["value"]
    largest = largest[: CHAIN["largest_accounts_checked"]]
    token_accts = [x["address"] for x in largest]

    # token account -> its owner address
    accts = rpc("getMultipleAccounts", [token_accts, {"encoding": "jsonParsed"}],
                session)["value"]
    owners = [((a or {}).get("data", {}).get("parsed", {}).get("info", {}) or {}).get("owner")
              for a in accts]

    # owner address -> which program owns it (System Program = a normal wallet)
    uniq = [o for o in dict.fromkeys(owners) if o]
    owner_accts = rpc("getMultipleAccounts", [uniq, {"encoding": "base64"}],
                      session)["value"] if uniq else []
    program_of = {o: (acc or {}).get("owner") for o, acc in zip(uniq, owner_accts)}

    wallets: dict[str, float] = {}
    excluded = 0.0
    amounts_known = finite_positive(supply) is not None or supply == 0
    for row, owner in zip(largest, owners):
        amount = finite_number(row.get("uiAmount"))
        if amount is None or amount < 0:
            amounts_known = False
            break
        prog = program_of.get(owner)
        if owner and prog not in (None, SYSTEM_PROGRAM):
            excluded += amount            # curve, pool, vault: not a seller in the usual sense
            continue
        wallets[owner or row["address"]] = wallets.get(owner or row["address"], 0) + amount

    ranked = sorted(wallets.values(), reverse=True)
    share = lambda xs: (sum(xs) / supply) if supply else None
    if not amounts_known or supply == 0:
        top_wallet = top_10 = program_held = None
    else:
        top_wallet = share(ranked[:1])
        top_10 = share(ranked[:10])
        program_held = share([excluded])
    return {
        "mint_authority": mint_authority,
        "freeze_authority": freeze_authority,
        "authorities_known": authorities_known,
        "supply": supply,
        "top_wallet_pct": top_wallet,
        "top_10_pct": top_10,
        "program_held_pct": program_held,
        "wallets_counted": len(ranked),
        # Not produced by these RPC calls. Absence is a reject in chain_kill.
        "creator_since_migration": None,
        "bundle_proof": None,
    }


def chain_kill(f: dict) -> str | None:
    """First failure wins. Missing evidence is unknown, never a pass.

    Creator history since migration and bundle proofs are not available from
    the RPC methods in this build. A value stuffed into the dict is not a
    proof, and top-10 concentration does not stand in for either one.
    """
    if not isinstance(f, dict):
        return "unknown_authority"
    if f.get("authorities_known") is False or "mint_authority" not in f or "freeze_authority" not in f:
        return "unknown_authority"
    if f["mint_authority"] and not CHAIN["allow_mint_authority"]:
        return "mint_authority"
    if f["freeze_authority"] and not CHAIN["allow_freeze_authority"]:
        return "freeze_authority"
    top_wallet = finite_number(f["top_wallet_pct"]) if "top_wallet_pct" in f else None
    top_10 = finite_number(f["top_10_pct"]) if "top_10_pct" in f else None
    if top_wallet is None or top_10 is None:
        return "unknown_concentration"
    if top_wallet > CHAIN["max_top_wallet"]:
        return "top_wallet"
    if top_10 > CHAIN["max_top_10"]:
        return "top_10"
    return "unknown_evidence"
