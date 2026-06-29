#!/usr/bin/env python3
"""
Resolve a company/fund name (or ticker) to its 10-digit SEC CIK, so you never
hand-search EDGAR when adding an entity to watchlist.json.

Usage:
  python resolve_cik.py "Berkshire Hathaway"
  python resolve_cik.py NVDA
"""
import json
import re
import sys
import urllib.parse
import urllib.request

UA = "sec-filing-alerts mrtsfn601 maratsafin601@gmail.com"
TICKERS = "https://www.sec.gov/files/company_tickers.json"
# Full-text-ish name search via EDGAR's company search autocomplete.
NAME_SEARCH = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={q}&type=&dateb=&owner=include&count=40&output=atom"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def resolve(query):
    q = query.strip()
    results = []

    # 1) exact ticker match (fast path)
    try:
        data = json.loads(_get(TICKERS))
        for row in data.values():
            if row["ticker"].upper() == q.upper():
                results.append((str(row["cik_str"]).zfill(10), row["title"], row["ticker"]))
    except Exception:  # noqa: BLE001
        pass

    # 2) substring name match
    if not results:
        try:
            data = json.loads(_get(TICKERS))
            ql = q.lower()
            for row in data.values():
                if ql in row["title"].lower():
                    results.append((str(row["cik_str"]).zfill(10), row["title"], row["ticker"]))
        except Exception:  # noqa: BLE001
            pass

    # 3) EDGAR company browse (catches funds/advisers without tickers)
    if not results:
        try:
            atom = _get(NAME_SEARCH.format(q=urllib.parse.quote(q)))
        except Exception:  # noqa: BLE001
            atom = ""
        for m in re.finditer(r"<cik>(\d+)</cik>.*?<title>(.*?)</title>", atom, re.S):
            results.append((m.group(1).zfill(10), m.group(2).strip(), ""))

    return results[:20]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python resolve_cik.py \"<company or fund name>\" | <TICKER>")
        sys.exit(1)
    query = " ".join(sys.argv[1:])
    hits = resolve(query)
    if not hits:
        print(f"No CIK found for: {query}")
        sys.exit(2)
    print(f"Matches for {query!r}:")
    for cik, title, ticker in hits:
        print(f"  CIK {cik}  {title}" + (f"  [{ticker}]" if ticker else ""))
    print("\nAdd to watchlist.json, e.g.:")
    cik, title, _ = hits[0]
    print(json.dumps({"name": title, "cik": cik, "forms": ["13F-HR", "13F-HR/A"]}, indent=2))
