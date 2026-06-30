#!/usr/bin/env python3
"""
congress_watch.py — alert on new U.S. HOUSE Periodic Transaction Reports (PTRs,
STOCK Act stock trades) for a watchlist of members, pushed to Telegram.

Separate data source from the EDGAR watcher: the official House Clerk bulk
disclosure feed. Detection is stdlib; the per-trade detail is parsed from the
filing PDF with `pdftotext -raw` (poppler).  Senate is NOT covered (different,
harder source — efdsearch.senate.gov).

Members are matched on LAST NAME + STATE (robust vs. formal/nickname mismatches).

Usage:
  python congress_watch.py            # detect new PTRs, alert, update state
  python congress_watch.py --seed     # mark all current PTRs seen, send nothing
  python congress_watch.py --demo     # re-send each member's latest PTR (no state write)
  python congress_watch.py --dry-run  # detect + print, send nothing, save nothing
  python congress_watch.py --test     # send a one-off test message

Env (GitHub Actions secrets): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (reused via watch.py)
Requires: poppler-utils (pdftotext) on PATH.
"""

import csv
import datetime
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

from watch import send_telegram, esc, money  # reuse Telegram pipe + helpers

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "congress.json")
STATE = os.path.join(HERE, "congress_state.json")

UA = "sec-filing-alerts mrtsfn601 maratsafin601@gmail.com"
INDEX_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP"
PTR_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{docid}.pdf"

OWNER = {"SP": "spouse", "JT": "joint", "DC": "dep.child", "": "self"}
VERB = {"P": ("🟢", "BUY"), "S": ("🔴", "SELL"), "S (partial)": ("🔴", "SELL(part)"), "E": ("🔁", "EXCH")}


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


def http_bytes(url, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + attempt)
    raise RuntimeError(f"GET failed: {url} ({last})")


def index_rows(year):
    """Return PTR index rows for a year: dicts with last/first/type/state/date/docid."""
    try:
        raw = http_bytes(INDEX_ZIP.format(year=year))
    except Exception:  # noqa: BLE001
        return []
    z = zipfile.ZipFile(io.BytesIO(raw))
    txt = next((n for n in z.namelist() if n.lower().endswith(".txt")), None)
    if not txt:
        return []
    lines = z.read(txt).decode("utf-8", "replace").splitlines()
    rows = []
    for r in csv.reader(lines[1:], delimiter="\t"):
        if len(r) < 9:
            continue
        rows.append({"last": r[1], "first": r[2], "type": r[4],
                     "state": r[5], "year": r[6], "date": r[7], "docid": r[8]})
    return rows


def pdf_text(year, docid):
    raw = http_bytes(PTR_PDF.format(year=year, docid=docid))
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(raw)
        path = f.name
    try:
        out = subprocess.run(["pdftotext", "-raw", path, "-"],
                             capture_output=True, text=True, timeout=60)
        return out.stdout
    finally:
        os.unlink(path)


_SPINE = re.compile(
    r"\(([A-Z0-9.\-]{1,6})\)\s*\[([A-Za-z]{1,3})\]\s*"       # (TICKER) [CODE]
    r"(S \(partial\)|P|S|E)\s+"                                # transaction type
    r"(\d{2}/\d{2}/\d{4})\s+\d{2}/\d{2}/\d{4}\s+"              # txn date, notification date
    r"\$([\d,]+(?:\.\d+)?)(?:\s*-\s*\$([\d,]+(?:\.\d+)?)|\s*(\+))?")  # range | single | open-ended


def parse_ptr(text):
    """Parse a House PTR -> list of transaction dicts (best-effort)."""
    t = re.sub(r"[ \t]+", " ", text.replace("\n", " "))
    out = []
    for m in _SPINE.finditer(t):
        ticker, code, typ, txn, low, high, plus = m.groups()
        pre = t[max(0, m.start() - 120):m.start()]
        mo = re.search(r"(SP|JT|DC)\s+[^()]*$", pre)
        owner = mo.group(1) if mo else ""
        post = t[m.end():m.end() + 300]
        md = re.search(r"D\s*:\s*(.+?)(?:\s+(?:SP|JT|DC)\s|\* For the complete|Filing ID|$)", post)
        desc = (md.group(1).strip() if md else "")
        out.append({"ticker": ticker, "code": code, "type": typ, "txn": txn,
                    "low": low, "high": high, "plus": plus, "owner": owner, "desc": desc})
    return out


def _amt(s):
    return int(float(s.replace(",", ""))) if s else 0


def band(low, high, plus=None):
    lo = _amt(low)
    if high:
        return f"{money(lo)}–{money(_amt(high))}"
    if plus:
        return f"{money(lo)}+"
    return money(lo)


def build_message(member, row, txns):
    pdfurl = PTR_PDF.format(year=row["year"], docid=row["docid"])
    head = f"🏛️ <b>{esc(member['name'])}</b> ({esc(member['party'])}-{esc(row['state'])}) — new PTR"
    lines = [head, f"Filed {row['date']}"]
    if not txns:
        lines.append("(could not parse transactions — see filing)")
    for x in txns:
        emoji, verb = VERB.get(x["type"], ("•", x["type"]))
        code = "" if x["code"].upper() == "ST" else f" [{esc(x['code'])}]"
        owner = OWNER.get(x["owner"], x["owner"])
        mmdd = x["txn"][:5]
        lines.append(f"• {emoji} {verb} <b>{esc(x['ticker'])}</b>{code} — {band(x['low'], x['high'], x.get('plus'))} · {owner} · txn {mmdd}")
        if x["desc"] and ("option" in x["desc"].lower() or len(txns) <= 6):
            d = x["desc"][:120]
            lines.append(f"   ↳ {esc(d)}")
    lines += ["", f'<a href="{pdfurl}">Filing PDF</a>']
    return "\n".join(lines)


def _datekey(r):
    try:
        return (int(r["year"]), datetime.datetime.strptime(r["date"], "%m/%d/%Y"))
    except Exception:  # noqa: BLE001
        return (int(r["year"] or 0), datetime.datetime.min)


def member_key(m):
    return f"{m['last'].lower()}|{m['state'].upper()}"


def matches(row, m):
    return (row["type"] == "P"
            and row["last"].lower() == m["last"].lower()
            and row["state"][:2].upper() == m["state"].upper())


def main():
    args = set(sys.argv[1:])
    if "--test" in args:
        send_telegram("✅ <b>congress-watch</b> test — House PTR alerts wired up.")
        return
    mode = "seed" if "--seed" in args else ("demo" if "--demo" in args else
           ("dry" if "--dry-run" in args else "normal"))

    members = load_json(WATCHLIST, [])
    demo_filter = os.environ.get("CONGRESS_MEMBER", "").strip().lower()
    if mode == "demo" and demo_filter:
        members = [m for m in members
                   if demo_filter in m["name"].lower() or demo_filter in m["last"].lower()]
    state = load_json(STATE, {})
    year = datetime.date.today().year
    rows = index_rows(year) + index_rows(year - 1)  # cover Jan boundary

    changed = False
    for m in members:
        key = member_key(m)
        st = state.setdefault(key, {"name": m["name"], "seen": [], "last_filed": None})
        ptrs = [r for r in rows if matches(r, m)]
        ptrs.sort(key=_datekey)  # oldest first (chronological)

        if mode == "seed":
            st["seen"] = sorted({r["docid"] for r in ptrs})
            if ptrs:
                st["last_filed"] = ptrs[-1]["date"]
            print(f"[seed] {m['name']}: {len(ptrs)} PTRs seen")
            changed = True
            continue

        if mode == "demo":
            if ptrs:
                r = ptrs[-1]
                try:
                    txns = parse_ptr(pdf_text(r["year"], r["docid"]))
                except Exception as e:  # noqa: BLE001
                    txns = []
                send_telegram(build_message(m, r, txns))
                print(f"[demo] {m['name']}: sent {r['docid']} ({len(txns)} txns)")
            else:
                print(f"[demo] {m['name']}: no PTRs found")
            continue

        seen = set(st["seen"])
        new = [r for r in ptrs if r["docid"] not in seen]
        for r in new:
            try:
                txns = parse_ptr(pdf_text(r["year"], r["docid"]))
            except Exception as e:  # noqa: BLE001
                txns = []
            send_telegram(build_message(m, r, txns), dry=(mode == "dry"))
            seen.add(r["docid"])
            changed = True
        if new and mode != "dry":
            st["seen"] = sorted(seen)
            st["last_filed"] = ptrs[-1]["date"]
        if not new:
            print(f"[ok] {m['name']}: no new PTRs")

    if mode == "seed" or (changed and mode == "normal"):
        save_json(STATE, state)
        print("congress_state.json updated")
    print("STATE_CHANGED=" + ("1" if (mode == "seed" or changed) else "0"))


if __name__ == "__main__":
    main()
