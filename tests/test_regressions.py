"""Regressions for the offline audit. No network and no paid client."""
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from desk import analyst, chain, scanner
from desk.broker import LedgerError, PaperBroker
from desk.cycle import run_cycle
from desk.ops import Bench, Journal
from desk.replay import run_fixture
from desk.risk import (
    Account, check_token_trade, modeled_stop, structural_block,
)
import run_crypto
import run_stocks

NOON = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 10, 26, 2, 30, tzinfo=timezone.utc)


def token(**kw):
    base = {
        "mint": "M", "symbol": "TEST", "age_minutes": 45, "liquidity_usd": 30_000,
        "volume_h1": 60_000, "mcap_usd": 200_000, "buys_h1": 400, "sells_h1": 250,
        "price_usd": 0.0002,
    }
    base.update(kw)
    return base


def thesis(**kw):
    base = dict(verdict="valid_setup", score=8, red_flags=[], green_flags=["crowd"],
                shape="crowd", invalidation="buyers stall", sources=["buyers_h1"])
    base.update(kw)
    return analyst.TokenThesis(**base)


def clean_facts(**kw):
    base = {
        "mint_authority": None, "freeze_authority": None, "authorities_known": True,
        "top_wallet_pct": 0.01, "top_10_pct": 0.10,
        "creator_since_migration": {"wallet": "not-a-proof"}, "bundle_proof": True,
    }
    base.update(kw)
    return base


class SizingUnchanged(unittest.TestCase):
    def test_equity_1000_and_300(self):
        ok, ticket, reason = check_token_trade(
            thesis(), Account(1_000, 0, 0, 1_000), 30_000, now=NOON)
        self.assertEqual((ok, ticket, reason), (True, 20.0, "approved"))
        ok, ticket, reason = check_token_trade(
            thesis(), Account(300, 0, 0, 300), 10_000_000, now=NOON)
        self.assertFalse(ok)
        self.assertEqual(ticket, 0.0)
        self.assertIn("fee floor", reason)
        self.assertIn("6.0", reason)

    def test_floor_boundary_and_three_positions(self):
        ok, ticket, _ = check_token_trade(thesis(), Account(500, 0, 0, 500), 30_000, now=NOON)
        self.assertEqual((ok, ticket), (True, 10.0))
        ok, _, reason = check_token_trade(thesis(), Account(499, 0, 0, 499), 30_000, now=NOON)
        self.assertFalse(ok)
        self.assertIn("fee floor", reason)
        ok, _, reason = check_token_trade(thesis(), Account(1_000, 0, 3, 1_000), 30_000, now=NOON)
        self.assertIn("max open", reason)

    def test_daily_baseline_is_day_start_not_shrunk_equity(self):
        # 29.13 lost on a 1 000 baseline used to halt because 3% was applied
        # to the equity after the loss. It must not.
        early = Account(970.87, -29.13, 0, 1_000)
        self.assertNotIn("daily loss", check_token_trade(thesis(), early, 30_000, now=NOON)[2])
        exact = Account(970, -30, 0, 1_000)
        self.assertIn("daily loss", check_token_trade(thesis(), exact, 30_000, now=NOON)[2])

    def test_stop_model_is_not_a_guarantee(self):
        model = modeled_stop(20)
        self.assertFalse(model["fill_guaranteed"])
        self.assertTrue(model["gap_unbounded"])
        self.assertGreater(model["loss_frac_of_ticket_at_exact_trigger"], 0.35)
        self.assertFalse(config.STOP_FILL_GUARANTEED)
        self.assertEqual(config.RISK["min_ticket_usd"], 10)
        self.assertEqual(config.ANALYST_CALLS_ALLOWED, 0)


class InvalidData(unittest.TestCase):
    def test_price_and_nan_do_not_pass(self):
        self.assertEqual(scanner.free_kill(token(price_usd=0)), "invalid_price")
        self.assertEqual(scanner.free_kill(token(price_usd=float("nan"))), "invalid_price")
        self.assertEqual(scanner.free_kill(token(liquidity_usd=float("nan"))), "unknown")
        self.assertEqual(scanner.free_kill({"mint": "M", "price_usd": 1}), "unknown")

    def test_bad_pool_does_not_abort_the_scan(self):
        created = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        good = {
            "attributes": {
                "address": "P", "name": "DOG / SOL", "base_token_price_usd": "0.001",
                "market_cap_usd": None, "fdv_usd": "150000", "reserve_in_usd": "20000",
                "volume_usd": {"h1": "50000"},
                "transactions": {"h1": {"buys": 300, "sells": 200, "buyers": 10}},
                "price_change_percentage": {"h1": "40"}, "pool_created_at": created,
            },
            "relationships": {"base_token": {"data": {"id": "solana_GOOD"}},
                              "dex": {"data": {"id": "pump-fun"}}},
        }
        bad = json.loads(json.dumps(good))
        bad["attributes"]["pool_created_at"] = "not-a-date"
        bad["relationships"]["base_token"]["data"]["id"] = "solana_BAD"
        body = {"included": [{"id": "solana_GOOD", "type": "token", "attributes": {"symbol": "DOG"}},
                             {"id": "solana_BAD", "type": "token", "attributes": {"symbol": "NOPE"}}],
                "data": [bad, good]}

        class Resp:
            status_code = 200
            def json(self):
                return body
            def raise_for_status(self):
                return None

        class Sess:
            def get(self, *args, **kwargs):
                return Resp()

        found = scanner.new_tokens(Sess())
        self.assertEqual([t["mint"] for t in found], ["BAD", "GOOD"])
        self.assertIsNone(found[0]["age_minutes"])
        self.assertEqual(scanner.free_kill(found[0]), "unknown")

    def test_quote_missing_price_is_not_zero(self):
        class Resp:
            def json(self):
                return {"pairs": [{"liquidity": {"usd": 10}, "volume": {"h24": "4000"}}]}
        class Sess:
            def get(self, *args, **kwargs):
                return Resp()
        self.assertIsNone(scanner.quote("M", Sess()))

        class Resp2:
            def json(self):
                return {"pairs": [{"priceUsd": "1.5", "liquidity": {"usd": 10},
                                   "volume": {"h24": "4000"}}]}
        class Sess2:
            def get(self, *args, **kwargs):
                return Resp2()
        quote = scanner.quote("M", Sess2())
        self.assertEqual(quote["price_usd"], 1.5)
        self.assertIsNone(quote["volume_h6"])


class Evidence(unittest.TestCase):
    def test_stuffed_proof_and_top10_do_not_pass(self):
        facts = clean_facts()
        self.assertLessEqual(facts["top_10_pct"], config.CHAIN["max_top_10"])
        self.assertEqual(chain.chain_kill(facts), "unknown_evidence")
        self.assertEqual(chain.chain_kill({**facts, "mint_authority": "X"}), "mint_authority")
        self.assertEqual(chain.chain_kill({**facts, "freeze_authority": "X"}), "freeze_authority")
        self.assertEqual(chain.chain_kill({**facts, "top_wallet_pct": 0.09}), "top_wallet")
        self.assertFalse(config.CHAIN["allow_mint_authority"])
        self.assertFalse(config.CHAIN["allow_freeze_authority"])

    def test_missing_authority_or_concentration_is_unknown(self):
        self.assertEqual(chain.chain_kill({"top_wallet_pct": 0.01, "top_10_pct": 0.1}),
                         "unknown_authority")
        self.assertEqual(chain.chain_kill({
            "mint_authority": None, "freeze_authority": None,
            "top_wallet_pct": None, "top_10_pct": 0.1,
        }), "unknown_concentration")


class Ledger(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "ledger.json"
        self.b = PaperBroker(self.path)

    def test_buy_rejects_bad_price_and_second_open(self):
        with self.assertRaises(LedgerError):
            self.b.buy("M", "T", 20, 0)
        with self.assertRaises(LedgerError):
            self.b.buy("M", "T", 20, float("nan"))
        self.assertEqual(self.b.s["cash"], 1_000)
        self.b.buy("M", "T", 20, 1.0, now=NOON)
        with self.assertRaises(LedgerError):
            self.b.buy("M", "T", 20, 1.0, now=NOON)
        self.assertEqual(self.b.open_count(), 1)
        self.assertAlmostEqual(self.b.s["cash"], 980)

    def test_concurrent_buys_keep_both_positions(self):
        errors = []

        def work(name):
            try:
                broker = PaperBroker(self.path)
                broker.buy(name, name, 20, 1.0, now=NOON, key=name)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(mint,)) for mint in ("A", "C")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        reloaded = PaperBroker(self.path)
        self.assertEqual(set(reloaded.s["positions"]), {"A", "C"})
        self.assertAlmostEqual(reloaded.s["cash"], 960)
        self.assertTrue(reloaded.reconcile()["finite"])

    def test_nan_ledger_is_refused(self):
        self.path.write_text(json.dumps({"v": 2, "cash": float("nan"), "positions": {},
                                         "day": "2026-10-26", "day_start": 1000,
                                         "closed": [], "realized_pnl": 0,
                                         "applied": {}, "applied_order": []}))
        with self.assertRaises(LedgerError):
            PaperBroker(self.path)

    def test_day_shift_keeps_positions_and_resets_baseline(self):
        self.b.buy("M", "T", 20, 1.0, now=NOON)
        equity_before = self.b.equity()
        # Next calendar day, still inside the 24h max hold, quote unchanged.
        later = datetime(2026, 10, 27, 0, 30, tzinfo=timezone.utc)
        # A valid quote with no exit rule: price stays near entry, hold is short.
        result = self.b.on_quote("M", {"price_usd": 1.02, "volume_h6": 1000, "volume_h24": 4000},
                                 now=later)
        self.assertEqual(result["status"], "hold")
        self.assertEqual(self.b.open_count(), 1)
        self.assertEqual(self.b.s["day"], "2026-10-27")
        self.assertEqual(self.b.s["day_start"], self.b.equity())
        self.assertEqual(self.b.day_pnl(), 0)
        self.assertEqual(self.b.equity(), equity_before)
        again = PaperBroker(self.path)
        self.assertEqual(again.open_count(), 1)
        self.assertEqual(again.s["day_start"], equity_before)

    def test_partial_only_after_fill_and_replay_is_idempotent(self):
        self.b.buy("M", "T", 20, 1.0, now=NOON)
        quote = {"price_usd": 2.1, "volume_h6": 1000, "volume_h24": 4000}
        self.assertEqual(self.b.exit_signal("M", quote, now=NOON)[0], 0.5)
        other = PaperBroker(self.path)
        self.assertFalse(other.s["positions"]["M"]["half_taken"])
        self.assertEqual(other.exit_signal("M", quote, now=NOON)[0], 0.5)
        first = other.on_quote("M", quote, now=NOON)
        self.assertEqual(first["status"], "filled")
        self.assertTrue(other.s["positions"]["M"]["half_taken"])
        qty = other.s["positions"]["M"]["qty"]
        second = other.on_quote("M", quote, now=NOON)
        self.assertEqual(second["status"], "hold")
        self.assertAlmostEqual(other.s["positions"]["M"]["qty"], qty)

    def test_invalid_quote_and_missing_volume_do_not_exit(self):
        self.b.buy("M", "T", 20, 1.0, now=NOON)
        cash = self.b.s["cash"]
        later = NOON + timedelta(minutes=31)
        for quote in (None, {"price_usd": 0}, {"price_usd": float("nan")},
                      {"price_usd": 1.0, "volume_h6": None, "volume_h24": 4000}):
            result = self.b.on_quote("M", quote, now=later)
            self.assertIn(result["status"], {"unknown", "hold"})
            self.assertEqual(self.b.open_count(), 1)
            self.assertAlmostEqual(self.b.s["cash"], cash)
        # An explicit zero print is real decay, a missing window is not.
        self.assertIsNone(self.b.exit_signal(
            "M", {"price_usd": 1.0, "volume_h6": None, "volume_h24": 4000}, now=later))
        self.assertIn("volume decay", self.b.exit_signal(
            "M", {"price_usd": 1.0, "volume_h6": 0, "volume_h24": 4000}, now=later)[1])

    def test_stop_fill_is_worse_than_the_trigger_and_not_repeated(self):
        self.b.buy("M", "T", 20, 1.0, now=NOON)
        entry = self.b.s["positions"]["M"]["entry"]
        trigger = entry * (1 - config.EXITS["stop_pct"])
        out = self.b.on_quote("M", {"price_usd": trigger, "volume_h6": 1000, "volume_h24": 4000},
                              now=NOON)
        self.assertEqual(out["status"], "filled")
        self.assertFalse(out["fill_guaranteed"])
        self.assertLess(out["fill"], trigger)
        self.assertGreater(-out["pnl_usd"] / 20, 0.35)
        again = self.b.on_quote("M", {"price_usd": trigger}, now=NOON)
        self.assertEqual(again["status"], "gone")
        self.assertEqual(self.b.open_count(), 0)


class CycleAndReplay(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "ledger.json"

    def test_quiet_and_errors_do_not_fan_out(self):
        broker = PaperBroker(self.path)
        calls = {"n": 0}

        def facts_for(mint):
            calls["n"] += 1
            raise RuntimeError("rpc down")

        stats = run_cycle(tokens=[token(mint=f"T{i}") for i in range(5)],
                          facts_for=facts_for, quote_for=lambda mint: None,
                          broker=broker, bench=Bench(str(Path(self.dir) / "b.db")),
                          now=NIGHT, chain_attempts=8, analyst=None)
        self.assertTrue(stats["halted"])
        self.assertEqual(calls["n"], 0)
        self.assertEqual(stats["analyst_calls"], 0)

        calls["n"] = 0
        stats = run_cycle(tokens=[token(mint=f"T{i}") for i in range(5)],
                          facts_for=facts_for, quote_for=lambda mint: None,
                          broker=broker, bench=Bench(str(Path(self.dir) / "b2.db")),
                          now=NOON, chain_attempts=2, analyst=None)
        self.assertEqual(calls["n"], 2)

    def test_passing_gates_still_do_not_call_analyst_or_buy(self):
        broker = PaperBroker(self.path)
        raw = json.loads(self.path.read_text())
        raw["cash"] = 300
        raw["day_start"] = 300
        self.path.write_text(json.dumps(raw))
        broker = PaperBroker(self.path)
        original = chain.chain_kill
        chain.chain_kill = lambda facts: None
        try:
            events = []
            stats = run_cycle(tokens=[token()], facts_for=lambda mint: clean_facts(),
                              quote_for=lambda mint: None, broker=broker,
                              bench=Bench(str(Path(self.dir) / "b.db")), now=NOON,
                              chain_attempts=8, events=events, analyst=None)
        finally:
            chain.chain_kill = original
        self.assertEqual(stats["entries"], 0)
        self.assertEqual(broker.open_count(), 0)
        self.assertTrue(any("fee floor" in e.get("reason", "") for e in events))
        with self.assertRaises(RuntimeError):
            run_cycle(tokens=[], facts_for=lambda mint: {}, quote_for=lambda mint: None,
                      broker=broker, bench=Bench(str(Path(self.dir) / "b3.db")),
                      now=NOON, chain_attempts=1, analyst=object())

    def test_replay_is_deterministic_and_rejects_stuffed_evidence(self):
        path = Path("tests/fixtures/replay_reject.json")
        if not path.exists():
            path = Path(__file__).resolve().parent / "fixtures" / "replay_reject.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        first = run_fixture(fixture, Path(self.dir) / "a.json")
        second = run_fixture(fixture, Path(self.dir) / "b.json")
        self.assertEqual(first, second)
        self.assertEqual(first["analyst_calls"], 0)
        self.assertEqual(first["positions"], [])
        self.assertEqual(first["stats"]["entries"], 0)
        self.assertTrue(any(e.get("reason") == "unknown_evidence" for e in first["events"]))
        self.assertFalse(first["risk_cap_is_fill_guarantee"])
        json.dumps(first, allow_nan=False)

    def test_disabled_entry_points(self):
        self.assertEqual(run_crypto.main([]), 2)
        self.assertEqual(run_stocks.main([]), 2)
        with self.assertRaises(analyst.AnalystDisabled):
            analyst.analyze_token(token(), {}, {})
        with self.assertRaises(RuntimeError):
            run_stocks.clients()
        with self.assertRaises(RuntimeError):
            run_stocks.execute()
        with self.assertRaises(RuntimeError):
            scanner.new_tokens()
        with self.assertRaises(RuntimeError):
            chain.rpc("getAccountInfo", ["M"])
        self.assertIsNone(scanner.quote("M"))
        self.assertEqual(scanner.socials("M")["status"], "unknown")

    def test_journal_is_bounded(self):
        path = Path(self.dir) / "j.jsonl"
        book = Journal(path, max_lines=3)
        for i in range(5):
            book.write("note", now=NOON, i=i)
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[-1])["i"], 4)


if __name__ == "__main__":
    unittest.main()
