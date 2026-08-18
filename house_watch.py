#!/usr/bin/env python3
"""
house_watch.py — alert on new U.S. HOUSE Periodic Transaction Reports (PTRs,
STOCK Act stock trades) for a watchlist of members, pushed to Telegram.

Separate data source from the EDGAR watcher: the official House Clerk bulk
disclosure feed. Detection is stdlib; the per-trade detail is parsed from the
filing PDF with `pdftotext -raw` (poppler). Senate is a separate watcher
(senate_watch.py — efdsearch.senate.gov).

Members are matched on LAST NAME + STATE (robust vs. formal/nickname mismatches).

Usage:
  python house_watch.py            # detect new PTRs, alert, update state
  python house_watch.py --seed     # mark all current PTRs seen, send nothing
  python house_watch.py --demo     # re-send each member's latest PTR (no state write)
  python house_watch.py --dry-run  # detect + print, send nothing, save nothing
  python house_watch.py --test     # send a one-off test message

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

from watch import send_telegram, esc, money, _isodate  # reuse Telegram pipe + helpers

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "house.json")
STATE = os.path.join(HERE, "house_state.json")

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


def index_rows(year, fresh=False):
    """Return PTR index rows for a year: dicts with last/first/type/state/date/docid.

    The Clerk serves the index through a CDN that caches it for ~2h and ignores
    no-cache, so `fresh` busts it by query string — worth it when re-sending a
    named filing, but not on the 5-minute poll.
    """
    url = INDEX_ZIP.format(year=year)
    if fresh:
        url += "?t=%d" % int(time.time())
    try:
        raw = http_bytes(url)
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


_TYPE = r"S \(partial\)|P|S|E"
_MONEY = r"\$[\d,]+(?:\.\d+)?"

# Repeated page furniture: the table header is reprinted on every page, and can
# land mid-row when a transaction straddles a page break.
_HEADER = re.compile(r"ID Owner Asset Transaction\s+Type\s+Date Notification\s+"
                     r"Date\s+Amount Cap\.\s+Gains >\s+\$200\?")
_JUNK = re.compile(r"Filing ID #\d+|\f")

_ANCHOR = re.compile(r"(?:" + _TYPE + r")\s+\d{2}/\d{2}/\d{4}\s+\d{2}/\d{2}/\d{4}\s+\$")

# A row split by a page break: the head of the asset name and the spine end one
# page, the rest of the name (carrying the ticker/asset code, and sometimes the
# upper bound of the amount) resumes on the next. A complete row always carries
# its [ASSET CODE] before the spine, so a spine-terminated line that does not is
# the signature of a split.
_ROW_OPEN = re.compile(
    r"^(?P<head>.*?[^\]\s])[ ]*"
    r"(?P<spine>(?:" + _TYPE + r") \d{2}/\d{2}/\d{4} \d{2}/\d{2}/\d{4} "
    + _MONEY + r"(?: -(?: " + _MONEY + r")?)?)$")
# Last line of a split name: ends at the [ASSET CODE], which the upper bound of
# the amount may trail on the same line.
_CODE_TAIL = re.compile(r"(?P<tail>.*\[[A-Za-z]{1,3}\])(?:[ ]*(?P<amt>" + _MONEY + r"))?$")
_MONEY_ONLY = re.compile(_MONEY + r"$")

# (TICKER) is absent on bonds, treasuries, structured notes and annuities, so
# only the [ASSET CODE] is required to anchor a row.
_SPINE = re.compile(
    r"(?:\((?P<ticker>[A-Z0-9.\-]{1,6})\)\s*)?"
    r"\[(?P<code>[A-Za-z]{1,3})\]\s*"
    r"(?P<type>" + _TYPE + r")\s+"
    r"(?P<txn>\d{2}/\d{2}/\d{4})\s+\d{2}/\d{2}/\d{4}\s+"
    r"\$(?P<low>[\d,]+(?:\.\d+)?)"
    r"(?:\s*-\s*\$(?P<high>[\d,]+(?:\.\d+)?)|\s*(?P<plus>\+))?")

# Lines that are never part of an asset name: per-row fields (F S / S O / D / C
# / L), section headings, amount continuations and cover/footer boilerplate.
_NOT_ASSET = re.compile(
    r"^\s*(?:[A-Z](?:\s+[A-Z])*\s*:|[A-Z](?:\s+[A-Z]){0,3}\s*$|\*|" + _MONEY + r"\s*$"
    r"|Yes No|I CERTIFY|my knowledge|Digitally Signed|Filing ID|Clerk of the House"
    r"|Name:|Status:|State/District:|ID Owner)")

_DESC_END = re.compile(r"\*\s*For the complete|Digitally Signed|Filing ID|I CERTIFY")

_OWNER_PREFIX = re.compile(r"^(SP|JT|DC)\s+")

# Widest asset-name line the PDF's asset column produces (observed max 36).
_ASSET_WRAP = 48


def _stitch(lines):
    """Re-join transaction rows broken across a page boundary."""
    out, i = [], 0
    while i < len(lines):
        m = _ROW_OPEN.match(lines[i].strip())
        if not m:
            out.append(lines[i])
            i += 1
            continue
        tail, high, done, j = [], None, False, i + 1
        while j < len(lines) and len(tail) < 3:
            nxt = lines[j].strip()
            if not nxt:                      # blank line left by the stripped header
                j += 1
                continue
            if _NOT_ASSET.match(nxt) or _ANCHOR.search(nxt):
                break
            mt = _CODE_TAIL.match(nxt)
            tail.append(mt.group("tail") if mt else nxt)
            j += 1
            if mt:
                high, done = mt.group("amt"), True
                break
        if not done:
            out.append(lines[i])
            i += 1
            continue
        spine = m.group("spine")
        if spine.endswith("-"):              # upper bound left behind on the next page
            k = j
            while k < len(lines) and not lines[k].strip():
                k += 1
            if high is None and k < len(lines) and _MONEY_ONLY.match(lines[k].strip()):
                high, j = lines[k].strip(), k + 1
            if high:
                spine += " " + high
        out.append("%s %s %s" % (m.group("head").strip(), " ".join(tail), spine))
        i = j
    return out


def _clean(text):
    """Drop repeated page furniture and re-join rows split by a page break."""
    t = re.sub(r"[ \t]+", " ", text)
    t = re.sub(r"[ \t]+", " ", _JUNK.sub(" ", _HEADER.sub("\n", t)))
    return "\n".join(_stitch(t.split("\n")))


def _asset_name(head):
    """Asset name = the trailing run of non-field lines before the row's spine.

    Returns (name, offset), where offset is where that run starts in `head` — it
    doubles as the end of the preceding row's description.
    """
    pos, lines = 0, []
    for ln in head.split("\n"):
        if ln.strip():
            lines.append((pos + len(ln) - len(ln.lstrip()), ln.strip()))
        pos += len(ln) + 1
    name, start = [], len(head)
    for off, ln in reversed(lines):
        if _NOT_ASSET.match(ln):
            break
        # The asset column wraps far narrower than the description column, so an
        # over-wide line is the previous row's description, not part of the name.
        # (Only the nearest line is exempt: it is cut short by the spine.)
        if name and len(ln) > _ASSET_WRAP:
            break
        name.insert(0, ln)
        start = off
        # An owner code always opens an asset block, so it bounds the scan.
        if _OWNER_PREFIX.match(ln) or len(name) == 4:
            break
    return re.sub(r"\s+", " ", " ".join(name)).strip(" -"), start


def _rows(t):
    """Extract transaction rows from already-cleaned PTR text."""
    out, spans = [], []
    for m in _SPINE.finditer(t):
        name, start = _asset_name(t[:m.start()])
        owner = ""
        mo = _OWNER_PREFIX.match(name)
        if mo:
            owner, name = mo.group(1), name[mo.end():]
        out.append({"ticker": m.group("ticker") or "", "name": name,
                    "code": m.group("code"), "type": m.group("type"),
                    "txn": m.group("txn"), "low": m.group("low"),
                    "high": m.group("high"), "plus": m.group("plus"),
                    "owner": owner, "desc": ""})
        spans.append((m.end(), start))
    # Description runs from the row's spine to the start of the next asset name.
    for i, x in enumerate(out):
        stop = spans[i + 1][1] if i + 1 < len(out) else len(t)
        win = t[spans[i][0]:max(spans[i][0], stop)]
        cut = _DESC_END.search(win)
        if cut:
            win = win[:cut.start()]
        md = re.search(r"(?:^|\n)\s*D\s*:\s*([\s\S]+)", win)
        if md:
            x["desc"] = re.sub(r"\s+", " ", md.group(1)).strip()
    return out


def parse_ptr(text):
    """Parse a House PTR -> list of transaction dicts (best-effort)."""
    return _rows(_clean(text))


def parse_ptr_full(text):
    """parse_ptr, plus how many rows the PDF actually holds.

    The two differ only if a row shape slips past the parser; surfacing the gap
    in the alert keeps such a miss from passing as a complete report.
    """
    t = _clean(text)
    return _rows(t), len(_ANCHOR.findall(t))


def _amt(s):
    return int(float(s.replace(",", ""))) if s else 0


def band(low, high, plus=None):
    lo = _amt(low)
    if high:
        return f"{money(lo)}–{money(_amt(high))}"
    if plus:
        return f"{money(lo)}+"
    return money(lo)


def label(x, width=44):
    """Ticker when the asset has one, else the (trimmed) asset name."""
    if x.get("ticker"):
        return x["ticker"]
    name = x.get("name") or "?"
    return name if len(name) <= width else name[:width - 1].rstrip(" ,-") + "…"


def build_message(member, row, txns, total=None):
    pdfurl = PTR_PDF.format(year=row["year"], docid=row["docid"])
    head = f"🏛️ <b>{esc(member['name'])}</b> ({esc(member['party'])}-{esc(row['state'])}) — new PTR"
    lines = [head, f"Filed {_isodate(row['date'])}"]
    if not txns:
        lines += ["", "(could not parse transactions — see filing)"]
    elif total and total > len(txns):
        lines += ["", f"({len(txns)} of {total} transactions parsed — see filing)"]
    groups, order = {}, []
    for x in txns:
        key = VERB.get(x["type"], ("•", x["type"]))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(x)
    for emoji, verb in order:
        lines += ["", f"{emoji} <b>{verb}</b>"]
        for x in groups[(emoji, verb)]:
            code = "" if x["code"].upper() == "ST" else f" [{esc(x['code'])}]"
            seg = f"• <b>{esc(label(x))}</b>{code} — {band(x['low'], x['high'], x.get('plus'))} · {x['txn'][:5]}"
            if x["desc"]:
                seg += f" · {esc(x['desc'])}"
            lines.append(seg)
    lines += ["", f'<a href="{pdfurl}">Filing ↗</a>']
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
        send_telegram("✅ <b>house-watch</b> test — House PTR alerts wired up.")
        return
    mode = "seed" if "--seed" in args else ("demo" if "--demo" in args else
           ("dry" if "--dry-run" in args else "normal"))

    members = load_json(WATCHLIST, [])
    demo_filter = os.environ.get("HOUSE_MEMBER", "").strip().lower()
    if mode == "demo" and demo_filter:
        members = [m for m in members
                   if demo_filter in m["name"].lower() or demo_filter in m["last"].lower()]
    state = load_json(STATE, {})
    year = datetime.date.today().year
    fresh = mode == "demo"
    rows = index_rows(year, fresh) + index_rows(year - 1, fresh)  # cover Jan boundary

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
                txns, total = [], 0
                try:
                    txns, total = parse_ptr_full(pdf_text(r["year"], r["docid"]))
                except Exception as e:  # noqa: BLE001
                    print(f"[warn] {m['name']}: {r['docid']} parse failed: {e}")
                send_telegram(build_message(m, r, txns, total))
                print(f"[demo] {m['name']}: sent {r['docid']} ({len(txns)}/{total} txns)")
            else:
                print(f"[demo] {m['name']}: no PTRs found")
            continue

        seen = set(st["seen"])
        new = [r for r in ptrs if r["docid"] not in seen]
        for r in new:
            txns, total = [], 0
            try:
                txns, total = parse_ptr_full(pdf_text(r["year"], r["docid"]))
            except Exception as e:  # noqa: BLE001
                print(f"[warn] {m['name']}: {r['docid']} parse failed: {e}")
            send_telegram(build_message(m, r, txns, total), dry=(mode == "dry"))
            seen.add(r["docid"])
            changed = True
        if new and mode != "dry":
            st["seen"] = sorted(seen)
            st["last_filed"] = ptrs[-1]["date"]
        if not new:
            print(f"[ok] {m['name']}: no new PTRs")

    if mode == "seed" or (changed and mode == "normal"):
        save_json(STATE, state)
        print("house_state.json updated")
    print("STATE_CHANGED=" + ("1" if (mode == "seed" or changed) else "0"))


if __name__ == "__main__":
    main()
