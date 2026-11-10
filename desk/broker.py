"""Seat 5a: paper execution. No keys, no signing, no live orders.

The ledger is one finite JSON document. Mutations reload it under an exclusive
file lock and publish it with os.replace, so two processes cannot drop each
other's positions. A partial flag is written in the same save as the fill that
earned it. The stop is a trigger: fills include fees and slippage and are not
guaranteed. Invalid prices are refused.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from config import EXITS, LEDGER_LOCK_SECONDS, LEDGER_MAX_APPLIED, LEDGER_MAX_CLOSED, PAPER

from desk.num import finite_number, finite_positive, quote_price, quote_volume

LEDGER = Path("paper_ledger.json")


class LedgerError(Exception):
    """Refused mutation or a ledger that cannot be reconciled."""


def _ts(now) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.timestamp()
    number = finite_number(now)
    if number is None:
        raise LedgerError("invalid clock")
    return number


def _day(now) -> date:
    if now is None:
        return date.today()
    if isinstance(now, datetime):
        return now.date()
    if isinstance(now, date):
        return now
    raise LedgerError("invalid clock")


class PaperBroker:
    def __init__(self, path: Path = LEDGER):
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")
        self._fh = None
        self._depth = 0
        self._thread = threading.RLock()
        self.s = None
        with self._exclusive():
            if self.path.exists():
                self._reload()
            else:
                eq = float(PAPER["starting_equity_usd"])
                self.s = self._blank(eq, date.today())
                self._validate()
                self._save()

    @staticmethod
    def _blank(equity: float, day: date) -> dict:
        return {
            "v": 2,
            "cash": equity,
            "positions": {},
            "day": day.isoformat(),
            "day_start": equity,
            "closed": [],
            "realized_pnl": 0.0,
            "applied": {},
            "applied_order": [],
        }

    # ---------- lock and atomic publish ----------
    @contextmanager
    def _exclusive(self):
        self._thread.acquire()
        try:
            if self._depth == 0:
                self._acquire_file()
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self._release_file()
        finally:
            self._thread.release()

    def _acquire_file(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_path, "a+")
        deadline = time.monotonic() + LEDGER_LOCK_SECONDS
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    self._fh.write("0")
                    self._fh.flush()
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise LedgerError("ledger lock timeout")
                time.sleep(0.01)

    def _release_file(self):
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def _reload(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LedgerError(f"ledger unreadable: {exc}") from exc
        if not isinstance(raw, dict):
            raise LedgerError("ledger is not an object")
        self.s = self._migrate(raw)
        self._validate()

    @staticmethod
    def _migrate(raw: dict) -> dict:
        state = dict(raw)
        state.setdefault("v", 2)
        state.setdefault("positions", {})
        state.setdefault("closed", [])
        state.setdefault("applied", {})
        state.setdefault("applied_order", [])
        if "realized_pnl" not in state:
            total = 0.0
            for row in state["closed"]:
                pnl = finite_number(row.get("pnl_usd")) if isinstance(row, dict) else None
                total += 0.0 if pnl is None else pnl
            state["realized_pnl"] = round(total, 2)
        if "day_start" not in state:
            cash = finite_number(state.get("cash"))
            state["day_start"] = cash if cash is not None else state.get("cash")
        positions = state.get("positions") or {}
        if isinstance(positions, dict):
            for pos in positions.values():
                if isinstance(pos, dict):
                    pos.setdefault("half_taken", False)
                    pos.setdefault("proceeds", 0.0)
        if isinstance(state.get("closed"), list) and len(state["closed"]) > LEDGER_MAX_CLOSED:
            state["closed"] = state["closed"][-LEDGER_MAX_CLOSED:]
        order = state.get("applied_order")
        applied = state.get("applied")
        if isinstance(order, list) and isinstance(applied, dict) and len(order) > LEDGER_MAX_APPLIED:
            state["applied_order"] = order[-LEDGER_MAX_APPLIED:]
            state["applied"] = {k: applied[k] for k in state["applied_order"] if k in applied}
        return state

    def _validate(self):
        state = self.s
        cash = finite_number(state.get("cash"))
        baseline = finite_number(state.get("day_start"))
        realized = finite_number(state.get("realized_pnl"))
        if cash is None or cash < -1e-9 or baseline is None or baseline <= 0 or realized is None:
            raise LedgerError("ledger cash or day baseline is not finite")
        if not isinstance(state.get("day"), str):
            raise LedgerError("ledger day is missing")
        positions = state.get("positions")
        if not isinstance(positions, dict):
            raise LedgerError("ledger positions are not an object")
        for mint, pos in positions.items():
            if not mint or not isinstance(pos, dict):
                raise LedgerError("ledger position is malformed")
            for key in ("qty", "entry", "last", "cost", "opened"):
                if finite_positive(pos.get(key)) is None and not (
                    key == "opened" and finite_number(pos.get(key)) is not None
                ):
                    raise LedgerError(f"position {mint} has a non-finite {key}")
            if finite_positive(pos.get("qty")) is None or finite_positive(pos.get("entry")) is None:
                raise LedgerError(f"position {mint} is not reconcilable")
            if finite_positive(pos.get("last")) is None or finite_positive(pos.get("cost")) is None:
                raise LedgerError(f"position {mint} is not reconcilable")
            if not isinstance(pos.get("half_taken"), bool):
                raise LedgerError(f"position {mint} half_taken is not boolean")
            proceeds = pos.get("proceeds", 0.0)
            if finite_number(proceeds) is None or finite_number(proceeds) < 0:
                raise LedgerError(f"position {mint} proceeds are not finite")
        closed = state.get("closed")
        if not isinstance(closed, list) or len(closed) > LEDGER_MAX_CLOSED:
            raise LedgerError("closed-trade list is missing or over the cap")
        if len(state.get("applied_order", [])) > LEDGER_MAX_APPLIED:
            raise LedgerError("idempotency list is over the cap")

    def _save(self):
        self._validate()
        payload = json.dumps(self.s, indent=1, allow_nan=False)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def _remember(self, key: str, result: dict):
        if not key:
            return
        applied = self.s["applied"]
        order = self.s["applied_order"]
        if key not in applied:
            order.append(key)
        applied[key] = result
        while len(order) > LEDGER_MAX_APPLIED:
            dropped = order.pop(0)
            applied.pop(dropped, None)

    def _roll_day(self, now=None):
        today = _day(now).isoformat()
        if self.s["day"] == today:
            return
        equity = self.equity()
        if finite_number(equity) is None:
            raise LedgerError("refusing to roll the day on a non-finite equity")
        self.s["day"] = today
        self.s["day_start"] = equity

    # ---------- reads ----------
    def equity(self, marks: dict | None = None) -> float:
        marks = marks or {}
        held = 0.0
        for mint, pos in self.s["positions"].items():
            mark = marks.get(mint, pos["last"])
            price = finite_positive(mark)
            qty = finite_positive(pos.get("qty"))
            if price is None or qty is None:
                raise LedgerError("equity mark is not finite")
            held += qty * price
        cash = finite_number(self.s["cash"])
        if cash is None:
            raise LedgerError("cash is not finite")
        return round(cash + held, 2)

    def day_pnl(self) -> float:
        return round(self.equity() - self.s["day_start"], 2)

    def open_count(self) -> int:
        return len(self.s["positions"])

    def account_view(self):
        from desk.risk import Account
        return Account(self.equity(), self.day_pnl(), self.open_count(), self.s["day_start"])

    def reconcile(self) -> dict:
        equity = self.equity()
        baseline = self.s["day_start"]
        pnl = round(equity - baseline, 2)
        return {
            "cash": self.s["cash"],
            "equity": equity,
            "day": self.s["day"],
            "day_start": baseline,
            "day_pnl": pnl,
            "realized_pnl": self.s["realized_pnl"],
            "open_positions": self.open_count(),
            "finite": True,
            "day_pnl_matches_baseline": pnl == self.day_pnl(),
        }

    # ---------- orders ----------
    def buy(self, mint: str, symbol: str, ticket_usd: float, price: float,
            now=None, key: str | None = None) -> dict:
        with self._exclusive():
            self._reload()
            if key and key in self.s["applied"]:
                return dict(self.s["applied"][key])
            self._roll_day(now)
            if not mint or not symbol:
                raise LedgerError("mint and symbol are required")
            px = finite_positive(price)
            ticket = finite_positive(ticket_usd)
            if px is None or ticket is None:
                raise LedgerError("invalid price or ticket")
            if mint in self.s["positions"]:
                raise LedgerError("position already open")
            if ticket > self.s["cash"] + 1e-9:
                raise LedgerError("insufficient cash")
            fill = px * (1 + PAPER["slippage_pct"])
            fee = ticket * PAPER["fee_pct"]
            qty = (ticket - fee) / fill
            if finite_positive(qty) is None or finite_positive(fill) is None:
                raise LedgerError("fill is not finite")
            self.s["cash"] = self.s["cash"] - ticket
            self.s["positions"][mint] = {
                "symbol": str(symbol), "qty": qty, "entry": fill, "last": fill,
                "opened": _ts(now), "cost": ticket, "half_taken": False, "proceeds": 0.0,
            }
            result = {
                "side": "buy", "mint": mint, "qty": qty, "fill": fill, "fee": fee,
                "fill_guaranteed": False, "slippage_pct": PAPER["slippage_pct"],
                "fee_pct": PAPER["fee_pct"],
            }
            self._remember(key, result)
            self._save()
            return dict(result)

    def sell(self, mint: str, fraction: float, price: float, reason: str,
             now=None, key: str | None = None) -> dict:
        with self._exclusive():
            self._reload()
            if key and key in self.s["applied"]:
                return dict(self.s["applied"][key])
            self._roll_day(now)
            return self._sell_unlocked(mint, fraction, price, reason, now=now, key=key)

    def _sell_unlocked(self, mint, fraction, price, reason, now=None, key=None) -> dict:
        if key and key in self.s["applied"]:
            return dict(self.s["applied"][key])
        pos = self.s["positions"].get(mint)
        if pos is None:
            raise LedgerError("no position")
        px = finite_positive(price)
        part = finite_positive(fraction)
        if px is None or part is None or part > 1:
            raise LedgerError("invalid sell price or fraction")
        qty = pos["qty"] * part
        fill = px * (1 - PAPER["slippage_pct"])
        gross = qty * fill
        fee = gross * PAPER["fee_pct"]
        if finite_number(fill) is None or finite_number(gross) is None or finite_number(fee) is None:
            raise LedgerError("sell fill is not finite")
        self.s["cash"] = self.s["cash"] + gross - fee
        pos["qty"] = pos["qty"] - qty
        pos["proceeds"] = pos.get("proceeds", 0.0) + gross - fee
        pos["last"] = px
        out = {
            "side": "sell", "mint": mint, "symbol": pos["symbol"], "qty": qty,
            "fill": fill, "fee": fee, "reason": reason, "fill_guaranteed": False,
            "slippage_pct": PAPER["slippage_pct"], "fee_pct": PAPER["fee_pct"],
            "status": "filled",
        }
        if part >= 1 or pos["qty"] <= 1e-12:
            pnl = round(pos["proceeds"] - pos["cost"], 2)
            out["pnl_usd"] = pnl
            self.s["realized_pnl"] = round(self.s["realized_pnl"] + pnl, 2)
            self.s["closed"].append({
                "mint": mint, "symbol": pos["symbol"], "pnl_usd": pnl,
                "reason": reason, "closed": _ts(now),
            })
            if len(self.s["closed"]) > LEDGER_MAX_CLOSED:
                self.s["closed"] = self.s["closed"][-LEDGER_MAX_CLOSED:]
            del self.s["positions"][mint]
        else:
            # The partial flag lands only with the fill that reduced the quantity.
            pos["half_taken"] = True
        self._remember(key, out)
        self._save()
        return dict(out)

    def exit_signal(self, mint: str, quote, now=None) -> tuple[float, str] | None:
        """Pure read. Does not write half_taken, cash, or the ledger."""
        pos = self.s["positions"].get(mint)
        if pos is None or quote_price(quote) is None:
            return None
        return self._plan(pos, quote, now)

    def _plan(self, pos, quote, now) -> tuple[float, str] | None:
        price = quote_price(quote)
        if price is None:
            return None
        held_min = (_ts(now) - pos["opened"]) / 60
        if price <= pos["entry"] * (1 - EXITS["stop_pct"]):
            return (1.0, f"stop -{int(EXITS['stop_pct'] * 100)}%")
        if not pos["half_taken"] and price >= pos["entry"] * EXITS["take_half_at"]:
            return (0.5, f"take half at {EXITS['take_half_at']}x")
        windows = quote_volume(quote)
        if held_min >= EXITS["volume_decay_min_minutes"] and windows is not None:
            h6, h24 = windows
            ratio = h6 / (h24 / 4)
            if ratio < EXITS["volume_decay_ratio"]:
                return (1.0, f"volume decay {ratio:.2f}")
        if held_min >= EXITS["max_hold_hours"] * 60:
            return (1.0, "max hold time")
        return None

    def on_quote(self, mint: str, quote, now=None, *, execute: bool = True) -> dict:
        """Mark and maybe exit. Unknown quotes do not sell and do not move last."""
        with self._exclusive():
            self._reload()
            before_day = self.s["day"]
            self._roll_day(now)
            pos = self.s["positions"].get(mint)
            if pos is None:
                if self.s["day"] != before_day:
                    self._save()
                return {"status": "gone", "mint": mint}
            price = quote_price(quote)
            if price is None:
                if self.s["day"] != before_day:
                    self._save()
                return {"status": "unknown", "mint": mint, "reason": "invalid_quote",
                        "fill_guaranteed": False}
            plan = self._plan(pos, quote, now)
            if not execute:
                if plan is None:
                    return {"status": "hold", "mint": mint}
                return {"status": "planned", "mint": mint, "fraction": plan[0],
                        "reason": plan[1], "fill_guaranteed": False}
            if plan is None:
                pos["last"] = price
                self._save()
                return {"status": "hold", "mint": mint}
            fraction, reason = plan
            key = f"{mint}:{pos['opened']}:{reason}"
            if key in self.s["applied"]:
                return dict(self.s["applied"][key])
            pos["last"] = price
            return self._sell_unlocked(mint, fraction, price, reason, now=now, key=key)
