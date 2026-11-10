"""Seat 4: the risk engine. Plain Python. No model touches this file. Ever.

It decides whether a trade happens and how big it is. It cannot be argued with,
which is the entire point.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from config import EXITS, MIN_SCORE, PAPER, RISK, STOP_FILL_GUARANTEED

from desk.num import finite_number, finite_positive

# Sizing input only. A stop trigger is not a fill, and a gap is not capped at 1%.
DISCLOSURE = (
    "max_risk_per_trade sizes the ticket as equity * 1% / stop. It is not a guaranteed "
    "maximum loss. The stop is a trigger, not a fill. Fees are 1% and slippage is 2% "
    "on entry and on exit. A gap can lose more than 1% of equity. Missing creator history "
    "since migration and missing bundle proofs reject the entry; top-10 concentration "
    "does not replace them."
)


@dataclass
class Account:
    equity: float
    day_pnl: float
    open_positions: int
    day_start: float | None = None


def in_quiet_hours(now: datetime | None = None) -> bool:
    start, end = RISK["quiet_hours"]
    h = (now or datetime.now()).hour
    return start <= h < end if start < end else (h >= start or h < end)


def day_baseline(account: Account) -> float | None:
    raw = account.equity if account.day_start is None else account.day_start
    number = finite_number(raw)
    if number is None or number <= 0:
        return None
    return number


def account_block(account: Account, now: datetime | None = None) -> str | None:
    """Daily baseline and quiet hours. No thesis, no network."""
    if finite_number(account.equity) is None or account.equity < 0:
        return "unknown account"
    if finite_number(account.day_pnl) is None:
        return "unknown account"
    baseline = day_baseline(account)
    if baseline is None:
        return "unknown day baseline"
    # Compare with the stored day baseline, not with the equity after the loss.
    if account.day_pnl <= -RISK["max_daily_loss_pct"] * baseline:
        return "daily loss limit hit, desk halted until tomorrow"
    if in_quiet_hours(now):
        return "quiet hours, no new entries"
    return None


def size_ticket(account: Account, liquidity_usd: float) -> tuple[bool, float, str]:
    if account.open_positions >= RISK["max_open_positions"]:
        return False, 0.0, "max open positions"
    liquidity = finite_number(liquidity_usd)
    equity = finite_positive(account.equity)
    if liquidity is None or liquidity < 0 or equity is None:
        return False, 0.0, "unknown liquidity"
    by_risk = RISK["max_risk_per_trade"] * equity / EXITS["stop_pct"]
    by_size = RISK["max_position_pct"] * equity
    by_pool = RISK["max_pool_share"] * liquidity
    ticket = round(min(by_risk, by_size, by_pool), 2)
    if not finite_number(ticket) or ticket < RISK["min_ticket_usd"]:
        return False, 0.0, f"ticket ${ticket} below fee floor ${RISK['min_ticket_usd']}"
    return True, ticket, "approved"


def structural_block(account: Account, liquidity_usd: float,
                     now: datetime | None = None) -> str | None:
    """Gates that do not need an analyst thesis."""
    blocked = account_block(account, now)
    if blocked:
        return blocked
    ok, _ticket, reason = size_ticket(account, liquidity_usd)
    if not ok:
        return reason
    return None


def check_token_trade(thesis, account: Account, liquidity_usd: float,
                      now: datetime | None = None) -> tuple[bool, float, str]:
    """Return (approved, ticket_usd, reason)."""
    blocked = account_block(account, now)
    if blocked:
        return False, 0.0, blocked
    if thesis.verdict != "valid_setup":
        return False, 0.0, f"analyst verdict: {thesis.verdict}"
    if thesis.score < MIN_SCORE:
        return False, 0.0, f"score {thesis.score} < {MIN_SCORE}"
    return size_ticket(account, liquidity_usd)


def modeled_stop(ticket_usd: float) -> dict:
    """Loss if the quote prints exactly the stop trigger. Not a guarantee."""
    fee = PAPER["fee_pct"]
    slip = PAPER["slippage_pct"]
    stop = EXITS["stop_pct"]
    proceeds_frac = (1 - fee) * (1 - stop) * (1 - slip) * (1 - fee)
    loss_frac = 1 - proceeds_frac
    return {
        "loss_frac_of_ticket_at_exact_trigger": loss_frac,
        "loss_usd_at_exact_trigger": round(ticket_usd * loss_frac, 2),
        "fill_guaranteed": STOP_FILL_GUARANTEED,
        "gap_unbounded": True,
        "fee_pct": fee,
        "slippage_pct": slip,
        "note": DISCLOSURE,
    }


# ---------- stocks (Alpaca paper) ----------
MAX_STOCK_POSITION_PCT = 0.05
ALLOWED_SYMBOLS = {"AAPL", "MSFT", "NVDA", "SPY", "QQQ"}


def check_stock_trade(thesis, account: Account, price: float) -> tuple[bool, int, str]:
    if account.day_pnl <= -RISK["max_daily_loss_pct"] * account.equity:
        return False, 0, "daily loss limit hit"
    if thesis.symbol.upper() not in ALLOWED_SYMBOLS:
        return False, 0, f"{thesis.symbol} not on the allowed list"
    if thesis.action.lower() == "hold":
        return False, 0, "model recommends no trade"
    if thesis.action.lower() == "sell":
        return False, 0, "long-only desk"
    if account.open_positions >= 5:
        return False, 0, "max open positions"
    if thesis.confidence.lower() != "high":   # enum casing is not guaranteed
        return False, 0, "only high-confidence theses trade"
    if thesis.stop_loss >= price or thesis.stop_loss <= 0:
        return False, 0, "stop must sit below entry for a long"
    stop_distance = price - thesis.stop_loss
    qty = int(min(RISK["max_risk_per_trade"] * account.equity / stop_distance,
                  MAX_STOCK_POSITION_PCT * account.equity / price))
    if qty < 1:
        return False, 0, "position too small after sizing"
    return True, qty, "approved"
