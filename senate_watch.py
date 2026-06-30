#!/usr/bin/env python3
"""
senate_watch.py — alert on new U.S. SENATE Periodic Transaction Reports (PTRs)
for a watchlist of senators, pushed to Telegram.

Source: efdsearch.senate.gov. The site requires accepting an agreement
(session cookie + CSRF token) before its AJAX search endpoint will respond;
electronic PTRs are then parsed from an HTML transaction table (stdlib only,
no PDF tooling). "Paper" (scanned) PTRs can't be parsed -> link-only alert.

Senators matched on LAST NAME + first-name filter (disambiguates e.g. R. Scott
vs T. Scott).

Usage:
  python senate_watch.py            # detect new PTRs, alert, update state
  python senate_watch.py --seed     # mark all current PTRs seen, send nothing
  python senate_watch.py --demo     # re-send each senator's latest PTR (no state write)
  python senate_watch.py --dry-run  # detect + print, send nothing, save nothing
  python senate_watch.py --test     # one-off test message
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (reused via watch.py); SENATE_MEMBER (demo filter)
"""

import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from watch import send_telegram, esc, money, _isodate

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "senate.json")
STATE = os.path.join(HERE, "senate_state.json")

UA = "sec-filing-alerts mrtsfn601 maratsafin601@gmail.com"
BASE = "https://efdsearch.senate.gov"
HOME = BASE + "/search/home/"
SEARCH = BASE + "/search/report/data/"

VERB = {"Purchase": ("🟢", "BUY"), "Sale (Full)": ("🔴", "SELL"),
        "Sale (Partial)": ("🔴", "SELL(part)"), "Exchange": ("🔁", "EXCH")}


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


def open_session():
    """Accept the eFD agreement; return (opener, csrftoken)."""
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA)]
    html = op.open(HOME, timeout=30).read().decode("utf-8", "replace")
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
    tok = m.group(1) if m else next((c.value for c in cj if c.name == "csrftoken"), "")
    op.open(urllib.request.Request(
        HOME, data=urllib.parse.urlencode({"prohibition_agreement": "1", "csrfmiddlewaretoken": tok}).encode(),
        headers={"Referer": HOME}), timeout=30).read()
    ctok = next((c.value for c in cj if c.name == "csrftoken"), tok)
    return op, ctok


def search_ptrs(op, ctok, first, last):
    payload = {
        "start": "0", "length": "100", "report_types": "[11]", "filer_types": "[]",
        "submitted_start_date": "01/01/2024 00:00:00", "submitted_end_date": "",
        "candidate_state": "", "senator_state": "", "office_id": "",
        "first_name": first, "last_name": last, "csrfmiddlewaretoken": ctok,
    }
    req = urllib.request.Request(SEARCH, data=urllib.parse.urlencode(payload).encode(),
        headers={"Referer": HOME, "X-CSRFToken": ctok, "X-Requested-With": "XMLHttpRequest"})
    data = json.load(op.open(req, timeout=30)).get("data", [])
    out = []
    for row in data:
        href_m = re.search(r'href="([^"]+)"', row[3])
        if not href_m:
            continue
        href = href_m.group(1)
        out.append({"first": re.sub(r"<[^>]+>", "", row[0]).strip(),
                    "last": re.sub(r"<[^>]+>", "", row[1]).strip(),
                    "title": re.sub(r"<[^>]+>", "", row[3]).strip(),
                    "date": row[4].strip(), "href": href,
                    "uuid": href.rstrip("/").split("/")[-1],
                    "paper": "/paper/" in href})
    return out


def parse_detail(op, href):
    """Parse an electronic PTR's transaction table -> list of dicts."""
    html = op.open(BASE + href, timeout=30).read().decode("utf-8", "replace")
    txns = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) >= 8 and re.match(r"\d{2}/\d{2}/\d{4}", cells[1]):
            txns.append({"date": cells[1], "owner": cells[2], "ticker": cells[3],
                         "asset": cells[4], "atype": cells[5], "ttype": cells[6], "amount": cells[7]})
    return txns


def band(s):
    nums = re.findall(r"\$([\d,]+)", s or "")
    if len(nums) >= 2:
        return f"{money(int(nums[0].replace(',', '')))}–{money(int(nums[1].replace(',', '')))}"
    if len(nums) == 1:
        return money(int(nums[0].replace(",", "")))
    return (s or "").strip()


def build_message(sen, ptr, txns):
    head = f"🏛️ <b>{esc(sen['name'])}</b> ({esc(sen['party'])}-{esc(sen['state'])}, Senate) — new PTR"
    lines = [head, f"Filed {_isodate(ptr['date'])}"]
    if ptr["paper"]:
        lines += ["", "(paper filing — see PDF)"]
    elif not txns:
        lines += ["", "(no transactions parsed — see filing)"]
    groups, order = {}, []
    for x in txns:
        key = VERB.get(x["ttype"], ("•", x["ttype"]))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(x)
    for emoji, verb in order:
        lines += ["", f"{emoji} <b>{verb}</b>"]
        for x in groups[(emoji, verb)]:
            tk = x["ticker"] if x["ticker"] and x["ticker"] != "--" else x["asset"][:24]
            suffix = "" if x["atype"].lower() == "stock" else f" [{esc(x['atype'])}]"
            lines.append(f"• <b>{esc(tk)}</b>{suffix} — {band(x['amount'])} · {x['date'][:5]}")
    lines += ["", f'<a href="{BASE}{ptr["href"]}">Filing ↗</a>']
    return "\n".join(lines)


def member_key(s):
    return f"{s['last'].lower()}|{s['state'].upper()}"


def main():
    args = set(sys.argv[1:])
    if "--test" in args:
        send_telegram("✅ <b>senate-watch</b> test — Senate PTR alerts wired up.")
        return
    mode = "seed" if "--seed" in args else ("demo" if "--demo" in args else
           ("dry" if "--dry-run" in args else "normal"))

    members = load_json(WATCHLIST, [])
    demo_filter = os.environ.get("SENATE_MEMBER", "").strip().lower()
    if mode == "demo" and demo_filter:
        members = [m for m in members if demo_filter in m["name"].lower() or demo_filter in m["last"].lower()]
    state = load_json(STATE, {})

    op, ctok = open_session()
    changed = False
    for m in members:
        key = member_key(m)
        st = state.setdefault(key, {"name": m["name"], "seen": [], "last_filed": None})
        try:
            ptrs = search_ptrs(op, ctok, m.get("first", ""), m["last"])
        except Exception as e:  # noqa: BLE001
            print(f"ERROR search {m['name']}: {e}")
            continue
        # client-side disambiguation (e.g., Rick vs Tim Scott)
        if m.get("first"):
            ptrs = [p for p in ptrs if p["first"].lower().startswith(m["first"].lower())]
        ptrs.reverse()  # API returns newest-first -> process oldest-first

        if mode == "seed":
            st["seen"] = sorted({p["uuid"] for p in ptrs})
            if ptrs:
                st["last_filed"] = ptrs[-1]["date"]
            print(f"[seed] {m['name']}: {len(ptrs)} PTRs seen")
            changed = True
            continue

        if mode == "demo":
            if ptrs:
                p = ptrs[-1]
                txns = [] if p["paper"] else parse_detail(op, p["href"])
                send_telegram(build_message(m, p, txns))
                print(f"[demo] {m['name']}: sent {p['uuid']} ({len(txns)} txns)")
            else:
                print(f"[demo] {m['name']}: no PTRs")
            continue

        seen = set(st["seen"])
        new = [p for p in ptrs if p["uuid"] not in seen]
        for p in new:
            txns = [] if p["paper"] else parse_detail(op, p["href"])
            send_telegram(build_message(m, p, txns), dry=(mode == "dry"))
            seen.add(p["uuid"])
            changed = True
        if new and mode != "dry":
            st["seen"] = sorted(seen)
            st["last_filed"] = ptrs[-1]["date"]
        if not new:
            print(f"[ok] {m['name']}: no new PTRs")
        time.sleep(0.3)  # polite pacing between senators

    if mode == "seed" or (changed and mode == "normal"):
        save_json(STATE, state)
        print("senate_state.json updated")
    print("STATE_CHANGED=" + ("1" if (mode == "seed" or changed) else "0"))


if __name__ == "__main__":
    main()
