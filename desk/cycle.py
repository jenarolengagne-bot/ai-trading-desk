"""One offline cycle. No network, no cloud analyst, no new entries.

Exits on positions already in the ledger still run, and only after a finite
quote. Creator/bundle evidence is absent in this build, so chain_kill rejects
before any order. Structural gates (day baseline, quiet hours, size, ticket)
run before the disabled analyst hook.
"""
from __future__ import annotations

from config import ANALYST_CALLS_ALLOWED

from desk import chain, scanner
from desk.broker import PaperBroker
from desk.ops import Bench
from desk.risk import DISCLOSURE, modeled_stop, size_ticket, structural_block


def run_cycle(*, tokens: list[dict], facts_for, quote_for, broker: PaperBroker,
              bench: Bench, now, chain_attempts: int, events: list | None = None,
              analyst=None, execute_exits: bool = True) -> dict:
    if analyst is not None or ANALYST_CALLS_ALLOWED != 0:
        raise RuntimeError("offline cycle refuses an analyst")
    events = events if events is not None else []
    stats = {
        "seen": 0, "benched": 0, "free": 0, "chain": 0, "unknown": 0,
        "risk": 0, "entries": 0, "analyst_calls": 0, "halted": False,
    }

    def emit(kind: str, **fields):
        events.append({"time": now.isoformat() if hasattr(now, "isoformat") else str(now),
                       "kind": kind, **fields})

    for mint in list(broker.s["positions"]):
        result = broker.on_quote(mint, quote_for(mint), now=now, execute=execute_exits)
        emit("exit_decision", **result)
        if result.get("status") == "filled" and "pnl_usd" in result:
            bench.sit(mint, "exited", now=now)

    account = broker.account_view()
    blocked = structural_block(account, liquidity_usd=10**12, now=now)
    # A huge pool isolates the account-level halt from the ticket floor.
    account_halts = {
        "daily loss limit hit, desk halted until tomorrow",
        "quiet hours, no new entries",
        "unknown account",
        "unknown day baseline",
    }
    if blocked in account_halts:
        stats["halted"] = True
        emit("halt", reason=blocked, equity=account.equity, day_start=account.day_start,
             day_pnl=account.day_pnl)
        emit("cycle", **stats, equity=broker.equity(), day_start=broker.s["day_start"],
             day_pnl=broker.day_pnl(), disclosure=DISCLOSURE)
        return stats

    ranked = sorted(tokens, key=lambda t: (t.get("volume_h1") or 0) / max(t.get("mcap_usd") or 1, 1),
                    reverse=True)
    attempts = chain_attempts
    for token in ranked:
        stats["seen"] += 1
        mint = token.get("mint")
        if mint in broker.s["positions"] or bench.benched(mint, now=now):
            stats["benched"] += 1
            continue
        reason = scanner.free_kill(token)
        if reason:
            bench.sit(mint, reason, now=now)
            stats["free"] += 1
            emit("reject", stage="scanner", mint=mint, reason=reason)
            continue
        if attempts <= 0:
            stats["halted"] = True
            emit("halt", reason="chain attempt budget exhausted")
            break
        try:
            facts = facts_for(mint)
        except Exception as exc:
            attempts -= 1
            emit("error", stage="chain", mint=mint, error=str(exc))
            continue
        attempts -= 1
        kill = chain.chain_kill(facts)
        if kill:
            bench.sit(mint, kill, now=now)
            if str(kill).startswith("unknown"):
                stats["unknown"] += 1
            else:
                stats["chain"] += 1
            emit("reject", stage="chain", mint=mint, reason=kill)
            continue
        # Reachable only if a test replaces chain_kill. Still no analyst and no buy.
        account = broker.account_view()
        structural = structural_block(account, token.get("liquidity_usd"), now=now)
        if structural:
            bench.sit(mint, "risk", now=now)
            stats["risk"] += 1
            emit("reject", stage="risk", mint=mint, reason=structural,
                 disclosure=DISCLOSURE)
            continue
        _ok, ticket, _why = size_ticket(account, token.get("liquidity_usd"))
        stats["risk"] += 1
        emit("reject", stage="analyst", mint=mint, reason="analyst_disabled",
             ticket_usd=ticket, disclosure=DISCLOSURE,
             modeled_stop=modeled_stop(ticket))
    emit("cycle", **stats, equity=broker.equity(), day_start=broker.s["day_start"],
         day_pnl=broker.day_pnl(), disclosure=DISCLOSURE,
         analyst_calls_allowed=ANALYST_CALLS_ALLOWED)
    return stats
