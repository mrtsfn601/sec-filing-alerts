#!/usr/bin/env python3
"""Tests for watch.form_matches — the watchlist `forms` filter.

Run: python3 test_form_matches.py

Guards the substring-matching bug: an entry of "4" used to match "S-4",
"424B3", "DEF 14A" and "144", and "3" matched "S-3" and "13F-HR", so adding
an operating-company CIK to watchlist.json alerted on its whole filing feed.
"""

import sys

from watch import form_matches

# (wanted, form, should_match)
CASES = [
    # --- wildcard / empty ------------------------------------------------
    (["*"], "S-4", True),
    (["*"], "8-K", True),
    ([], "8-K", True),

    # --- insider forms: exact, plus the /A amendment ---------------------
    (["4"], "4", True),
    (["4"], "4/A", True),
    (["3"], "3", True),
    (["3"], "3/A", True),
    (["5"], "5/A", True),

    # --- insider forms must NOT swallow the rest of the feed -------------
    (["4"], "S-4", False),
    (["4"], "F-4", False),
    (["4"], "424B3", False),
    (["4"], "424B5", False),
    (["4"], "DEF 14A", False),
    (["4"], "SC 14D9", False),
    (["4"], "144", False),
    (["3"], "S-3", False),
    (["3"], "S-3/A", False),
    (["3"], "S-3ASR", False),
    (["3"], "13F-HR", False),
    (["3"], "SC 13E3", False),
    (["3"], "424B3", False),

    # --- 13F: bare root matches the -HR / -NT variants -------------------
    (["13F"], "13F-HR", True),
    (["13F"], "13F-HR/A", True),
    (["13F"], "13F-NT", True),
    (["13F-HR"], "13F-HR", True),
    (["13F-HR"], "13F-HR/A", True),
    (["13F"], "8-K", False),
    # 13F confidential-treatment requests (Pershing Square files these).
    # Alphabetic suffix, so the family root still matches.
    (["13F"], "13FCONP", True),
    (["13F"], "13FCONP/A", True),
    (["13F"], "13FCONNT", True),
    # ...but a digit after the root is a different form number, not a variant.
    (["4"], "4XYZ", True),
    (["14"], "144", False),
    (["42"], "424B3", False),

    # --- 13D / 13G across EDGAR's prefix spellings -----------------------
    (["13D"], "13D", True),
    (["13D"], "SC 13D", True),
    (["13D"], "SC 13D/A", True),
    (["13D"], "SCHEDULE 13D", True),
    (["13G"], "SCHEDULE 13G", True),
    (["13G"], "SC 13G/A", True),
    (["13D"], "SC 13G", False),
    (["13G"], "SC 13E3", False),

    # --- a full form string is still a valid entry -----------------------
    (["SCHEDULE 13G"], "SCHEDULE 13G", True),
    (["SCHEDULE 13G"], "SCHEDULE 13G/A", True),

    # --- case / whitespace tolerance -------------------------------------
    (["13d"], "SC 13D", True),
    ([" 4 "], "4", True),

    # --- multi-entry lists behave as OR ----------------------------------
    (["13D", "13G", "4", "3"], "4/A", True),
    (["13D", "13G", "4", "3"], "SC 13G", True),
    (["13D", "13G", "4", "3"], "424B3", False),
    (["13D", "13G", "4", "3"], "S-4", False),
    (["13D", "13G", "4", "3"], "8-K", False),
]


def main():
    failed = []
    for wanted, form, expected in CASES:
        got = form_matches(form, wanted)
        if got != expected:
            failed.append(f"  form_matches({form!r}, {wanted!r}) -> {got}, want {expected}")
    if failed:
        print(f"FAILED {len(failed)}/{len(CASES)}")
        print("\n".join(failed))
        return 1
    print(f"ok — {len(CASES)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
