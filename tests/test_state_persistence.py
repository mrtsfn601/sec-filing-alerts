#!/usr/bin/env python3
"""Tests that a failed Telegram send does not discard already-sent alerts.

Run: python3 test_state_persistence.py

send_telegram re-raises on HTTP errors. The watchers used to record `seen`
only after the whole batch finished, so a transient 429/5xx part-way through
threw away every delivery in that batch and re-alerted them on the next run.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watchers import edgar


def _filings(n):
    # EDGAR returns newest-first; process_entity walks it in reverse.
    return [{"accession": f"acc-{i}", "form": "8-K", "filed": "2026-08-20",
             "period": "", "primaryDoc": "", "desc": ""} for i in reversed(range(n))]


def _run(n, fail_after=None, mode="normal"):
    """Drive process_entity with a stubbed feed; return (sent, seen)."""
    sent = []

    def flaky(msg, dry=False):
        if fail_after is not None and len(sent) == fail_after:
            raise RuntimeError("HTTP Error 429: Too Many Requests")
        sent.append(msg)

    orig = (edgar.recent_filings, edgar.send_telegram, edgar.build_generic_message)
    edgar.recent_filings = lambda cik: ("Test Co", _filings(n))
    edgar.send_telegram = flaky
    edgar.build_generic_message = lambda *a, **k: "msg"
    state = {}
    try:
        edgar.process_entity({"name": "Test Co", "cik": "0000000001", "forms": ["*"]},
                             state, mode)
    except RuntimeError:
        pass
    finally:
        edgar.recent_filings, edgar.send_telegram, edgar.build_generic_message = orig
    return sent, state.get("0000000001", {}).get("seen", [])


def main():
    failures = []

    def check(label, cond, detail=""):
        if not cond:
            failures.append(f"  {label}: {detail}")

    # 1. happy path — everything sent is recorded
    sent, seen = _run(4)
    check("full batch sent", len(sent) == 4, f"sent {len(sent)}")
    check("full batch recorded", len(seen) == 4, f"seen {seen}")

    # 2. THE BUG: fail on the 3rd send; the first two must stay recorded
    sent, seen = _run(4, fail_after=2)
    check("partial batch sent", len(sent) == 2, f"sent {len(sent)}")
    check("partial batch recorded", len(seen) == 2,
          f"seen {seen} — deliveries discarded, they will re-alert")
    check("recorded the ones actually sent", set(seen) == {"acc-0", "acc-1"}, f"seen {seen}")

    # 3. failure on the very first send records nothing (nothing was delivered)
    sent, seen = _run(4, fail_after=0)
    check("no send, no record", len(sent) == 0 and len(seen) == 0, f"sent {sent} seen {seen}")

    # 4. dry-run must never write state
    sent, seen = _run(4, mode="dry")
    check("dry-run records nothing", len(seen) == 0, f"seen {seen}")

    if failures:
        print(f"FAILED {len(failures)}")
        print("\n".join(failures))
        return 1
    print("ok — 4 scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
