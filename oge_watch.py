#!/usr/bin/env python3
"""
oge_watch.py — alert on new Executive-Branch OGE Form 278-T Periodic
Transaction Reports (STOCK Act securities trades) published on
whitehouse.gov/disclosures, pushed to Telegram.

Primary target: President Donald J. Trump's PTRs — the high-volume trading
stream (municipal bonds + equities like the widely-reported Lockheed Martin /
defense buys) that is NOT in SEC EDGAR. Generalizable to any executive-branch
filer via oge.json (match by the name substring the White House uses in the
PDF filename, e.g. "Bessent").

Design notes
------------
* Detection is ROBUST: the White House index is diffed by PDF URL, so a newly
  posted report fires exactly once.
* Parsing is BEST-EFFORT: the 278-T PDFs are *scanned* (poor OCR — "purchase"
  renders as "ourehase", "$1,000,001" as "S1 ooo001"), and the description and
  date/amount columns are flattened into separate blocks by `pdftotext -raw`.
  So the alert is a SUMMARY (transaction count, buy/sell split, aggregate
  $-range, notable non-bond assets) with the authoritative PDF linked — not a
  fragile per-line reconstruction. Message text is generated deterministically
  in Python (no LLM).

Usage:
  python oge_watch.py            # detect new PTRs, alert, update state
  python oge_watch.py --seed     # mark all current PTRs seen, send nothing
  python oge_watch.py --demo     # re-send each filer's latest PTR (no state write)
  python oge_watch.py --dry-run  # detect + print, send nothing, save nothing
  python oge_watch.py --test     # send a one-off test message

Env (GitHub Actions secrets): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (via watch.py)
Requires: poppler-utils (pdftotext) on PATH.
"""

import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

from watch import send_telegram, esc, money  # reuse Telegram pipe + helpers

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "oge.json")
STATE = os.path.join(HERE, "oge_state.json")

INDEX_URL = "https://www.whitehouse.gov/disclosures/"
# A browser-ish UA; the White House CDN is fine with this (unlike SEC's WAF).
UA = "Mozilla/5.0 (sec-filing-alerts; maratsafin601@gmail.com)"

# Only treat a PDF as a Periodic Transaction Report (278-T), not an annual 278e.
PTR_HINT = re.compile(r"periodic|transaction|278.?t", re.I)
# Bond / fund markers — a description WITHOUT these is a candidate equity.
BOND = re.compile(r"DUE|B/E|\bREV\b|SCH\s?D|CNTY|%|MTG|\bSER\b|OBLIG|HSG|AUTH|"
                  r"\bGAS\b|UTIL|PWR|BD\b|NOTE|MUNI|MUN\b|CTF|PREPAY|TAX", re.I)


def load_json(path, default):
    import json
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    import json
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


def http_bytes(url, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + attempt)
    raise RuntimeError(f"GET failed: {url} ({last})")


# ----------------------------- index -----------------------------

def index_pdfs():
    """All PTR PDFs on the White House disclosures page: [{url, filename, text}]."""
    html = http_bytes(INDEX_URL).decode("utf-8", "replace")
    out, seen = [], set()
    for href, txt in re.findall(r'<a[^>]+href="([^"]+\.pdf)"[^>]*>(.*?)</a>', html, re.S | re.I):
        if href in seen:
            continue
        seen.add(href)
        fn = href.rsplit("/", 1)[-1]
        label = re.sub(r"<[^>]+>", "", txt).strip()
        if PTR_HINT.search(fn) or PTR_HINT.search(label):
            out.append({"url": href, "filename": fn, "text": label})
    return out


def _filename_date(fn):
    """Best-effort 'M.D.YY(YY)' in a WH filename -> ISO. Returns '' on failure."""
    m = re.findall(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", fn)
    if not m:
        return ""
    mm, dd, yy = m[-1]  # trailing date token is the filing date
    try:
        mm, dd = int(mm), int(dd)
        yy = int(yy)
        yy = yy + 2000 if yy < 100 else yy
        if 1 <= mm <= 12 and 1 <= dd <= 31 and 2020 <= yy <= 2100:
            return f"{yy:04d}-{mm:02d}-{dd:02d}"
    except ValueError:
        pass
    return ""


# ----------------------------- PDF parse -----------------------------

def pdf_text(url):
    raw = http_bytes(url)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(raw)
        path = f.name
    try:
        out = subprocess.run(["pdftotext", "-raw", path, "-"],
                             capture_output=True, text=True, timeout=180)
        return out.stdout
    finally:
        os.unlink(path)


def _ttype(word):
    """OCR-tolerant transaction-type classifier from the trailing word."""
    s = word.lower()
    if re.search(r"sal|s0l", s):
        return "SELL"
    if re.search(r"[eo]xch", s):
        return "EXCH"
    if re.search(r"rehas|rchas|urcha|urehas|p[uo]r[ce]h", s):
        return "BUY"
    return None


def _norm_amt(s):
    return (s.replace("S", "$").replace("o", "0").replace("O", "0")
            .replace(" ", "").replace(".", ",").replace("•", "-").replace("·", "-"))


def parse_278t(text):
    """Return a summary dict parsed best-effort from scanned 278-T text."""
    # description rows end in an OCR'd type word; keep (desc, type)
    descs = []
    for ln in text.splitlines():
        ln = ln.strip()
        if len(ln) < 15 or ln.isdigit():
            continue
        parts = ln.split()
        ty = _ttype(parts[-1]) if parts else None
        if ty:
            desc = " ".join(parts[:-1]).strip(" -.")
            descs.append((desc, ty))

    counts = {"BUY": 0, "SELL": 0, "EXCH": 0}
    for _, ty in descs:
        counts[ty] += 1

    # dollar ranges (self-contained; robust to desc<->amount misalignment)
    lo_sum = hi_sum = 0
    n_amt = 0
    for ln in text.splitlines():
        if "$" not in ln and "S" not in ln:
            continue
        for lo, hi in re.findall(r"\$?([\d,]{3,})\s*-\s*\$?([\d,]{3,})", _norm_amt(ln)):
            lo = int(re.sub(r"\D", "", lo) or 0)
            hi = int(re.sub(r"\D", "", hi) or 0)
            if 1000 <= lo < hi <= 60_000_000:
                lo_sum += lo
                hi_sum += hi
                n_amt += 1

    # candidate non-bond assets (equities / the LMT-type signal)
    equities = []
    for desc, ty in descs:
        if not BOND.search(desc) and len(desc) > 6:
            equities.append((desc, ty))

    # authoritative filing date from the scan, if legible
    recv = re.search(r"OGE\s*RECEIVED\s*(\d{1,2})/(\d{1,2})/(\d{4})", text, re.I)
    received = ""
    if recv:
        mm, dd, yy = recv.groups()
        received = f"{int(yy):04d}-{int(mm):02d}-{int(dd):02d}"

    return {"n": len(descs), "counts": counts, "lo": lo_sum, "hi": hi_sum,
            "n_amt": n_amt, "equities": equities, "received": received}


# ----------------------------- message -----------------------------

def build_message(filer, item, summ):
    filed = summ.get("received") or _filename_date(item["filename"])
    amend = "Amendment" in item["filename"] or "/A" in item["text"]
    part = ""
    pm = re.search(r"-(\d)\.pdf$", item["filename"])
    if pm:
        part = f" · part {pm.group(1)}"

    head = f"\U0001F3DB️ <b>{esc(filer['name'])}</b> — new 278-T PTR"
    if amend:
        head += " (amendment)"
    lines = [head]
    sub = "OGE Periodic Transaction Report" + part
    if filed:
        sub = f"Filed {filed} · " + sub
    lines.append(sub)

    c = summ["counts"]
    if summ["n"]:
        split = []
        if c["BUY"]:
            split.append(f"\U0001F7E2 {c['BUY']} buy")
        if c["SELL"]:
            split.append(f"\U0001F534 {c['SELL']} sell")
        if c["EXCH"]:
            split.append(f"\U0001F501 {c['EXCH']} exch")
        lines += ["", f"\U0001F4CA {summ['n']} transactions · " + " · ".join(split)]
        if summ["n_amt"]:
            approx = "" if summ["n_amt"] >= summ["n"] else f" ({summ['n_amt']}/{summ['n']} legible)"
            lines.append(f"\U0001F4B5 Total value: {money(summ['lo'])}–{money(summ['hi'])}{approx}")
        eq = summ["equities"]
        if eq:
            lines += ["", f"\U0001F4C8 <b>Non-bond assets ({len(eq)})</b>"]
            for desc, ty in eq[:12]:
                tag = {"BUY": "\U0001F7E2", "SELL": "\U0001F534", "EXCH": "\U0001F501"}.get(ty, "•")
                lines.append(f"{tag} {esc(desc[:60])}")
            if len(eq) > 12:
                lines.append(f"…and {len(eq) - 12} more")
        else:
            lines.append("\U0001F3E6 All holdings appear to be bonds/funds")
        lines.append("<i>OCR-approx from scanned filing — see PDF for exact detail</i>")
    else:
        # image-only scan (no text layer) — common for the 2026 reports
        lines += ["", "\U0001F4C4 Scanned-image filing (no text layer) — "
                      "open the PDF for the full transaction list."]

    lines += ["", f'<a href="{item["url"]}">Filing ↗</a>']
    return "\n".join(lines)


# ----------------------------- main -----------------------------

def filer_key(f):
    return f["match"].lower()


def _sort_key(p):
    """Best-effort chronological key: filename date, else the /YYYY/MM/ upload
    path, else empty — so dated items always rank above undated ones."""
    d = _filename_date(p["filename"])
    if d:
        return d
    mo = re.search(r"/uploads/(\d{4})/(\d{2})/", p["url"])
    if mo:
        return f"{mo.group(1)}-{mo.group(2)}-00"
    return "0000-00-00"


def items_for(filer, pdfs):
    m = filer["match"].lower()
    got = [p for p in pdfs if m in p["filename"].lower() or m in p["text"].lower()]
    got.sort(key=_sort_key, reverse=True)  # newest first
    return got


def main():
    args = set(sys.argv[1:])
    if "--test" in args:
        send_telegram("✅ <b>oge-watch</b> test — Executive-Branch 278-T PTR alerts wired up.")
        return
    mode = ("seed" if "--seed" in args else "demo" if "--demo" in args else
            "dry" if "--dry-run" in args else "normal")

    filers = load_json(WATCHLIST, [])
    demo_filter = os.environ.get("OGE_FILER", "").strip().lower()
    if mode == "demo" and demo_filter:
        filers = [f for f in filers if demo_filter in f["name"].lower() or demo_filter in f["match"].lower()]

    state = load_json(STATE, {})
    pdfs = index_pdfs()
    changed = False

    for filer in filers:
        key = filer_key(filer)
        st = state.setdefault(key, {"name": filer["name"], "seen": [], "last": None})
        mine = items_for(filer, pdfs)

        if mode == "seed":
            st["seen"] = sorted({p["url"] for p in mine})
            if mine:
                st["last"] = mine[0]["filename"]
            print(f"[seed] {filer['name']}: {len(mine)} PTRs seen")
            changed = True
            continue

        if mode == "demo":
            if mine:
                p = mine[0]
                summ = parse_278t(pdf_text(p["url"]))
                send_telegram(build_message(filer, p, summ))
                print(f"[demo] {filer['name']}: sent {p['filename']} ({summ['n']} txns)")
            else:
                print(f"[demo] {filer['name']}: no PTRs found")
            continue

        seen = set(st["seen"])
        new = [p for p in mine if p["url"] not in seen]
        for p in reversed(new):  # oldest first -> chronological
            try:
                summ = parse_278t(pdf_text(p["url"]))
            except Exception as e:  # noqa: BLE001
                summ = {"n": 0, "counts": {"BUY": 0, "SELL": 0, "EXCH": 0},
                        "lo": 0, "hi": 0, "n_amt": 0, "equities": [], "received": ""}
                print(f"  parse failed for {p['filename']}: {e}")
            send_telegram(build_message(filer, p, summ), dry=(mode == "dry"))
            seen.add(p["url"])
            changed = True
        if new and mode != "dry":
            st["seen"] = sorted(seen)
            st["last"] = mine[0]["filename"]
        if not new:
            print(f"[ok] {filer['name']}: no new PTRs")

    if mode == "seed" or (changed and mode == "normal"):
        save_json(STATE, state)
        print("oge_state.json updated")
    print("STATE_CHANGED=" + ("1" if (mode == "seed" or changed) else "0"))


if __name__ == "__main__":
    main()
