"""Stocks mode is disabled in this offline build.

Alpaca is not imported and no order function remains. The sizing rule for a
thesis still lives in desk/risk.py and is covered by the offline tests. Do not
re-enable this seat in the current phase.
"""
from __future__ import annotations


def clients():
    raise RuntimeError("stocks mode is disabled in this offline build")


def execute(*_args, **_kwargs):
    raise RuntimeError("stocks mode is disabled in this offline build")


def main(argv=None) -> int:
    print("offline mode: stocks and Alpaca are disabled. No order is sent.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
