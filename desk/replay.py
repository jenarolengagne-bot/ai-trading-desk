"""Deterministic offline replay of the covered seats.

Covers scanner cuts, chain_kill, risk gates, paper exits and the ledger.
Does not call a network and does not reproduce the cloud analyst.
The clock and the fixtures are the only inputs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from config import RISK
from desk.broker import PaperBroker
from desk.cycle import run_cycle
from desk.ops import Bench


def _clock(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def run_fixture(fixture: dict, ledger_path: Path | None = None) -> dict:
    now = _clock(fixture["now"])
    if ledger_path is None:
        raise ValueError("ledger_path is required so replay does not touch the repo tree")
    if ledger_path.exists():
        ledger_path.unlink()
    lock = Path(str(ledger_path) + ".lock")
    if lock.exists():
        lock.unlink()
    broker = PaperBroker(ledger_path)
    if "ledger" in fixture and fixture["ledger"] is not None:
        with broker._exclusive():
            broker.s = broker._migrate(fixture["ledger"])
            broker._validate()
            broker._save()
    bench = Bench(str(ledger_path.with_suffix(".bench.db")))
    facts = fixture.get("facts") or {}
    errors = set(fixture.get("chain_errors") or [])
    quotes = fixture.get("quotes") or {}

    def facts_for(mint: str):
        if mint in errors:
            raise RuntimeError(f"fixture chain error for {mint}")
        if mint not in facts:
            raise RuntimeError(f"fixture has no facts for {mint}")
        return facts[mint]

    def quote_for(mint: str):
        return quotes.get(mint)

    events: list = []
    stats = run_cycle(
        tokens=list(fixture.get("tokens") or []),
        facts_for=facts_for,
        quote_for=quote_for,
        broker=broker,
        bench=bench,
        now=now,
        chain_attempts=int(fixture.get("chain_attempts", 8)),
        events=events,
        analyst=None,
        execute_exits=bool(fixture.get("execute_exits", True)),
    )
    report = {
        "now": now.isoformat(),
        "stats": stats,
        "events": events,
        "reconcile": broker.reconcile(),
        "positions": sorted(broker.s["positions"]),
        "analyst_calls": stats["analyst_calls"],
        "risk_cap_is_fill_guarantee": False,
        "min_ticket_usd": RISK["min_ticket_usd"],
    }
    # Canonical dump so two runs compare equal, including key order.
    return json.loads(json.dumps(report, sort_keys=True, allow_nan=False))
