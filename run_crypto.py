"""Offline entry point.

Live scan, Telegram, the cloud analyst and the network loop are disabled.
The supported run is a fixture replay:

    python run_crypto.py --replay tests/fixtures/replay_reject.json
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from desk.replay import run_fixture


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--shadow", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.replay is None:
        print("offline mode: live scan, analyst, telegram and network are disabled. "
              "Pass --replay FIXTURE.json")
        return 2
    fixture = json.loads(args.replay.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        report = run_fixture(fixture, Path(tmp) / "ledger.json")
    print(json.dumps(report, indent=1, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
