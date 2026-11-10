"""Journal, bench, alerts, kill switch. The boring seats that keep the desk honest."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from config import BENCH_MINUTES, DEFAULT_BENCH, JOURNAL_MAX_LINES

JOURNAL = Path("journal.jsonl")
KILL_FILE = Path("KILL")


def _stamp(now) -> str:
    if now is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.isoformat()
    return str(now)


class Journal:
    """Append-only log with a hard line cap. The oldest lines are dropped."""

    def __init__(self, path: Path | str = JOURNAL, max_lines: int = JOURNAL_MAX_LINES):
        self.path = Path(path)
        self.max_lines = max_lines

    def write(self, kind: str, now=None, **fields) -> dict:
        row = {"time": _stamp(now), "kind": kind, **fields}
        line = json.dumps(row, default=str, allow_nan=False)
        existing = []
        if self.path.exists():
            existing = [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        existing.append(line)
        if len(existing) > self.max_lines:
            existing = existing[-self.max_lines:]
        payload = "\n".join(existing) + "\n"
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)
        return row


# ---------- journal: every decision, including every rejection ----------
def journal(kind: str, **fields) -> dict:
    return Journal().write(kind, **fields)


# ---------- bench: a rejected token sits out, for as long as its failure deserves ----------
class Bench:
    def __init__(self, path: str = "desk.db"):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS bench(mint TEXT PRIMARY KEY, reason TEXT, until REAL)")
        self.db.commit()

    def _clock(self, now) -> float:
        if now is None:
            return time.time()
        if isinstance(now, datetime):
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            return now.timestamp()
        return float(now)

    def benched(self, mint: str, now=None) -> bool:
        r = self.db.execute("SELECT until FROM bench WHERE mint=?", (mint,)).fetchone()
        return bool(r and r[0] > self._clock(now))

    def sit(self, mint: str, reason: str, now=None):
        mins = BENCH_MINUTES.get(reason, DEFAULT_BENCH)
        clock = self._clock(now)
        self.db.execute("DELETE FROM bench WHERE until<=?", (clock,))
        self.db.execute("INSERT OR REPLACE INTO bench VALUES (?,?,?)",
                        (mint, reason, clock + mins * 60))
        self.db.commit()


# ---------- alerts: Telegram if configured, stdout otherwise ----------
def alert(text: str):
    """Local stdout only. Telegram is disabled in this offline build."""
    print(text)


# ---------- kill switch: create a file named KILL and the desk stops ----------
def killed() -> bool:
    return KILL_FILE.exists()


def load_env(path: str = ".env"):
    """Tiny .env loader so the desk has no extra dependency. Real env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
